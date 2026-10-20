from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
for entry in (ROOT / "RemoteAgent", ROOT / "Windows"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))
os.environ.setdefault("MODELMATRIX_TEST_SECRET_STORE", "1")

import native_windows  # noqa: E402
from hf_local import LocalHF  # noqa: E402


class NativeAndReleaseFixtureTests(unittest.TestCase):
    def test_native_module_uses_pointer_sized_abi_types(self):
        self.assertEqual(native_windows.HWND, native_windows.ctypes.c_void_p)
        self.assertEqual(native_windows.LPARAM, native_windows.ctypes.c_ssize_t)
        self.assertEqual(native_windows.WPARAM, native_windows.ctypes.c_size_t)
        apps = native_windows.inventory_apps()
        if os.name != "nt":
            self.assertEqual(apps, [])
        else:
            for app in apps:
                self.assertGreater(app["pid"], 0)
                self.assertGreater(app["hwnd"], 0)
                self.assertGreater(app["creation_time"], 0)
                self.assertTrue(app["path"])
                self.assertNotEqual(app["pid"], os.getpid())

    def test_latest_marker_is_followed_only_when_latest_has_no_cpu_asset(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            local = LocalHF(SimpleNamespace(configuration=SimpleNamespace(root=root, profiles_directory=root / "profiles")))
            self.addCleanup(local.close)
            digest = "a" * 64
            latest = {"tag_name": "v0.7.0", "assets": []}
            nightly = {"tag_name": "b123", "assets": [{
                "name": "llama-b123-bin-win-x64.zip", "digest": f"sha256:{digest}", "size": 12,
                "browser_download_url": "https://github.com/ggml-org/llama.cpp/releases/download/b123/llama-b123-bin-win-x64.zip",
            }]}
            with mock.patch.object(local, "_request_json", side_effect=[latest, nightly]), mock.patch.object(local, "_request_text", return_value="b123") as marker:
                plan = local._plan({"engine": "llama.cpp", "action": "install"})
            marker.assert_called_once()
            self.assertEqual(plan["version"], "b123")
            self.assertEqual(plan["downloads"][0]["sha256"], digest)


if __name__ == "__main__":
    unittest.main()
