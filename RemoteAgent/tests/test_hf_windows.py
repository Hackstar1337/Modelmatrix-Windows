"""Windows HF primitives that can run on Linux CI with injected I/O."""
from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

AGENT = Path(__file__).resolve().parents[1]
if str(AGENT) not in sys.path:
    sys.path.insert(0, str(AGENT))

import hf_agent  # noqa: E402
from hf_agent import EngineRegistry, HFAgentError, HFHostPaths, WindowsEngineInstaller, WindowsHostFacts, safe_extract_zip  # noqa: E402


class _Response:
    status = 200

    def __init__(self, payload: bytes):
        self.payload, self.offset = payload, 0
        self.headers = {}

    def read(self, size=-1):
        if size < 0:
            size = len(self.payload)
        data = self.payload[self.offset:self.offset + size]
        self.offset += len(data)
        return data

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class WindowsHFTests(unittest.TestCase):
    def test_windows_facts_are_explicit_and_not_linux(self):
        facts = WindowsHostFacts(system="Windows", machine="AMD64", read=lambda _: "", run=lambda *_: (1, ""), exists=lambda _: False, find=lambda _: [])
        payload = facts.payload(HFHostPaths(Path(tempfile.mkdtemp()), home=Path(tempfile.mkdtemp()), environ={}))
        self.assertEqual(payload["kind"], "windows")
        self.assertIsNone(payload["vulkan"])
        self.assertIsNone(payload["glibc"])
        self.assertIn(payload["architecture"], ("x86_64", "AMD64"))

    def test_zip_traversal_and_reserved_names_are_rejected(self):
        for member in ("../escape.exe", "C:/escape.exe", "folder/CON.txt"):
            with tempfile.TemporaryDirectory() as directory:
                archive = Path(directory) / "engine.zip"
                with zipfile.ZipFile(archive, "w") as zipped:
                    zipped.writestr(member, b"bad")
                with self.assertRaises(HFAgentError):
                    safe_extract_zip(archive, Path(directory) / "out")

    def test_official_windows_zip_is_checksum_verified_and_installed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = HFHostPaths(root / "agent", home=root / "home", environ={})
            registry = EngineRegistry(paths.state / "engines.json")
            payload = io.BytesIO()
            with zipfile.ZipFile(payload, "w") as zipped:
                zipped.writestr("llama-b123/llama-server.exe", b"MZ-test")
                zipped.writestr("llama-b123/ggml.dll", b"dll")
            data = payload.getvalue()
            url = "https://github.com/ggml-org/llama.cpp/releases/download/b123/llama-b123-bin-win-x64.zip"

            class Finder:
                def forget_versions(self):
                    pass

            installer = WindowsEngineInstaller(paths, registry, Finder(), opener=lambda *_: _Response(data),
                                                run=lambda *_: (0, "version: b123"))
            job = installer.start_install({"engine": "llama.cpp", "archives": [{"url": url, "size": len(data),
                "sha256": __import__("hashlib").sha256(data).hexdigest()}], "version": "b123"}, run_async=False)
            self.assertEqual(job["state"], "succeeded", job)
            self.assertTrue((paths.engines / "llama.cpp" / "bin" / "llama-server.exe").is_file())
            self.assertEqual(registry.get("llama.cpp")["path"].lower().endswith("llama-server.exe"), True)

    def test_windows_archive_url_and_digest_are_required(self):
        with self.assertRaises(HFAgentError):
            WindowsEngineInstaller.validate({"engine": "llama.cpp", "archives": [{"url": "https://example.com/a.zip", "size": 1, "sha256": "a" * 64}]})


if __name__ == "__main__":
    unittest.main()
