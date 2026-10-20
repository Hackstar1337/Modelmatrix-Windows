from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
WINDOWS = ROOT / "Windows"
REMOTE_AGENT = ROOT / "RemoteAgent"
if str(WINDOWS) not in sys.path:
    sys.path.insert(0, str(WINDOWS))
if str(REMOTE_AGENT) not in sys.path:
    sys.path.insert(0, str(REMOTE_AGENT))
if str(Path(__file__).parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).parent))

import native_windows  # noqa: E402
from hf_local import LocalHF  # noqa: E402
from secrets_store import SecretStore  # noqa: E402
from test_expanded_distribution import NativeWindowsDistributionFixtureTests  # noqa: E402


@unittest.skipUnless(os.name == "nt", "native desktop regressions require Windows")
class NativePCDesktopTests(unittest.TestCase):
    """Real Win32 checks confined to a task-owned QA directory and processes."""

    @classmethod
    def setUpClass(cls):
        cls.work = ROOT.parent / ".work" / "pc-desktop-qa"
        cls.work.mkdir(parents=True, exist_ok=True)
        cls._servers = []

    @classmethod
    def tearDownClass(cls):
        for server in cls._servers:
            server.shutdown()
            server.server_close()
        shutil.rmtree(cls.work, ignore_errors=True)

    @staticmethod
    def _windows_for_pid(pid: int, class_name: str | None = None) -> list[int]:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM), wintypes.LPARAM]
        user32.EnumWindows.restype = wintypes.BOOL
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetClassNameW.restype = ctypes.c_int
        found: list[int] = []

        def callback(hwnd, _lparam):
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if int(owner.value) == int(pid):
                buf = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(hwnd, buf, len(buf))
                if class_name is None or buf.value == class_name:
                    found.append(int(getattr(hwnd, "value", hwnd) or 0))
            return True

        user32.EnumWindows(ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(callback), 0)
        return found

    @staticmethod
    def _window_title(hwnd: int) -> str:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowTextW.restype = ctypes.c_int
        handle = wintypes.HWND(hwnd)
        length = max(0, int(user32.GetWindowTextLengthW(handle)))
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(handle, buf, len(buf))
        return buf.value

    @staticmethod
    def _post_quit(hwnd: int) -> None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.PostMessageW.restype = wintypes.BOOL
        # WM_COMMAND / tray.py ID_QUIT.
        if not user32.PostMessageW(wintypes.HWND(hwnd), 0x0111, 1003, 0):
            raise ctypes.WinError(ctypes.get_last_error())

    @staticmethod
    def _wait(predicate, timeout: float = 10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.1)
        return predicate()

    def _status_server(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def do_GET(self):  # noqa: N802
                if self.path == "/api/status":
                    body = json.dumps({"statuses": []}).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_error(404)

        try:
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        except OSError as error:
            self.skipTest(f"loopback HTTP fixture blocked: {error!r}")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self._servers.append(server)
        return f"http://127.0.0.1:{server.server_port}"

    def test_tray_real_window_icon_duplicate_start_and_quit(self):
        endpoint = self._status_server()
        env = dict(os.environ)
        env.update(
            {
                "MODELMATRIX_CONTROLLER_URL": endpoint,
                "MODELMATRIX_DATA_ROOT": str(self.work / "tray-data"),
                "MODELMATRIX_CONTROLLER_LAUNCHER": str(self.work / "does-not-exist.cmd"),
                "PYTHONPATH": str(WINDOWS),
            }
        )
        tray = subprocess.Popen(
            [sys.executable, str(WINDOWS / "tray.py"), "--no-browser"],
            cwd=str(WINDOWS),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            windows = self._wait(lambda: self._windows_for_pid(tray.pid, "ModelMatrixTrayWindow"))
            self.assertTrue(windows, "tray did not create its owned hidden window")
            duplicate = subprocess.run(
                [sys.executable, str(WINDOWS / "tray.py"), "--no-browser"],
                cwd=str(WINDOWS),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self.assertEqual(duplicate.returncode, 0, duplicate.stderr.decode(errors="replace"))
            self._post_quit(windows[0])
            tray.wait(timeout=10)
            stderr = tray.stderr.read().decode(errors="replace") if tray.stderr else ""
            if tray.stdout:
                tray.stdout.close()
            if tray.stderr:
                tray.stderr.close()
            self.assertEqual(tray.returncode, 0, stderr)
        finally:
            if tray.poll() is None:
                for hwnd in self._windows_for_pid(tray.pid, "ModelMatrixTrayWindow"):
                    try:
                        self._post_quit(hwnd)
                    except OSError:
                        pass
                try:
                    tray.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    tray.kill()
                    tray.wait(timeout=5)

    def test_explorer_inventory_and_close_are_title_scoped(self):
        token = f"ModelMatrix QA Explorer {os.getpid()} {time.time_ns()}"
        first_folder = self.work / (token + " A")
        second_folder = self.work / (token + " B")
        first_folder.mkdir(parents=True, exist_ok=True)
        second_folder.mkdir(parents=True, exist_ok=True)
        explorer = Path(os.environ.get("WINDIR", r"C:\Windows")) / "explorer.exe"
        first = subprocess.Popen([str(explorer), str(first_folder)], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        second = subprocess.Popen([str(explorer), str(second_folder)], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            hwnds = self._wait(
                lambda: [
                    hwnd
                    for hwnd in self._windows_for_pid(first.pid) + self._windows_for_pid(second.pid)
                    if token in self._window_title(hwnd)
                ],
                timeout=15,
            )
            # Explorer can hand off both requests to the existing shell process;
            # inventory identity, rather than Popen PID, is the authoritative data.
            if not hwnds:
                candidates = [row for row in native_windows.inventory_apps() if token in row.get("title", "")]
                hwnds = [int(candidates[0]["hwnd_ids"][0])] if candidates else []
            if not hwnds:
                self.skipTest("Explorer handed the isolated QA folder to the existing shell window; no positively identified QA window was created")
            own_hwnd = hwnds[-1]
            title = self._window_title(own_hwnd)
            rows = [row for row in native_windows.inventory_apps() if own_hwnd in row.get("hwnd_ids", [])]
            self.assertTrue(rows, "inventory did not expose the positively identified Explorer HWND")
            row = rows[0]
            request = dict(row, title=title, hwnd_ids=[own_hwnd])
            result = native_windows.close_apps([request], timeout=5)
            self.assertEqual(len(result), 1)
            self.assertTrue(result[0]["quit_requested"], result)
            self._wait(lambda: not self._window_title(own_hwnd), timeout=8)
            remaining = [row for row in native_windows.inventory_apps() if token in row.get("title", "")]
            self.assertFalse(any(own_hwnd in row.get("hwnd_ids", []) for row in remaining), remaining)
        finally:
            # Only close the two positively identified QA titles; never issue a
            # blanket Explorer/process close that could affect user windows.
            for hwnd in set(self._windows_for_pid(first.pid) + self._windows_for_pid(second.pid)):
                if token in self._window_title(hwnd):
                    try:
                        rows = [row for row in native_windows.inventory_apps() if hwnd in row.get("hwnd_ids", [])]
                        if rows:
                            native_windows.close_apps([dict(rows[0], title=self._window_title(hwnd), hwnd_ids=[hwnd])])
                    except OSError:
                        pass
            for child in (first, second):
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass

    def test_native_inventory_reports_memory_and_cooperative_close(self):
        child_code = NativeWindowsDistributionFixtureTests.CHILD
        child = subprocess.Popen([sys.executable, "-c", child_code], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            row = self._wait(
                lambda: next((item for item in native_windows.inventory_apps() if item.get("pid") == child.pid and "ModelMatrix native fixture" in item.get("title", "")), None),
                timeout=10,
            )
            self.assertIsNotNone(row)
            self.assertGreater(int(row.get("bytes") or 0), 0)
            result = native_windows.close_apps([row])
            self.assertTrue(result[0]["quit_requested"], result)
            child.wait(timeout=8)
            self.assertIsNotNone(child.returncode)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)

    def test_clipboard_preserves_text_and_refuses_non_text(self):
        before = native_windows.clipboard_text()
        if before is None:
            # Empty or non-text clipboard: the conservative setter must not
            # guess what to destroy. It returns False for non-text formats.
            changed = native_windows.set_clipboard_text("ModelMatrix QA clipboard")
            if changed:
                try:
                    self.assertEqual(native_windows.clipboard_text(), "ModelMatrix QA clipboard")
                finally:
                    # Restore the empty case only; if another format appeared
                    # while the test ran, the setter refuses to overwrite it.
                    native_windows.set_clipboard_text("")
            return
        marker = "ModelMatrix QA clipboard"
        if not native_windows.set_clipboard_text(marker):
            # Existing HTML/image/file-drop formats are intentionally not
            # overwritten even when CF_UNICODETEXT is also present.
            self.assertEqual(native_windows.clipboard_text(), before)
            return
        try:
            self.assertEqual(native_windows.clipboard_text(), marker)
        finally:
            self.assertTrue(native_windows.set_clipboard_text(before))
        self.assertEqual(native_windows.clipboard_text(), before)

    def test_dpapi_store_round_trip_does_not_leave_plaintext(self):
        root = self.work / "dpapi"
        store = SecretStore(root)
        value = "desktop-qa-secret"
        store.set("desktop-qa", value)
        raw = (root / "secrets" / "store.bin").read_bytes()
        self.assertNotIn(value.encode("utf-8"), raw)
        self.assertEqual(store.get("desktop-qa"), value)
        store.delete("desktop-qa")
        self.assertFalse((root / "secrets" / "store.bin").exists())

    def test_keep_awake_release_calls_continuous_clear(self):
        states: list[bool] = []
        dummy = SimpleNamespace(_awake_stop=threading.Event())
        dummy._set_keep_awake = lambda enabled: states.append(bool(enabled))
        dummy._awake_stop.set()
        LocalHF._keep_awake_loop(dummy)
        self.assertEqual(states, [False])
        # Exercise the actual native calls as well; release must not raise.
        LocalHF._set_keep_awake(True)
        LocalHF._set_keep_awake(False)


if __name__ == "__main__":
    unittest.main()
