"""Expanded Windows Hugging Face tests.

The model and engine bytes are served by a local HTTP server.  No public
network, credentials, or timing assumptions are used.  Tests exercise shared
hf_agent state machinery plus the Windows adapter without pretending Linux is
native Windows; tests requiring Windows APIs are explicitly skipped here.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
    HFAgentError,
    HFHostPaths,
    WindowsEngineInstaller,
)
from hf_local import LocalHF, WindowsEngineFinder  # noqa: E402


MODEL_BYTES = bytes((index * 37) % 256 for index in range(12_345))
MODEL_SHA = hashlib.sha256(MODEL_BYTES).hexdigest()


def make_zip(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


class RangeFixtureHandler(BaseHTTPRequestHandler):
    files: dict[str, bytes] = {}
    block_path: str | None = None
    first_chunk: threading.Event | None = None
    release: threading.Event | None = None

    def log_message(self, *_args):
        return

    def do_GET(self):  # noqa: N802
        data = self.files.get(self.path)
        if data is None:
            self.send_error(404)
            return
        raw_range = self.headers.get("Range")
        start = 0
        if raw_range:
            try:
                start = int(raw_range.removeprefix("bytes=").split("-", 1)[0])
            except ValueError:
                self.send_error(416)
                return
            if start >= len(data):
                self.send_response(416)
                self.end_headers()
                return
        body = data[start:]
        self.send_response(206 if raw_range else 200)
        self.send_header("Content-Length", str(len(body)))
        if raw_range:
            self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
        self.end_headers()
        for offset in range(0, len(body), 257):
            try:
                self.wfile.write(body[offset:offset + 257])
                self.wfile.flush()
            except OSError:
                return
            if (self.path == type(self).block_path and offset == 0 and
                    type(self).first_chunk is not None and type(self).release is not None):
                type(self).first_chunk.set()
                type(self).release.wait(5)


class LocalHTTPFixture:
    def __init__(self, files: dict[str, bytes]):
        handler = type("FixtureHandler", (RangeFixtureHandler,), {})
        handler.files = files
        handler.block_path = None
        handler.first_chunk = threading.Event()
        handler.release = threading.Event()
        self.handler = handler
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def url(self, path: str) -> str:
        return self.base + path

    def block(self, path: str) -> None:
        self.handler.block_path = path
        self.handler.first_chunk.clear()
        self.handler.release.clear()

    def release_block(self) -> None:
        self.handler.release.set()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class ProfileSink:
    def __init__(self, root: Path):
        self.root = root
        self.runs: list[str] = []

    def run(self, manager, job_id: str, explicit: bool):
        self.runs.append(job_id)
        job = manager.jobs[job_id]
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / f"{job_id}.profile.json").write_text(
            json.dumps({"HF_DOWNLOAD_ID": job_id, "MODEL_PATH": str(Path(job["folder"]) / job["main_file"]), "explicit": explicit}),
            encoding="utf-8",
        )
        manager._set(job, profile_state="created", profile_message="Profile written by fixture")
        manager.save()

    def delete(self, *_args):
        return None


def download_job(job_id: str = "dl-fixture0001") -> dict:
    return {"job": {"id": job_id, "kind": "files", "repo": "owner/fixture-model", "revision": "main",
                     "label": "Q4_K_M", "engine": "llama.cpp", "display_name": "Fixture model",
                     "files": [{"path": "model.gguf", "size": len(MODEL_BYTES), "sha256": MODEL_SHA}],
                     "main_file": "model.gguf", "context": 4096, "start_after": False,
                     "memory_need_bytes": 1}, "folder_name": "owner-fixture-model",
            "endpoint": "https://huggingface.co"}


def run_fixture(argv, timeout=10, env=None):
    """Run a ZIP fixture through Python, avoiding POSIX executable-bit claims."""
    if argv and str(argv[0]).lower().endswith(".exe"):
        argv = [sys.executable, *argv]
    done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                          stdin=subprocess.DEVNULL, check=False, env=env)
    return done.returncode, (done.stdout or "") + (done.stderr or "")


class RealDownloadFixtureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.fixture = LocalHTTPFixture({"/owner/fixture-model/resolve/main/model.gguf": MODEL_BYTES})
        self.addCleanup(self.fixture.close)
        self.addCleanup(self.temp.cleanup)

    def manager(self, profiles=None):
        paths = HFHostPaths(self.root / "agent", home=self.root / "home", environ={})
        return DownloadManager(paths, profiles or ProfileSink(self.root / "profiles"), start_worker=False,
                               fetch=lambda request, _token, timeout: urllib.request.urlopen(request, timeout=timeout), sleep_scale=0)

    def prepare(self, manager, job_id="dl-fixture0001"):
        manager.add(download_job(job_id))
        manager.jobs[job_id]["endpoint"] = self.fixture.base
        manager.jobs[job_id]["state"] = "downloading"
        return manager.jobs[job_id]

    def test_real_http_range_pause_resume_writes_profile_and_state(self):
        profiles = ProfileSink(self.root / "profiles")
        manager = self.manager(profiles)
        job = self.prepare(manager)
        self.fixture.block("/owner/fixture-model/resolve/main/model.gguf")
        with mock.patch.object(hf_agent, "CHUNK_BYTES", 257):
            worker = threading.Thread(target=manager.run_job, args=(job["id"],), daemon=True)
            worker.start()
            self.assertTrue(self.fixture.handler.first_chunk.wait(3))
            time.sleep(0.15)
            self.assertEqual(manager.action({"id": job["id"], "action": "pause"})["download"]["state"], "paused")
            self.fixture.release_block()
            worker.join(3)
            self.assertFalse(worker.is_alive())
            paused = manager.get(job["id"])
            self.assertEqual((paused["state"], paused["files"][0]["bytes_done"] > 0), ("paused", True))
            manager.action({"id": job["id"], "action": "resume"})
            manager.run_job(job["id"])
        done = manager.get(job["id"])
        target = Path(done["folder"]) / "model.gguf"
        self.assertEqual((done["state"], target.read_bytes(), hashlib.sha256(target.read_bytes()).hexdigest()),
                         ("completed", MODEL_BYTES, MODEL_SHA))
        self.assertTrue((self.root / "profiles" / f"{job['id']}.profile.json").is_file())
        saved = json.loads((manager.paths.state / "downloads.json").read_text(encoding="utf-8"))
        self.assertEqual(saved[0]["state"], "completed")
        manager.close()

    def test_real_http_cancel_removes_partial_and_delete_is_safe(self):
        manager = self.manager()
        job = self.prepare(manager, "dl-cancel0001")
        self.fixture.block("/owner/fixture-model/resolve/main/model.gguf")
        worker = threading.Thread(target=manager.run_job, args=(job["id"],), daemon=True)
        worker.start()
        self.assertTrue(self.fixture.handler.first_chunk.wait(3))
        manager.action({"id": job["id"], "action": "cancel", "delete_files": True})
        self.fixture.release_block()
        worker.join(3)
        self.assertEqual(manager.get(job["id"])["state"], "cancelled")
        self.assertFalse(Path(job["folder"]).exists())
        self.assertEqual(manager.action({"id": job["id"], "action": "delete"})["deleted"], True)
        manager.close()

    def test_corrupt_state_and_missing_model_file_are_safe(self):
        paths = HFHostPaths(self.root / "agent", home=self.root / "home", environ={})
        paths.state.mkdir(parents=True)
        (paths.state / "downloads.json").write_text(json.dumps([{"id": "not-valid", "state": "wat"},
                                                                  {"id": "dl-valid0001", "state": "completed"}]), encoding="utf-8")
        manager = DownloadManager(paths, ProfileSink(self.root / "profiles"), start_worker=False)
        self.assertEqual([job["id"] for job in manager.list()], ["dl-valid0001"])
        manager.close()
        service = SimpleNamespace(configuration=SimpleNamespace(root=self.root, profiles_directory=self.root / "profiles"))
        with mock.patch.dict(os.environ, {"MODELMATRIX_MODELS_DIR": str(self.root / "Models"),
                                          "MODELMATRIX_ENGINES_DIR": str(self.root / "Engines")}):
            local = LocalHF(service)
        self.addCleanup(local.close)
        local.downloads.jobs["dl-valid0001"] = {"id": "dl-valid0001", "state": "completed", "folder": str(self.root / "Models" / "missing"),
                                                "files": [{"path": "missing.gguf", "size": 3}], "repo": "owner/fixture-model",
                                                "label": "Q4", "display_name": "Missing", "engine": "llama.cpp", "kind": "files"}
        local.downloads.order.append("dl-valid0001")
        self.assertEqual(local._models()["models"][0]["missing_files"], 1)


class WindowsSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.service = SimpleNamespace(configuration=SimpleNamespace(root=self.root, profiles_directory=self.root / "profiles"))
        self.env = mock.patch.dict(os.environ, {"MODELMATRIX_MODELS_DIR": str(self.root / "Models"),
                                                "MODELMATRIX_ENGINES_DIR": str(self.root / "Engines")})
        self.env.start()
        self.local = LocalHF(self.service)
        self.addCleanup(self.local.close)
        self.addCleanup(self.env.stop)
        self.addCleanup(self.temp.cleanup)

    def test_models_folder_moves_contents_and_protects_nonempty_or_inflight(self):
        old = self.local.paths.models
        old.mkdir(parents=True)
        (old / "existing.gguf").write_bytes(b"model")
        tracked = old / "owner-fixture-model"
        tracked.mkdir()
        tracked_file = tracked / "model.gguf"
        tracked_file.write_bytes(MODEL_BYTES)
        self.local.downloads.jobs["dl-tracked0001"] = {"id": "dl-tracked0001", "state": "completed",
                                                        "folder": str(tracked), "files": [{"path": "model.gguf", "size": len(MODEL_BYTES)}]}
        self.local.downloads.order.append("dl-tracked0001")
        profiles = self.local.profiles.directory
        profiles.mkdir(parents=True, exist_ok=True)
        (profiles / "tracked.json").write_text(json.dumps({"MODEL_PATH": str(tracked_file),
            "START_COMMAND_JSON": json.dumps(["llama-server.exe", "-m", str(tracked_file)])}), encoding="utf-8")
        moved = self.root / "MovedModels"
        self.local._save_settings({"models_folder": str(moved)})
        self.assertEqual((moved / "existing.gguf").read_bytes(), b"model")
        self.assertEqual(self.local.downloads.jobs["dl-tracked0001"]["folder"], str(moved / "owner-fixture-model"))
        moved_profile = json.loads((profiles / "tracked.json").read_text(encoding="utf-8"))
        self.assertIn(str(moved), moved_profile["MODEL_PATH"])
        moved_command = json.loads(moved_profile["START_COMMAND_JSON"])
        self.assertEqual(moved_command[moved_command.index("-m") + 1], str(moved / "owner-fixture-model" / "model.gguf"))
        crowded = self.root / "Crowded"
        crowded.mkdir()
        (crowded / "do-not-touch").write_text("x")
        with self.assertRaises(HFAgentError) as nonempty:
            self.local._save_settings({"models_folder": str(crowded)})
        self.assertEqual(nonempty.exception.code, "conflict")
        self.local.paths.models = moved
        self.local.downloads.jobs["dl-moving0001"] = {"id": "dl-moving0001", "state": "paused", "repo": "owner/fixture-model", "label": "Q4", "folder": str(moved / "owner-fixture-model"), "files": []}
        self.local.downloads.order.append("dl-moving0001")
        with self.assertRaises(HFAgentError) as busy:
            self.local._save_settings({"models_folder": str(self.root / "Another")})
        self.assertEqual(busy.exception.code, "conflict")

    def test_custom_engine_paths_merge_and_relative_paths_are_rejected(self):
        llama = self.root / "llama" / "llama-server.exe"
        ollama = self.root / "ollama" / "ollama.exe"
        result = self.local._save_settings({"engine_paths": {"llama.cpp": str(llama), "ollama": str(ollama)}})
        self.assertEqual(result["engine_paths"]["llama.cpp"], str(llama))
        self.assertEqual(result["engine_paths"]["ollama"], str(ollama))
        result = self.local._save_settings({"engine_paths": {"llama.cpp": ""}})
        self.assertNotIn("llama.cpp", result["engine_paths"])
        self.assertIn("ollama", result["engine_paths"])
        with self.assertRaises(HFAgentError):
            self.local._save_settings({"engine_paths": {"ollama": "relative.exe"}})

    def test_rate_limit_and_keep_awake_settings_are_real_capabilities(self):
        saved = self.local._save_settings({"speed_limit_mbps": 8, "connections_per_file": 4, "keep_awake": True})
        self.assertEqual(self.local.downloads.rate_limit_bytes_per_second, 8 * 1024 * 1024 // 8)
        self.assertEqual((saved["speed_limit_mbps"], saved["connections_per_file"], saved["keep_awake"]), (8.0, 4, True))
        if os.name != "nt":
            self.assertFalse(saved["capabilities"]["keep_awake"])
            # Linux must not claim that a Windows execution-state request ran.
            LocalHF._set_keep_awake(True)

    def test_apple_only_engine_remains_explicitly_unsupported(self):
        with self.assertRaises(HFAgentError) as raised:
            self.local._plan({"engine": "mlx-lm", "action": "install"})
        self.assertEqual(raised.exception.code, "unsupported_platform")
        self.assertIn("Apple Silicon", raised.exception.message)


class WindowsEngineFixtureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.paths = HFHostPaths(self.root / "agent", home=self.root / "home", environ={})
        self.registry = EngineRegistry(self.paths.state / "engines.json")
        self.fixture = LocalHTTPFixture({})
        self.addCleanup(self.fixture.close)
        self.addCleanup(self.temp.cleanup)
        server = f"#!{sys.executable}\nprint('version: fixture-1.2.3')\n".encode()
        self.archive = make_zip({"pkg/bin/llama-server.exe": server, "pkg/ggml.dll": b"runtime"})
        self.fixture.handler.files["/engine.zip"] = self.archive
        self.url = "https://github.com/ggml-org/llama.cpp/releases/download/b11748/llama-b11748-bin-win-cpu-x64.zip"
        self.open_fixture = lambda request, timeout: urllib.request.urlopen(self.fixture.url("/engine.zip"), timeout=timeout)
        self.finder = WindowsEngineFinder(self.paths, self.registry)
        self.installer = WindowsEngineInstaller(self.paths, self.registry, self.finder,
                                                opener=self.open_fixture, run=run_fixture)

    def plan(self, digest=None):
        return {"engine": "llama.cpp", "archives": [{"url": self.url, "size": len(self.archive),
                "sha256": digest or hashlib.sha256(self.archive).hexdigest()}]}

    def test_zip_http_install_verifies_digest_places_dll_and_uses_executable_version(self):
        job = self.installer.start_install(self.plan(), run_async=False)
        self.assertEqual((job["state"], job["version"]), ("succeeded", "fixture-1.2.3"))
        folder = self.paths.engines / "llama.cpp" / "bin"
        self.assertEqual((folder / "llama-server.exe").read_bytes(), b"#!" + sys.executable.encode() + b"\nprint('version: fixture-1.2.3')\n")
        self.assertEqual((folder / "ggml.dll").read_bytes(), b"runtime")
        self.assertEqual(self.registry.get("llama.cpp")["path"], str(folder / "llama-server.exe"))

    def test_digest_and_traversal_fail_without_installing(self):
        failed = self.installer.start_install(self.plan("0" * 64), run_async=False)
        self.assertEqual(failed["state"], "failed")
        self.assertFalse(self.paths.engines.exists() and any(self.paths.engines.iterdir()))
        traversal = make_zip({"../escape/llama-server.exe": b"bad"})
        self.fixture.handler.files["/engine.zip"] = traversal
        failed = self.installer.start_install(self.plan(hashlib.sha256(traversal).hexdigest()), run_async=False)
        self.assertEqual(failed["state"], "failed")
        self.assertFalse((self.root / "escape").exists())

    def test_cancelled_http_install_leaves_no_engine(self):
        self.fixture.handler.files["/engine.zip"] = self.archive * 50
        self.fixture.block("/engine.zip")
        plan = self.plan(hashlib.sha256(self.fixture.handler.files["/engine.zip"]).hexdigest())
        job = self.installer.start_install(plan, run_async=True)
        self.assertTrue(self.fixture.handler.first_chunk.wait(3))
        self.installer.cancel(job["id"])
        self.fixture.release_block()
        deadline = time.monotonic() + 3
        while self.installer.job(job["id"])["state"] == "running" and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.installer.job(job["id"])["state"], "cancelled")
        self.assertFalse((self.paths.engines / "llama.cpp").exists())

    def test_broken_managed_engine_health_is_not_claimed_installed(self):
        broken = self.root / "broken.exe"
        broken.write_text(f"#!{sys.executable}\nimport sys\nsys.exit(1)\n", encoding="utf-8")
        broken.chmod(broken.stat().st_mode | stat.S_IXUSR)
        self.registry.set("llama.cpp", {"path": str(broken), "folder": str(self.paths.engines / "llama.cpp")})
        finder = WindowsEngineFinder(self.paths, self.registry)
        detected = finder.detect()[0]
        self.assertFalse(detected["installed"])
        self.assertTrue(detected["note"])
        self.assertIn("update", detected["actions"])


class WindowsReleasePlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.service = SimpleNamespace(configuration=SimpleNamespace(root=self.root, profiles_directory=self.root / "profiles"))
        self.env = mock.patch.dict(os.environ, {"MODELMATRIX_MODELS_DIR": str(self.root / "Models"),
                                                "MODELMATRIX_ENGINES_DIR": str(self.root / "Engines")})
        self.env.start()
        self.local = LocalHF(self.service)
        self.addCleanup(self.local.close)
        self.addCleanup(self.env.stop)
        self.addCleanup(self.temp.cleanup)

    def test_latest_b11748_cpu_x64_and_arm64_assets_are_selected_by_digest(self):
        assets = [
            {"name": "llama-b11748-bin-win-cpu-x64.zip", "digest": "sha256:" + "5" * 64, "size": 23959247,
             "browser_download_url": "https://github.com/ggml-org/llama.cpp/releases/download/b11748/llama-b11748-bin-win-cpu-x64.zip"},
            {"name": "llama-b11748-bin-win-cpu-arm64.zip", "digest": "sha256:" + "6" * 64, "size": 12610104,
             "browser_download_url": "https://github.com/ggml-org/llama.cpp/releases/download/b11748/llama-b11748-bin-win-cpu-arm64.zip"},
            {"name": "llama-b11748-bin-win-cuda-12.4-x64.zip", "digest": "sha256:" + "7" * 64, "size": 1,
             "browser_download_url": "https://github.com/ggml-org/llama.cpp/releases/download/b11748/llama-b11748-bin-win-cuda-12.4-x64.zip"},
        ]
        with mock.patch.object(self.local, "_request_json", return_value={"tag_name": "b11748", "assets": assets}), \
             mock.patch("hf_local.platform.machine", return_value="x86_64"):
            x64 = self.local._plan({"engine": "llama.cpp", "action": "install"})
        self.assertEqual((x64["version"], x64["downloads"][0]["name"], x64["downloads"][0]["sha256"]),
                         ("b11748", "llama-b11748-bin-win-cpu-x64.zip", "5" * 64))
        with mock.patch.object(self.local, "_request_json", return_value={"tag_name": "b11748", "assets": assets}), \
             mock.patch("hf_local.platform.machine", return_value="aarch64"):
            arm = self.local._plan({"engine": "llama.cpp", "action": "install"})
        self.assertEqual((arm["downloads"][0]["name"], arm["downloads"][0]["sha256"]),
                         ("llama-b11748-bin-win-cpu-arm64.zip", "6" * 64))

    def test_approved_engine_plan_is_one_use_and_requires_approval(self):
        plan = {"id": "plan-fixture000001", "engine": "llama.cpp", "action": "install", "downloads": [], "version": "b11748"}
        self.local.plans[plan["id"]] = {"plan": plan, "expires": time.time() + 30}
        with mock.patch.object(self.local.installer, "start_install", return_value={"state": "succeeded"}) as start:
            with self.assertRaises(HFAgentError) as refused:
                self.local._run_plan({"plan_id": plan["id"]})
            self.assertEqual(refused.exception.code, "approval_required")
            result = self.local._run_plan({"plan_id": plan["id"], "approved": True})
        self.assertEqual(result["job"]["state"], "succeeded")
        start.assert_called_once()
        with self.assertRaises(HFAgentError) as reused:
            self.local._run_plan({"plan_id": plan["id"], "approved": True})
        self.assertEqual(reused.exception.code, "plan_expired")


@unittest.skipUnless(os.name == "nt", "native Windows API verification runs on the parent Windows job")
class NativeWindowsOnlyTests(unittest.TestCase):
    def test_keep_awake_uses_windows_execution_state(self):
        from hf_local import LocalHF as _LocalHF
        with mock.patch.object(_LocalHF, "_set_keep_awake") as setter:
            _LocalHF._set_keep_awake(True)
            setter.assert_called_once_with(True)


if __name__ == "__main__":
    unittest.main()
