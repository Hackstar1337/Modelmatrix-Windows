"""Small native Windows tray companion; no third-party GUI dependency."""
from __future__ import annotations
import argparse, ctypes, json, os, subprocess, sys, time, webbrowser
from ctypes import wintypes
from pathlib import Path
from urllib.request import Request, urlopen

if sys.platform != "win32":
    def main() -> int:
        print(json.dumps({"ok": False, "error": "the tray companion requires Windows"}))
        return 2
else:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    WM_USER, WM_COMMAND, WM_DESTROY, WM_LBUTTONUP, WM_RBUTTONUP = 0x400, 0x111, 0x2, 0x0202, 0x0205
    NIM_ADD, NIM_DELETE, NIF_MESSAGE, NIF_ICON, NIF_TIP = 0, 2, 1, 2, 4
    MF_STRING, MF_SEPARATOR = 0, 0x800
    ID_OPEN, ID_REFRESH, ID_QUIT, ID_START_CONTROLLER, ID_START = 1001, 1002, 1003, 1004, 2000
    WM_TRAY = WM_USER + 1
    LRESULT = ctypes.c_ssize_t
    UINT_PTR = ctypes.c_size_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]; kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
    kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]; kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]; kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.GetLastError.argtypes = []; kernel32.GetLastError.restype = wintypes.DWORD
    user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]; user32.CreateWindowExW.restype = wintypes.HWND
    user32.CreatePopupMenu.argtypes = []; user32.CreatePopupMenu.restype = wintypes.HMENU
    user32.DestroyMenu.argtypes = [wintypes.HMENU]; user32.DestroyMenu.restype = wintypes.BOOL
    user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, UINT_PTR, wintypes.LPCWSTR]; user32.AppendMenuW.restype = wintypes.BOOL
    user32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, ctypes.POINTER(wintypes.RECT)]; user32.TrackPopupMenu.restype = wintypes.BOOL
    user32.DestroyWindow.argtypes = [wintypes.HWND]; user32.DestroyWindow.restype = wintypes.BOOL
    user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]; user32.DefWindowProcW.restype = LRESULT
    user32.LoadIconW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]; user32.LoadIconW.restype = wintypes.HICON

    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT), ("uFlags", wintypes.UINT), ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON), ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD), ("szInfo", wintypes.WCHAR * 256), ("uVersion", wintypes.UINT), ("szInfoTitle", wintypes.WCHAR * 64), ("dwInfoFlags", wintypes.DWORD), ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", wintypes.HICON)]
    class WNDCLASSEXW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH), ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", wintypes.HICON)]

    user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]; user32.RegisterClassExW.restype = wintypes.ATOM
    shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]; shell32.Shell_NotifyIconW.restype = wintypes.BOOL
    user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]; user32.GetMessageW.restype = wintypes.BOOL
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]; user32.TranslateMessage.restype = wintypes.BOOL
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]; user32.DispatchMessageW.restype = LRESULT
    user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]; user32.GetCursorPos.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]; user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]; user32.PostMessageW.restype = wintypes.BOOL

    def _root() -> Path: return Path(os.environ.get("MODELMATRIX_DATA_ROOT") or (Path(os.environ.get("LOCALAPPDATA", Path.home())) / "ModelMatrix"))
    def _endpoint() -> str: return os.environ.get("MODELMATRIX_CONTROLLER_URL", "http://127.0.0.1:7700")
    def _auth_headers() -> dict[str, str]:
        headers = {"Accept": "application/json"}
        token = os.environ.get("MODELMATRIX_TOKEN")
        if not token:
            try:
                from modelmatrixctl import _secret
                token = _secret(_root())
            except Exception:
                token = None
        if token and not any(char in token for char in "\r\n"):
            headers["Authorization"] = "Bearer " + token
        return headers
    def _controller_launcher() -> Path:
        configured = os.environ.get("MODELMATRIX_CONTROLLER_LAUNCHER")
        if configured: return Path(configured)
        installed = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Programs" / "ModelMatrix" / "ModelMatrixController.cmd"
        if installed.is_file(): return installed
        return Path(__file__).resolve().parents[1] / "Scripts" / "start-windows.cmd"
    def _status() -> dict:
        req = Request(_endpoint().rstrip("/") + "/api/status", headers=_auth_headers())
        with urlopen(req, timeout=3) as response: return json.loads(response.read().decode("utf-8"))
    _launching = False
    def _ensure_controller() -> bool:
        global _launching
        try:
            _status(); return True
        except Exception:
            if _launching: return False
            launcher = _controller_launcher()
            if not launcher.is_file(): return False
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            try:
                _launching = True
                if launcher.suffix.lower() in {".cmd", ".bat"}:
                    command = '"' + os.environ.get("COMSPEC", "cmd.exe") + '" /d /s /c ""' + str(launcher) + '" -NoBrowser"'
                elif launcher.suffix.lower() == ".ps1":
                    command = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(launcher), "-NoBrowser"]
                else:
                    command = [str(launcher), "--no-browser"]
                subprocess.Popen(command, cwd=str(launcher.parent), creationflags=flags)
            except OSError:
                _launching = False
                return False
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                time.sleep(0.25)
                try:
                    _status(); _launching = False; return True
                except Exception: pass
            _launching = False
            return False
    def _invoke(command: str, *extra: str) -> bool:
        from modelmatrixctl import main as cli_main
        return cli_main([command, *extra, "--json"]) == 0
    def _profiles(status: dict) -> list[str]:
        rows = status.get("statuses") or status.get("profiles") or []
        if isinstance(rows, dict): return sorted(str(k) for k in rows)
        return sorted(str(row.get("profile") or row.get("name")) for row in rows if isinstance(row, dict) and (row.get("profile") or row.get("name")))

    class Tray:
        def __init__(self, no_browser: bool = False) -> None:
            self.no_browser = no_browser; self.hwnd = None; self.menu = None; self.profile_ids: dict[int, str] = {}; self._proc = None; self._mutex = None
        def _wndproc(self, hwnd, msg, wparam, lparam):
            if msg == WM_TRAY and lparam in (WM_LBUTTONUP, WM_RBUTTONUP):
                self._show_menu(); return 0
            if msg == WM_COMMAND:
                ident = int(wparam) & 0xFFFF
                if ident == ID_OPEN: _ensure_controller(); webbrowser.open(_endpoint()); return 0
                if ident == ID_REFRESH: self._rebuild_menu(); return 0
                if ident == ID_QUIT: user32.DestroyWindow(hwnd); return 0
                if ident == ID_START_CONTROLLER: _ensure_controller(); self._rebuild_menu(); return 0
                if ident in self.profile_ids:
                    profile = self.profile_ids[ident]
                    _invoke("start", profile) if ident < ID_START + 1000 else _invoke("stop", profile)
                    return 0
            if msg == WM_DESTROY:
                data = NOTIFYICONDATAW(); data.cbSize = ctypes.sizeof(data); data.hWnd = hwnd; data.uID = 1; shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(data)); user32.PostQuitMessage(0); return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
        def _rebuild_menu(self) -> None:
            if self.menu: user32.DestroyMenu(self.menu)
            self.menu = user32.CreatePopupMenu(); user32.AppendMenuW(self.menu, MF_STRING, ID_OPEN, "Open ModelMatrix Dashboard")
            user32.AppendMenuW(self.menu, MF_STRING, ID_START_CONTROLLER, "Start controller")
            user32.AppendMenuW(self.menu, MF_STRING, ID_REFRESH, "Refresh profiles"); user32.AppendMenuW(self.menu, MF_SEPARATOR, 0, None)
            try: profiles = _profiles(_status())
            except Exception: profiles = []
            self.profile_ids = {}
            for index, name in enumerate(profiles):
                sid, tid = ID_START + index, ID_START + 1000 + index; self.profile_ids[sid] = name; self.profile_ids[tid] = name
                user32.AppendMenuW(self.menu, MF_STRING, sid, "Start " + name); user32.AppendMenuW(self.menu, MF_STRING, tid, "Stop " + name)
            user32.AppendMenuW(self.menu, MF_SEPARATOR, 0, None); user32.AppendMenuW(self.menu, MF_STRING, ID_QUIT, "Quit tray")
        def _show_menu(self) -> None:
            point = wintypes.POINT(); user32.GetCursorPos(ctypes.byref(point)); user32.SetForegroundWindow(self.hwnd); user32.TrackPopupMenu(self.menu, 0, point.x, point.y, 0, self.hwnd, None); user32.PostMessageW(self.hwnd, 0, 0, 0)
        def run(self) -> int:
            self._mutex = kernel32.CreateMutexW(None, True, "Local\\ModelMatrixTray")
            if not self._mutex or kernel32.GetLastError() == 183:
                if self._mutex: kernel32.CloseHandle(self._mutex); self._mutex = None
                return 0
            _ensure_controller()
            instance = kernel32.GetModuleHandleW(None); cls_name = "ModelMatrixTrayWindow"
            self._proc = WNDPROC(self._wndproc); wc = WNDCLASSEXW(ctypes.sizeof(WNDCLASSEXW), 0, self._proc, 0, 0, instance, user32.LoadIconW(None, ctypes.c_void_p(32512)), None, None, None, cls_name, None)
            if not user32.RegisterClassExW(ctypes.byref(wc)) and ctypes.get_last_error() != 1410: raise ctypes.WinError()
            self.hwnd = user32.CreateWindowExW(0, cls_name, "ModelMatrix", 0, 0, 0, 0, 0, None, None, instance, None)
            if not self.hwnd: raise ctypes.WinError()
            self._rebuild_menu(); data = NOTIFYICONDATAW(); data.cbSize = ctypes.sizeof(data); data.hWnd = self.hwnd; data.uID = 1; data.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP; data.uCallbackMessage = WM_TRAY; data.hIcon = user32.LoadIconW(None, ctypes.c_void_p(32512)); data.szTip = "ModelMatrix"
            if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(data)):
                user32.DestroyWindow(self.hwnd); self.hwnd = None
                if self.menu: user32.DestroyMenu(self.menu); self.menu = None
                if self._mutex: kernel32.CloseHandle(self._mutex); self._mutex = None
                raise ctypes.WinError(ctypes.get_last_error() or 1)
            if not self.no_browser: webbrowser.open(_endpoint())
            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0: user32.TranslateMessage(ctypes.byref(msg)); user32.DispatchMessageW(ctypes.byref(msg))
            if self.menu: user32.DestroyMenu(self.menu); self.menu = None
            if self._mutex: kernel32.CloseHandle(self._mutex); self._mutex = None
            return int(msg.wParam)
    def main(argv: list[str] | None = None) -> int:
        p = argparse.ArgumentParser(description="ModelMatrix native Windows tray companion"); p.add_argument("--no-browser", action="store_true"); args = p.parse_args(argv); return Tray(args.no_browser).run()

if __name__ == "__main__": raise SystemExit(main())
