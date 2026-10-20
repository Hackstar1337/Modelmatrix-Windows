"""Small, dependency-free Win32 helpers used by the Windows Dashboard bridge.

The module is importable on non-Windows CI.  On Windows every handle and HWND
uses a pointer-sized ctypes type and every API has explicit ``argtypes`` and
``restype`` declarations.  Closing is deliberately cooperative: this module
only posts WM_CLOSE after re-reading the HWND's PID, creation time, and image
path.  It never calls TerminateProcess, taskkill, or any other forced stop.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import os
import platform
import time
from pathlib import Path
from typing import Any

IS_WINDOWS = os.name == "nt" or platform.system().lower() == "windows"

# Pointer-sized aliases.  wintypes.HWND is c_void_p on supported CPython, but
# declaring the aliases here makes the ABI explicit and easy to test.
HWND = ctypes.c_void_p
HANDLE = ctypes.c_void_p
LPARAM = ctypes.c_ssize_t
WPARAM = ctypes.c_size_t
LRESULT = ctypes.c_ssize_t

WM_CLOSE = 0x0010
GW_OWNER = 4
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
STILL_ACTIVE = 259


class FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]


class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def _api(dll: Any, name: str, argtypes: list[Any], restype: Any) -> Any:
    """Get a Win32 function and install its ABI declarations."""
    fn = getattr(dll, name)
    fn.argtypes = argtypes
    fn.restype = restype
    return fn


def _apis() -> dict[str, Any]:
    if not IS_WINDOWS:
        return {}
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    callback = ctypes.WINFUNCTYPE(wintypes.BOOL, HWND, LPARAM)
    return {
        "EnumWindows": _api(user32, "EnumWindows", [callback, LPARAM], wintypes.BOOL),
        "IsWindowVisible": _api(user32, "IsWindowVisible", [HWND], wintypes.BOOL),
        "GetWindowTextLengthW": _api(user32, "GetWindowTextLengthW", [HWND], ctypes.c_int),
        "GetWindowTextW": _api(user32, "GetWindowTextW", [HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
        "GetWindowLongPtrW": _api(user32, "GetWindowLongPtrW", [HWND, ctypes.c_int], ctypes.c_ssize_t),
        "GetWindow": _api(user32, "GetWindow", [HWND, wintypes.UINT], HWND),
        "GetClassNameW": _api(user32, "GetClassNameW", [HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
        "GetWindowThreadProcessId": _api(user32, "GetWindowThreadProcessId", [HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
        "PostMessageW": _api(user32, "PostMessageW", [HWND, wintypes.UINT, WPARAM, LPARAM], wintypes.BOOL),
        "OpenProcess": _api(kernel32, "OpenProcess", [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], HANDLE),
        "CloseHandle": _api(kernel32, "CloseHandle", [HANDLE], wintypes.BOOL),
        "QueryFullProcessImageNameW": _api(kernel32, "QueryFullProcessImageNameW", [HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
        "GetProcessTimes": _api(kernel32, "GetProcessTimes", [HANDLE, ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME)], wintypes.BOOL),
        "GetExitCodeProcess": _api(kernel32, "GetExitCodeProcess", [HANDLE, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
        "GetCurrentProcessId": _api(kernel32, "GetCurrentProcessId", [], wintypes.DWORD),
        "GetProcessMemoryInfo": _api(psapi, "GetProcessMemoryInfo", [HANDLE, ctypes.POINTER(PROCESS_MEMORY_COUNTERS), wintypes.DWORD], wintypes.BOOL),
        "callback": callback,
    }


def _filetime_value(value: FILETIME) -> int:
    return (int(value.dwHighDateTime) << 32) | int(value.dwLowDateTime)


def _process_details(pid: int, api: dict[str, Any]) -> tuple[str | None, int | None, int | None]:
    handle = api["OpenProcess"](PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, int(pid))
    if not handle:
        handle = api["OpenProcess"](PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return None, None, None
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        length = wintypes.DWORD(len(buffer))
        path = None
        if api["QueryFullProcessImageNameW"](handle, 0, buffer, ctypes.byref(length)):
            path = buffer.value
        created = None
        started, exited, kernel, user = FILETIME(), FILETIME(), FILETIME(), FILETIME()
        if api["GetProcessTimes"](handle, ctypes.byref(started), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)):
            created = _filetime_value(started)
        bytes_used = None
        counters = PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(counters)
        if api["GetProcessMemoryInfo"](handle, ctypes.byref(counters), counters.cb):
            bytes_used = int(counters.WorkingSetSize)
        return path, created, bytes_used
    finally:
        api["CloseHandle"](handle)


def _title(hwnd: HWND, api: dict[str, Any]) -> str:
    length = max(0, int(api["GetWindowTextLengthW"](hwnd)))
    buffer = ctypes.create_unicode_buffer(length + 1)
    api["GetWindowTextW"](hwnd, buffer, len(buffer))
    return buffer.value.strip()


def _class_name(hwnd: HWND, api: dict[str, Any]) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    api["GetClassNameW"](hwnd, buffer, len(buffer))
    return buffer.value


def _window_identity(hwnd: int | HWND, api: dict[str, Any]) -> dict[str, Any] | None:
    raw_handle = getattr(hwnd, "value", hwnd)
    if raw_handle is None:
        return None
    handle = HWND(int(raw_handle))
    pid = wintypes.DWORD()
    if not api["GetWindowThreadProcessId"](handle, ctypes.byref(pid)) or not pid.value:
        return None
    path, created, bytes_used = _process_details(int(pid.value), api)
    if not path or created is None:
        return None
    return {"hwnd": int(handle.value or 0), "pid": int(pid.value), "path": path, "creation_time": int(created), "bytes": int(bytes_used or 0)}


def _is_system_path(path: str) -> bool:
    normalized = os.path.normcase(os.path.abspath(path)).replace("/", "\\")
    windows = os.path.normcase(os.environ.get("WINDIR", r"C:\Windows")).replace("/", "\\").rstrip("\\")
    return normalized == windows or normalized.startswith(windows + "\\")


def inventory_apps() -> list[dict[str, Any]]:
    """Enumerate visible user application windows with stable process identity."""
    api = _apis()
    if not api:
        return []
    own_pid = int(api["GetCurrentProcessId"]())
    rows: dict[tuple[str, int, int], dict[str, Any]] = {}
    system_classes = {"Shell_TrayWnd", "Shell_SecondaryTrayWnd", "NotifyIconOverflowWindow", "Progman", "WorkerW", "DV2ControlHost"}

    def callback(hwnd: HWND, _lparam: LPARAM) -> bool:
        try:
            if not api["IsWindowVisible"](hwnd):
                return True
            if api["GetWindow"](hwnd, GW_OWNER):
                return True
            style = int(api["GetWindowLongPtrW"](hwnd, GWL_EXSTYLE))
            class_name = _class_name(hwnd, api)
            if style & WS_EX_TOOLWINDOW or class_name in system_classes:
                return True
            title = _title(hwnd, api)
            if not title:
                return True
            identity = _window_identity(hwnd, api)
            if not identity or identity["pid"] == own_pid or _is_system_path(identity["path"]):
                return True
            key = (identity["path"].casefold(), identity["pid"], identity["creation_time"])
            row = rows.setdefault(key, {"title": title, "name": Path(identity["path"]).stem or identity["path"], "hwnd_ids": [], **identity})
            if int(identity["hwnd"]) not in row["hwnd_ids"]:
                row["hwnd_ids"].append(int(identity["hwnd"]))
            row["title"] = row.get("title") or title
        except (OSError, TypeError, ValueError):
            pass
        return True

    cb = api["callback"](callback)
    if not api["EnumWindows"](cb, 0):
        return []
    return sorted(rows.values(), key=lambda row: (str(row.get("name", "")).casefold(), int(row.get("pid", 0))))


def close_apps(apps: list[dict[str, Any]], *, timeout: float = 8.0) -> list[dict[str, Any]]:
    """Revalidate and request WM_CLOSE for each selected app; never force-kill."""
    api = _apis()
    if not api:
        return [{"path": str(item.get("path") or ""), "name": str(item.get("name") or ""), "status": "refused", "ok": False, "quit_requested": False, "message": "Windows window control is unavailable."} for item in apps]
    results: list[dict[str, Any]] = []
    for requested in apps:
        path = str(requested.get("path") or "")
        name = str(requested.get("name") or Path(path).stem or "application")
        result = {"path": path, "name": name, "status": "refused", "ok": False, "quit_requested": False, "message": "Window identity could not be verified."}
        try:
            handles = [int(value) for value in requested.get("hwnd_ids", []) if str(value).isdigit()]
            if not handles and requested.get("hwnd"):
                handles = [int(requested["hwnd"])]
            if not handles:
                matches = [row for row in inventory_apps() if row.get("path", "").casefold() == path.casefold() and (not name or row.get("name") == name)]
                handles = [int(value) for value in (matches[0].get("hwnd_ids", [matches[0].get("hwnd")]) if matches else []) if value]
            expected_pid = int(requested.get("pid") or 0)
            expected_creation = int(requested.get("creation_time") or 0)
            actuals = [_window_identity(hwnd, api) for hwnd in handles]
            if not actuals or any(not actual or (expected_pid and actual["pid"] != expected_pid) or (expected_creation and actual["creation_time"] != expected_creation) or actual["path"].casefold() != path.casefold() for actual in actuals):
                results.append(result)
                continue
            visible = [actual for actual in actuals if api["IsWindowVisible"](HWND(actual["hwnd"]))]
            if not visible:
                result.update(status="still_open", message="The application window is no longer visible.")
                results.append(result)
                continue
            posted = all(bool(api["PostMessageW"](HWND(actual["hwnd"]), WM_CLOSE, WPARAM(0), LPARAM(0))) for actual in visible)
            result["quit_requested"] = posted
            result["status"] = "quit" if posted else "still_open"
            result["ok"] = posted
            result["message"] = "WM_CLOSE was requested; the application may still ask to save work." if posted else "Windows rejected the close request."
        except (OSError, TypeError, ValueError):
            pass
        results.append(result)
    return results
