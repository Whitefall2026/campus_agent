# -*- coding: utf-8 -*-
"""Platform behavior and macOS packaging regressions, with isolated data."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("RUC_AGENT_DATA_DIR", os.path.join(
    tempfile.gettempdir(), "ruc-agent-tests", "desktop-macos"))

import desktop
from app import paths

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class TestMacDataPaths(unittest.TestCase):
    def test_mac_data_lives_outside_the_bundle_and_source_tree(self):
        with patch.object(paths, "sys", SimpleNamespace(platform="darwin")), \
                patch.object(paths.os.path, "expanduser", return_value="/Users/test"):
            for frozen in (True, False):
                with self.subTest(frozen=frozen), patch.object(paths, "FROZEN", frozen):
                    self.assertEqual(paths._default_data_dir(), os.path.join(
                        "/Users/test", "Library", "Application Support", "RUC Agent", "data"))

    def test_windows_source_and_frozen_defaults_are_unchanged(self):
        with patch.object(paths, "sys", SimpleNamespace(platform="win32")):
            with patch.object(paths, "FROZEN", False):
                self.assertEqual(paths._default_data_dir(), os.path.join(paths.SOURCE_ROOT, "data"))
            with patch.object(paths, "FROZEN", True), \
                    patch.dict(os.environ, {"LOCALAPPDATA": "local-app-data"}):
                self.assertEqual(paths._default_data_dir(), os.path.join(
                    "local-app-data", "RUC Agent", "data"))

    def test_mac_environment_override_is_applied_before_any_data_access(self):
        with tempfile.TemporaryDirectory(prefix="ruc-mac-path-") as root:
            script = (
                "import sys; sys.platform='darwin'; "
                "from app.paths import DATA_DIR; print(DATA_DIR)"
            )
            result = subprocess.run([sys.executable, "-B", "-c", script],
                                    cwd=PROJECT_ROOT, capture_output=True, text=True,
                                    env={**os.environ, "RUC_AGENT_DATA_DIR": root},
                                    check=True, timeout=15)
            self.assertEqual(result.stdout.strip(), os.path.abspath(root))


class TestMacDesktop(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory(prefix="ruc-mac-desktop-")
        self.addCleanup(root.cleanup)
        self.data_dir = root.name
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(patch.object(desktop, "DATA_DIR", root.name))
        self.patches.enter_context(patch.object(desktop, "RUNTIME_FILE", os.path.join(root.name, "runtime.json")))
        self.patches.enter_context(patch.object(desktop, "_instance_lock", None))
        self.patches.enter_context(patch.dict(os.environ, {"RUC_AGENT_DATA_DIR": root.name}))
        self.addCleanup(desktop._release_single_instance)

    def simulate_mac(self):
        self.patches.enter_context(patch.object(desktop, "sys", SimpleNamespace(
            platform="darwin", stdout=sys.stdout, stderr=sys.stderr,
            maxsize=sys.maxsize, executable=sys.executable)))
        self.patches.enter_context(patch.object(desktop, "os", SimpleNamespace(
            name="posix", path=os.path, makedirs=os.makedirs, getpid=os.getpid,
            replace=os.replace, remove=os.remove, environ=os.environ)))

    def test_duplicate_mac_launch_closes_failed_lock_and_honors_no_browser(self):
        self.simulate_mac()
        flock = Mock(side_effect=[None, BlockingIOError(), BlockingIOError()])
        fcntl = SimpleNamespace(flock=flock, LOCK_EX=2, LOCK_NB=4)
        with patch.dict(sys.modules, {"fcntl": fcntl}), \
                patch.object(desktop.webbrowser, "open") as browser:
            self.assertTrue(desktop._acquire_single_instance(True))
            owned = desktop._instance_lock
            desktop._write_runtime(8765)
            self.assertFalse(desktop._acquire_single_instance(True))
            browser.assert_not_called()
            self.assertFalse(desktop._acquire_single_instance(False))
            browser.assert_called_once_with("http://127.0.0.1:8765/")
            self.assertIs(desktop._instance_lock, owned)
            desktop._release_single_instance()
            self.assertTrue(owned.closed)
            self.assertIsNone(desktop._instance_lock)
            self.assertTrue(os.path.isfile(os.path.join(self.data_dir, "desktop.lock")))
            desktop._release_single_instance()  # Repeated shutdown is harmless.

    def test_other_lock_errors_fail_startup_without_leaking_the_handle(self):
        self.simulate_mac()
        fcntl = SimpleNamespace(flock=Mock(side_effect=OSError("lock failed")),
                                LOCK_EX=2, LOCK_NB=4)
        handle = Mock()
        with patch.dict(sys.modules, {"fcntl": fcntl}), \
                patch("builtins.open", return_value=handle):
            with self.assertRaisesRegex(OSError, "lock failed"):
                desktop._acquire_single_instance(True)
        handle.close.assert_called_once()
        self.assertIsNone(desktop._instance_lock)

    @unittest.skipUnless(sys.platform == "darwin", "Requires native fcntl on macOS")
    def test_real_mac_lock_blocks_another_process_and_releases_on_close(self):
        self.assertTrue(desktop._acquire_single_instance(True))
        script = (
            "import desktop; "
            "print(desktop._acquire_single_instance(True)); "
            "desktop._release_single_instance()"
        )
        env = {**os.environ, "RUC_AGENT_DATA_DIR": self.data_dir}
        command = [sys.executable, "-B", "-c", script]
        blocked = subprocess.run(command, cwd=PROJECT_ROOT, env=env,
                                 capture_output=True, text=True, check=True, timeout=15)
        self.assertEqual(blocked.stdout.strip(), "False")
        desktop._release_single_instance()
        released = subprocess.run(command, cwd=PROJECT_ROOT, env=env,
                                  capture_output=True, text=True, check=True, timeout=15)
        self.assertEqual(released.stdout.strip(), "True")

    def test_open_data_uses_argument_list_so_paths_with_spaces_are_preserved(self):
        self.simulate_mac()
        with patch.object(desktop.subprocess, "run") as run:
            desktop._open_data_dir()
        run.assert_called_once_with(["open", self.data_dir], check=True)

    def test_windows_duplicate_launch_keeps_mutex_and_browser_behavior(self):
        kernel = Mock()
        kernel.CreateMutexW.return_value = 123
        windows_os = SimpleNamespace(name="nt", environ=os.environ,
                                     makedirs=os.makedirs, path=os.path)
        with patch.object(desktop, "sys", SimpleNamespace(platform="win32")), \
                patch.object(desktop, "os", windows_os), \
                patch.object(desktop.ctypes, "WinDLL", return_value=kernel, create=True), \
                patch.object(desktop.ctypes, "set_last_error", create=True), \
                patch.object(desktop.ctypes, "get_last_error", return_value=183, create=True), \
                patch.object(desktop.webbrowser, "open") as browser:
            self.assertFalse(desktop._acquire_single_instance(True))
            browser.assert_not_called()
            self.assertFalse(desktop._acquire_single_instance(False))
            browser.assert_called_once_with("http://127.0.0.1:8000/")

    def test_mac_gui_runs_menu_on_main_thread_and_cleans_up_after_tray_exit(self):
        self.simulate_mac()
        server = Mock(server_address=("127.0.0.1", 8765))
        observed = []

        def run_tray(url):
            observed.append((url, threading.current_thread() is threading.main_thread()))
            self.assertTrue(os.path.isfile(desktop.RUNTIME_FILE))

        # Socket/thread mocks avoid starting a background service or native UI.
        with patch.object(desktop, "_make_server", return_value=server), \
                patch.object(desktop.threading, "Thread") as worker, \
                patch.object(desktop.threading, "Timer") as timer, \
                patch.object(desktop, "_run_tray", side_effect=run_tray), \
                patch.object(desktop.webbrowser, "open") as browser:
            self.assertEqual(desktop._run_gui(8765, True), 0)
            timer.assert_not_called()
            browser.assert_not_called()
            worker.return_value.start.assert_called_once()
            worker.return_value.join.assert_called_once_with(timeout=5)
        self.assertEqual(observed, [("http://127.0.0.1:8765/", True)])
        server.shutdown.assert_called_once()
        server.server_close.assert_called_once()
        self.assertFalse(os.path.exists(desktop.RUNTIME_FILE))

    def test_tray_failure_still_shuts_down_service_and_removes_runtime(self):
        self.simulate_mac()
        server = Mock(server_address=("127.0.0.1", 8765))
        with patch.object(desktop, "_make_server", return_value=server), \
                patch.object(desktop.threading, "Thread"), \
                patch.object(desktop, "_run_tray", side_effect=RuntimeError("tray failed")):
            with self.assertRaisesRegex(RuntimeError, "tray failed"):
                desktop._run_gui(8765, True)
        server.shutdown.assert_called_once()
        server.server_close.assert_called_once()
        self.assertFalse(os.path.exists(desktop.RUNTIME_FILE))

    def test_mac_never_attempts_windows_wechat_imports(self):
        self.simulate_mac()
        with patch.dict(sys.modules, {"app.wechat.bridge": None}):
            desktop._start_wechat()
            desktop._stop_wechat()

    def test_mac_smoke_reaches_real_http_static_and_excel_without_windows_packages(self):
        script = """
import os
from types import SimpleNamespace
import sys
import desktop
desktop.os = SimpleNamespace(name='posix')
sys.modules['wechatauto'] = None
sys.modules['uiautomation'] = None
raise SystemExit(desktop._smoke_test(0))
"""
        result = subprocess.run([sys.executable, "-B", "-c", script],
                                cwd=PROJECT_ROOT,
                                env={**os.environ, "RUC_AGENT_DATA_DIR": self.data_dir},
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
