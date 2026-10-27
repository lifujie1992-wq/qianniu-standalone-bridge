"""Automatic upgrade discovery, safe replacement and failure recovery."""
import hashlib
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

from tools import upgrade_installed as upgrade


class AutoUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / "existing-custom-location"
        self.root.mkdir()
        (self.root / "QianniuAgent.exe").write_bytes(b"old-program")
        (self.root / "config.json").write_text('{"api_token":"private-test-token"}', encoding="utf-8")
        (self.root / "build-info.json").write_text('{"version":"1.6.23"}', encoding="utf-8")
        for name in ("_internal", "state", "runtime", "build"):
            (self.root / name).mkdir()
        (self.root / "_internal/old.dll").write_bytes(b"old-runtime")
        (self.root / "runtime/native.dll").write_bytes(b"native-unchanged")
        (self.root / "build/other.dll").write_bytes(b"custom-build-unchanged")
        with closing(sqlite3.connect(self.root / "state/bridge.sqlite3")) as db, db:
            db.execute("CREATE TABLE sends(status TEXT, response TEXT)")
            db.execute("INSERT INTO sends VALUES('unknown','keep-dedup-ledger')")
        self.source = self.base / "payload"
        self.source.mkdir()
        (self.source / "QianniuAgent.exe").write_bytes(b"new-program")
        (self.source / "_internal").mkdir()
        (self.source / "_internal/new.dll").write_bytes(b"new-runtime")
        (self.source / "build").mkdir()
        (self.source / "build/appbiz_adapter.dll").write_bytes(b"new-adapter")
        (self.source / "build-info.json").write_text('{"version":"1.6.25"}', encoding="utf-8")
        self.backup = self.root / "upgrade-backups/test"

    def tearDown(self):
        self.temp.cleanup()

    def test_discovery_keeps_priority_deduplicates_and_requires_real_config(self):
        false = self.base / "not-an-installation"
        false.mkdir()
        (false / "QianniuAgent.exe").write_bytes(b"x")
        found = upgrade.discover([false,self.root,self.root])
        self.assertEqual(found,[{"path":str(self.root.resolve()),"version":"1.6.23"}])

    def test_default_discovery_uses_running_then_registry_then_common_paths(self):
        with patch.object(upgrade,"running_candidates",return_value=[self.root]), \
                patch.object(upgrade,"registry_candidates",return_value=[self.root]), \
                patch.object(upgrade,"common_candidates",return_value=[]):
            self.assertEqual(len(upgrade.discover()),1)

    def test_success_preserves_config_native_runtime_chat_and_unknown_send(self):
        stop = Mock()
        start = Mock(return_value={"ok":True})
        result = upgrade.replace_program(self.root,self.source,self.backup,stop,start)
        self.assertEqual(result["version"], upgrade.VERSION)
        self.assertEqual((self.root / "QianniuAgent.exe").read_bytes(),b"new-program")
        self.assertIn("private-test-token",(self.root / "config.json").read_text())
        self.assertEqual((self.root / "runtime/native.dll").read_bytes(),b"native-unchanged")
        self.assertEqual((self.root / "build/other.dll").read_bytes(),b"custom-build-unchanged")
        self.assertTrue((self.root / "_internal/new.dll").is_file())
        self.assertFalse((self.root / "_internal/old.dll").exists())
        self.assertEqual((self.backup / "QianniuAgent.exe").read_bytes(),b"old-program")
        with closing(sqlite3.connect(self.root / "state/bridge.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT status,response FROM sends").fetchone(),("unknown","keep-dedup-ledger"))
        self.assertTrue((self.backup / "bridge.sqlite3").is_file())
        stop.assert_called_once()
        start.assert_called_once_with(upgrade.VERSION)

    def test_failed_start_restores_old_program_dependencies_and_restarts_old(self):
        start = Mock(side_effect=[RuntimeError("failed startup"),{"ok":True}])
        with self.assertRaisesRegex(RuntimeError,"已恢复旧版"):
            upgrade.replace_program(self.root,self.source,self.backup,Mock(),start)
        self.assertEqual((self.root / "QianniuAgent.exe").read_bytes(),b"old-program")
        self.assertTrue((self.root / "_internal/old.dll").is_file())
        self.assertFalse((self.root / "_internal/new.dll").exists())
        self.assertEqual(upgrade.read_json(self.root / "build-info.json")["version"],"1.6.23")
        self.assertEqual(start.call_args_list[-1].args,(None,))

    def test_partial_copy_failure_restores_every_moved_file(self):
        real_copy = upgrade.shutil.copy2
        def fail(source,target,*args,**kwargs):
            if Path(source).name == "QianniuAgent.exe":
                raise OSError("disk full")
            return real_copy(source,target,*args,**kwargs)
        with patch.object(upgrade.shutil,"copy2",side_effect=fail), self.assertRaisesRegex(RuntimeError,"已恢复旧版"):
            upgrade.replace_program(self.root,self.source,self.backup,Mock(),Mock(return_value={"ok":True}))
        self.assertEqual((self.root / "QianniuAgent.exe").read_bytes(),b"old-program")

    def test_protected_payload_is_rejected_before_stopping(self):
        (self.source / "config.json").write_text("{}")
        stop = Mock()
        with self.assertRaisesRegex(RuntimeError,"文件清单"):
            upgrade.replace_program(self.root,self.source,self.backup,stop,Mock())
        stop.assert_not_called()

    def test_backup_outside_selected_installation_is_rejected_before_stopping(self):
        stop = Mock()
        with self.assertRaisesRegex(RuntimeError,"备份路径"):
            upgrade.replace_program(self.root,self.source,self.base / "outside",stop,Mock())
        stop.assert_not_called()

    def test_active_send_stops_upgrade_but_terminal_unknown_is_allowed(self):
        seat = upgrade.Seat(self.root)
        seat.processes = Mock(return_value=[])
        seat.status = Mock(side_effect=OSError("not running"))
        seat.ensure_idle()
        with closing(sqlite3.connect(self.root / "state/bridge.sqlite3")) as db, db:
            db.execute("INSERT INTO sends VALUES('in_flight','active')")
        with self.assertRaisesRegex(RuntimeError,"正在发送"):
            seat.ensure_idle()

    def test_other_installation_on_same_port_is_not_stopped_or_reused(self):
        seat = upgrade.Seat(self.root)
        seat.processes = Mock(return_value=[])
        seat.status = Mock(return_value={"ok":True})
        with self.assertRaisesRegex(RuntimeError,"另一个安装目录"):
            seat.ensure_idle()

    def make_archive(self, entries):
        path = self.base / "test.zip"
        with zipfile.ZipFile(path,"w") as package:
            for name,data in entries.items():
                package.writestr(name,data)
        return path,{"zip_sha256":hashlib.sha256(path.read_bytes()).hexdigest(),"exe_sha256":hashlib.sha256(b"new").hexdigest()}

    def test_verified_payload_extracts_to_isolated_folder(self):
        path,expected = self.make_archive({"QianniuAIService/QianniuAgent.exe":b"new","QianniuAIService/build-info.json":json.dumps({"version": upgrade.VERSION}).encode()})
        self.assertTrue((upgrade.extract_payload(path,self.base / "extract",expected)/"QianniuAgent.exe").is_file())

    def test_corrupted_zip_fails_hash_validation(self):
        path,expected = self.make_archive({"QianniuAIService/QianniuAgent.exe":b"new"})
        path.write_bytes(path.read_bytes()+b"changed")
        with self.assertRaisesRegex(RuntimeError,"校验失败"):
            upgrade.extract_payload(path,self.base / "extract",expected)

    def test_traversal_private_state_and_unknown_top_level_are_rejected(self):
        for entry in ("QianniuAIService/../../outside", "QianniuAIService/state/data", "QianniuAIService/config.json", "QianniuAIService/runtime/new.exe", "QianniuAIService/unknown.exe"):
            with self.subTest(entry=entry):
                path,expected = self.make_archive({entry:b"bad"})
                with self.assertRaises(RuntimeError):
                    upgrade.extract_payload(path,self.base / "extract",expected)

    def test_only_selected_helper_executable_is_managed(self):
        seat = upgrade.Seat(self.root)
        owned = Mock(info={"exe":str(self.root / "QianniuAgent.exe"),"cmdline":["QianniuAgent.exe","--role","bridge"]})
        native = Mock(info={"exe":str(self.root / "runtime/AliRender.exe"),"cmdline":[]})
        other = Mock(info={"exe":str(self.base / "other/QianniuAgent.exe"),"cmdline":[]})
        with patch.object(upgrade.psutil,"process_iter",return_value=[owned,native,other]):
            self.assertEqual(seat.processes(),[(owned,"bridge")])

    def test_normal_application_config_migration_keeps_credentials(self):
        path = self.root / "config.json"
        original = path.read_bytes()
        updated = json.loads(original)
        upgrade.apply_operational_defaults(updated)
        updated["device_id"] = "stable-test-device"
        path.write_text(json.dumps(updated),encoding="utf-8")
        with patch.object(upgrade,"stable_device_id",return_value="stable-test-device"):
            upgrade.check_preserved_config(original,path)

    def test_lost_credentials_are_rejected_even_when_other_migrations_are_valid(self):
        path = self.root / "config.json"
        original = path.read_bytes()
        updated = json.loads(original)
        upgrade.apply_operational_defaults(updated)
        updated["device_id"] = "stable-test-device"
        updated["api_token"] = "wrong-token"
        path.write_text(json.dumps(updated),encoding="utf-8")
        with patch.object(upgrade,"stable_device_id",return_value="stable-test-device"), self.assertRaisesRegex(RuntimeError,"配置"):
            upgrade.check_preserved_config(original,path)


if __name__ == "__main__":
    unittest.main()
