"""Real HTTP protocol tests for response compression; loopback only."""
import gzip
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib import request as urlrequest
from standalone_bridge import BrainConnector, Config, StateDB

class CompressionTests(unittest.TestCase):
    def setUp(self):
        self.requests=[]
        owner=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def handle_request(self):
                size=int(self.headers.get('Content-Length') or 0)
                incoming=self.rfile.read(size) if size else b''
                owner.requests.append({'method':self.command,'body':incoming,
                    'encoding':self.headers.get('Accept-Encoding'),'token':self.headers.get('X-Agent-Token')})
                raw=json.dumps({'ok':True,'text':'客户咨询套餐'},ensure_ascii=False).encode()
                zipped=self.path!='/plain'
                if self.path=='/error': raw=json.dumps({'error':'权限已变更'},ensure_ascii=False).encode()
                if self.path=='/nonobject': raw=b'[]'
                if self.path=='/empty': raw=b''
                body=gzip.compress(raw) if zipped else raw
                if self.path=='/corrupt': body=b'not-gzip'
                if self.path=='/corrupt-block': body=gzip.compress(raw)[:10]+b'\x06'+bytes(8)
                self.send_response(403 if self.path=='/error' else 200)
                self.send_header('Content-Length',str(len(body)))
                if zipped:self.send_header('Content-Encoding','gzip')
                self.end_headers();self.wfile.write(body)
            do_GET=handle_request
            do_POST=handle_request
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        url=f'http://127.0.0.1:{self.server.server_port}'
        with patch.dict(os.environ,{'QN_BRAIN_SERVER_URL':url}):
            config=Config(Path(self.temp.name)/'config.json',{'brain_server_url':url})
        self.brain=BrainConnector(SimpleNamespace(config=config,db=StateDB(Path(self.temp.name)/'state.db')))
        self.brain._headers=lambda:{'Content-Type':'application/json','X-Agent-Token':'synthetic-test'}
        opener=urlrequest.build_opener(urlrequest.ProxyHandler({}))
        patcher=patch('standalone_bridge.urllib.request.urlopen',side_effect=opener.open)
        patcher.start();self.addCleanup(patcher.stop)

    def test_gzip_json_is_negotiated_and_decoded(self):
        self.assertEqual(self.brain.request('GET','/gzip')['text'],'客户咨询套餐')
        self.assertEqual(self.requests[0]['encoding'],'gzip')
        self.assertEqual(self.requests[0]['token'],'synthetic-test')

    def test_plain_response_still_works(self):
        self.assertTrue(self.brain.request('GET','/plain')['ok'])

    def test_post_payload_and_auth_are_preserved(self):
        self.assertTrue(self.brain.request('POST','/gzip',{'content':'正常回复','request_id':'test-id'})['ok'])
        self.assertEqual(json.loads(self.requests[0]['body']),{'content':'正常回复','request_id':'test-id'})
        self.assertEqual(self.requests[0]['token'],'synthetic-test')

    def test_compressed_http_error_preserves_reason(self):
        with self.assertRaisesRegex(RuntimeError,'权限已变更'):
            self.brain.request('GET','/error')

    def test_corrupt_compression_never_becomes_success(self):
        with self.assertRaises(RuntimeError):self.brain.request('GET','/corrupt')

    def test_corrupt_deflate_block_never_becomes_success(self):
        with self.assertRaises(RuntimeError):self.brain.request('GET','/corrupt-block')

    def test_nonobject_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError,'不是 JSON 对象'):self.brain.request('GET','/nonobject')

    def test_empty_response_remains_compatible(self):
        self.assertEqual(self.brain.request('GET','/empty'),{})

