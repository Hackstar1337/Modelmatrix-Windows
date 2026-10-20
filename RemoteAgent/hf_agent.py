"""Hugging Face downloads, approved engine installs and automatic profiles
on a Linux host.

The Mac does the thinking: it checks the model on Hugging Face, works out
which version fits this host (from the facts reported here) and builds the
install plan the user approves. This module does the work on the host: it
reports the host's facts, downloads files straight onto the host (resumable,
checked against Hugging Face's SHA-256), installs an approved official
llama.cpp build into ~/ModelMatrix/Engines, and creates, starts and tests the
profile. Standard library only.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import tarfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path
from typing import Any, Callable

from agent_core import AgentError

HF_API_VERSION = 1
GIB = 1 << 30
CHUNK_BYTES = 1 << 20
SAVE_INTERVAL_SECONDS = 2.0
USER_AGENT = "ModelMatrix-Agent"
OLLAMA_URL = "http://127.0.0.1:11434"
LOG_LINES = 400
MAX_ENGINE_ARCHIVE_BYTES = 2 * GIB
MAX_UNPACKED_BYTES = 8 * GIB
LLAMA_URL_RE = re.compile(
    r"^https://github\.com/ggml-org/llama\.cpp/releases/download/b[0-9]{1,7}/"
    r"(?:cudart-)?llama-b[0-9]{1,7}-bin-[a-z0-9.-]+\.tar\.gz$"
)
WINDOWS_LLAMA_URL_RE = re.compile(
    r"^https://github\.com/ggml-org/llama\.cpp/releases/download/b[0-9]{1,7}/"
    r"(?:cudart-)?llama-b[0-9]{1,7}-bin-(?:win|windows)(?:-[a-z0-9._]+)*-?(?:x64|amd64|arm64)\.zip$",
    re.IGNORECASE,
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
JOB_ID_RE = re.compile(r"^(?:dl|eng)-[a-z0-9]{6,32}$")
REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
REVISION_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
PATH_PART_RE = re.compile(r"^[A-Za-z0-9._+()\- ]{1,200}$")
LABEL_RE = re.compile(r"^[A-Za-z0-9._+\- ]{1,64}$")
MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/\-]{0,199}$")
PROFILE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")
LLAMA_SEARCH_DIRS = (
    "~/.local/bin", "/usr/local/bin", "/usr/bin", "/opt/llama.cpp/bin", "/opt/llama.cpp/build/bin",
    "~/llama.cpp/build/bin", "/snap/bin",
)
OLLAMA_SEARCH_DIRS = ("/usr/local/bin", "/usr/bin", "~/.local/bin", "/snap/bin")
VULKAN_LIBRARY_DIRS = (
    "/usr/lib/x86_64-linux-gnu", "/usr/lib/aarch64-linux-gnu", "/usr/lib64", "/usr/lib",
    "/lib/x86_64-linux-gnu", "/lib/aarch64-linux-gnu",
)


class HFAgentError(AgentError):
    """An error whose message is shown to the user as is."""

    def __init__(self, message: str, status: int = 400, code: str = "hf_error"):
        super().__init__(message)
        self.http_status = status
        self.code = code
        self.public_message = message


class _Stopped(Exception):
    """A download or install was paused or cancelled."""


def now_text() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def gb_text(value: int | None) -> str:
    """Sizes in the Mac's units: MB below 1 GB, whole GB from 100 GB."""
    if not value:
        return "0 GB"
    if value < GIB:
        return f"{value / (1 << 20):.1f} MB"
    return f"{value / GIB:.1f} GB" if value < 100 * GIB else f"{value / GIB:.0f} GB"

def run_command(argv: list[str], timeout: float = 10, env: dict[str, str] | None = None) -> tuple[int | None, str]:
    """Exit code and combined output of a command; (None, reason) if it couldn't run."""
    try:
        done = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL, check=False, env=env,
        )
    except subprocess.TimeoutExpired:
        return None, "timed out"
    except (OSError, ValueError) as error:
        return None, str(error)
    return done.returncode, (done.stdout or "") + (done.stderr or "")


def read_text(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def nearest_existing(path: Path) -> Path:
    current = path
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


def free_disk_bytes(path: Path) -> int | None:
    try:
        return shutil.disk_usage(nearest_existing(path)).free
    except OSError:
        return None


def write_json_atomically(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


class HFHostPaths:
    """Where models, engines and saved state live on this host."""

    def __init__(self, agent_root: Path, home: Path | None = None, environ: dict[str, str] | None = None):
        environ = os.environ if environ is None else environ
        home = home or Path.home()
        self.home = home
        self.models = Path(environ.get("MODELMATRIX_MODELS_DIR") or home / "ModelMatrix" / "Models").expanduser()
        self.engines = Path(environ.get("MODELMATRIX_ENGINES_DIR") or home / "ModelMatrix" / "Engines").expanduser()
        self.state = Path(agent_root).expanduser() / "hf"

    def expand(self, path: str) -> Path:
        return self.home / path[2:] if path.startswith("~/") else Path(path)


# MARK: Host facts


class HostFacts:
    """Reads this host's processor, memory, GPUs and disk. Every reader is replaceable in tests."""

    def __init__(
        self,
        run: Callable[[list[str], float], tuple[int | None, str]] | None = None,
        read: Callable[[str], str | None] = read_text,
        exists: Callable[[str], bool] = os.path.exists,
        find: Callable[[str], list[str]] = glob.glob,
        system: str | None = None,
        machine: str | None = None,
        libc: Callable[[], str | None] | None = None,
    ):
        self.run = run or (lambda argv, timeout: run_command(argv, timeout))
        self.read = read
        self.exists = exists
        self.find = find
        self.system = (system or platform.system()).lower()
        self.machine = (machine or platform.machine()).lower()
        self.libc = libc or _glibc_version

    def memory(self) -> tuple[int, int | None]:
        values: dict[str, int] = {}
        for line in (self.read("/proc/meminfo") or "").splitlines():
            key, _, rest = line.partition(":")
            parts = rest.split()
            if parts and parts[0].isdigit():
                values[key.strip()] = int(parts[0]) * 1024
        total = values.get("MemTotal") or _sysconf_memory()
        return total, values.get("MemAvailable")

    def nvidia(self) -> tuple[list[dict[str, Any]], str | None]:
        code, out = self.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader,nounits"], 8,
        )
        if code != 0 or not out.strip():
            return [], None
        gpus: list[dict[str, Any]] = []
        for line in out.splitlines():
            parts = [part.strip() for part in line.split(",")]
            if len(parts) < 3 or not parts[0]:
                continue
            try:
                memory: int | None = int(float(parts[1]) * (1 << 20))
            except ValueError:
                memory = None  # "[N/A]" on unified-memory machines
            gpus.append({"vendor": "nvidia", "name": parts[0], "memory_bytes": memory, "driver": parts[2]})
        _, header = self.run(["nvidia-smi"], 8)
        match = re.search(r"CUDA Version:\s*([0-9]+\.[0-9]+)", header or "")
        return gpus, match.group(1) if match else None

    def amd(self) -> list[dict[str, Any]]:
        gpus: list[dict[str, Any]] = []
        for device in sorted(self.find("/sys/class/drm/card[0-9]*/device")):
            if (self.read(f"{device}/vendor") or "").strip().lower() != "0x1002":
                continue
            raw = (self.read(f"{device}/mem_info_vram_total") or "").strip()
            memory = int(raw) if raw.isdigit() else None
            name = (self.read(f"{device}/product_name") or "").strip() or "AMD GPU"
            gpus.append({"vendor": "amd", "name": name, "memory_bytes": memory, "driver": None})
        return gpus

    def has_vulkan(self) -> bool:
        """A Vulkan loader and a real GPU. Software renderers (llvmpipe) run on
        the processor, where the plain processor build is faster."""
        if not any(self.exists(f"{folder}/libvulkan.so.1") for folder in VULKAN_LIBRARY_DIRS):
            return False
        if not self.find("/dev/dri/renderD*"):
            return False  # no GPU device node (typical for containers and cloud VMs)
        code, out = self.run(["vulkaninfo", "--summary"], 8)
        types = re.findall(r"deviceType\s*=\s*(\S+)", out or "") if code == 0 else []
        return any(not kind.endswith("_CPU") for kind in types) if types else True

    def os_name(self) -> str:
        for line in (self.read("/etc/os-release") or "").splitlines():
            if line.startswith("PRETTY_NAME="):
                return line.split("=", 1)[1].strip().strip('"')
        return f"{platform.system()} {platform.release()}".strip()

    def cpu_name(self) -> str:
        text = self.read("/proc/cpuinfo") or ""
        for key in ("model name", "Model", "Hardware", "cpu model"):
            match = re.search(rf"^{key}\s*:\s*(.+)$", text, re.MULTILINE)
            if match and match.group(1).strip():
                return match.group(1).strip()
        return platform.processor() or self.machine

    @property
    def architecture(self) -> str:
        return {"aarch64": "arm64", "arm64": "arm64", "x86_64": "x86_64", "amd64": "x86_64"}.get(
            self.machine, self.machine
        )

    def payload(self, paths: HFHostPaths) -> dict[str, Any]:
        total, available = self.memory()
        nvidia, cuda = self.nvidia()
        amd = [] if nvidia else self.amd()
        gpus = nvidia + amd
        vram = sum(gpu["memory_bytes"] or 0 for gpu in gpus)
        unified = bool(nvidia) and all(gpu["memory_bytes"] is None for gpu in nvidia)
        if unified:
            budget, source, accelerator = total * 3 // 4, "unified", "cuda"
        elif nvidia and vram:
            budget, source, accelerator = vram * 9 // 10, "gpu", "cuda"
        elif amd and vram >= 2 * GIB:
            budget, source, accelerator = vram * 9 // 10, "gpu", "amd"
        else:
            budget, source, accelerator = total * 3 // 5, "cpu", "cpu"
        return {
            "kind": "linux" if self.system == "linux" else self.system,
            "os": self.os_name(),
            "architecture": self.architecture,
            "chip": self.cpu_name(),
            "cores": os.cpu_count() or 1,
            "memory_bytes": total,
            "available_memory_bytes": available,
            "gpus": gpus,
            "gpu": _gpu_summary(gpus, unified),
            "cuda_version": cuda,
            "vulkan": self.has_vulkan(),
            "rocm": self.exists("/opt/rocm"),
            "glibc": self.libc(),
            "accelerator": accelerator,
            "budget_bytes": budget,
            "budget_source": source,
            "models_folder": str(paths.models),
            "engines_folder": str(paths.engines),
            "free_disk_bytes": free_disk_bytes(paths.models),
            "hf_api": HF_API_VERSION,
        }


class WindowsHostFacts(HostFacts):
    """Windows host facts with conservative, injectable hardware discovery."""

    def __init__(self, *args: Any, **kwargs: Any):
        kwargs.setdefault("system", "Windows")
        super().__init__(*args, **kwargs)

    def memory(self) -> tuple[int, int | None]:
        if self.system != "windows":
            return super().memory()
        try:
            import ctypes

            class _Memory(ctypes.Structure):
                _fields_ = [("length", ctypes.c_ulong), ("memory_load", ctypes.c_ulong),
                            ("total", ctypes.c_ulonglong), ("available", ctypes.c_ulonglong),
                            ("pagefile", ctypes.c_ulonglong), ("avail_pagefile", ctypes.c_ulonglong),
                            ("virtual", ctypes.c_ulonglong), ("avail_virtual", ctypes.c_ulonglong),
                            ("avail_extended", ctypes.c_ulonglong)]

            info = _Memory()
            info.length = ctypes.sizeof(_Memory)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(info)):
                return int(info.total), int(info.available)
        except (AttributeError, OSError, TypeError):
            pass
        return super().memory()

    def amd(self) -> list[dict[str, Any]]:
        return []

    def os_name(self) -> str:
        return f"Windows {platform.release()}".strip()

    def cpu_name(self) -> str:
        return platform.processor() or self.machine

    def payload(self, paths: HFHostPaths) -> dict[str, Any]:
        total, available = self.memory()
        nvidia, cuda = self.nvidia()
        vram = sum(gpu.get("memory_bytes") or 0 for gpu in nvidia)
        if nvidia and vram:
            budget, source, accelerator = vram * 9 // 10, "gpu", "cuda"
        else:
            budget, source, accelerator = total * 3 // 5, "cpu", "cpu"
        return {
            "kind": "windows", "os": self.os_name(), "architecture": self.architecture,
            "chip": self.cpu_name(), "cores": os.cpu_count() or 1,
            "memory_bytes": total, "available_memory_bytes": available, "gpus": nvidia,
            "gpu": _gpu_summary(nvidia, False), "cuda_version": cuda, "vulkan": None,
            "rocm": None, "glibc": None, "accelerator": accelerator,
            "budget_bytes": budget, "budget_source": source,
            "models_folder": str(paths.models), "engines_folder": str(paths.engines),
            "free_disk_bytes": free_disk_bytes(paths.models), "hf_api": HF_API_VERSION,
        }


def _gpu_summary(gpus: list[dict[str, Any]], unified: bool) -> str | None:
    if not gpus:
        return None
    first = gpus[0]
    count = f"{len(gpus)} x " if len(gpus) > 1 else ""
    if unified:
        size = " (shares system memory)"
    elif first["memory_bytes"]:
        size = f" ({gb_text(first['memory_bytes'])}{' each' if count else ''})"
    else:
        size = ""
    return f"{count}{first['name']}{size}"


def _sysconf_memory() -> int:
    try:
        return int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"))
    except (ValueError, OSError, AttributeError):
        return 0


def _glibc_version() -> str | None:
    try:
        text = os.confstr("CS_GNU_LIBC_VERSION") or ""
    except (ValueError, OSError, AttributeError):
        return None
    match = re.search(r"([0-9]+\.[0-9]+)", text)
    return match.group(1) if match else None


# MARK: Engines


class EngineRegistry:
    """Engines ModelMatrix installed on this host (engines.json)."""

    def __init__(self, file: Path):
        self.file = file
        self.lock = threading.Lock()

    def all(self) -> dict[str, dict[str, Any]]:
        with self.lock:
            data = read_json(self.file, {})
            return data if isinstance(data, dict) else {}

    def get(self, engine: str) -> dict[str, Any] | None:
        value = self.all().get(engine)
        return value if isinstance(value, dict) else None

    def set(self, engine: str, record: dict[str, Any] | None) -> None:
        with self.lock:
            data = read_json(self.file, {})
            data = data if isinstance(data, dict) else {}
            if record is None:
                data.pop(engine, None)
            else:
                data[engine] = record
            write_json_atomically(self.file, data)


def llama_environment(server: str) -> dict[str, str]:
    """Environment for a llama-server: its own folder first in the library path."""
    environment = dict(os.environ)
    folder = str(Path(server).parent)
    existing = environment.get("LD_LIBRARY_PATH")
    environment["LD_LIBRARY_PATH"] = f"{folder}:{existing}" if existing else folder
    return environment


class EngineFinder:
    """Finds llama.cpp and Ollama on this host and reads their versions."""

    def __init__(
        self,
        paths: HFHostPaths,
        registry: EngineRegistry,
        run: Callable[..., tuple[int | None, str]] = run_command,
        which: Callable[[str], str | None] = shutil.which,
        is_executable: Callable[[str], bool] | None = None,
        ollama_version: Callable[[], str | None] | None = None,
    ):
        self.paths = paths
        self.registry = registry
        self.run = run
        self.which = which
        self.is_executable = is_executable or (lambda path: os.path.isfile(path) and os.access(path, os.X_OK))
        self.ollama_version = ollama_version or _ollama_server_version
        self._versions: dict[tuple[str, float], tuple[str | None, str | None]] = {}
        self.lock = threading.Lock()

    def _find(self, program: str, folders: tuple[str, ...]) -> str | None:
        found = self.which(program)
        if found and self.is_executable(found):
            return found
        code, out = self.run(["/bin/bash", "-lc", f"command -v {program}"], 5)
        lines = out.strip().splitlines() if code == 0 else []
        if lines and lines[-1].startswith("/") and self.is_executable(lines[-1]):
            return lines[-1]
        for folder in folders:
            candidate = str(self.paths.expand(folder) / program)
            if self.is_executable(candidate):
                return candidate
        return None

    def llama_server(self) -> tuple[str | None, bool]:
        """The llama-server to use and whether ModelMatrix installed it."""
        record = self.registry.get("llama.cpp")
        managed = str(record.get("path") or "") if record else ""
        if managed and self.is_executable(managed):
            return managed, True
        return self._find("llama-server", LLAMA_SEARCH_DIRS), False

    def llama_version(self, path: str) -> tuple[str | None, str | None]:
        """(version, problem): version is None when the program doesn't run."""
        try:
            stamp = os.path.getmtime(path)
        except OSError:
            stamp = 0.0
        with self.lock:
            cached = self._versions.get((path, stamp))
        if cached:
            return cached
        code, out = self.run([path, "--version"], 30, llama_environment(path))
        if code == 0:
            match = re.search(r"version:\s*([^\n]+)", out)
            result: tuple[str | None, str | None] = (match.group(1).strip() if match else "unknown", None)
        else:
            lines = [line.strip() for line in out.splitlines() if line.strip()]
            result = (None, lines[-1][:200] if lines else "it exited straight away")
        with self.lock:
            self._versions[(path, stamp)] = result
        return result

    def forget_versions(self) -> None:
        with self.lock:
            self._versions.clear()

    def detect(self) -> list[dict[str, Any]]:
        server, managed = self.llama_server()
        record = self.registry.get("llama.cpp") if managed else None
        llama: dict[str, Any] = {
            "id": "llama.cpp", "installed": False, "path": server, "version": None, "managed": managed,
            "installed_by": ("ModelMatrix" if managed else "manual") if server else None,
            "build": record.get("build") if record else None, "note": None,
        }
        if server:
            version, problem = self.llama_version(server)
            llama["version"] = version
            llama["installed"] = version is not None
            if problem:
                llama["note"] = f"llama-server at {server} doesn't run on this host: {problem}"
        ollama_path = self._find("ollama", OLLAMA_SEARCH_DIRS)
        served = self.ollama_version()
        version = served
        if version is None and ollama_path:
            code, out = self.run([ollama_path, "--version"], 10)
            match = re.search(r"([0-9]+\.[0-9]+\.[0-9]+)", out or "")
            version = match.group(1) if match else None
        ollama = {
            "id": "ollama", "installed": bool(ollama_path or served), "path": ollama_path, "version": version,
            "running": served is not None, "managed": False, "installed_by": "manual" if ollama_path else None,
            "note": None if served is not None or not ollama_path else "Ollama is installed but its service isn't running.",
        }
        return [llama, ollama]


def _ollama_server_version() -> str | None:
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/version", timeout=2) as response:
            return str(json.loads(response.read(4096)).get("version") or "") or None
    except (OSError, ValueError):
        return None


# MARK: Verified downloads of single files (engine archives)


def open_url(request: urllib.request.Request, timeout: float = 30):
    return urllib.request.urlopen(request, timeout=timeout)


def fetch_verified(
    url: str, target: Path, size: int, sha256: str, opener: Callable[..., Any],
    should_stop: Callable[[], bool], progress: Callable[[int], None],
) -> None:
    """Downloads `url` to `target`, refusing anything but the expected size and SHA-256."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    digest = hashlib.sha256()
    received = 0
    with opener(request, 60) as response, target.open("wb") as out:
        while True:
            if should_stop():
                raise _Stopped()
            chunk = response.read(CHUNK_BYTES)
            if not chunk:
                break
            received += len(chunk)
            if received > size:
                raise HFAgentError(f"{target.name} is larger than the approved {size} bytes; stopped.")
            digest.update(chunk)
            out.write(chunk)
            progress(received)
    if received != size:
        raise HFAgentError(f"{target.name} ended early ({received} of {size} bytes).")
    if digest.hexdigest() != sha256:
        raise HFAgentError(f"{target.name} failed its checksum check, so it was not installed.")


def safe_extract(archive: Path, destination: Path) -> None:
    """Unpacks a .tar.gz, refusing absolute paths, '..', devices and links that leave the folder."""
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        if len(members) > 5000:
            raise HFAgentError(f"{archive.name} has too many files.")
        total = 0
        for member in members:
            name = member.name
            parts = Path(name).parts
            if not parts or name.startswith("/") or ".." in parts:
                raise HFAgentError(f"{archive.name} contains an unsafe path ({name}).")
            if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
                raise HFAgentError(f"{archive.name} contains a device or special file ({name}).")
            if member.issym():
                resolved = os.path.normpath(os.path.join(os.path.dirname(name), member.linkname))
                if os.path.isabs(member.linkname) or resolved == ".." or resolved.startswith("../"):
                    raise HFAgentError(f"{archive.name} contains a link that leaves its folder ({name}).")
            if member.islnk():
                resolved = os.path.normpath(member.linkname)
                if os.path.isabs(resolved) or resolved == ".." or resolved.startswith("../"):
                    raise HFAgentError(f"{archive.name} contains a link that leaves its folder ({name}).")
            total += max(member.size, 0)
            if total > MAX_UNPACKED_BYTES:
                raise HFAgentError(f"{archive.name} unpacks to more than {gb_text(MAX_UNPACKED_BYTES)}.")
        if hasattr(tarfile, "data_filter"):
            tar.extractall(destination, members=members, filter="data")
        else:  # pragma: no cover - Python without the 2023 tarfile filters
            tar.extractall(destination, members=members)


def _single_root(folder: Path) -> Path:
    """The archive's top folder when everything is inside one, else the folder itself."""
    entries = [entry for entry in folder.iterdir() if not entry.name.startswith("._")]
    if len(entries) == 1 and entries[0].is_dir() and not entries[0].is_symlink():
        return entries[0]
    return folder


def _find_program(folder: Path, program: str, depth: int = 4) -> Path | None:
    base = len(folder.parts)
    for current, folders, files in os.walk(folder):
        if len(Path(current).parts) - base >= depth:
            folders[:] = []
        if program in files:
            return Path(current) / program
    return None


# MARK: Approved engine installs


class EngineInstaller:
    """Runs one approved llama.cpp install, update or uninstall at a time, with a live log."""

    def __init__(
        self, paths: HFHostPaths, registry: EngineRegistry, finder: EngineFinder,
        opener: Callable[..., Any] = open_url, on_change: Callable[[str], None] | None = None,
        in_use: Callable[[str], str | None] | None = None,
    ):
        self.paths = paths
        self.registry = registry
        self.finder = finder
        self.opener = opener
        self.on_change = on_change or (lambda engine: None)
        self.in_use = in_use or (lambda folder: None)
        self.lock = threading.Lock()
        self.jobs: dict[str, dict[str, Any]] = {}
        self.order: list[str] = []
        self.cancelled: set[str] = set()

    @property
    def folder(self) -> Path:
        return self.paths.engines / "llama.cpp"

    def _running(self) -> dict[str, Any] | None:
        return next((job for job in self.jobs.values() if job["state"] == "running"), None)

    def _new_job(self, job_id: str | None, action: str, title: str, extra: dict[str, Any]) -> dict[str, Any]:
        if job_id is not None and not JOB_ID_RE.match(job_id):
            raise HFAgentError("Invalid job id.")
        with self.lock:
            running = self._running()
            if running:
                raise HFAgentError(f"Wait for \"{running['title']}\" to finish first.", 409, "conflict")
            job = {
                "id": job_id or f"eng-{uuid.uuid4().hex[:12]}", "engine": "llama.cpp", "action": action,
                "method": "official", "title": title, "state": "running", "log": [], "log_count": 0,
                "error": None, "created_at": now_text(), "finished_at": None, **extra,
            }
            self.jobs[job["id"]] = job
            self.order.append(job["id"])
            del self.order[:-20]
            for stale in [key for key in self.jobs if key not in self.order]:
                self.jobs.pop(stale, None)
        return job

    def log(self, job: dict[str, Any], line: str) -> None:
        with self.lock:
            job["log"].append(line)
            job["log_count"] += 1
            del job["log"][:-LOG_LINES]

    def finish(self, job: dict[str, Any], state: str, error: str | None = None) -> None:
        with self.lock:
            job["state"] = state
            job["error"] = error
            job["finished_at"] = now_text()

    def public(self, job: dict[str, Any], since: int | None = None) -> dict[str, Any]:
        with self.lock:
            copy = {key: value for key, value in job.items() if key != "log"}
            lines = list(job["log"])
            count = job["log_count"]
        first = count - len(lines)
        if since is not None:
            lines = lines[max(since - first, 0):]
        copy["log"] = lines
        copy["next"] = count
        return copy

    def list(self) -> list[dict[str, Any]]:
        with self.lock:
            jobs = [self.jobs[key] for key in self.order if key in self.jobs]
        return [self.public(job) for job in reversed(jobs)]

    def job(self, job_id: str) -> dict[str, Any]:
        with self.lock:
            job = self.jobs.get(job_id)
        if job is None:
            raise HFAgentError(f"No engine job with id {job_id}.", 404, "unknown_item")
        return job

    def cancel(self, job_id: str) -> None:
        job = self.job(job_id)
        if job["state"] == "running" and job["action"] == "install":
            with self.lock:
                self.cancelled.add(job_id)

    # Install

    @staticmethod
    def validate(payload: dict[str, Any]) -> list[dict[str, Any]]:
        if payload.get("engine") != "llama.cpp":
            raise HFAgentError("Only llama.cpp can be installed on a Linux host. Install Ollama with its own installer.")
        archives = payload.get("archives")
        if not isinstance(archives, list) or not 1 <= len(archives) <= 2:
            raise HFAgentError("The install plan must list one or two archives.")
        checked = []
        for archive in archives:
            if not isinstance(archive, dict):
                raise HFAgentError("Invalid archive entry.")
            url, sha, size = archive.get("url"), archive.get("sha256"), archive.get("size")
            if not isinstance(url, str) or not LLAMA_URL_RE.match(url):
                raise HFAgentError("Only official llama.cpp release archives from GitHub can be installed.")
            if not isinstance(sha, str) or not SHA256_RE.match(sha):
                raise HFAgentError("Each archive needs its SHA-256 checksum.")
            if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= MAX_ENGINE_ARCHIVE_BYTES:
                raise HFAgentError("Invalid archive size.")
            checked.append({"url": url, "sha256": sha, "size": size, "name": url.rsplit("/", 1)[-1]})
        if not any(not archive["name"].startswith("cudart-") for archive in checked):
            raise HFAgentError("The install plan has no llama.cpp archive.")
        return checked

    def start_install(self, payload: dict[str, Any], run_async: bool = True) -> dict[str, Any]:
        archives = self.validate(payload)
        build = str(payload.get("build") or "")[:80]
        tag = str(payload.get("tag") or "")[:20]
        action = "update" if self.registry.get("llama.cpp") else "install"
        title = f"{'Update' if action == 'update' else 'Install'} llama.cpp {tag} ({build or 'official build'})"
        job = self._new_job(payload.get("job_id"), action, title, {"build": build, "tag": tag})
        if run_async:
            threading.Thread(target=self._install, args=(job, archives), daemon=True).start()
        else:
            self._install(job, archives)
        return self.public(job)

    def _install(self, job: dict[str, Any], archives: list[dict[str, Any]]) -> None:
        engines = self.paths.engines
        staging = engines / f".llama.cpp-new-{job['id']}"
        old = engines / f".llama.cpp-old-{job['id']}"
        try:
            engines.mkdir(parents=True, exist_ok=True)
            shutil.rmtree(staging, ignore_errors=True)
            staging.mkdir(parents=True)
            needed = sum(archive["size"] for archive in archives) * 3
            free = free_disk_bytes(engines)
            if free is not None and free < needed:
                raise HFAgentError(f"Not enough free disk space: {gb_text(needed)} needed, {gb_text(free)} free.")
            unpacked: list[Path] = []
            for index, archive in enumerate(archives):
                target = staging / archive["name"]
                self.log(job, f"Downloading {archive['name']} ({gb_text(archive['size'])}) from {archive['url']}")
                marks = {"next": 25}

                def progress(received: int, archive=archive, marks=marks) -> None:
                    percent = received * 100 // archive["size"]
                    if percent >= marks["next"]:
                        self.log(job, f"  {percent}% ({gb_text(received)} of {gb_text(archive['size'])})")
                        marks["next"] = (percent // 25 + 1) * 25

                fetch_verified(
                    archive["url"], target, archive["size"], archive["sha256"], self.opener,
                    lambda: job["id"] in self.cancelled, progress,
                )
                self.log(job, f"Checksum OK: SHA-256 {archive['sha256'][:16]}...")
                folder = staging / f"unpacked-{index}"
                folder.mkdir()
                safe_extract(target, folder)
                target.unlink()
                unpacked.append(folder)
                if job["id"] in self.cancelled:
                    raise _Stopped()
            main = next(
                (folder for folder, archive in zip(unpacked, archives) if not archive["name"].startswith("cudart-")),
                unpacked[0],
            )
            server = _find_program(main, "llama-server")
            if server is None:
                raise HFAgentError("The archive has no llama-server program.")
            final = staging / "llama.cpp"
            final.mkdir()
            shutil.move(str(server.parent), str(final / "bin"))
            bin_folder = final / "bin"
            for folder in unpacked:
                if not folder.exists() or folder == main:
                    continue
                root = _single_root(folder)
                for entry in root.iterdir():
                    destination = bin_folder / entry.name
                    if not destination.exists() and not destination.is_symlink():
                        shutil.move(str(entry), str(destination))
            program = bin_folder / "llama-server"
            program.chmod(program.stat().st_mode | 0o755)
            self.log(job, "Checking that llama-server runs on this host...")
            code, out = run_command([str(program), "--version"], 60, llama_environment(str(program)))
            lines = [line.strip() for line in out.splitlines() if line.strip()]
            if code != 0:
                detail = lines[-1] if lines else "it exited straight away"
                raise HFAgentError(f"This llama.cpp build doesn't run on this host: {detail}")
            version = next((line.split("version:", 1)[1].strip() for line in lines if "version:" in line), "unknown")
            self.log(job, f"llama-server {version} runs.")
            if self.folder.exists():
                os.replace(self.folder, old)
            os.replace(final, self.folder)
            shutil.rmtree(old, ignore_errors=True)
            self.registry.set("llama.cpp", {
                "path": str(self.folder / "bin" / "llama-server"), "folder": str(self.folder), "version": version,
                "build": job.get("build"), "tag": job.get("tag"), "installed_at": now_text(), "method": "official",
            })
            self.finder.forget_versions()
            self.log(job, f"Installed in {self.folder}. Remove it any time with ModelMatrix's engine uninstall for this host.")
            self.finish(job, "succeeded")
            self.on_change("llama.cpp")
        except _Stopped:
            self.log(job, "Cancelled. Nothing was installed.")
            self.finish(job, "cancelled")
        except HFAgentError as error:
            self.log(job, error.message)
            self.finish(job, "failed", error.message)
        except urllib.error.HTTPError as error:
            message = f"GitHub answered {error.code} while downloading llama.cpp. Nothing was installed."
            self.log(job, message)
            self.finish(job, "failed", message)
        except (OSError, tarfile.TarError, ValueError) as error:
            message = f"The install failed: {error}. Nothing was changed."
            self.log(job, message)
            self.finish(job, "failed", message)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
            if old.exists() and not self.folder.exists():
                os.replace(old, self.folder)  # put the previous copy back
            with self.lock:
                self.cancelled.discard(job["id"])

    def uninstall(self, job_id: str | None = None) -> dict[str, Any]:
        record = self.registry.get("llama.cpp")
        if not record:
            raise HFAgentError("ModelMatrix didn't install llama.cpp on this host, so it won't remove it.", 409, "conflict")
        user = self.in_use(str(self.folder))
        if user:
            raise HFAgentError(f"Stop {user} first: it is running on this llama.cpp.", 409, "conflict")
        job = self._new_job(job_id, "uninstall", "Uninstall llama.cpp", {"build": record.get("build"), "tag": record.get("tag")})
        try:
            self.log(job, f"Removing {self.folder}")
            shutil.rmtree(self.folder)
            self.registry.set("llama.cpp", None)
            self.finder.forget_versions()
            self.log(job, "Removed. Nothing outside that folder was changed.")
            self.finish(job, "succeeded")
            self.on_change("llama.cpp")
        except OSError as error:
            self.log(job, f"Could not remove {self.folder}: {error}")
            self.finish(job, "failed", str(error))
        return self.public(job)


# MARK: Windows engine installs


def _safe_windows_zip_name(name: str) -> str:
    """Validate a ZIP member and return its normalized slash-separated name."""
    value = name.replace("\\", "/")
    parts = value.split("/")
    reserved = re.compile(r"^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?$", re.IGNORECASE)
    if not value or value.startswith("/") or ":" in parts[0] or any(part in ("", ".", "..") for part in parts):
        raise HFAgentError(f"The Windows engine archive contains an unsafe path ({name}).")
    if any(reserved.match(part) for part in parts):
        raise HFAgentError(f"The Windows engine archive contains a reserved path ({name}).")
    return "/".join(parts)


def safe_extract_zip(archive: Path, destination: Path) -> None:
    """Extract only regular ZIP files beneath *destination*, with strict limits."""
    try:
        with zipfile.ZipFile(archive) as zipped:
            members = zipped.infolist()
            if len(members) > 5000:
                raise HFAgentError(f"{archive.name} has too many files.")
            total = 0
            for member in members:
                name = _safe_windows_zip_name(member.filename)
                mode = (member.external_attr >> 16) & 0xFFFF
                if mode and (mode & 0o170000) not in (0, 0o100000, 0o040000):
                    raise HFAgentError(f"{archive.name} contains a link or special file ({name}).")
                total += max(0, member.file_size)
                if total > MAX_UNPACKED_BYTES:
                    raise HFAgentError(f"{archive.name} unpacks to more than {gb_text(MAX_UNPACKED_BYTES)}.")
            destination.mkdir(parents=True, exist_ok=True)
            for member in members:
                name = _safe_windows_zip_name(member.filename)
                target = destination / name
                if member.is_dir() or member.filename.endswith(("/", "\\")):
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zipped.open(member, "r") as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output, CHUNK_BYTES)
    except zipfile.BadZipFile as error:
        raise HFAgentError(f"{archive.name} is not a valid Windows ZIP archive.") from error


def _find_windows_program(folder: Path) -> Path | None:
    for current, folders, files in os.walk(folder):
        folders[:] = [name for name in folders if not name.startswith(".")]
        for name in files:
            if name.lower() == "llama-server.exe":
                return Path(current) / name
    return None


class WindowsEngineInstaller:
    """Install official llama.cpp Windows ZIP assets transactionally.

    Plans are produced by ``Windows.LocalHF`` from GitHub release metadata;
    this class still validates every field so callers cannot turn the installer
    into an arbitrary URL or command runner.
    """

    def __init__(self, paths: HFHostPaths, registry: EngineRegistry, finder: Any,
                 opener: Callable[..., Any] = open_url, run: Callable[..., Any] = run_command,
                 on_change: Callable[[str], None] | None = None,
                 in_use: Callable[[str], str | None] | None = None):
        self.paths, self.registry, self.finder, self.opener, self.run = paths, registry, finder, opener, run
        self.on_change = on_change or (lambda engine: None)
        self.in_use = in_use or (lambda folder: None)
        self.lock = threading.Lock()
        self.jobs: dict[str, dict[str, Any]] = {}
        self.order: list[str] = []
        self.cancelled: set[str] = set()

    @property
    def folder(self) -> Path:
        return self.paths.engines / "llama.cpp"

    def _new_job(self, plan: dict[str, Any], action: str) -> dict[str, Any]:
        job_id = str(plan.get("job_id") or f"eng-{uuid.uuid4().hex[:12]}")
        if not JOB_ID_RE.match(job_id):
            raise HFAgentError("Invalid engine job id.")
        with self.lock:
            if any(item["state"] == "running" for item in self.jobs.values()):
                raise HFAgentError("Wait for the current engine operation to finish first.", 409, "conflict")
            job = {"id": job_id, "engine": "llama.cpp", "action": action, "method": "official",
                   "title": f"{'Update' if action == 'update' else 'Install'} llama.cpp for Windows",
                   "state": "running", "log": [], "log_count": 0, "error": None,
                   "started_at": now_text(), "finished_at": None, "path": None, "version": plan.get("version")}
            self.jobs[job_id] = job
            self.order.append(job_id)
            return job

    def _log(self, job: dict[str, Any], line: str) -> None:
        with self.lock:
            job["log"].append(str(line)[:500])
            job["log"] = job["log"][-LOG_LINES:]
            job["log_count"] += 1

    def public(self, job: dict[str, Any], since: int | None = None) -> dict[str, Any]:
        with self.lock:
            result = dict(job)
            lines = list(job.get("log", []))
            count = int(job.get("log_count", len(lines)))
        if since is not None:
            first = count - len(lines)
            lines = lines[max(0, since - first):]
        result["log"] = lines
        result["next"] = count
        return result

    def list(self) -> list[dict[str, Any]]:
        return [self.public(self.jobs[key]) for key in reversed(self.order) if key in self.jobs]

    def job(self, job_id: str) -> dict[str, Any]:
        job = self.jobs.get(job_id)
        if not job:
            raise HFAgentError(f"No engine job with id {job_id}.", 404, "unknown_item")
        return job

    @staticmethod
    def validate(payload: dict[str, Any]) -> list[dict[str, Any]]:
        if payload.get("engine") != "llama.cpp":
            raise HFAgentError("Only llama.cpp has a verified Windows installer.", 400, "unsupported_platform")
        archives = payload.get("archives")
        if not isinstance(archives, list) or len(archives) != 1:
            raise HFAgentError("A Windows install plan must contain exactly one ZIP archive.")
        archive = archives[0]
        if not isinstance(archive, dict):
            raise HFAgentError("Invalid Windows archive entry.")
        url, digest, size = archive.get("url"), archive.get("sha256"), archive.get("size")
        if not isinstance(url, str) or not WINDOWS_LLAMA_URL_RE.match(url):
            raise HFAgentError("Only official llama.cpp Windows release ZIPs can be installed.")
        if not isinstance(digest, str) or not SHA256_RE.match(digest.lower()):
            raise HFAgentError("The Windows archive needs a SHA-256 digest from GitHub.")
        if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= MAX_ENGINE_ARCHIVE_BYTES:
            raise HFAgentError("Invalid Windows archive size.")
        return [{"url": url, "sha256": digest.lower(), "size": size, "name": url.rsplit("/", 1)[-1]}]

    def start_install(self, payload: dict[str, Any], run_async: bool = True) -> dict[str, Any]:
        archives = self.validate(payload)
        old = self.registry.get("llama.cpp")
        job = self._new_job(payload, "update" if old else "install")
        if run_async:
            threading.Thread(target=self._install, args=(job, archives, payload), daemon=True).start()
        else:
            self._install(job, archives, payload)
        return self.public(job)

    def _install(self, job: dict[str, Any], archives: list[dict[str, Any]], plan: dict[str, Any]) -> None:
        staging = self.paths.engines / f".llama.cpp-new-{job['id']}"
        backup = self.paths.engines / f".llama.cpp-old-{job['id']}"
        try:
            self.paths.engines.mkdir(parents=True, exist_ok=True)
            shutil.rmtree(staging, ignore_errors=True)
            staging.mkdir()
            archive = archives[0]
            downloaded = staging / archive["name"]
            self._log(job, f"Downloading {archive['name']} ({gb_text(archive['size'])})")
            fetch_verified(archive["url"], downloaded, archive["size"], archive["sha256"], self.opener,
                           lambda: job["id"] in self.cancelled,
                           lambda done: self._log(job, f"Downloaded {done} of {archive['size']} bytes") if done == archive["size"] else None)
            self._log(job, "Checksum OK")
            unpacked = staging / "unpacked"
            safe_extract_zip(downloaded, unpacked)
            server = _find_windows_program(unpacked)
            if server is None:
                raise HFAgentError("The Windows archive has no llama-server.exe.")
            final = staging / "llama.cpp"
            shutil.move(str(server.parent), str(final / "bin"))
            program = final / "bin" / "llama-server.exe"
            path_env = dict(os.environ)
            path_env["PATH"] = str(program.parent) + os.pathsep + path_env.get("PATH", "")
            code, output = self.run([str(program), "--version"], 60, path_env)
            if code != 0:
                raise HFAgentError(f"This llama.cpp Windows build does not run: {(output or '').strip()[-200:]}")
            version = str(plan.get("version") or "unknown")
            self._log(job, f"llama-server {version} runs.")
            if self.folder.exists():
                os.replace(self.folder, backup)
            os.replace(final, self.folder)
            self.registry.set("llama.cpp", {"path": str(self.folder / "bin" / "llama-server.exe"),
                                             "folder": str(self.folder), "version": version,
                                             "tag": plan.get("tag"), "build": plan.get("build"),
                                             "installed_at": now_text(), "method": "official"})
            shutil.rmtree(backup, ignore_errors=True)
            self._log(job, f"Installed in {self.folder}")
            job["path"], job["version"] = str(self.folder / "bin" / "llama-server.exe"), version
            job["state"] = "succeeded"
            self.on_change("llama.cpp")
        except _Stopped:
            job["state"] = "cancelled"
            self._log(job, "Cancelled. Nothing was installed.")
        except (HFAgentError, OSError, zipfile.BadZipFile, urllib.error.HTTPError) as error:
            message = str(error)
            job["state"], job["error"] = "failed", message
            self._log(job, message)
        finally:
            job["finished_at"] = now_text()
            shutil.rmtree(staging, ignore_errors=True)
            if backup.exists() and not self.folder.exists():
                os.replace(backup, self.folder)
            self.cancelled.discard(job["id"])

    def cancel(self, job_id: str) -> None:
        job = self.job(job_id)
        if job["state"] == "running":
            self.cancelled.add(job_id)

    def uninstall(self, job_id: str | None = None) -> dict[str, Any]:
        record = self.registry.get("llama.cpp")
        if not record:
            raise HFAgentError("ModelMatrix did not install llama.cpp on this host.", 409, "conflict")
        user = self.in_use(str(self.folder))
        if user:
            raise HFAgentError(f"Stop {user} first: it is running on llama.cpp.", 409, "conflict")
        plan = {"job_id": job_id} if job_id else {}
        job = self._new_job(plan, "uninstall")
        try:
            shutil.rmtree(self.folder)
            self.registry.set("llama.cpp", None)
            self._log(job, "Removed the ModelMatrix llama.cpp folder.")
            job["state"] = "succeeded"
            self.on_change("llama.cpp")
        except OSError as error:
            job["state"], job["error"] = "failed", str(error)
            self._log(job, str(error))
        job["finished_at"] = now_text()
        return self.public(job)


# MARK: Downloads


ACTIVE_STATES = ("queued", "downloading", "waiting", "verifying")
FINISHED_STATES = ("completed", "failed", "cancelled")


class _Wait(Exception):
    """Wait, then try again: `reason` is network, disk, rate_limit or retry."""

    def __init__(self, reason: str, seconds: float, message: str):
        super().__init__(message)
        self.reason = reason
        self.seconds = seconds
        self.message = message


class _AuthRedirect(urllib.request.HTTPRedirectHandler):
    """Keeps the Hugging Face token on redirects to the same host only, never on CDN links."""

    def __init__(self, host: str | None, token: str | None):
        super().__init__()
        self.host = host
        self.token = token

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and self.token and urllib.parse.urlparse(newurl).hostname == self.host:
            new.add_unredirected_header("Authorization", f"Bearer {self.token}")
        return new


def fetch_with_token(request: urllib.request.Request, token: str | None, timeout: float):
    """Opens `request` through the system proxy settings, adding the token for Hugging Face only."""
    host = urllib.parse.urlparse(request.full_url).hostname
    if token:
        request.add_unredirected_header("Authorization", f"Bearer {token}")
    return urllib.request.build_opener(_AuthRedirect(host, token)).open(request, timeout=timeout)


def _safe_relative(path: str, what: str) -> str:
    if not isinstance(path, str) or not path or path.startswith("/") or "\\" in path or "\x00" in path:
        raise HFAgentError(f"Invalid {what}.")
    parts = path.split("/")
    if any(part in ("", ".", "..") or not PATH_PART_RE.match(part) for part in parts):
        raise HFAgentError(f"Invalid {what}: {path[:120]}")
    return path


def _int_field(value: Any, what: str, low: int = 0, high: int = 1 << 50) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise HFAgentError(f"Invalid {what}.")
    return value


def _endpoint(value: Any) -> str:
    endpoint = str(value or "https://huggingface.co").rstrip("/")
    parsed = urllib.parse.urlparse(endpoint)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query:
        raise HFAgentError("The Hugging Face address must be a plain https:// address.")
    return endpoint


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9._-]+", "-", text.lower()).strip("-._")
    return (slug or "model")[:60].rstrip("-.")


# Name endings that describe the packaging rather than the model (same list as the Mac).
PACKAGING_WORDS = {
    "gguf", "ggml", "mlx", "imatrix", "i1", "awq", "gptq",
    "2bit", "3bit", "4bit", "5bit", "6bit", "8bit", "bf16", "fp16", "f16", "fp32", "f32",
    "dwq", "mxfp4", "nvfp4", "quantized", "q4", "q8",
}


def base_model_name(repo: str) -> str:
    """"bartowski/Qwen2.5-7B-Instruct-GGUF" -> "qwen2.5-7b-instruct"."""
    words = [word for word in re.split(r"[-_ ]+", repo.split("/")[-1].lower()) if word]
    while len(words) > 1 and words[-1] in PACKAGING_WORDS:
        words.pop()
    return _slug("-".join(words))[:48].rstrip("-.") or "model"


class DownloadManager:
    """Saved queue of downloads on this host, one at a time, resumable across restarts."""

    def __init__(
        self, paths: HFHostPaths, profiles: "ProfileMaker",
        fetch: Callable[[urllib.request.Request, str | None, float], Any] = fetch_with_token,
        ollama: "OllamaClient | None" = None, sleep_scale: float = 1.0, start_worker: bool = True,
        token_provider: Callable[[], str | None] | None = None,
    ):
        self.paths = paths
        self.profiles = profiles
        self.fetch = fetch
        self.ollama = ollama or OllamaClient()
        # Tokens are deliberately not part of a serialized job.  A host adapter
        # may provide a secure store lookup here so a queued job can resume after
        # an agent restart without putting credentials in downloads.json.
        self.token_provider = token_provider
        self.rate_limit_bytes_per_second: int | None = None
        self.sleep_scale = sleep_scale
        self.file = paths.state / "downloads.json"
        self.lock = threading.RLock()
        self.wake = threading.Condition(self.lock)
        self.jobs: dict[str, dict[str, Any]] = {}
        self.order: list[str] = []
        self.tokens: dict[str, str] = {}
        self.flags: dict[str, str] = {}
        self.closed = False
        self._last_save = 0.0
        self._load()
        self.worker: threading.Thread | None = None
        if start_worker:
            self.worker = threading.Thread(target=self._work_loop, name="hf-downloads", daemon=True)
            self.worker.start()

    # Saved state

    def _load(self) -> None:
        saved = read_json(self.file, [])
        for job in saved if isinstance(saved, list) else []:
            if not isinstance(job, dict) or not isinstance(job.get("id"), str):
                continue
            if job.get("state") in ACTIVE_STATES:
                job["state"] = "queued"
                job["waiting_for"] = None
                job["message"] = "Continuing after the agent restarted"
            if job.get("profile_state") in ("creating", "starting"):
                job["profile_state"] = "created"
                job["profile_message"] = "The agent restarted before the model was tested. Press Start to try again."
            job["speed_bytes_per_second"] = 0
            job["eta_seconds"] = None
            self.jobs[job["id"]] = job
            self.order.append(job["id"])
        for job in list(self.jobs.values()):
            if job.get("state") == "cancelled" and job.get("remove_files_on_cancel"):
                self._remove_files(job)

    def save(self, force: bool = True) -> None:
        with self.lock:
            now = time.monotonic()
            if not force and now - self._last_save < SAVE_INTERVAL_SECONDS:
                return
            self._last_save = now
            snapshot = [self.jobs[key] for key in self.order if key in self.jobs]
            write_json_atomically(self.file, snapshot)

    def close(self) -> None:
        with self.lock:
            self.closed = True
            self.wake.notify_all()

    # Public API

    def list(self) -> list[dict[str, Any]]:
        with self.lock:
            return [json.loads(json.dumps(self.jobs[key])) for key in reversed(self.order) if key in self.jobs]

    def get(self, job_id: str) -> dict[str, Any]:
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                raise HFAgentError(f"No download with id {job_id}.", 404, "unknown_item")
            return json.loads(json.dumps(job))

    def add(self, payload: dict[str, Any]) -> dict[str, Any]:
        job = self._validated_job(payload)
        token = payload.get("token")
        with self.lock:
            if job["id"] in self.jobs:
                raise HFAgentError("A download with this id already exists.", 409, "conflict")
            for other in self.jobs.values():
                if other["state"] in ACTIVE_STATES + ("paused",) and other["repo"] == job["repo"] and (
                    other["label"] == job["label"] or other["folder"] == job["folder"] and
                    {file["path"] for file in other["files"]} & {file["path"] for file in job["files"]}
                ):
                    raise HFAgentError(
                        f"{job['repo']} ({other['label']}) is already downloading on this host.", 409, "conflict",
                    )
            if isinstance(token, str) and token:
                self.tokens[job["id"]] = token
            self.jobs[job["id"]] = job
            self.order.append(job["id"])
            self.save()
            self.wake.notify_all()
            return json.loads(json.dumps(job))

    def _validated_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw = payload.get("job")
        if not isinstance(raw, dict):
            raise HFAgentError("Missing download details.")
        job_id = str(raw.get("id") or "")
        if not JOB_ID_RE.match(job_id) or not job_id.startswith("dl-"):
            raise HFAgentError("Invalid download id.")
        kind = raw.get("kind")
        if kind not in ("files", "ollama"):
            raise HFAgentError("Invalid download kind.")
        repo = str(raw.get("repo") or "")
        if not REPO_RE.match(repo):
            raise HFAgentError("Invalid repository name.")
        revision = str(raw.get("revision") or "main")
        if not REVISION_RE.match(revision):
            raise HFAgentError("Invalid repository version.")
        label = str(raw.get("label") or "")
        if not LABEL_RE.match(label):
            raise HFAgentError("Invalid version label.")
        engine = raw.get("engine")
        if engine in ("mlx", "mlx-lm"):
            raise HFAgentError("MLX models run only on Apple Silicon Macs. Pick a GGUF version for this host.")
        if engine not in ("llama.cpp", "ollama"):
            raise HFAgentError("Invalid engine.")
        files_in = raw.get("files") if kind == "files" else []
        if not isinstance(files_in, list) or len(files_in) > 200 or (kind == "files" and not files_in):
            raise HFAgentError("Invalid file list.")
        files = []
        for item in files_in:
            if not isinstance(item, dict):
                raise HFAgentError("Invalid file entry.")
            sha = item.get("sha256")
            if sha is not None and (not isinstance(sha, str) or not SHA256_RE.match(sha)):
                raise HFAgentError("Invalid SHA-256 checksum.")
            files.append({
                "path": _safe_relative(item.get("path"), "file name"), "size": _int_field(item.get("size"), "file size"),
                "sha256": sha, "bytes_done": 0, "done": False, "reused": None,
            })
        folder_name = _safe_relative(str(payload.get("folder_name") or repo), "folder name")
        main_file = raw.get("main_file")
        projector = raw.get("projector_file")
        paths = {file["path"] for file in files}
        for value, what in ((main_file, "main file"), (projector, "image adapter file")):
            if value is not None and value not in paths:
                raise HFAgentError(f"The {what} is not in the file list.")
        if kind == "files" and engine == "llama.cpp" and not main_file:
            raise HFAgentError("The download has no main GGUF file.")
        ollama_model = raw.get("ollama_model")
        if kind == "ollama" and (not isinstance(ollama_model, str) or not MODEL_ID_RE.match(ollama_model)):
            raise HFAgentError("Invalid Ollama model name.")
        hint_name = str(payload.get("profile_name") or "")
        hint_model = str(payload.get("model_name") or "")
        now = now_text()
        return {
            "id": job_id, "kind": kind, "repo": repo, "revision": revision, "label": label,
            "format": "gguf", "engine": engine, "display_name": str(raw.get("display_name") or repo.split("/")[-1])[:120],
            "folder": str(self.paths.models / folder_name), "files": files, "main_file": main_file,
            "projector_file": projector, "ollama_model": ollama_model if kind == "ollama" else None,
            "ollama_had_model": None, "context": _int_field(raw.get("context", 8192), "context length", 512, 1 << 20),
            "parameters": raw.get("parameters") if isinstance(raw.get("parameters"), int) else None,
            "vision": bool(raw.get("vision")), "start_after": bool(raw.get("start_after", True)), "host": None,
            "state": "queued", "waiting_for": None, "bytes_done": 0, "bytes_total": sum(file["size"] for file in files),
            "speed_bytes_per_second": 0, "eta_seconds": None, "message": "Waiting to start", "error": None,
            "attempts": 0, "warnings": [str(item)[:300] for item in (raw.get("warnings") if isinstance(raw.get("warnings"), list) else [])[:10] if isinstance(item, str)],
            "gated": raw.get("gated") is True, "remove_files_on_cancel": False, "created_at": now, "updated_at": now,
            "completed_at": None, "profile": None, "profile_state": None, "profile_message": None,
            "ready_url": None, "ready_model": None,
            "memory_need_bytes": raw.get("memory_need_bytes") if isinstance(raw.get("memory_need_bytes"), int) else None,
            "profile_log": None, "endpoint": _endpoint(payload.get("endpoint")),
            "profile_hint": hint_name if PROFILE_NAME_RE.match(hint_name) else None,
            "model_hint": hint_model if MODEL_ID_RE.match(hint_model) else None,
        }

    def action(self, payload: dict[str, Any]) -> dict[str, Any]:
        job_id = str(payload.get("id") or "")
        action = payload.get("action")
        token = payload.get("token")
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                raise HFAgentError(f"No download with id {job_id}.", 404, "unknown_item")
            if isinstance(token, str) and token:
                self.tokens[job_id] = token
            state = job["state"]
            if action == "pause":
                if state not in ACTIVE_STATES:
                    raise HFAgentError("Only a running or waiting download can be paused.", 409, "conflict")
                self._set(job, state="paused", waiting_for=None, message="Paused", speed_bytes_per_second=0, eta_seconds=None)
                self.flags[job_id] = "pause"
            elif action == "resume":
                if state not in ("paused", "failed"):
                    raise HFAgentError("Only a paused or failed download can be resumed.", 409, "conflict")
                self._set(job, state="queued", error=None, attempts=0, message="Waiting to start")
                self.flags.pop(job_id, None)
            elif action == "cancel":
                if state in FINISHED_STATES:
                    raise HFAgentError("This download has already finished.", 409, "conflict")
                remove = bool(payload.get("delete_files"))
                self._set(job, state="cancelled", waiting_for=None, message="Cancelled", remove_files_on_cancel=remove,
                          speed_bytes_per_second=0, eta_seconds=None)
                self.flags[job_id] = "cancel"
                if state not in ("downloading", "verifying") and remove:
                    self._remove_files(job)
            elif action == "start":
                if state != "completed":
                    raise HFAgentError("The model can be started once its download has finished.", 409, "conflict")
                if job.get("profile_state") in ("creating", "starting"):
                    raise HFAgentError("The model is already starting.", 409, "conflict")
                threading.Thread(target=self.profiles.run, args=(self, job_id, True), daemon=True).start()
            elif action == "delete":
                if state in ACTIVE_STATES + ("paused",):
                    raise HFAgentError("Cancel the download before deleting it.", 409, "conflict")
                if job.get("profile_state") in ("creating", "starting"):
                    raise HFAgentError("Wait for the model to finish starting first.", 409, "conflict")
                kept = self.profiles.delete(job, bool(payload.get("delete_files")), self.ollama)
                if payload.get("delete_files"):
                    self._remove_files(job)
                self.jobs.pop(job_id, None)
                self.order.remove(job_id)
                self.tokens.pop(job_id, None)
                self.save()
                return {"deleted": True, "id": job_id, "kept_in_ollama": kept}
            else:
                raise HFAgentError("Unknown action. Use pause, resume, cancel, start or delete.")
            self.save()
            self.wake.notify_all()
            return {"download": json.loads(json.dumps(job))}

    def engine_changed(self) -> None:
        """An engine was installed: start profiles that were waiting for it."""
        with self.lock:
            waiting = [job["id"] for job in self.jobs.values() if job.get("profile_state") == "needs_engine"]
        for job_id in waiting:
            threading.Thread(target=self.profiles.run, args=(self, job_id, False), daemon=True).start()

    # Internals

    def _set(self, job: dict[str, Any], **values: Any) -> None:
        with self.lock:
            job.update(values)
            job["updated_at"] = now_text()

    def _target(self, job: dict[str, Any], file: dict[str, Any]) -> Path:
        return Path(job["folder"]) / file["path"]

    def _remove_files(self, job: dict[str, Any]) -> None:
        folder = Path(job["folder"])
        for file in job.get("files", []):
            target = folder / file["path"]
            for path in (target, target.with_name(target.name + ".part")):
                try:
                    if path.is_file() and not self._shared(job, file["path"]):
                        path.unlink()
                except OSError:
                    pass
        current = folder
        while current != self.paths.models and self.paths.models in current.parents:
            try:
                current.rmdir()
            except OSError:
                break
            current = current.parent
        for file in job.get("files", []):
            parent = (folder / file["path"]).parent
            while parent != folder and folder in parent.parents:
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent

    def _shared(self, job: dict[str, Any], path: str) -> bool:
        """Whether another kept download uses the same file."""
        return any(
            other is not job and other["folder"] == job["folder"] and other["state"] != "cancelled" and
            any(file["path"] == path for file in other["files"])
            for other in self.jobs.values()
        )

    def _stop_requested(self, job_id: str) -> bool:
        return job_id in self.flags or self.closed

    def _sleep(self, job_id: str, seconds: float) -> None:
        deadline = time.monotonic() + seconds * self.sleep_scale
        with self.lock:
            while not self._stop_requested(job_id):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                self.wake.wait(timeout=min(remaining, 1.0))

    def _work_loop(self) -> None:
        while True:
            with self.lock:
                if self.closed:
                    return
                job = next((self.jobs[key] for key in self.order if self.jobs.get(key, {}).get("state") == "queued"), None)
                if job is None:
                    self.wake.wait(timeout=5)
                    continue
                self.flags.pop(job["id"], None)
                self._set(job, state="downloading", message="Starting", error=None)
            self.run_job(job["id"])

    def run_job(self, job_id: str) -> None:
        """Runs one download until it finishes, waits, pauses or is cancelled."""
        with self.lock:
            job = self.jobs[job_id]
        try:
            if job["kind"] == "ollama":
                self._pull_ollama(job)
            else:
                self._download_files(job)
        except _Stopped:
            self._after_stop(job)
            self.save()
            return
        except _Wait as wait:
            with self.lock:
                if self._stop_requested(job_id):
                    self._after_stop(job)
                    self.save()
                    return
                self._set(job, state="waiting", waiting_for=wait.reason, message=wait.message,
                          speed_bytes_per_second=0, eta_seconds=None)
            self.save()
            self._sleep(job_id, wait.seconds)
            with self.lock:
                if job["state"] == "waiting":
                    self._set(job, state="queued", waiting_for=None)
            self.save()
            return
        except HFAgentError as error:
            self._set(job, state="failed", error=error.message, message="Failed", speed_bytes_per_second=0, eta_seconds=None)
            self.save()
            return
        self._set(job, state="completed", message="Downloaded and verified", completed_at=now_text(),
                  speed_bytes_per_second=0, eta_seconds=None, waiting_for=None, attempts=0)
        self.save()
        self.profiles.run(self, job_id, False)

    def _after_stop(self, job: dict[str, Any]) -> None:
        with self.lock:
            flag = self.flags.pop(job["id"], None)
            if flag == "cancel" and job.get("remove_files_on_cancel"):
                self._remove_files(job)
            elif flag is None and job["state"] in ACTIVE_STATES and not self.closed:
                self._set(job, state="queued")

    def _classify(self, job: dict[str, Any], error: Exception) -> Exception:
        """Turns a network or HTTP problem into a wait or a user-facing failure."""
        job["attempts"] = job.get("attempts", 0) + 1
        attempts = job["attempts"]
        backoff = min(5 * 2 ** min(attempts - 1, 4), 60)
        if isinstance(error, urllib.error.HTTPError):
            code = error.code
            try:
                error.close()  # release the connection now, not at garbage collection
            except Exception:  # noqa: BLE001 - nothing to release
                pass
            if code in (401, 403):
                return HFAgentError(
                    f"{job['repo']} needs a Hugging Face login. Save your token on the Mac "
                    "(modelmatrixctl hf-token set), accept the model's terms on huggingface.co, then press Resume.",
                    401, "login_required",
                )
            if code == 404:
                return HFAgentError(f"A file of {job['repo']} is no longer on Hugging Face (it may have been renamed or removed).")
            if code == 429:
                retry = error.headers.get("Retry-After") if error.headers else None
                seconds = float(retry) if retry and retry.isdigit() else backoff
                return _Wait("rate_limit", max(5.0, min(seconds, 300.0)), "Hugging Face asked to slow down; waiting")
            if code >= 500:
                if attempts > 10:
                    return HFAgentError(f"Hugging Face kept answering {code}. Press Resume to try again later.")
                return _Wait("retry", backoff, f"Hugging Face answered {code}; trying again")
            return HFAgentError(f"Hugging Face answered {code} for {job['repo']}.")
        if isinstance(error, OSError) and getattr(error, "errno", None) == 28:
            return _Wait("disk", 30, "The disk is full; waiting for free space")
        return _Wait("network", backoff, f"Waiting for the network ({str(error)[:80]})")

    def _download_files(self, job: dict[str, Any]) -> None:
        folder = Path(job["folder"])
        endpoint = job.get("endpoint") or "https://huggingface.co"
        count = len(job["files"])
        for index, file in enumerate(job["files"]):
            if file["done"]:
                continue
            target = self._target(job, file)
            if target.is_file() and target.stat().st_size == file["size"]:
                self._set(job, state="verifying", message=f"Checking the file already on disk ({file['path']})")
                if self._verify(job, target, file):
                    with self.lock:
                        file.update(done=True, bytes_done=file["size"], reused="existing")
                    self._recount(job)
                    continue
            remaining = sum(item["size"] - (item["bytes_done"] if not item["done"] else item["size"]) for item in job["files"])
            free = free_disk_bytes(folder)
            if free is not None and free < remaining + (512 << 20):
                raise _Wait("disk", 30, f"Waiting for free disk space: {gb_text(remaining)} needed, {gb_text(free)} free")
            self._set(job, state="downloading", waiting_for=None, message=f"Downloading file {index + 1} of {count}")
            try:
                self._fetch_file(job, file, target, endpoint)
            except (_Stopped, _Wait, HFAgentError):
                raise
            except (urllib.error.URLError, OSError, TimeoutError, ValueError) as error:
                raise self._classify(job, error) from error
            with self.lock:
                file.update(done=True, bytes_done=file["size"])
                job["attempts"] = 0
            self._recount(job)
            self.save()

    def _fetch_file(self, job: dict[str, Any], file: dict[str, Any], target: Path, endpoint: str) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_name(target.name + ".part")
        size = file["size"]
        offset = part.stat().st_size if part.exists() else 0
        if offset > size:
            part.unlink()
            offset = 0
        if offset < size or size == 0:
            url = f"{endpoint}/{job['repo']}/resolve/{urllib.parse.quote(job['revision'])}/{urllib.parse.quote(file['path'])}"
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            if offset:
                request.add_header("Range", f"bytes={offset}-")
            try:
                token = self.tokens.get(job["id"])
                if not token and self.token_provider is not None:
                    token = self.token_provider()
                response = self.fetch(request, token, 60)
            except urllib.error.HTTPError as error:
                if error.code == 416 and offset == size:
                    response = None
                elif error.code == 416:
                    part.unlink(missing_ok=True)
                    raise _Wait("retry", 1, "Restarting a file that changed on the server") from error
                else:
                    raise
            if response is not None:
                with response:
                    status = getattr(response, "status", 200)
                    if offset and status == 206:
                        match = re.match(r"bytes (\d+)-", response.headers.get("Content-Range") or "")
                        if not match or int(match.group(1)) != offset:
                            offset = 0
                    elif offset:
                        offset = 0  # the server ignored the range: start the file again
                    self._stream(job, file, response, part, offset)
        if part.exists() and part.stat().st_size != size:
            raise _Wait("retry", 2, f"{file['path']} ended early; continuing")
        if not part.exists():
            part.touch()
        self._set(job, state="verifying", message=f"Checking {file['path']}")
        if not self._verify(job, part, file):
            part.unlink(missing_ok=True)
            with self.lock:
                file["bytes_done"] = 0
                job.setdefault("warnings", []).append(f"{file['path']} failed its checksum check and was downloaded again.")
                failures = sum(1 for warning in job["warnings"] if warning.startswith(file["path"]))
            if failures >= 2:
                raise HFAgentError(f"{file['path']} failed its checksum check twice, so it was not kept.")
            raise _Wait("retry", 1, f"{file['path']} failed its checksum check; downloading it again")
        os.replace(part, target)

    def _stream(self, job: dict[str, Any], file: dict[str, Any], response: Any, part: Path, offset: int) -> None:
        window: list[tuple[float, int]] = [(time.monotonic(), offset)]
        with part.open("ab" if offset else "wb") as out:
            if not offset:
                out.truncate(0)
            while True:
                if self._stop_requested(job["id"]):
                    raise _Stopped()
                chunk = response.read(CHUNK_BYTES)
                if not chunk:
                    break
                offset += len(chunk)
                if offset > file["size"]:
                    raise HFAgentError(f"{file['path']} is larger than Hugging Face said; stopped.")
                out.write(chunk)
                limit = self.rate_limit_bytes_per_second
                if limit and limit > 0:
                    expected = offset / limit
                    elapsed_since_start = time.monotonic() - window[0][0]
                    if expected > elapsed_since_start:
                        time.sleep(min(expected - elapsed_since_start, 2.0))
                now = time.monotonic()
                window.append((now, offset))
                while len(window) > 2 and now - window[0][0] > 10:
                    window.pop(0)
                elapsed = now - window[0][0]
                speed = int((offset - window[0][1]) / elapsed) if elapsed > 0.5 else 0
                with self.lock:
                    file["bytes_done"] = offset
                    self._recount(job)
                    job["speed_bytes_per_second"] = speed
                    left = job["bytes_total"] - job["bytes_done"]
                    job["eta_seconds"] = int(left / speed) if speed > 0 else None
                    job["updated_at"] = now_text()
                self.save(force=False)

    def _verify(self, job: dict[str, Any], path: Path, file: dict[str, Any]) -> bool:
        if path.stat().st_size != file["size"]:
            return False
        if not file.get("sha256"):
            return True
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(4 * CHUNK_BYTES), b""):
                if self._stop_requested(job["id"]):
                    raise _Stopped()
                digest.update(chunk)
        return digest.hexdigest() == file["sha256"]

    def _recount(self, job: dict[str, Any]) -> None:
        with self.lock:
            job["bytes_done"] = sum(
                file["size"] if file["done"] else min(file["bytes_done"], file["size"]) for file in job["files"]
            )

    def _pull_ollama(self, job: dict[str, Any]) -> None:
        model = job["ollama_model"]
        if job.get("ollama_had_model") is None:
            had = self.ollama.has_model(model)
            if had is None:
                raise _Wait("retry", 15, "Waiting for Ollama on this host to answer")
            self._set(job, ollama_had_model=had)
        self._set(job, state="downloading", message=f"Ollama is pulling {model}")

        def progress(done: int, total: int, status: str) -> None:
            with self.lock:
                job["bytes_done"], job["bytes_total"] = done, max(total, done)
                job["message"] = status[:120] or f"Ollama is pulling {model}"
                job["updated_at"] = now_text()
            self.save(force=False)

        try:
            self.ollama.pull(model, progress, lambda: self._stop_requested(job["id"]))
        except (_Stopped, HFAgentError):
            raise
        except (urllib.error.URLError, OSError, TimeoutError, ValueError) as error:
            raise self._classify(job, error) from error


class OllamaClient:
    """The local Ollama service on this host."""

    def __init__(self, base: str = OLLAMA_URL):
        self.base = base

    def has_model(self, model: str) -> bool | None:
        try:
            with urllib.request.urlopen(f"{self.base}/api/tags", timeout=5) as response:
                names = {item.get("name") for item in json.loads(response.read()).get("models", [])}
        except (OSError, ValueError):
            return None
        return model in names or f"{model}:latest" in names

    def pull(self, model: str, progress: Callable[[int, int, str], None], should_stop: Callable[[], bool]) -> None:
        request = urllib.request.Request(
            f"{self.base}/api/pull", data=json.dumps({"model": model, "stream": True}).encode(),
            headers={"Content-Type": "application/json"},
        )
        layers: dict[str, tuple[int, int]] = {}
        with urllib.request.urlopen(request, timeout=60) as response:
            for raw in response:
                if should_stop():
                    raise _Stopped()
                line = json.loads(raw.decode("utf-8", "replace") or "{}")
                if line.get("error"):
                    raise HFAgentError(f"Ollama could not pull {model}: {line['error']}")
                if line.get("digest"):
                    layers[line["digest"]] = (int(line.get("completed") or 0), int(line.get("total") or 0))
                done = sum(value[0] for value in layers.values())
                total = sum(value[1] for value in layers.values())
                progress(done, total, str(line.get("status") or ""))
                if line.get("status") == "success":
                    return
        raise HFAgentError(f"Ollama stopped before {model} finished downloading.")

    def delete(self, model: str) -> bool:
        request = urllib.request.Request(
            f"{self.base}/api/delete", data=json.dumps({"model": model}).encode(),
            headers={"Content-Type": "application/json"}, method="DELETE",
        )
        try:
            with urllib.request.urlopen(request, timeout=30):
                return True
        except (OSError, ValueError):
            return False

    def chat_ok(self, model: str) -> tuple[bool, str]:
        return chat_test(f"{self.base}/v1", model)


def chat_test(base_url: str, model: str, timeout: float = 180) -> tuple[bool, str]:
    """Sends one short chat message; (answered, detail)."""
    body = {"model": model, "messages": [{"role": "user", "content": "Reply with the word ready."}],
            "max_tokens": 8, "temperature": 0}
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            answer = json.loads(response.read(1 << 20))
    except urllib.error.HTTPError as error:
        return False, f"the test message failed with HTTP {error.code}"
    except (OSError, ValueError) as error:
        return False, f"the test message failed ({error})"
    choices = answer.get("choices") if isinstance(answer, dict) else None
    if not choices:
        return False, "the server answered without a reply"
    return True, "answered the test message"


# MARK: Automatic profiles


class AgentProfileHost:
    """What the profile maker needs from the agent service."""

    def __init__(self, service: Any):
        self.service = service

    def directory(self) -> Path:
        return Path(self.service.configuration.profiles_directory).expanduser()

    def profiles(self) -> dict[str, dict[str, str]]:
        return {name: dict(profile.values) for name, profile in self.service.profiles.load().items()}

    def start(self, name: str) -> None:
        self.service.start(name)

    def stop(self, name: str) -> None:
        self.service.stop(name, force=False)

    def pid(self, name: str) -> int | None:
        return self.service._read_pid(name)  # noqa: SLF001 - the agent's own pid file

    def relay_url(self) -> str | None:
        relay = getattr(self.service, "usage", None)
        try:
            return str(relay.status().get("url")) if relay is not None else None
        except Exception:  # noqa: BLE001 - the relay is optional
            return None

    def reserved_ports(self) -> set[int]:
        ports = {int(getattr(self.service.configuration, "port", 0) or 0)}
        relay = getattr(self.service, "usage", None)
        if relay is not None:
            ports.add(int(getattr(relay, "port", 0) or 0))
        return ports


def _env_line(key: str, value: str) -> str:
    if "\n" in value or "\r" in value or "\x00" in value:
        raise HFAgentError(f"Invalid value for {key}.")
    return f'{key}="' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _profile_port(values: dict[str, str]) -> int | None:
    port = values.get("PORT", "")
    if port.isdigit():
        return int(port)
    parsed = urllib.parse.urlparse(values.get("BASE_URL") or "")
    return parsed.port


# Ending of the Mac's warning for a download whose engine isn't installed yet.
ENGINE_MISSING_SUFFIX = "isn't installed yet. The download can run now; the model starts once the engine is installed."


def start_failure_hint(log: list[str]) -> str:
    text = "\n".join(log).lower()
    if "glibc" in text:
        return "This llama.cpp build needs a newer Linux. Install a build made for this system, or update the OS."
    if "out of memory" in text or "failed to allocate" in text or "cudamalloc failed" in text:
        return "It ran out of memory. Pick a smaller version or a shorter context and download that."
    if "unknown argument" in text or "invalid argument" in text or "error: invalid" in text:
        return "The llama.cpp on this host is too old for these settings. Update it with: modelmatrixctl engine install llama.cpp --host <host>"
    if "address already in use" in text:
        return "Its port is in use by another program. Press Start again and ModelMatrix will pick another port."
    if "error while loading shared libraries" in text:
        return "A library llama.cpp needs is missing. Install the processor-only build, or install the GPU drivers it needs."
    return "See the log above. You can edit the profile, or try a smaller version."


class ProfileMaker:
    """Creates, starts and tests the profile for a finished download."""

    def __init__(
        self, host: Any, finder: EngineFinder, facts: Callable[[], dict[str, Any]],
        port_in_use: Callable[[int], bool] | None = None, process_alive: Callable[[int | None], bool] | None = None,
        models_url: Callable[[str], bool] | None = None, chat: Callable[[str, str], tuple[bool, str]] = chat_test,
        health_seconds: Callable[[dict[str, Any]], float] | None = None, poll_seconds: float = 1.0,
    ):
        from agent_core import port_is_listening, process_is_alive

        self.host = host
        self.finder = finder
        self.facts = facts
        self.port_in_use = port_in_use or (lambda port: port_is_listening(str(port)))
        self.process_alive = process_alive or process_is_alive
        self.models_url = models_url or _answers_models
        self.chat = chat
        self.health_seconds = health_seconds or (lambda job: min(120 + 20 * job.get("bytes_total", 0) / GIB, 900))
        self.poll_seconds = poll_seconds
        self.lock = threading.Lock()

    def run(self, manager: DownloadManager, job_id: str, explicit: bool) -> None:
        with manager.lock:
            job = manager.jobs.get(job_id)
            if job is None or job["state"] != "completed":
                return
            if job.get("profile_state") in ("creating", "starting"):
                return
            manager._set(job, profile_state="creating", profile_message="Creating the profile", profile_log=None)
        try:
            self._run(manager, job, explicit)
        except HFAgentError as error:
            manager._set(job, profile_state="start_failed", profile_message=error.message)
        except Exception as error:  # noqa: BLE001 - report, never crash the agent
            manager._set(job, profile_state="start_failed", profile_message=f"Could not set up the model: {error}")
        manager.save()

    def _run(self, manager: DownloadManager, job: dict[str, Any], explicit: bool) -> None:
        if job["kind"] == "ollama":
            ok, detail = manager.ollama.chat_ok(job["ollama_model"])
            manager._set(
                job, profile_state="ready" if ok else "start_failed", ready_model=job["ollama_model"] if ok else None,
                ready_url=f"{OLLAMA_URL}/v1" if ok else None,
                profile_message=f"Ready in Ollama on this host as {job['ollama_model']}" if ok else f"Ollama has the model, but {detail}.",
            )
            return
        server, _ = self.finder.llama_server()
        version = self.finder.llama_version(server)[0] if server else None
        existing = job.get("profile")
        if existing and self.process_alive(self.host.pid(existing)):
            values = self.host.profiles().get(existing, {})
            port, model = _profile_port(values), values.get("REQUEST_MODEL") or job.get("ready_model")
            if port and model:
                ok, detail = self.chat(f"http://127.0.0.1:{port}/v1", model)
                manager._set(
                    job, profile_state="ready" if ok else "start_failed", ready_model=model,
                    ready_url=(self.host.relay_url() or f"http://127.0.0.1:{port}/v1") if ok else None,
                    profile_message=f"{existing} is already running and {detail}." if ok else f"{existing} is running, but {detail}.",
                )
                return
        with self.lock:
            name, model, port = self._write_profile(job, server if version else None)
        manager._set(job, profile=name, ready_model=model)
        manager.save()
        if not version:
            manager._set(
                job, profile_state="needs_engine",
                profile_message=f"Profile {name} is ready but llama.cpp isn't installed on this host. "
                "Install it with: modelmatrixctl engine install llama.cpp --host <host>. The model starts once it is installed.",
            )
            return
        # The engine is here now, so a "not installed yet" warning from the check no longer applies.
        warnings = job.get("warnings") or []
        current = [item for item in warnings if not item.endswith(ENGINE_MISSING_SUFFIX)]
        if len(current) != len(warnings):
            manager._set(job, warnings=current)
        if not job.get("start_after") and not explicit:
            manager._set(job, profile_state="created", profile_message=f"Profile {name} created. Start it when you want to use it.")
            return
        facts = self.facts()
        need = job.get("memory_need_bytes") or 0
        available = facts.get("available_memory_bytes")
        if facts.get("budget_source") in ("cpu", "unified") and available and need and available < need:
            manager._set(
                job, profile_state="needs_memory",
                profile_message=f"Not started: it needs about {gb_text(need)} but only {gb_text(available)} is free on this host. "
                "Stop other models, then press Start.",
            )
            return
        manager._set(job, profile_state="starting", profile_message=f"Starting {name} on port {port}")
        manager.save()
        self.host.start(name)
        ready, detail = self._wait_ready(manager, job, name, port)
        if ready:
            ready, detail = self.chat(f"http://127.0.0.1:{port}/v1", model)
        if not ready:
            log = _tail(Path(f"/tmp/{''.join(c if c.isalnum() or c in '_.-' else '_' for c in name)}.log"), 25)
            manager._set(job, profile_state="start_failed", profile_log=log,
                         profile_message=f"{name} did not start: {detail}. {start_failure_hint(log)}")
            try:
                self.host.stop(name)
            except Exception:  # noqa: BLE001 - it may already be gone
                pass
            return
        relay = self.host.relay_url()
        manager._set(job, profile_state="ready", ready_url=relay or f"http://127.0.0.1:{port}/v1",
                     profile_message=f"{name} is running and answered the test message.")

    def _wait_ready(self, manager: DownloadManager, job: dict[str, Any], name: str, port: int) -> tuple[bool, str]:
        deadline = time.monotonic() + self.health_seconds(job)
        while time.monotonic() < deadline:
            if job["id"] not in manager.jobs:
                return False, "the download was deleted"
            pid = self.host.pid(name)
            if pid is not None and not self.process_alive(pid):
                return False, "the server stopped while loading"
            if self.models_url(f"http://127.0.0.1:{port}/v1/models"):
                return True, "ready"
            time.sleep(self.poll_seconds)
        return False, "it was still loading when the time limit ran out"

    def _write_profile(self, job: dict[str, Any], server: str | None) -> tuple[str, str, int]:
        directory = self.host.directory()
        profiles = self.host.profiles()
        existing = job.get("profile")
        mine = existing if existing and profiles.get(existing, {}).get("HF_DOWNLOAD_ID") == job["id"] else None
        others = {key: value for key, value in profiles.items() if key != mine}
        name = mine or self._unique_name(job, others, directory)
        model = profiles.get(mine, {}).get("REQUEST_MODEL") if mine else None
        model = model or self._unique_model(job, others)
        used_ports = {port for port in (_profile_port(values) for values in others.values()) if port}
        old_port = _profile_port(profiles.get(mine, {})) if mine else None
        port = old_port if old_port and not self.port_in_use(old_port) else None
        if port is None:
            reserved = self.host.reserved_ports()
            port = next(
                (candidate for candidate in range(8081, 9000)
                 if candidate not in used_ports and candidate not in reserved and not self.port_in_use(candidate)),
                None,
            )
        if port is None:
            raise HFAgentError("No free port between 8081 and 8999 for the new model.")
        folder = Path(job["folder"])
        model_path = str(folder / job["main_file"])
        program = server or str(self.finder.paths.engines / "llama.cpp" / "bin" / "llama-server")
        parts = [shlex.quote(program), "-m", shlex.quote(model_path), "--host", "127.0.0.1", "--port", str(port),
                 "--alias", shlex.quote(model), "-c", str(job["context"]), "-ngl", "999"]
        if job.get("projector_file"):
            parts += ["--mmproj", shlex.quote(str(folder / job["projector_file"]))]
        library = shlex.quote(str(Path(program).parent))
        command = f"env LD_LIBRARY_PATH={library}${{LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}} " + " ".join(parts)
        lines = [
            f"# Created by ModelMatrix from Hugging Face: {job['repo']} ({job['label']}).",
            "# Edit freely; delete it with: modelmatrixctl model delete <id> --host <host>",
            _env_line("DISPLAY_NAME", f"{job['display_name']} ({job['label']})"),
            _env_line("RUNTIME", "llama.cpp"),
            _env_line("HOST", "127.0.0.1"),
            _env_line("PORT", str(port)),
            _env_line("REQUEST_MODEL", model),
            _env_line("SERVER_MODEL_ID", model),
            _env_line("MODEL_PATH", model_path),
            _env_line("CONTEXT_SIZE", str(job["context"])),
            _env_line("HF_REPO", job["repo"]),
            _env_line("HF_REVISION", job["revision"]),
            _env_line("HF_VERSION", job["label"]),
            _env_line("HF_DOWNLOAD_ID", job["id"]),
            _env_line("START_COMMAND", command),
        ]
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{name}.env"
        temporary = directory / f".{name}.env.tmp"
        temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(temporary, target)
        return name, model, port

    @staticmethod
    def _unique_name(job: dict[str, Any], profiles: dict[str, dict[str, str]], directory: Path) -> str:
        base = job.get("profile_hint") or _slug(f"{base_model_name(job['repo'])}-{job['label']}")
        name, number = base, 2
        while name in profiles or (directory / f"{name}.env").exists():
            name, number = f"{base}-{number}", number + 1
        return name

    @staticmethod
    def _unique_model(job: dict[str, Any], profiles: dict[str, dict[str, str]]) -> str:
        taken = {value.get("REQUEST_MODEL") or key for key, value in profiles.items()}
        taken |= {value.get("SERVER_MODEL_ID") for value in profiles.values() if value.get("SERVER_MODEL_ID")}
        base = job.get("model_hint") or base_model_name(job["repo"])
        candidates = [base, f"{base}-{_slug(job['label'])}"]
        for candidate in candidates:
            if candidate not in taken:
                return candidate
        number = 2
        while f"{candidates[-1]}-{number}" in taken:
            number += 1
        return f"{candidates[-1]}-{number}"

    def delete(self, job: dict[str, Any], delete_files: bool, ollama: OllamaClient) -> bool | None:
        """Removes the profile ModelMatrix made; returns whether an Ollama model was kept."""
        name = job.get("profile")
        if name:
            values = self.host.profiles().get(name)
            if values and values.get("HF_DOWNLOAD_ID") == job["id"]:
                try:
                    self.host.stop(name)
                except Exception:  # noqa: BLE001 - not running
                    pass
                (self.host.directory() / f"{name}.env").unlink(missing_ok=True)
        if job["kind"] == "ollama" and delete_files:
            if job.get("ollama_had_model") is False:
                ollama.delete(job["ollama_model"])
                return False
            return True
        return None


def _answers_models(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            return 200 <= response.status < 300
    except (OSError, ValueError):
        return False


def _tail(path: Path, count: int) -> list[str]:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(handle.tell() - 64 * 1024, 0))
            lines = handle.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []
    return [line[:300] for line in lines[-count:]]


# MARK: Routes


class HFHost:
    """Everything Hugging Face on this host, behind /api/hf/*."""

    def __init__(
        self, service: Any, paths: HFHostPaths | None = None, facts: HostFacts | None = None,
        finder: EngineFinder | None = None, profile_host: Any = None, start_worker: bool = True,
        fetch: Callable[..., Any] = fetch_with_token, opener: Callable[..., Any] = open_url,
        ollama: OllamaClient | None = None, profile_options: dict[str, Any] | None = None,
    ):
        self.service = service
        self.paths = paths or HFHostPaths(Path(service.configuration.root))
        self.facts = facts or HostFacts()
        self.registry = EngineRegistry(self.paths.state / "engines.json")
        self.finder = finder or EngineFinder(self.paths, self.registry)
        self.profile_host = profile_host or AgentProfileHost(service)
        self.profiles = ProfileMaker(
            self.profile_host, self.finder, lambda: self.facts.payload(self.paths), **(profile_options or {}),
        )
        self.downloads = DownloadManager(self.paths, self.profiles, fetch=fetch, ollama=ollama, start_worker=start_worker)
        self.installer = EngineInstaller(
            self.paths, self.registry, self.finder, opener=opener,
            on_change=lambda engine: self.downloads.engine_changed(), in_use=self._running_profile_using,
        )

    def _running_profile_using(self, folder: str) -> str | None:
        for name, values in self.profile_host.profiles().items():
            if folder in values.get("START_COMMAND", "") and self.profiles.process_alive(self.profile_host.pid(name)):
                return name
        return None

    def host_payload(self) -> dict[str, Any]:
        return {"machine": self.facts.payload(self.paths), "engines": self.finder.detect(), "hf_api": HF_API_VERSION}

    def routes(self) -> dict[tuple[str, str], Callable[[dict[str, Any]], dict[str, Any]]]:
        def engine_job(query: dict[str, Any]) -> dict[str, Any]:
            since = str(query.get("since") or "")
            return {"job": self.installer.public(self.installer.job(str(query.get("id") or "")), int(since) if since.isdigit() else None)}

        def cancel_engine(payload: dict[str, Any]) -> dict[str, Any]:
            job_id = str(payload.get("id") or "")
            self.installer.cancel(job_id)
            return {"job": self.installer.public(self.installer.job(job_id))}

        return {
            ("GET", "/api/hf/host"): lambda _: self.host_payload(),
            ("GET", "/api/hf/downloads"): lambda _: {"downloads": self.downloads.list(), "models_folder": str(self.paths.models)},
            ("POST", "/api/hf/downloads"): lambda payload: {"download": self.downloads.add(payload)},
            ("POST", "/api/hf/downloads/action"): self.downloads.action,
            ("GET", "/api/hf/engines"): lambda _: {"engines": self.finder.detect(), "jobs": self.installer.list()},
            ("POST", "/api/hf/engines/install"): lambda payload: {"job": self.installer.start_install(payload)},
            ("POST", "/api/hf/engines/uninstall"): lambda payload: {"job": self.installer.uninstall(payload.get("job_id"))},
            ("GET", "/api/hf/engines/job"): engine_job,
            ("POST", "/api/hf/engines/cancel"): cancel_engine,
        }
