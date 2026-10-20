"""Profile consistency and side-effect-free native checks; never send a message."""
from __future__ import annotations

import ctypes
import importlib.util
import os
from pathlib import Path
import re
import unittest

from client_support import agent_profiles

ROOT=Path(__file__).resolve().parents[1]

def load_tool(name):
    spec=importlib.util.spec_from_file_location(name,ROOT/'tools'/f'{name}.py')
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

class ProfileConsistencyTests(unittest.TestCase):
    def test_99849_is_declared_in_all_profile_tables(self):
        profiles=load_tool('map_appbiz_offsets').KNOWN_PROFILES
        profile=profiles[0x2039000]
        self.assertEqual(profile['version'],'9.98.49N')
        self.assertEqual(agent_profiles(ROOT)[0x2039000],'9.98.49N')
        source=(ROOT/'appbiz_agent.js').read_text(encoding='utf-8')
        block=re.search(r'0x2039000\s*:\s*\{([^}]+)\}',source,re.S).group(1)
        names={'serviceVtable':'service_vtable','secondaryVtable1':'secondary_vtable_1',
               'secondaryVtable2':'secondary_vtable_2','serviceGetNewMsg':'service_get_new_msg',
               'serviceOnMessageArrive':'service_on_message_arrive','serviceSendText':'service_send_text'}
        for js,key in names.items():
            value=int(re.search(r'\b'+js+r'\s*:\s*(0x[0-9a-f]+)',block,re.I).group(1),16)
            self.assertEqual(value,profile[key],js)
        cpp=(ROOT/'native/appbiz_adapter.cpp').read_text(encoding='utf-8')
        entries=[tuple(int(value,16) for value in group) for group in re.findall(r'\{(0x[0-9A-F]+),\s*(0x[0-9A-F]+),\s*(0x[0-9A-F]+)\}',cpp,re.I)]
        self.assertIn(tuple(profile[key] for key in ('service_vtable','message_biz_vtable','message_biz_send_text')),entries)

    def test_verifier_profiles_match_the_agent_and_native_map(self):
        mapper=load_tool('map_appbiz_offsets')
        verifier=load_tool('verify_appbiz_profile')
        expected=mapper.KNOWN_PROFILES[0x2039000]
        actual=verifier.PROFILES['0x2039000']
        for key,mapped in {'serviceVtable':'service_vtable','serviceGetNewMsg':'service_get_new_msg',
                           'serviceOnMessageArrive':'service_on_message_arrive','serviceSendText':'service_send_text',
                           'messageBizVtable':'message_biz_vtable','messageBizSendText':'message_biz_send_text'}.items():
            self.assertEqual(int(actual[key],16),expected[mapped])
        self.assertLess(verifier.AGENT.index('if (!profile.serviceVtable)'),verifier.AGENT.index("at(profile.serviceVtable)"))

    def test_new_profile_refuses_a_stale_helper(self):
        source=(ROOT/'appbiz_agent.js').read_text(encoding='utf-8')
        helper=source[source.index('function ensureHelper()'):source.index('rpc.exports =')]
        self.assertIn('appbiz_adapter_profile_supported_v1',helper)
        self.assertIn("hookProfile.version === '9.98.49N'",helper)
        self.assertLess(helper.index('native adapter does not support'),helper.index("'appbiz_send_text_v2'"))

    @unittest.skipUnless(os.environ.get('QN_COMPAT_APPBIZ_DLL'),'offline client DLL was not supplied')
    def test_actual_new_client_vtables_and_entrypoints(self):
        mapper=load_tool('map_appbiz_offsets')
        image=mapper.PeImage(Path(os.environ['QN_COMPAT_APPBIZ_DLL']))
        self.assertEqual(image.size_of_image,0x2039000)
        profile=mapper.KNOWN_PROFILES[image.size_of_image]
        self.assertEqual(image.read_pointer(profile['service_vtable']+0x18)-image.image_base,profile['service_get_new_msg'])
        self.assertEqual(image.read_pointer(profile['service_vtable']+0x90)-image.image_base,profile['service_send_text'])
        for key in ('service_vtable','secondary_vtable_1','secondary_vtable_2','message_biz_vtable'):
            self.assertTrue(image.vtable_shape_ok(profile[key],4),key)
        text_rva,text=image.section('.text')
        for key in ('service_get_new_msg','service_on_message_arrive','service_send_text','message_biz_send_text'):
            self.assertTrue(text_rva<=profile[key]<text_rva+len(text),key)

    @unittest.skipUnless(os.name=='nt' and (ROOT/'build/appbiz_adapter.dll').exists(),'compiled Windows adapter is not present')
    def test_compiled_adapter_layout_and_profile_probe_without_sending(self):
        dll=ctypes.WinDLL(str(ROOT/'build/appbiz_adapter.dll'))
        probe=dll.appbiz_adapter_profile_supported_v1
        probe.argtypes=[ctypes.c_uint64];probe.restype=ctypes.c_int
        for profile in load_tool('map_appbiz_offsets').KNOWN_PROFILES.values():
            self.assertEqual(probe(profile['service_vtable']),1)
        self.assertEqual(probe(0x1234),0)
        layout=dll.appbiz_adapter_layout
        layout.argtypes=[ctypes.POINTER(ctypes.c_uint32)]*3;layout.restype=ctypes.c_int
        sizes=[ctypes.c_uint32() for _ in range(3)]
        self.assertEqual(layout(*(ctypes.byref(value) for value in sizes)),0)
        self.assertEqual([value.value for value in sizes],[32,64,64])

if __name__=='__main__':unittest.main()
