from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
WINDOWS = ROOT / "Windows"
if str(WINDOWS) not in sys.path:
    sys.path.insert(0, str(WINDOWS))
import modelmatrixctl  # noqa: E402
import native_windows  # noqa: E402
import secrets_store  # noqa: E402


class ExpandedDistributionTests(unittest.TestCase):
    def read(self, relative: str) -> str:
        return (ROOT / relative).read_text(encoding="utf-8")

    def test_package_is_source_complete_and_state_free(self):
        source = self.read("Scripts/package-windows.ps1")
        self.assertIn("Copy-Tree 'Dashboard'", source)
        self.assertIn("node_modules", source)
        self.assertIn("__pycache__", source)
        self.assertIn("ReparsePoint", source)
        self.assertIn("Version -notmatch", source)
        self.assertIn("OutputDirectory", source)
        self.assertNotIn("Copy-Item -LiteralPath $src -Destination $destination -Recurse", source)

    def test_install_launchers_use_explicit_environment_and_arguments(self):
        source = self.read("Scripts/install-windows.ps1")
        self.assertNotIn("$args =", source.lower())
        self.assertIn("MODELMATRIX_DATA_ROOT", source)
        self.assertIn("ValueFromRemainingArguments", source)
        self.assertIn("WindowsApps", source)

    def test_lifecycle_records_identity_environment_and_port_owner(self):
        start = self.read("Scripts/start-windows.ps1")
        stop = self.read("Scripts/stop-windows.ps1")
        verify = self.read("Scripts/verify-windows.ps1")
        self.assertIn("arguments = @($controllerArgs)", start)
        self.assertIn("environment = @{", start)
        self.assertIn("Get-NetTCPConnection", start)
        self.assertIn("started_utc", start)
        self.assertIn("AuthTokenFile", stop)
        self.assertIn("Authenticated shutdown failed", stop)
        self.assertIn("AuthTokenFile", verify)
        self.assertIn("refusing to remove its identity record", stop)

    def test_uninstall_keeps_data_without_explicit_purge(self):
        source = self.read("Scripts/uninstall-windows.ps1")
        self.assertIn("if ($Purge", source)
        self.assertIn("KeepStartup", source)
        self.assertNotIn("Remove-Item -LiteralPath $DataRoot", source.split("if ($Purge", 1)[0])
        self.assertNotIn("reg.exe", source.lower())

    def test_secret_acl_helper_is_safe_to_mock(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "secret.bin"
            path.write_bytes(b"x")
            completed = type("Completed", (), {"returncode": 0})()
            with patch.object(secrets_store.os, "name", "nt"), patch.object(
                secrets_store.subprocess, "run", return_value=completed
            ) as run:
                secrets_store._harden_acl(path, directory=False)
            command = run.call_args.args[0]
            self.assertEqual(command[0], "icacls")
            self.assertIn("/inheritance:r", command)
            self.assertIn("/grant:r", command)

    def test_cli_never_prints_or_sends_scoped_token_as_payload(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self):
                return b'{"started":true}'

        class FakeOpener:
            def __init__(self):
                self.requests = []

            def open(self, request, timeout=0):
                self.requests.append(request)
                return FakeResponse()

        opener = FakeOpener()
        with tempfile.TemporaryDirectory() as folder:
            token_file = Path(folder) / "token.txt"
            token_file.write_text("scoped-secret", encoding="utf-8")
            out = io.StringIO()
            with patch.object(modelmatrixctl, "build_opener", return_value=opener), patch("sys.stdout", out):
                self.assertEqual(
                    modelmatrixctl.main(
                        ["--auth-token-file", str(token_file), "--json", "start", "demo"]
                    ),
                    0,
                )
            rendered = out.getvalue()
            self.assertNotIn("scoped-secret", rendered)
            self.assertEqual(opener.requests[-1].get_header("Authorization"), "Bearer scoped-secret")
            body = opener.requests[-1].data.decode("utf-8")
            self.assertNotIn("scoped-secret", body)


@unittest.skipUnless(os.name == "nt", "native Win32 child-window fixture requires Windows")
class NativeWindowsDistributionFixtureTests(unittest.TestCase):
    CHILD = r'''
import ctypes, sys
from ctypes import wintypes
u = ctypes.WinDLL("user32", use_last_error=True)
k = ctypes.WinDLL("kernel32", use_last_error=True)
k.GetModuleHandleW.argtypes=[wintypes.LPCWSTR]; k.GetModuleHandleW.restype=wintypes.HINSTANCE
WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH), ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", wintypes.HICON)]
u.DefWindowProcW.argtypes=[wintypes.HWND,wintypes.UINT,wintypes.WPARAM,wintypes.LPARAM]; u.DefWindowProcW.restype=ctypes.c_ssize_t
u.RegisterClassExW.argtypes=[ctypes.POINTER(WNDCLASSEXW)]; u.RegisterClassExW.restype=wintypes.ATOM
u.CreateWindowExW.argtypes=[wintypes.DWORD,wintypes.LPCWSTR,wintypes.LPCWSTR,wintypes.DWORD,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_int,wintypes.HWND,wintypes.HMENU,wintypes.HINSTANCE,wintypes.LPVOID]; u.CreateWindowExW.restype=wintypes.HWND
u.ShowWindow.argtypes=[wintypes.HWND,ctypes.c_int]; u.ShowWindow.restype=wintypes.BOOL
u.GetMessageW.argtypes=[ctypes.POINTER(wintypes.MSG),wintypes.HWND,wintypes.UINT,wintypes.UINT]; u.GetMessageW.restype=wintypes.BOOL
u.TranslateMessage.argtypes=[ctypes.POINTER(wintypes.MSG)]; u.DispatchMessageW.argtypes=[ctypes.POINTER(wintypes.MSG)]; u.DispatchMessageW.restype=ctypes.c_ssize_t
u.PostQuitMessage.argtypes=[ctypes.c_int]
proc = WNDPROC(lambda hwnd,msg,w,l: (u.PostQuitMessage(0) or 0) if msg == 2 else u.DefWindowProcW(hwnd,msg,w,l))
name = "ModelMatrixNativeFixture"
wc = WNDCLASSEXW(ctypes.sizeof(WNDCLASSEXW), 0, proc, 0, 0, k.GetModuleHandleW(None), None, None, None, None, name, None)
u.RegisterClassExW(ctypes.byref(wc))
hwnd = u.CreateWindowExW(0, name, "ModelMatrix native fixture", 0, 50, 50, 400, 200, None, None, wc.hInstance, None)
u.ShowWindow(hwnd, 5)
msg = wintypes.MSG()
while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
    u.TranslateMessage(ctypes.byref(msg)); u.DispatchMessageW(ctypes.byref(msg))
'''

    def test_controlled_child_window_only_receives_cooperative_close(self):
        child = subprocess.Popen([sys.executable, "-c", self.CHILD], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.addCleanup(lambda: child.poll() is None and child.kill())
        row = None
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and row is None:
            for candidate in native_windows.inventory_apps():
                if candidate.get("pid") == child.pid and "fixture" in candidate.get("title", "").lower():
                    row = candidate
                    break
            time.sleep(0.1)
        self.assertIsNotNone(row)
        result = native_windows.close_apps([row])
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0]["quit_requested"])
        child.wait(timeout=5)
        self.assertNotEqual(child.returncode, None)

    def test_tray_fixture_does_not_install_persistent_startup_tasks(self):
        install = (ROOT / "Scripts" / "install-windows.ps1").read_text(encoding="utf-8")
        self.assertIn("StartupTask", install)
        self.assertIn("StartupShortcut", install)
        self.assertNotIn("Registry", install)


if __name__ == "__main__":
    unittest.main()
