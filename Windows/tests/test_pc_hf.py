"""PC-level Windows Hugging Face and engine-management regressions.

The suite intentionally uses task-owned temporary roots and in-memory HTTP-like
responses so it can validate the state machine even when the device policy
blocks loopback sockets. Existing local-HTTP tests remain the evidence for the
real socket path when loopback permission is available.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
for entry in (ROOT / "RemoteAgent", ROOT / "Windows"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))
os.environ.setdefault("MODELMATRIX_TEST_SECRET_STORE", "1")

import hf_agent  # noqa: E402
from hf_agent import (  # noqa: E402
    DownloadManager,
    EngineRegistry,
    HFHost,
    HFHostPaths,
    HFAgentError,
    WindowsEngineInstaller,
    WindowsEngineFinder,
    WindowsHostFacts,
    WindowsProfileMaker,
)
from hf_local import LocalHF  # noqa: E402


MODEL_BYTES = bytes((index * 29) % 256 for index in range(1_201))
MODEL_SHA = hashlib.sha256(MODEL_BYTES).hexdigest()
ENGINE_URL = (
    "https://github.com/ggml-org/llama.cpp/releases/download/b11748/"
    "llama-b11748-bin-win-cpu-x64.zip"
)


class MemoryResponse(io.BytesIO):
    def __init__(self, body: bytes, *, start: int = 0, blocked: bool = False):
        super().__init__(body)
        self.status = 206 if start else 200
        self.headers = {"Content-Length": str(len(body))}
        if start:
            self.headers["Content-Range"] = f"bytes {start}-{len(MODEL_BYTES) - 1}/{len(MODEL_BYTES)}"
        self.blocked = blocked
        self.first_read = threading.Event()
        self.release = threading.Event()
        self._waited = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def read(self, size: int = -1):  # noqa: D401
        value = super().read(size)
        if value and self.blocked and not self._waited:
            self._waited = True
            self.first_read.set()
            self.release.wait(3)
        return value


class MemoryProfiles:
    def run(self, manager, job_id: str, explicit: bool) -> None:
        manager._set(manager.jobs[job_id], profile_state="created", profile_message="Fixture profile")
        manager.save()

    def delete(self, *_args):
        return None


class PCWindowsHFTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def service(self):
        return SimpleNamespace(
            configuration=SimpleNamespace(root=self.root, profiles_directory=self.root / "profiles", port=7700)
        )

    def test_windows_facts_report_cpu_budget_without_posix_gpu_claims(self):
        paths = HFHostPaths(self.root / "agent", home=self.root / "home", environ={})
        facts = WindowsHostFacts(system="Windows", machine="AMD64")
        facts.memory = lambda: (16 * (1 << 30), 12 * (1 << 30))
        facts.nvidia = lambda: ([], None)
        payload = facts.payload(paths)
        self.assertEqual((payload["kind"], payload["accelerator"], payload["budget_source"]), ("windows", "cpu", "cpu"))
        self.assertEqual(payload["budget_bytes"], int(16 * (1 << 30) * 0.6))
        self.assertIsNone(payload["vulkan"])
        self.assertIsNone(payload["rocm"])

    def test_model_check_selects_verified_variant_using_memory_and_disk(self):
        local = LocalHF(self.service())
        self.addCleanup(local.close)
        local.finder.detect = lambda: []
        local.facts.payload = lambda _paths: {
            "budget_bytes": 8 * (1 << 20),
            "free_disk_bytes": 2 * (1 << 30),
        }
        local._request_json = lambda url, _token=None, **_kwargs: (
            {
                "sha": "a" * 40,
                "pipeline_tag": "text-generation",
                "gated": False,
                "cardData": {"license": "mit"},
            }
            if "/tree/" not in url
            else [
                {"path": "model-Q4_K_M.gguf", "size": len(MODEL_BYTES), "lfs": {"sha256": "b" * 64}},
                {"path": "model-Q8_0.gguf", "size": 20 * (1 << 20), "lfs": {"sha256": "c" * 64}},
            ]
        )
        report = local._repo_report("owner/fixture-model", "main", 512)
        levels = {item["label"]: item["fit"]["level"] for item in report["variants"]}
        self.assertEqual(report["verdict"]["code"], "ready")
        self.assertEqual(levels["Q4_K_M"], "good")
        self.assertEqual(levels["Q8_0"], "too_big")
        self.assertEqual(report["recommendation"]["label"], "Q4_K_M")
        self.assertTrue(report["download_available"])

    def test_download_pause_resume_uses_range_and_verifies_checksum(self):
        paths = HFHostPaths(self.root / "agent", home=self.root / "home", environ={})
        seen_ranges: list[str | None] = []
        blocked_once = {"value": True}
        blocking_response: MemoryResponse | None = None

        def fetch(request, _token, _timeout):
            nonlocal blocking_response
            raw = request.headers.get("Range")
            seen_ranges.append(raw)
            start = int(raw.split("=", 1)[1].split("-", 1)[0]) if raw else 0
            response = MemoryResponse(MODEL_BYTES[start:], start=start, blocked=blocked_once["value"])
            if blocked_once["value"]:
                blocked_once["value"] = False
                blocking_response = response
            return response

        manager = DownloadManager(paths, MemoryProfiles(), fetch=fetch, start_worker=False, sleep_scale=0)
        self.addCleanup(manager.close)
        job_id = "dl-pchf0001"
        manager.add({
            "job": {"id": job_id, "kind": "files", "repo": "owner/fixture-model", "revision": "main",
                     "label": "Q4_K_M", "engine": "llama.cpp", "display_name": "Fixture",
                     "files": [{"path": "model.gguf", "size": len(MODEL_BYTES), "sha256": MODEL_SHA}],
                     "main_file": "model.gguf", "context": 4096, "start_after": False},
            "folder_name": "owner-fixture-model", "endpoint": "https://fixture.invalid",
        })
        with mock.patch.object(hf_agent, "free_disk_bytes", return_value=10 * (1 << 30)), \
                mock.patch.object(hf_agent, "CHUNK_BYTES", 257):
            worker = threading.Thread(target=manager.run_job, args=(job_id,), daemon=True)
            worker.start()
            deadline = time.monotonic() + 3
            while blocking_response is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertIsNotNone(blocking_response, manager.get(job_id))
            self.assertTrue(blocking_response.first_read.wait(3))
            manager.action({"id": job_id, "action": "pause"})
            blocking_response.release.set()
            worker.join(3)
            self.assertFalse(worker.is_alive())
            paused = manager.get(job_id)
            self.assertEqual(paused["state"], "paused")
            self.assertGreater(paused["files"][0]["bytes_done"], 0)
            manager.action({"id": job_id, "action": "resume"})
            manager.run_job(job_id)
        done = manager.get(job_id)
        target = Path(done["folder"]) / "model.gguf"
        self.assertEqual(done["state"], "completed")
        self.assertEqual(target.read_bytes(), MODEL_BYTES)
        self.assertEqual(seen_ranges[1], "bytes=257-")

    def test_windows_zip_installer_keeps_dll_and_rejects_traversal(self):
        archive_buffer = io.BytesIO()
        with zipfile.ZipFile(archive_buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("release/bin/llama-server.exe", b"fixture-server")
            archive.writestr("release/ggml.dll", b"fixture-dll")
        archive_bytes = archive_buffer.getvalue()
        paths = HFHostPaths(self.root / "agent", home=self.root / "home", environ={})
        registry = EngineRegistry(paths.state / "engines.json")

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

        installer = WindowsEngineInstaller(
            paths,
            registry,
            WindowsEngineFinder(paths, registry),
            opener=lambda _request, _timeout: Response(archive_bytes),
            run=lambda _argv, _timeout=60, _env=None: (0, "version: fixture-1.2.3"),
        )
        job = installer.start_install({"engine": "llama.cpp", "archives": [{"url": ENGINE_URL,
            "size": len(archive_bytes), "sha256": hashlib.sha256(archive_bytes).hexdigest()}]}, run_async=False)
        self.assertEqual((job["state"], job["version"]), ("succeeded", "fixture-1.2.3"))
        bin_dir = paths.engines / "llama.cpp" / "bin"
        self.assertEqual((bin_dir / "llama-server.exe").read_bytes(), b"fixture-server")
        self.assertEqual((bin_dir / "ggml.dll").read_bytes(), b"fixture-dll")
        self.assertEqual(registry.get("llama.cpp")["path"], str(bin_dir / "llama-server.exe"))
        traversal = io.BytesIO()
        with zipfile.ZipFile(traversal, "w") as archive:
            archive.writestr("../escape/llama-server.exe", b"bad")
        installer.opener = lambda _request, _timeout: Response(traversal.getvalue())
        failed = installer.start_install({"engine": "llama.cpp", "archives": [{"url": ENGINE_URL,
            "size": len(traversal.getvalue()), "sha256": hashlib.sha256(traversal.getvalue()).hexdigest()}]}, run_async=False)
        self.assertEqual(failed["state"], "failed")
        self.assertFalse((self.root / "escape").exists())

    def test_engine_plan_is_not_installed_until_explicit_one_use_approval(self):
        local = LocalHF(self.service())
        self.addCleanup(local.close)
        local.finder.detect = lambda: []
        digest = "d" * 64
        local._request_json = lambda _url, **_kwargs: {"tag_name": "b11748", "assets": [{
            "name": "llama-b11748-bin-win-cpu-x64.zip", "digest": f"sha256:{digest}", "size": 123,
            "browser_download_url": ENGINE_URL,
        }]}
        with mock.patch.object(hf_agent.platform, "machine", return_value="AMD64"):
            plan = local._plan({"engine": "llama.cpp", "action": "install"})
        self.assertEqual(plan["downloads"][0]["sha256"], digest)
        self.assertFalse(local.registry.get("llama.cpp"))
        with self.assertRaises(HFAgentError) as refused:
            local._run_plan({"plan_id": plan["id"]})
        self.assertEqual(refused.exception.code, "approval_required")
        with mock.patch.object(local.installer, "start_install", return_value={"state": "succeeded"}) as start:
            result = local._run_plan({"plan_id": plan["id"], "approved": True})
        self.assertEqual(result["job"]["state"], "succeeded")
        start.assert_called_once()
        with self.assertRaises(HFAgentError) as reused:
            local._run_plan({"plan_id": plan["id"], "approved": True})
        self.assertEqual(reused.exception.code, "plan_expired")

    def test_windows_hf_factory_selects_native_adapters(self):
        service = self.service()
        with mock.patch.object(hf_agent.platform, "system", return_value="Windows"):
            host = HFHost(service, start_worker=False, profile_options={"port_in_use": lambda _port: False})
        self.addCleanup(host.downloads.close)
        self.assertIsInstance(host.facts, WindowsHostFacts)
        self.assertIsInstance(host.finder, hf_agent.WindowsEngineFinder)
        self.assertIsInstance(host.profiles, WindowsProfileMaker)
        self.assertIsInstance(host.installer, hf_agent.WindowsEngineInstaller)


if __name__ == "__main__":
    unittest.main()
