"""qn_client_settings 单测：千牛「无障碍模式」开关的读写。"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import qn_client_settings as cs


def make_db(path: Path, value: str | None) -> None:
    connection = sqlite3.connect(str(path))
    try:
        connection.execute("create table key2value (key text primary key, value text)")
        if value is not None:
            connection.execute("insert into key2value(key, value) values(?, ?)",
                               (cs.SETTING_KEY, value))
        connection.commit()
    finally:
        connection.close()


class ReadTests(unittest.TestCase):
    def test_reads_true(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "Global.db"
            make_db(db, "true")
            ok, raw = cs._read_setting(db, cs.SETTING_KEY)
            self.assertTrue(ok)
            self.assertEqual(raw, "true")

    def test_reads_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "Global.db"
            make_db(db, "false")
            ok, raw = cs._read_setting(db, cs.SETTING_KEY)
            self.assertTrue(ok)
            self.assertFalse(cs._as_bool(raw))

    def test_missing_key_is_not_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "Global.db"
            make_db(db, None)
            ok, reason = cs._read_setting(db, cs.SETTING_KEY)
            self.assertFalse(ok)
            self.assertIn("not found", reason)

    def test_state_reports_advice_when_disabled(self):
        state = {"enabled": False, "readable": True, "path": "x"}
        text = cs.advice(state)
        self.assertIn("无障碍模式", text)
        self.assertIn("重启千牛", text)

    def test_state_advice_empty_when_enabled(self):
        self.assertEqual(cs.advice({"enabled": True, "readable": True}), "")

    def test_locate_prefers_existing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / cs.RELATIVE_DB
            db.parent.mkdir(parents=True)
            make_db(db, "true")
            with mock.patch.object(cs, "candidate_data_dirs", return_value=[root]):
                self.assertEqual(cs.locate_global_db(), db)


class EnableTests(unittest.TestCase):
    def test_enable_refuses_while_qianniu_running(self):
        with mock.patch.object(cs, "qianniu_running", return_value=[1234]):
            result = cs.enable(Path("whatever.db"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "qianniu_running")

    def test_enable_writes_true_and_keeps_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "Global.db"
            make_db(db, "false")
            with mock.patch.object(cs, "qianniu_running", return_value=[]):
                result = cs.enable(db)
            self.assertTrue(result["ok"], result)
            ok, raw = cs._read_setting(db, cs.SETTING_KEY)
            self.assertTrue(ok)
            self.assertTrue(cs._as_bool(raw))
            self.assertTrue(Path(result["backup"]).is_file())

    def test_enable_inserts_key_when_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "Global.db"
            make_db(db, None)
            with mock.patch.object(cs, "qianniu_running", return_value=[]):
                result = cs.enable(db)
            self.assertTrue(result["ok"], result)
            self.assertTrue(cs._as_bool(cs._read_setting(db, cs.SETTING_KEY)[1]))
