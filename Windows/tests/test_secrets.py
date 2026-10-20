from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "Windows"))
from secrets_store import SecretStore, SecretStoreError


class SecretStoreTests(unittest.TestCase):
    def test_linux_requires_explicit_test_mode(self):
        if os.name == "nt":
            self.skipTest("DPAPI is the production backend on Windows")
        previous = os.environ.pop("MODELMATRIX_TEST_SECRET_STORE", None)
        try:
            with tempfile.TemporaryDirectory() as folder:
                with self.assertRaises(SecretStoreError):
                    SecretStore(folder)
        finally:
            if previous is not None:
                os.environ["MODELMATRIX_TEST_SECRET_STORE"] = previous

    def test_explicit_test_store_persists_and_deletes(self):
        with tempfile.TemporaryDirectory() as folder:
            first = SecretStore(folder, test_mode=True)
            first.set("gateway-a", "secret-value")
            second = SecretStore(folder, test_mode=True)
            self.assertEqual(second.get("gateway-a"), "secret-value")
            self.assertTrue(second.has("gateway-a"))
            second.delete("gateway-a")
            self.assertIsNone(first.get("gateway-a"))
            self.assertFalse((Path(folder) / "secrets" / "store.bin").exists())

    def test_names_are_validated(self):
        with tempfile.TemporaryDirectory() as folder:
            store = SecretStore(folder, test_mode=True)
            with self.assertRaises(SecretStoreError):
                store.set("../escape", "x")
            with self.assertRaises(SecretStoreError):
                store.get("bad/name")

    def test_store_file_is_user_only_in_test_backend(self):
        if os.name == "nt":
            self.skipTest("Unix mode bits are not the Windows ACL check")
        with tempfile.TemporaryDirectory() as folder:
            store = SecretStore(folder, test_mode=True)
            store.set("x", "y")
            mode = (Path(folder) / "secrets" / "store.bin").stat().st_mode & 0o777
            self.assertEqual(mode, 0o600)


if __name__ == "__main__":
    unittest.main()
