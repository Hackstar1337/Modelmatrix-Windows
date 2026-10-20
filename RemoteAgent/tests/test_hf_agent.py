"""Hugging Face on a Linux host: facts, approved engine installs, resumable downloads, profiles, routes."""

from __future__ import annotations

import email.message
import hashlib
import io
import json
import os
import shlex
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

AGENT_DIR = Path(__file__).resolve().parents[1]
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

import hf_agent  # noqa: E402
import modelmatrix_agent as agent  # noqa: E402
from hf_agent import (  # noqa: E402
    DownloadManager, EngineFinder, EngineInstaller, EngineRegistry, HFAgentError, HFHost, HFHostPaths,
    HostFacts, ProfileMaker,
)

GIB = 1 << 30
DATA = bytes(range(256)) * 40  # 10,240 bytes
PROJECTOR = b"mmproj" * 500
REPO = "owner/Model-GGUF"
ENDPOINT = "https://huggingface.co"
POSIX_EXECUTABLE_FIXTURE = unittest.skipUnless(
    os.name != "nt",
    "POSIX executable fixture; native Windows executable equivalents are covered by Windows tests",
)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_url(path: str, repo: str = REPO) -> str:
    return f"{ENDPOINT}/{repo}/resolve/main/{urllib.parse.quote(path)}"


def headers(values: dict[str, str] | None = None) -> email.message.Message:
    message = email.message.Message()
    for key, value in (values or {}).items():
        message[key] = value
    return message


class FakeResponse:
    def __init__(self, data: bytes, status: int = 200, extra: dict[str, str] | None = None,
                 fail_after: int | None = None, on_read=None):
        self.data, self.status, self.headers = data, status, headers(extra)
        self.fail_after, self.on_read, self.pos = fail_after, on_read, 0

    def read(self, size: int = -1) -> bytes:
        if self.on_read:
            self.on_read(self.pos)
        limit = len(self.data) if self.fail_after is None else min(len(self.data), self.fail_after)
        if self.fail_after is not None and self.pos >= limit and self.pos < len(self.data):
            raise ConnectionResetError("connection reset by peer")
        end = limit if size < 0 else min(limit, self.pos + size)
        chunk = self.data[self.pos:end]
        self.pos = end
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeHub:
    """Serves files like Hugging Face's resolve URLs, with scripted failures per URL."""

    def __init__(self, files: dict[str, bytes]):
        self.files = files
        self.requests: list[tuple[str, str | None, str | None]] = []
        self.plan: dict[str, list] = {}
        self.on_read = None

    def __call__(self, request: urllib.request.Request, token: str | None, timeout: float):
        url = request.full_url
        wanted = request.get_header("Range")
        self.requests.append((url, wanted, token))
        actions = self.plan.get(url) or []
        action = actions.pop(0) if actions else None
        if isinstance(action, int):
            raise urllib.error.HTTPError(url, action, "error", headers({"Retry-After": "7"} if action == 429 else None), None)
        data = self.files[url]
        if action == "corrupt":
            data = bytes([data[0] ^ 0xFF]) + data[1:]
        fail = int(action.split(":")[1]) if isinstance(action, str) and action.startswith("reset:") else None
        if wanted and action != "ignore_range":
            start = int(wanted.split("=")[1].split("-")[0])
            return FakeResponse(data[start:], 206, {"Content-Range": f"bytes {start}-{len(data) - 1}/{len(data)}"},
                                on_read=self.on_read)
        return FakeResponse(data, 200, fail_after=fail, on_read=self.on_read)


class FakeOllama:
    def __init__(self, had: bool | None = False):
        self.had = had
        self.pulled: list[str] = []
        self.deleted: list[str] = []

    def has_model(self, model: str):
        return self.had

    def pull(self, model, progress, should_stop):
        self.pulled.append(model)
        progress(50, 100, "pulling layer")
        progress(100, 100, "success")

    def delete(self, model: str) -> bool:
        self.deleted.append(model)
        return True

    def chat_ok(self, model: str):
        return True, "answered the test message"


class RecordingProfiles:
    def __init__(self):
        self.runs: list[tuple[str, bool]] = []

    def run(self, manager, job_id, explicit):
        self.runs.append((job_id, explicit))

    def delete(self, job, delete_files, ollama):
        return None


def parse_env(text: str) -> dict[str, str]:
    values = {}
    for line in text.splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        values[key] = value
    return values


class FakeProfileHost:
    def __init__(self, directory: Path):
        self.folder = directory
        self.started: list[str] = []
        self.stopped: list[str] = []
        self.pids: dict[str, int] = {}
        self.relay: str | None = None

    def directory(self) -> Path:
        return self.folder

    def profiles(self) -> dict[str, dict[str, str]]:
        if not self.folder.exists():
            return {}
        return {path.stem: parse_env(path.read_text(encoding="utf-8")) for path in self.folder.glob("*.env")}

    def start(self, name: str) -> None:
        self.started.append(name)
        self.pids[name] = 4242

    def stop(self, name: str) -> None:
        self.stopped.append(name)
        self.pids.pop(name, None)

    def pid(self, name: str):
        return self.pids.get(name)

    def relay_url(self):
        return self.relay

    def reserved_ports(self):
        return {7700}


class FakeFinder:
    def __init__(self, paths: HFHostPaths, server: str | None = "/opt/llama/bin/llama-server"):
        self.paths = paths
        self.server = server

    def llama_server(self):
        return self.server, True

    def llama_version(self, path):
        return ("4567 (abc1234)", None) if self.server else (None, "missing")

    def detect(self):
        return [{"id": "llama.cpp", "installed": bool(self.server), "path": self.server, "version": "4567"}]


class FakeFacts:
    def __init__(self, **values):
        self.values = {"kind": "linux", "architecture": "x86_64", "memory_bytes": 64 * GIB,
                       "available_memory_bytes": 48 * GIB, "budget_source": "cpu", "accelerator": "cpu", **values}

    def payload(self, paths):
        return dict(self.values, models_folder=str(paths.models))


def job_payload(job_id: str = "dl-abc123def", files: list[dict] | None = None, **extra) -> dict:
    files = files or [{"path": "Model-Q4_K_M.gguf", "size": len(DATA), "sha256": sha(DATA)}]
    job = {
        "id": job_id, "kind": "files", "repo": REPO, "revision": "main", "label": "Q4_K_M", "engine": "llama.cpp",
        "display_name": "Model", "files": files, "main_file": files[0]["path"], "context": 8192,
        "memory_need_bytes": 2 * GIB, "start_after": True,
    }
    job.update(extra)
    return {"job": job, "folder_name": REPO, "endpoint": ENDPOINT}


class Workspace:
    """Temporary home with models, engines and agent state folders."""

    def __init__(self, test: unittest.TestCase):
        self.tmp = tempfile.TemporaryDirectory()
        test.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.paths = HFHostPaths(self.root / "agent", home=self.home, environ={})
        self.hub = FakeHub({file_url("Model-Q4_K_M.gguf"): DATA, file_url("mmproj/mmproj-F16.gguf"): PROJECTOR})

    def manager(self, profiles=None, ollama=None) -> DownloadManager:
        manager = DownloadManager(self.paths, profiles or RecordingProfiles(), fetch=self.hub, ollama=ollama,
                                  sleep_scale=0, start_worker=False)
        self.sleeps: list[float] = []
        return manager


def run(manager: DownloadManager, job_id: str) -> dict:
    manager.jobs[job_id]["state"] = "downloading"
    manager.run_job(job_id)
    return manager.get(job_id)


# MARK: Host facts


def fake_run(outputs: dict[str, tuple[int | None, str]]):
    def runner(argv, timeout):
        key = argv[0] if len(argv) == 1 else f"{argv[0]} {argv[1]}"
        return outputs.get(key, (None, "not found"))
    return runner


MEMINFO = "MemTotal:       65536000 kB\nMemAvailable:   50000000 kB\n"


class HostFactsTests(unittest.TestCase):
    def facts(self, outputs, files=None, exists=(), find=(), machine="x86_64") -> dict:
        files = {"/proc/meminfo": MEMINFO, "/etc/os-release": 'PRETTY_NAME="Ubuntu 24.04 LTS"\n', **(files or {})}
        facts = HostFacts(run=fake_run(outputs), read=files.get, exists=lambda path: path in exists,
                          find=lambda pattern: list(find), system="Linux", machine=machine, libc=lambda: "2.39")
        return facts.payload(HFHostPaths(Path("/tmp/agent"), home=Path("/home/me"), environ={}))

    def test_nvidia_card_uses_its_memory_and_reports_cuda(self) -> None:
        facts = self.facts({
            "nvidia-smi --query-gpu=name,memory.total,driver_version": (0, "NVIDIA GeForce RTX 4090, 24564, 550.54\n"),
            "nvidia-smi": (0, "| NVIDIA-SMI 550.54   Driver Version: 550.54   CUDA Version: 12.4 |"),
        })
        self.assertEqual((facts["accelerator"], facts["budget_source"], facts["cuda_version"]), ("cuda", "gpu", "12.4"))
        self.assertEqual(facts["budget_bytes"], 24564 * (1 << 20) * 9 // 10)
        self.assertEqual(facts["gpu"], "NVIDIA GeForce RTX 4090 (24.0 GB)")
        self.assertEqual((facts["os"], facts["glibc"], facts["kind"]), ("Ubuntu 24.04 LTS", "2.39", "linux"))

    def test_unified_memory_nvidia_uses_system_memory(self) -> None:
        facts = self.facts({
            "nvidia-smi --query-gpu=name,memory.total,driver_version": (0, "NVIDIA GB10, [N/A], 580.95\n"),
            "nvidia-smi": (0, "CUDA Version: 13.0"),
        }, machine="aarch64")
        self.assertEqual((facts["budget_source"], facts["architecture"]), ("unified", "arm64"))
        self.assertEqual(facts["budget_bytes"], 65536000 * 1024 * 3 // 4)
        self.assertIn("shares system memory", facts["gpu"])

    def test_amd_card_is_read_from_sysfs(self) -> None:
        device = "/sys/class/drm/card0/device"
        facts = self.facts({}, files={f"{device}/vendor": "0x1002\n", f"{device}/mem_info_vram_total": "17163091968\n"},
                           exists=("/usr/lib/x86_64-linux-gnu/libvulkan.so.1",), find=(device,))
        self.assertEqual((facts["accelerator"], facts["budget_source"], facts["vulkan"]), ("amd", "gpu", True))
        self.assertEqual(facts["budget_bytes"], 17163091968 * 9 // 10)

    def test_software_vulkan_is_not_a_gpu(self) -> None:
        lib = ("/usr/lib/x86_64-linux-gnu/libvulkan.so.1",)
        # Cloud VM / container: the Vulkan library is there but no GPU device node.
        self.assertFalse(self.facts({}, exists=lib)["vulkan"])
        # A device node, but the only Vulkan device is llvmpipe on the processor.
        llvmpipe = {"vulkaninfo --summary": (0, "deviceType = PHYSICAL_DEVICE_TYPE_CPU\ndeviceName = llvmpipe\n")}
        self.assertFalse(self.facts(llvmpipe, exists=lib, find=("/dev/dri/renderD128",))["vulkan"])
        # A real GPU next to llvmpipe counts.
        real = {"vulkaninfo --summary": (0, "deviceType = PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU\n"
                                            "deviceType = PHYSICAL_DEVICE_TYPE_CPU\n")}
        self.assertTrue(self.facts(real, exists=lib, find=("/dev/dri/renderD128",))["vulkan"])
        # Without vulkaninfo, a GPU device node is enough.
        self.assertTrue(self.facts({}, exists=lib, find=("/dev/dri/renderD128",))["vulkan"])

    def test_processor_only_host_uses_three_fifths_of_memory(self) -> None:
        facts = self.facts({})
        self.assertEqual((facts["accelerator"], facts["budget_source"], facts["gpus"]), ("cpu", "cpu", []))
        self.assertEqual(facts["budget_bytes"], 65536000 * 1024 * 3 // 5)
        self.assertEqual(facts["available_memory_bytes"], 50000000 * 1024)
        self.assertEqual(Path(facts["models_folder"]), Path("/home/me/ModelMatrix/Models"))


# MARK: Engines


def fake_server_script(version: str = "4567 (abc1234)") -> bytes:
    return f"#!/bin/sh\necho 'version: {version}'\necho 'built with cc for x86_64'\n".encode()


def make_archive(entries: dict[str, bytes], symlinks: dict[str, str] | None = None) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, data in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755
            tar.addfile(info, io.BytesIO(data))
        for name, target in (symlinks or {}).items():
            info = tarfile.TarInfo(name)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            tar.addfile(info)
    return buffer.getvalue()


LLAMA_URL = "https://github.com/ggml-org/llama.cpp/releases/download/b9999/llama-b9999-bin-ubuntu-x64.tar.gz"
CUDART_URL = "https://github.com/ggml-org/llama.cpp/releases/download/b9999/cudart-llama-b9999-bin-ubuntu-cuda-12.8-x64.tar.gz"


class EngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.space = Workspace(self)
        self.registry = EngineRegistry(self.space.paths.state / "engines.json")
        self.served: dict[str, bytes] = {}
        self.changes: list[str] = []
        self.finder = EngineFinder(self.space.paths, self.registry, which=lambda program: None,
                                   run=self.run_command, ollama_version=lambda: None)
        self.installer = EngineInstaller(self.space.paths, self.registry, self.finder, opener=self.open,
                                         on_change=self.changes.append)

    def run_command(self, argv, timeout=10, env=None):
        if argv[0] == "/bin/bash":
            return 1, ""
        return hf_agent.run_command(argv, timeout, env)

    def open(self, request, timeout):
        return FakeResponse(self.served[request.full_url])

    def plan(self, archive: bytes, url: str = LLAMA_URL, checksum: str | None = None, extra=()) -> dict:
        self.served[url] = archive
        archives = [{"url": url, "size": len(archive), "sha256": checksum or sha(archive)}, *extra]
        return {"engine": "llama.cpp", "archives": archives, "build": "Linux x64 (processor)", "tag": "b9999"}

    @POSIX_EXECUTABLE_FIXTURE
    def test_approved_official_build_is_installed_checked_and_registered(self) -> None:
        archive = make_archive({"llama-b9999/llama-server": fake_server_script(), "llama-b9999/libggml.so": b"lib"})
        job = self.installer.start_install(self.plan(archive), run_async=False)
        self.assertEqual(job["state"], "succeeded", job["log"])
        server = self.space.paths.engines / "llama.cpp" / "bin" / "llama-server"
        self.assertTrue(server.is_file() and (server.parent / "libggml.so").is_file())
        self.assertTrue(any("Checksum OK" in line for line in job["log"]))
        self.assertEqual(self.registry.get("llama.cpp")["path"], str(server))
        self.assertEqual(self.changes, ["llama.cpp"])
        detected = self.finder.detect()[0]
        self.assertEqual((detected["installed"], detected["managed"], detected["version"]), (True, True, "4567 (abc1234)"))
        self.assertEqual([entry.name for entry in self.space.paths.engines.iterdir()], ["llama.cpp"])

    @POSIX_EXECUTABLE_FIXTURE
    def test_cuda_runtime_archive_is_merged_into_the_program_folder(self) -> None:
        main = make_archive({"build/bin/llama-server": fake_server_script(), "build/bin/libggml-cuda.so": b"cuda"})
        runtime = make_archive({"libcudart.so.12": b"rt", "libcublas.so.12": b"blas"})
        self.served[CUDART_URL] = runtime
        plan = self.plan(main, extra=[{"url": CUDART_URL, "size": len(runtime), "sha256": sha(runtime)}])
        job = self.installer.start_install(plan, run_async=False)
        self.assertEqual(job["state"], "succeeded", job["log"])
        folder = self.space.paths.engines / "llama.cpp" / "bin"
        self.assertEqual(sorted(path.name for path in folder.iterdir()),
                         ["libcublas.so.12", "libcudart.so.12", "libggml-cuda.so", "llama-server"])

    def test_wrong_checksum_installs_nothing(self) -> None:
        archive = make_archive({"llama-b9999/llama-server": fake_server_script()})
        job = self.installer.start_install(self.plan(archive, checksum="0" * 64), run_async=False)
        self.assertEqual(job["state"], "failed")
        self.assertIn("checksum", job["error"])
        self.assertFalse((self.space.paths.engines / "llama.cpp").exists())
        self.assertEqual(list(self.space.paths.engines.iterdir()), [])
        self.assertIsNone(self.registry.get("llama.cpp"))

    def test_only_official_release_archives_with_checksums_are_accepted(self) -> None:
        for archives in (
            [{"url": "https://example.com/llama-b1-bin-ubuntu-x64.tar.gz", "size": 1, "sha256": "a" * 64}],
            [{"url": LLAMA_URL.replace("ggml-org", "someone"), "size": 1, "sha256": "a" * 64}],
            [{"url": LLAMA_URL, "size": 1, "sha256": "not-a-checksum"}],
            [{"url": CUDART_URL, "size": 1, "sha256": "a" * 64}],
            [],
        ):
            with self.assertRaises(HFAgentError):
                EngineInstaller.validate({"engine": "llama.cpp", "archives": archives})
        with self.assertRaises(HFAgentError):
            EngineInstaller.validate({"engine": "ollama", "archives": []})

    def test_archive_with_paths_outside_its_folder_is_refused(self) -> None:
        for archive in (
            make_archive({"../escape/llama-server": fake_server_script()}),
            make_archive({"llama/llama-server": fake_server_script()}, symlinks={"llama/evil": "../../../etc/passwd"}),
        ):
            job = self.installer.start_install(self.plan(archive), run_async=False)
            self.assertEqual(job["state"], "failed")
            self.assertTrue(
                any(marker in job["error"] for marker in ("leaves its folder", "outside the destination", "unsafe path")),
                job["error"],
            )
        self.assertFalse((self.space.root / "agent" / "escape").exists())
        self.assertFalse((self.space.paths.engines / "llama.cpp").exists())

    @POSIX_EXECUTABLE_FIXTURE
    def test_build_that_does_not_run_is_not_installed(self) -> None:
        broken = b"#!/bin/sh\necho 'llama-server: /lib/libc.so.6: version GLIBC_2.38 not found' >&2\nexit 1\n"
        job = self.installer.start_install(self.plan(make_archive({"x/llama-server": broken})), run_async=False)
        self.assertEqual(job["state"], "failed")
        self.assertIn("GLIBC_2.38", job["error"])
        self.assertFalse((self.space.paths.engines / "llama.cpp").exists())

    @POSIX_EXECUTABLE_FIXTURE
    def test_failed_update_keeps_the_working_copy(self) -> None:
        good = make_archive({"llama-b9999/llama-server": fake_server_script("1000 (old)")})
        self.assertEqual(self.installer.start_install(self.plan(good), run_async=False)["state"], "succeeded")
        newer = make_archive({"llama-b9999/llama-server": fake_server_script("2000 (new)")})
        job = self.installer.start_install(self.plan(newer, checksum="1" * 64), run_async=False)
        self.assertEqual((job["state"], job["action"]), ("failed", "update"))
        output = subprocess.run([str(self.space.paths.engines / "llama.cpp" / "bin" / "llama-server")],
                                capture_output=True, text=True, check=False).stdout
        self.assertIn("1000 (old)", output)

    @POSIX_EXECUTABLE_FIXTURE
    def test_uninstall_removes_only_what_modelmatrix_installed(self) -> None:
        with self.assertRaises(HFAgentError) as refused:
            self.installer.uninstall()
        self.assertIn("didn't install", refused.exception.message)
        archive = make_archive({"llama-b9999/llama-server": fake_server_script()})
        self.installer.start_install(self.plan(archive), run_async=False)
        keep = self.space.paths.engines / "other-tool"
        keep.mkdir()
        self.installer.in_use = lambda folder: "qwen-7b"
        with self.assertRaises(HFAgentError) as busy:
            self.installer.uninstall()
        self.assertIn("Stop qwen-7b first", busy.exception.message)
        self.installer.in_use = lambda folder: None
        job = self.installer.uninstall()
        self.assertEqual(job["state"], "succeeded")
        self.assertFalse((self.space.paths.engines / "llama.cpp").exists())
        self.assertTrue(keep.is_dir())
        self.assertIsNone(self.registry.get("llama.cpp"))

    @POSIX_EXECUTABLE_FIXTURE
    def test_live_log_can_be_read_from_a_position(self) -> None:
        archive = make_archive({"llama-b9999/llama-server": fake_server_script()})
        job = self.installer.start_install(self.plan(archive), run_async=False)
        later = self.installer.public(self.installer.job(job["id"]), since=2)
        self.assertEqual(later["log"], job["log"][2:])
        self.assertEqual(later["next"], len(job["log"]))

    @POSIX_EXECUTABLE_FIXTURE
    def test_manual_llama_server_that_fails_is_reported_not_used(self) -> None:
        folder = self.space.home / ".local" / "bin"
        folder.mkdir(parents=True)
        broken = folder / "llama-server"
        broken.write_text("#!/bin/sh\necho 'error while loading shared libraries: libcuda.so.1' >&2\nexit 127\n")
        broken.chmod(0o755)
        detected = self.finder.detect()[0]
        self.assertEqual((detected["installed"], detected["managed"], detected["path"]), (False, False, str(broken)))
        self.assertIn("libcuda.so.1", detected["note"])


# MARK: Downloads


class DownloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.space = Workspace(self)
        patcher = mock.patch.object(hf_agent, "CHUNK_BYTES", 1000)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.profiles = RecordingProfiles()
        self.manager = self.space.manager(self.profiles)
        self.sleeps: list[float] = []
        self.manager._sleep = lambda job_id, seconds: self.sleeps.append(seconds)  # noqa: SLF001
        self.target = self.space.paths.models / REPO / "Model-Q4_K_M.gguf"

    def test_files_download_into_the_models_folder_and_are_verified(self) -> None:
        files = [{"path": "Model-Q4_K_M.gguf", "size": len(DATA), "sha256": sha(DATA)},
                 {"path": "mmproj/mmproj-F16.gguf", "size": len(PROJECTOR), "sha256": sha(PROJECTOR)}]
        self.manager.add(job_payload(files=files, projector_file="mmproj/mmproj-F16.gguf"))
        job = run(self.manager, "dl-abc123def")
        self.assertEqual((job["state"], job["bytes_done"], job["bytes_total"]), ("completed", len(DATA) + len(PROJECTOR),
                                                                              len(DATA) + len(PROJECTOR)))
        self.assertEqual(self.target.read_bytes(), DATA)
        self.assertEqual((self.target.parent / "mmproj" / "mmproj-F16.gguf").read_bytes(), PROJECTOR)
        self.assertEqual(list(self.target.parent.rglob("*.part")), [])
        self.assertEqual(self.profiles.runs, [("dl-abc123def", False)])
        saved = json.loads((self.space.paths.state / "downloads.json").read_text())
        self.assertEqual(saved[0]["state"], "completed")

    def test_interrupted_download_resumes_from_where_it_stopped(self) -> None:
        self.space.hub.plan[file_url("Model-Q4_K_M.gguf")] = ["reset:3000"]
        self.manager.add(job_payload())
        job = run(self.manager, "dl-abc123def")
        self.assertEqual((job["state"], job["bytes_done"]), ("queued", 3000))
        self.assertEqual(self.sleeps, [5])
        part = self.target.with_name(self.target.name + ".part")
        self.assertEqual(part.stat().st_size, 3000)
        job = run(self.manager, "dl-abc123def")
        self.assertEqual(job["state"], "completed")
        self.assertEqual(self.space.hub.requests[1][1], "bytes=3000-")
        self.assertEqual(self.target.read_bytes(), DATA)

    def test_server_that_ignores_the_range_starts_the_file_again(self) -> None:
        self.target.parent.mkdir(parents=True)
        self.target.with_name(self.target.name + ".part").write_bytes(b"x" * 4000)
        self.space.hub.plan[file_url("Model-Q4_K_M.gguf")] = ["ignore_range"]
        self.manager.add(job_payload())
        self.assertEqual(run(self.manager, "dl-abc123def")["state"], "completed")
        self.assertEqual(self.target.read_bytes(), DATA)

    def test_checksum_failure_downloads_again_and_fails_after_two(self) -> None:
        url = file_url("Model-Q4_K_M.gguf")
        self.space.hub.plan[url] = ["corrupt"]
        self.manager.add(job_payload())
        self.assertEqual(run(self.manager, "dl-abc123def")["state"], "queued")
        job = run(self.manager, "dl-abc123def")
        self.assertEqual(job["state"], "completed")
        self.assertEqual(len(job["warnings"]), 1)
        self.assertEqual(self.target.read_bytes(), DATA)

        self.manager.add(job_payload("dl-second0001", label="Q8_0", files=[
            {"path": "Model-Q4_K_M.gguf", "size": len(DATA), "sha256": sha(b"different")}]))
        run(self.manager, "dl-second0001")
        job = run(self.manager, "dl-second0001")
        self.assertEqual(job["state"], "failed")
        self.assertIn("failed its checksum check twice", job["error"])

    def test_gated_model_needs_a_login_and_the_token_is_never_saved(self) -> None:
        url = file_url("Model-Q4_K_M.gguf")
        self.space.hub.plan[url] = [401]
        payload = job_payload()
        payload["token"] = "hf_secret_token_value"
        self.manager.add(payload)
        job = run(self.manager, "dl-abc123def")
        self.assertEqual(job["state"], "failed")
        self.assertIn("needs a Hugging Face login", job["error"])
        self.assertEqual(self.space.hub.requests[0][2], "hf_secret_token_value")
        self.assertNotIn("hf_secret", (self.space.paths.state / "downloads.json").read_text())
        self.assertNotIn("hf_secret", json.dumps(self.manager.list()))
        self.manager.action({"id": "dl-abc123def", "action": "resume"})
        self.assertEqual(run(self.manager, "dl-abc123def")["state"], "completed")

    def test_rate_limit_waits_as_asked(self) -> None:
        self.space.hub.plan[file_url("Model-Q4_K_M.gguf")] = [429]
        self.manager.add(job_payload())
        job = run(self.manager, "dl-abc123def")
        self.assertEqual((job["state"], self.sleeps), ("queued", [7.0]))
        self.assertIn("slow down", job["message"])

    def test_server_errors_retry_and_missing_files_fail(self) -> None:
        self.space.hub.plan[file_url("Model-Q4_K_M.gguf")] = [503, 404]
        self.manager.add(job_payload())
        self.assertEqual(run(self.manager, "dl-abc123def")["state"], "queued")
        job = run(self.manager, "dl-abc123def")
        self.assertEqual(job["state"], "failed")
        self.assertIn("no longer on Hugging Face", job["error"])

    def test_waits_for_disk_space_before_downloading(self) -> None:
        self.manager.add(job_payload())
        with mock.patch.object(hf_agent, "free_disk_bytes", return_value=1000):
            job = run(self.manager, "dl-abc123def")
        self.assertEqual((job["state"], self.sleeps, self.space.hub.requests), ("queued", [30], []))
        self.assertIn("Waiting for free disk space", job["message"])

    def test_file_already_on_disk_is_reused_without_downloading(self) -> None:
        self.target.parent.mkdir(parents=True)
        self.target.write_bytes(DATA)
        self.manager.add(job_payload())
        job = run(self.manager, "dl-abc123def")
        self.assertEqual((job["state"], job["files"][0]["reused"], self.space.hub.requests), ("completed", "existing", []))

    def test_cancel_while_downloading_removes_partial_files(self) -> None:
        def cancel_midway(position: int) -> None:
            if position >= 2000:
                self.space.hub.on_read = None
                self.manager.action({"id": "dl-abc123def", "action": "cancel", "delete_files": True})

        self.space.hub.on_read = cancel_midway
        self.manager.add(job_payload())
        job = run(self.manager, "dl-abc123def")
        self.assertEqual(job["state"], "cancelled")
        self.assertFalse(self.target.with_name(self.target.name + ".part").exists())
        self.assertFalse((self.space.paths.models / "owner").exists())

    def test_pause_resume_cancel_and_delete(self) -> None:
        self.manager.add(job_payload())
        self.assertEqual(self.manager.action({"id": "dl-abc123def", "action": "pause"})["download"]["state"], "paused")
        self.assertEqual(self.manager.action({"id": "dl-abc123def", "action": "resume"})["download"]["state"], "queued")
        with self.assertRaises(HFAgentError):
            self.manager.action({"id": "dl-abc123def", "action": "delete"})
        part = self.target.with_name(self.target.name + ".part")
        part.parent.mkdir(parents=True)
        part.write_bytes(b"partial")
        self.manager.action({"id": "dl-abc123def", "action": "cancel", "delete_files": True})
        self.assertFalse(part.exists())
        self.assertEqual(self.manager.action({"id": "dl-abc123def", "action": "delete"})["deleted"], True)
        self.assertEqual(self.manager.list(), [])
        with self.assertRaises(HFAgentError) as unknown:
            self.manager.action({"id": "dl-abc123def", "action": "pause"})
        self.assertEqual(unknown.exception.http_status, 404)

    def test_downloads_continue_after_the_agent_restarts(self) -> None:
        self.manager.add(job_payload())
        self.manager.jobs["dl-abc123def"]["state"] = "downloading"
        self.manager.save()
        restarted = self.space.manager(self.profiles)
        job = restarted.get("dl-abc123def")
        self.assertEqual((job["state"], job["message"]), ("queued", "Continuing after the agent restarted"))

    def test_unsafe_or_unsupported_requests_are_refused(self) -> None:
        bad = [
            job_payload(files=[{"path": "../../.bashrc", "size": 1, "sha256": sha(b"x")}]),
            job_payload(files=[{"path": "/etc/passwd", "size": 1, "sha256": sha(b"x")}]),
            job_payload(files=[{"path": "a.gguf", "size": 1, "sha256": "zz"}]),
            job_payload(engine="mlx"),
            job_payload(repo="../etc"),
            job_payload("not-a-valid-id"),
            job_payload(main_file="other.gguf"),
            {**job_payload(), "endpoint": "http://plain-http.example"},
            {**job_payload(), "folder_name": "../outside"},
        ]
        for payload in bad:
            with self.assertRaises(HFAgentError, msg=str(payload)[:120]):
                self.manager.add(payload)
        self.manager.add(job_payload())
        with self.assertRaises(HFAgentError) as duplicate:
            self.manager.add(job_payload("dl-other00001"))
        self.assertEqual(duplicate.exception.http_status, 409)

    def test_redirect_keeps_the_token_only_on_the_hugging_face_host(self) -> None:
        handler = hf_agent._AuthRedirect("huggingface.co", "hf_token")  # noqa: SLF001
        request = urllib.request.Request(file_url("Model-Q4_K_M.gguf"), headers={"Range": "bytes=10-"})
        request.add_unredirected_header("Authorization", "Bearer hf_token")
        cdn = handler.redirect_request(request, None, 302, "Found", headers(), "https://cdn-lfs.hf.co/abc?sig=1")
        self.assertIsNone(cdn.get_header("Authorization"))
        self.assertEqual(cdn.get_header("Range"), "bytes=10-")
        same = handler.redirect_request(request, None, 307, "Moved", headers(), f"{ENDPOINT}/api/resolve-cache/x")
        self.assertEqual(same.get_header("Authorization"), "Bearer hf_token")


class OllamaDownloadTests(unittest.TestCase):
    def payload(self) -> dict:
        return job_payload(kind="ollama", engine="ollama", files=None, main_file=None,
                           ollama_model="hf.co/owner/Model-GGUF:Q4_K_M") | {}

    def make(self, had: bool):
        space = Workspace(self)
        ollama = FakeOllama(had)
        host = FakeProfileHost(space.root / "profiles")
        profiles = ProfileMaker(host, FakeFinder(space.paths), lambda: FakeFacts().payload(space.paths))
        manager = space.manager(profiles, ollama)
        payload = self.payload()
        payload["job"]["files"] = []
        manager.add(payload)
        return manager, ollama

    def test_pull_reports_progress_then_tests_the_model(self) -> None:
        manager, ollama = self.make(had=False)
        job = run(manager, "dl-abc123def")
        self.assertEqual((job["state"], job["ollama_had_model"], ollama.pulled), ("completed", False,
                                                                                 ["hf.co/owner/Model-GGUF:Q4_K_M"]))
        self.assertEqual((job["profile_state"], job["ready_url"]), ("ready", "http://127.0.0.1:11434/v1"))
        result = manager.action({"id": "dl-abc123def", "action": "delete", "delete_files": True})
        self.assertEqual((result["kept_in_ollama"], ollama.deleted), (False, ["hf.co/owner/Model-GGUF:Q4_K_M"]))

    def test_model_the_user_already_had_is_never_removed(self) -> None:
        manager, ollama = self.make(had=True)
        run(manager, "dl-abc123def")
        result = manager.action({"id": "dl-abc123def", "action": "delete", "delete_files": True})
        self.assertEqual((result["kept_in_ollama"], ollama.deleted), (True, []))


# MARK: Profiles


class ProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.space = Workspace(self)
        self.host = FakeProfileHost(self.space.root / "profiles")
        self.finder = FakeFinder(self.space.paths)
        self.facts = FakeFacts()
        self.busy: set[int] = set()
        self.alive = True
        self.answers = True
        self.chats: list[tuple[str, str]] = []
        self.maker = ProfileMaker(
            self.host, self.finder, lambda: self.facts.payload(self.space.paths),
            port_in_use=lambda port: port in self.busy, process_alive=lambda pid: pid is not None and self.alive,
            models_url=lambda url: self.answers, chat=self.chat, health_seconds=lambda job: 0.3, poll_seconds=0.01,
        )
        self.manager = self.space.manager(self.maker)

    def chat(self, base: str, model: str):
        self.chats.append((base, model))
        return True, "answered the test message"

    def download(self, job_id: str = "dl-abc123def", **extra) -> dict:
        files = [{"path": "Model-Q4_K_M.gguf", "size": len(DATA), "sha256": sha(DATA)},
                 {"path": "mmproj/mmproj-F16.gguf", "size": len(PROJECTOR), "sha256": sha(PROJECTOR)}]
        self.manager.add(job_payload(job_id, files=files, projector_file="mmproj/mmproj-F16.gguf", **extra))
        return run(self.manager, job_id)

    def test_finished_download_gets_a_profile_that_starts_and_answers(self) -> None:
        self.host.relay = "http://192.168.1.20:7701/v1"
        job = self.download()
        self.assertEqual((job["profile_state"], job["profile"], job["ready_model"]), ("ready", "model-q4_k_m", "model"))
        self.assertEqual(job["ready_url"], "http://192.168.1.20:7701/v1")
        self.assertEqual(self.host.started, ["model-q4_k_m"])
        self.assertEqual(self.chats, [("http://127.0.0.1:8081/v1", "model")])
        values = self.host.profiles()["model-q4_k_m"]
        folder = self.space.paths.models / REPO
        self.assertEqual((values["PORT"], values["RUNTIME"], values["HF_DOWNLOAD_ID"]), ("8081", "llama.cpp", "dl-abc123def"))
        self.assertEqual(Path(values["MODEL_PATH"]), folder / "Model-Q4_K_M.gguf")
        if values.get("START_COMMAND_JSON"):
            command = json.loads(values["START_COMMAND_JSON"])
            self.assertEqual(Path(command[command.index("--mmproj") + 1]), folder / "mmproj" / "mmproj-F16.gguf")
        else:
            command = shlex.split(values["START_COMMAND"], posix=True)
            self.assertEqual(Path(command[command.index("--mmproj") + 1]), folder / "mmproj" / "mmproj-F16.gguf")
            if os.name != "nt":
                self.assertIn("LD_LIBRARY_PATH=/opt/llama/bin${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}", values["START_COMMAND"])

    def test_names_models_and_ports_never_clash(self) -> None:
        self.host.folder.mkdir(parents=True)
        (self.host.folder / "model-q4_k_m.env").write_text('PORT="8081"\nREQUEST_MODEL="model"\n')
        self.busy = {8082}
        job = self.download()
        self.assertEqual((job["profile"], job["ready_model"]), ("model-q4_k_m-2", "model-q4_k_m"))
        self.assertEqual(self.host.profiles()["model-q4_k_m-2"]["PORT"], "8083")

    def test_profile_waits_for_the_engine_then_starts_once_installed(self) -> None:
        self.finder.server = None
        stale = "llama.cpp isn't installed yet. The download can run now; the model starts once the engine is installed."
        job = self.download(warnings=[stale, "Other note"])
        self.assertEqual(job["profile_state"], "needs_engine")
        self.assertIn("engine install llama.cpp", job["profile_message"])
        command = self.host.profiles()["model-q4_k_m"]["START_COMMAND"]
        self.assertIn(str(self.space.paths.engines / "llama.cpp" / "bin" / "llama-server"), command)
        self.finder.server = "/opt/llama/bin/llama-server"
        self.manager.engine_changed()
        deadline = time.monotonic() + 5
        while self.manager.get("dl-abc123def")["profile_state"] != "ready" and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.manager.get("dl-abc123def")["profile_state"], "ready")
        self.assertEqual(self.manager.get("dl-abc123def")["warnings"], ["Other note"])

    def test_not_started_when_memory_is_short(self) -> None:
        self.facts.values["available_memory_bytes"] = 1 * GIB
        job = self.download()
        self.assertEqual((job["profile_state"], self.host.started), ("needs_memory", []))
        self.assertIn("only 1.0 GB is free", job["profile_message"])

    @unittest.skipUnless(os.name != "nt", "POSIX log path and Unix launch output; Windows lifecycle coverage uses native fixtures")
    def test_start_failure_shows_the_log_and_a_suggestion(self) -> None:
        self.alive = False
        self.answers = False
        log = Path("/tmp/model-q4_k_m.log")
        previous = log.read_bytes() if log.exists() else None
        self.addCleanup(lambda: log.write_bytes(previous) if previous is not None else log.unlink(missing_ok=True))
        log.write_text("loading model\nggml_backend_cuda_buffer_type_alloc_buffer: allocating 9000 MiB failed: out of memory\n")
        job = self.download()
        self.assertEqual(job["profile_state"], "start_failed")
        self.assertIn("the server stopped while loading", job["profile_message"])
        self.assertIn("smaller version", job["profile_message"])
        self.assertIn("out of memory", job["profile_log"][-1])
        self.assertEqual(self.host.stopped, ["model-q4_k_m"])

    def test_start_after_off_only_creates_the_profile(self) -> None:
        job = self.download(start_after=False)
        self.assertEqual((job["profile_state"], self.host.started), ("created", []))
        self.manager.action({"id": "dl-abc123def", "action": "start"})
        deadline = time.monotonic() + 5
        while self.manager.get("dl-abc123def")["profile_state"] != "ready" and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.host.started, ["model-q4_k_m"])

    def test_start_again_while_running_only_retests(self) -> None:
        self.download()
        self.maker.run(self.manager, "dl-abc123def", True)
        self.assertEqual((self.host.started, len(self.chats)), (["model-q4_k_m"], 2))
        self.assertEqual(self.manager.get("dl-abc123def")["profile_state"], "ready")

    def test_delete_removes_the_profile_unless_the_user_took_it_over(self) -> None:
        self.download()
        self.download("dl-second0002", label="Q8_0")
        edited = self.host.folder / "model-q8_0.env"
        edited.write_text(edited.read_text().replace("dl-second0002", "mine"))
        self.manager.action({"id": "dl-abc123def", "action": "delete", "delete_files": False})
        self.manager.action({"id": "dl-second0002", "action": "delete", "delete_files": False})
        self.assertEqual(sorted(path.name for path in self.host.folder.glob("*.env")), ["model-q8_0.env"])
        self.assertTrue((self.space.paths.models / REPO / "Model-Q4_K_M.gguf").exists())

    def test_failure_hints(self) -> None:
        hint = hf_agent.start_failure_hint
        self.assertIn("newer Linux", hint(["/lib/x86_64-linux-gnu/libc.so.6: version `GLIBC_2.38' not found"]))
        self.assertIn("too old", hint(["error: unknown argument: --mmproj"]))
        self.assertIn("another port", hint(["bind: Address already in use"]))
        self.assertIn("processor-only build", hint(["error while loading shared libraries: libcuda.so.1"]))


class AgentIntegrationTests(unittest.TestCase):
    @unittest.skipUnless(os.name != "nt", "/bin/bash integration fixture; Windows structured-launch coverage is native")
    def test_agent_reads_the_profile_and_the_start_command_runs_as_written(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            models = root / "My \"Models\" $HOME"
            bin_folder = root / "engine bin"
            bin_folder.mkdir()
            record = root / "argv.txt"
            server = bin_folder / "llama-server"
            server.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$LD_LIBRARY_PATH\" \"$@\" > '{record}'\n")
            server.chmod(server.stat().st_mode | stat.S_IEXEC)
            service = agent.AgentService(agent.AgentConfiguration(root=root / "agent", profiles_dir=root / "profiles"))
            paths = HFHostPaths(root / "agent", home=root, environ={"MODELMATRIX_MODELS_DIR": str(models)})
            maker = ProfileMaker(hf_agent.AgentProfileHost(service), FakeFinder(paths, str(server)), lambda: {})
            job = {"id": "dl-abc123def", "repo": REPO, "revision": "main", "label": "Q4_K_M", "display_name": "Model",
                   "folder": str(models / REPO), "main_file": "Model-Q4_K_M.gguf", "projector_file": None,
                   "context": 4096, "profile": None, "profile_hint": None, "model_hint": None}
            name, model, port = maker._write_profile(job, str(server))  # noqa: SLF001
            values = service.profiles.load()[name].values
            self.assertEqual(values["MODEL_PATH"], str(models / REPO / "Model-Q4_K_M.gguf"))
            env = {key: value for key, value in os.environ.items() if key != "LD_LIBRARY_PATH"}
            subprocess.run(["/bin/bash", "-lc", values["START_COMMAND"]], env=env, check=True, timeout=20)
            argv = record.read_text().splitlines()
            self.assertEqual(argv[0], str(bin_folder))
            self.assertEqual(argv[1:5], ["-m", str(models / REPO / "Model-Q4_K_M.gguf"), "--host", "127.0.0.1"])
            self.assertEqual(argv[5:], ["--port", str(port), "--alias", model, "-c", "4096", "-ngl", "999"])


class RouteTests(unittest.TestCase):
    def test_routes_work_over_the_agent_http_server(self) -> None:
        space = Workspace(self)
        service = agent.AgentService(agent.AgentConfiguration(root=space.root / "agent", profiles_dir=space.root / "p",
                                                              port=0))
        service.hf = HFHost(service, paths=space.paths, facts=FakeFacts(), finder=FakeFinder(space.paths),
                            start_worker=False, fetch=space.hub, profile_host=FakeProfileHost(space.root / "p"))
        server = agent.make_server(service)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_address[1]}"

        def call(method: str, path: str, body: dict | None = None):
            data = json.dumps(body).encode() if body is not None else None
            request = urllib.request.Request(base + path, data=data, method=method,
                                             headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    return response.status, json.loads(response.read())
            except urllib.error.HTTPError as error:
                with error:
                    return error.code, json.loads(error.read())

        status, host = call("GET", "/api/hf/host")
        self.assertEqual((status, host["machine"]["kind"], host["hf_api"]), (200, "linux", 1))
        status, added = call("POST", "/api/hf/downloads", job_payload())
        self.assertEqual((status, added["download"]["state"]), (200, "queued"))
        status, listed = call("GET", "/api/hf/downloads")
        self.assertEqual((status, [job["id"] for job in listed["downloads"]]), (200, ["dl-abc123def"]))
        status, paused = call("POST", "/api/hf/downloads/action", {"id": "dl-abc123def", "action": "pause"})
        self.assertEqual(paused["download"]["state"], "paused")
        status, refused = call("POST", "/api/hf/downloads", job_payload(engine="mlx"))
        self.assertEqual(status, 400)
        self.assertIn("Apple Silicon", json.dumps(refused))
        status, missing = call("GET", "/api/hf/engines/job?id=eng-missing01")
        self.assertEqual(status, 404)
        status, engines = call("GET", "/api/hf/engines")
        self.assertEqual((status, engines["jobs"]), (200, []))



class SizeTextTests(unittest.TestCase):
    def test_small_files_show_in_megabytes(self) -> None:
        self.assertEqual(hf_agent.gb_text(18_000_000), "17.2 MB")
        self.assertEqual(hf_agent.gb_text(5 * GIB // 2), "2.5 GB")
        self.assertEqual(hf_agent.gb_text(512 * GIB), "512 GB")
        self.assertEqual((hf_agent.gb_text(0), hf_agent.gb_text(None)), ("0 GB", "0 GB"))


class NamingTests(unittest.TestCase):
    """Host profiles get the same names a download on the Mac would get."""

    def test_base_name_drops_packaging_words(self) -> None:
        self.assertEqual(hf_agent.base_model_name("bartowski/SmolLM2-135M-Instruct-GGUF"), "smollm2-135m-instruct")
        self.assertEqual(hf_agent.base_model_name("mlx-community/Qwen2.5-7B-Instruct-4bit"), "qwen2.5-7b-instruct")
        self.assertEqual(hf_agent.base_model_name("unsloth/Qwen3-30B-A3B-GGUF"), "qwen3-30b-a3b")
        self.assertEqual(hf_agent.base_model_name("someone/GGUF"), "gguf")

    def test_fallback_names_add_the_version_once(self) -> None:
        job = {"repo": "bartowski/SmolLM2-135M-Instruct-GGUF", "label": "Q4_K_M",
               "display_name": "SmolLM2-135M-Instruct-GGUF Q4_K_M"}
        with tempfile.TemporaryDirectory() as folder:
            name = hf_agent.ProfileMaker._unique_name(job, {}, Path(folder))
        self.assertEqual(name, "smollm2-135m-instruct-q4_k_m")
        self.assertEqual(hf_agent.ProfileMaker._unique_model(job, {}), "smollm2-135m-instruct")
        taken = {"smollm2-135m-instruct": {}}
        self.assertEqual(hf_agent.ProfileMaker._unique_model(job, taken), "smollm2-135m-instruct-q4_k_m")

    def test_hints_from_the_mac_win(self) -> None:
        job = {"repo": "a/b-GGUF", "label": "Q8_0", "display_name": "b-GGUF Q8_0",
               "profile_hint": "custom-q8_0", "model_hint": "custom"}
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(hf_agent.ProfileMaker._unique_name(job, {}, Path(folder)), "custom-q8_0")
        self.assertEqual(hf_agent.ProfileMaker._unique_model(job, {}), "custom")

if __name__ == "__main__":
    unittest.main()
