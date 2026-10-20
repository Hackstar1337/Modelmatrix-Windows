"""Native Windows HFHost regression tests.

The tests run on any host by patching platform detection; they do not import
Windows/hf_local.py because the standalone installer ships RemoteAgent only.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

AGENT_DIR = Path(__file__).resolve().parents[1]
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

import hf_agent  # noqa: E402


class _ProfileStore:
    def load(self):
        return {}


class _Service:
    def __init__(self, root: Path):
        self.configuration = SimpleNamespace(root=root, profiles_directory=root / "profiles", port=7700)
        self.profiles = _ProfileStore()

    def _read_pid(self, _name):
        return None


class WindowsRemoteHFTests(unittest.TestCase):
    def _host(self, root: Path):
        service = _Service(root)
        with mock.patch.object(hf_agent.platform, "system", return_value="Windows"):
            host = hf_agent.HFHost(
                service,
                start_worker=False,
                profile_options={"port_in_use": lambda _port: False},
            )
        self.addCleanup(host.downloads.close)
        return host

    def test_factory_selects_native_windows_adapters(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = self._host(Path(tmp))
            self.assertIsInstance(host.facts, hf_agent.WindowsHostFacts)
            self.assertIsInstance(host.finder, hf_agent.WindowsEngineFinder)
            self.assertIsInstance(host.profiles, hf_agent.WindowsProfileMaker)
            self.assertIsInstance(host.installer, hf_agent.WindowsEngineInstaller)

    def test_profile_uses_structured_windows_argv_and_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            host = self._host(root)
            server = root / "engine folder" / "llama-server.exe"
            server.parent.mkdir(parents=True)
            server.write_text("fixture", encoding="utf-8")
            model_dir = root / "models" / "Qwen 7B"
            model_dir.mkdir(parents=True)
            job = {
                "id": "dl-123456789abc", "repo": "owner/Qwen-GGUF", "revision": "main",
                "label": "Q4_K_M", "display_name": "Qwen 7B", "folder": str(model_dir),
                "main_file": "qwen model.gguf", "projector_file": None, "context": 8192,
                "profile_hint": None, "model_hint": None,
            }
            name, model, port = host.profiles._write_profile(job, str(server))
            profile_path = root / "profiles" / f"{name}.json"
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
            argv = json.loads(profile["START_COMMAND_JSON"])
            args = json.loads(profile["SERVER_ARGS_JSON"])
            self.assertEqual(argv[0], str(server))
            self.assertEqual(argv[1:], args)
            self.assertTrue(any(value.endswith("qwen model.gguf") for value in args))
            self.assertEqual(profile["PORT"], str(port))
            self.assertEqual(profile["REQUEST_MODEL"], model)
            self.assertEqual(profile["RUNTIME"], "llama.cpp")
            self.assertEqual(profile["WORKING_DIRECTORY"], str(server.parent))
            self.assertNotIn("START_COMMAND", profile)

    def test_windows_log_path_is_writable_and_not_posix_tmp(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(root / "AppData")}, clear=False):
                host = self._host(root)
                path = host.profiles._log_path("ignored", {"LOG_ALIAS": "Qwen: 7B/unsafe"})
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("native log\n", encoding="utf-8")
                self.assertTrue(path.is_file())
                self.assertNotIn(":", path.name)
                self.assertNotIn("\\", path.name)
                self.assertEqual(path.parent, root / "AppData" / "ModelMatrix" / "logs")

    def test_native_engine_registry_is_used_for_managed_server(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = self._host(Path(tmp))
            server = Path(tmp) / "Engines" / "llama.cpp" / "bin" / "llama-server.exe"
            server.parent.mkdir(parents=True)
            server.write_text("fixture", encoding="utf-8")
            host.registry.set("llama.cpp", {"path": str(server), "version": "b123"})
            self.assertEqual(host.finder.llama_server(), (str(server), True))
            row = host.finder.detect()[0]
            self.assertEqual(row["path"], str(server))
            self.assertTrue(row["managed"])
            self.assertEqual(row["installed_by"], "ModelMatrix")


if __name__ == "__main__":
    unittest.main()
