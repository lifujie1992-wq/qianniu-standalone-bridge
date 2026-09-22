from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

PROJECT = Path(__file__).resolve().parents[1]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

import launcher
import qianniu_app
import tray_app
import tray_control


class TrayStatusTests(unittest.TestCase):
    def test_status_payload_is_normalized(self):
        status = tray_control.coerce_status({
            "state": "healthy",
            "version": "1.5.2",
            "components": {"bridge": {"state": "healthy", "detail": "ready"}},
        })
        self.assertEqual(status.label, "运行正常")
        self.assertEqual(status.components["bridge"].detail, "ready")

    def test_build_status_distinguishes_healthy_degraded_and_stopped(self):
        config = {"api_port": 42111, "dock_enabled": True}
        with patch.object(launcher, "load_config_silent", return_value=config), \
                patch.object(launcher, "fetch_status", return_value={"ok": True, "version": "1.5.2"}), \
                patch.object(tray_app, "_managed_qianniu_count", return_value=2), \
                patch.object(tray_app, "_pid_file_running", return_value=True):
            self.assertEqual(tray_app.build_status(PROJECT).state, "healthy")
        with patch.object(launcher, "load_config_silent", return_value=config), \
                patch.object(launcher, "fetch_status", return_value={"ok": True}), \
                patch.object(tray_app, "_managed_qianniu_count", return_value=0), \
                patch.object(tray_app, "_pid_file_running", return_value=False):
            self.assertEqual(tray_app.build_status(PROJECT).state, "degraded")
        with patch.object(launcher, "load_config_silent", return_value=config), \
                patch.object(launcher, "fetch_status", return_value=None), \
                patch.object(tray_app, "_managed_qianniu_count", return_value=0), \
                patch.object(tray_app, "_pid_file_running", return_value=False):
            self.assertEqual(tray_app.build_status(PROJECT).state, "stopped")


class TrayLifecycleTests(unittest.TestCase):
    def test_second_launch_requests_existing_window(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tray_app.request_existing_window(root)
            self.assertEqual((root / "state" / "tray.show").read_text(encoding="ascii"), "show\n")

    def test_default_entry_uses_persistent_tray_owner(self):
        with patch.object(sys, "argv", ["QianniuAgent.exe"]), \
                patch.object(tray_app, "run", return_value=23) as run:
            self.assertEqual(qianniu_app.main(), 23)
            run.assert_called_once_with()

    def test_scoped_qianniu_cleanup_never_kills_foreign_process(self):
        root = Path(r"C:\Apps\QianniuAIService")
        owned = Mock(pid=101, info={"exe": str(root / "runtime" / "AliWorkbench.exe")})
        owned.ppid.return_value = 1
        foreign = Mock(pid=202, info={"exe": r"C:\Program Files\Qianniu\AliWorkbench.exe"})
        foreign.ppid.return_value = 1
        with patch.object(launcher.psutil, "process_iter", return_value=[owned, foreign]), \
                patch.object(launcher, "kill_tree", return_value=True) as kill:
            self.assertEqual(launcher.stop_managed_qianniu(root), [])
            kill.assert_called_once_with(101)

    def test_foreign_reused_tray_pid_is_not_terminated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            state.mkdir()
            (state / "tray.pid").write_text("999", encoding="ascii")
            foreign = Mock()
            foreign.exe.return_value = r"C:\Windows\System32\notepad.exe"
            foreign.cmdline.return_value = [r"C:\Windows\System32\notepad.exe"]
            with patch.object(launcher.psutil, "pid_exists", return_value=True), \
                    patch.object(launcher.psutil, "Process", return_value=foreign), \
                    patch.object(launcher, "kill_tree") as kill:
                self.assertTrue(launcher.request_tray_exit(root))
                kill.assert_not_called()
                self.assertFalse((state / "tray.pid").exists())

    def test_complete_exit_runs_cleanup_off_the_tk_thread(self):
        control = tray_control.TrayControlCenter.__new__(tray_control.TrayControlCenter)
        control._exiting = False
        control.root = Mock()
        control._apply_status = Mock()
        control.close = Mock()
        cleanup_thread = []

        def completely_exit():
            cleanup_thread.append(threading.current_thread().name)

        control.callbacks = Mock(completely_exit=completely_exit)
        control._begin_exit()
        for thread in threading.enumerate():
            if thread.name == "tray-complete-exit":
                thread.join(timeout=2.0)

        self.assertTrue(control._exiting)
        self.assertEqual(cleanup_thread, ["tray-complete-exit"])
        control.root.after.assert_called_once_with(0, control.close)

    def test_exit_confirmation_is_forced_to_the_foreground(self):
        source = (PROJECT / "tray_control.py").read_text(encoding="utf-8")
        self.assertIn("MB_SETFOREGROUND | MB_TOPMOST", source)
        self.assertIn("MessageBoxW(\n            0,", source)


if __name__ == "__main__":
    unittest.main()
