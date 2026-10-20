"""Windows-local Hugging Face controller surface.

This module deliberately owns only Windows behavior.  The download state
machine, resumable fetcher, and validation primitives remain in ``hf_agent``;
this adapter supplies Windows facts, JSON argv profiles, Credential Store
access, and the Dashboard-compatible route contract.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from agent_core import AgentError, Profile, parse_json_profile
from hf_agent import (
    HF_API_VERSION, HFAgentError, HFHostPaths, DownloadManager, EngineRegistry,
    WindowsEngineInstaller, WindowsHostFacts, fetch_with_token, now_text,
    _endpoint, _safe_relative, _slug, base_model_name, gb_text, MODEL_ID_RE,
)

try:
    from secrets_store import SecretStore
except ImportError:  # useful when imported as Windows.hf_local in a package
    from .secrets_store import SecretStore  # type: ignore


REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
REVISION_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
PLAN_RE = re.compile(r"^plan-[a-z0-9]{12,32}$")
HF_TOKEN_NAME = "huggingface"
PLAN_SECONDS = 10 * 60


def _json_copy(value: Any) -> Any:
    return json.loads(json.dumps(value))


def _iso_after(seconds: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + seconds))


def _valid_repo(value: Any) -> str:
    if not isinstance(value, str) or not REPO_RE.match(value):
        raise HFAgentError("Enter a Hugging Face repository such as TheBloke/TinyLlama-GGUF.")
    return value


def _model_input(value: Any) -> tuple[str, str | None]:
    """Normalize repo, Hugging Face URL, and repo:variant inputs."""
    if not isinstance(value, str) or not value.strip():
        raise HFAgentError("Enter a Hugging Face repository such as TheBloke/TinyLlama-GGUF.")
    raw = value.strip()
    variant = None
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme:
        if parsed.scheme != "https" or parsed.hostname not in {"huggingface.co", "www.huggingface.co", "hf.co"}:
            raise HFAgentError("Enter a huggingface.co model URL or owner/repository.")
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) < 2:
            raise HFAgentError("The Hugging Face URL does not contain a repository.")
        raw = "/".join(parts[:2])
        if len(parts) >= 4 and parts[2] in {"tree", "resolve"}:
            variant = parts[3]
    elif raw.count(":") == 1 and "/" in raw:
        raw, variant = raw.rsplit(":", 1)
    return _valid_repo(raw), variant


def _valid_revision(value: Any) -> str:
    value = str(value or "main")
    if not REVISION_RE.match(value):
        raise HFAgentError("Invalid Hugging Face revision.")
    return value


def _sha(value: Any) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{64}", value):
        return value.lower()
    return None


def _file_size(item: dict[str, Any]) -> int:
    value = item.get("size")
    return int(value) if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _variant_label(path: str) -> str:
    name = Path(path).stem
    name = re.sub(r"-\d{5}-of-\d{5}$", "", name, flags=re.I)
    name = re.sub(r"(?:model|weights)[-_]?", "", name, flags=re.I)
    return (name[-80:] or "GGUF")


def _is_projector(path: str) -> bool:
    return bool(re.search(r"(?:mmproj|projector)", Path(path).name, re.I))


def _complete_shard_group(files: list[dict[str, Any]]) -> bool:
    indices: list[int] = []
    total: int | None = None
    for item in files:
        match = re.search(r"-(\d{5})-of-(\d{5})\.gguf$", item["path"], re.I)
        if not match:
            continue
        index, declared = int(match.group(1)), int(match.group(2))
        total = declared if total is None else total
        if declared != total or index < 1 or index > declared:
            return False
        indices.append(index)
    return not indices or sorted(set(indices)) == list(range(1, (total or 0) + 1))


def _variant_quality(label: str) -> tuple[str, float | None, int]:
    upper = label.upper()
    match = re.search(r"(?:Q|IQ)(\d+(?:\.\d+)?)(?:[._-]?([A-Z]+))?", upper)
    if match:
        bits = float(match.group(1))
        suffix = (match.group(2) or "").replace("_", "")
        return f"{upper} quantized", bits, int(bits * 100) + (5 if suffix in {"KM", "KMS", "KML", "K"} else 0)
    if re.search(r"(?:F|FP)16|BF16", upper):
        return "16-bit full precision", 16.0, 1600
    if re.search(r"(?:F|FP)32", upper):
        return "32-bit full precision", 32.0, 3200
    return "GGUF weights", None, 0


def _task_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """Use Hub metadata, never a blanket chat=True assumption."""
    pipeline = str(metadata.get("pipeline_tag") or "").lower()
    tags = {str(tag).lower() for tag in metadata.get("tags", []) if isinstance(tag, str)}
    library = str(metadata.get("library_name") or "").lower()
    if pipeline in {"text-to-speech", "audio-to-audio", "automatic-speech-recognition", "audio-classification"} or "speech" in pipeline or "audio" in tags:
        kind, description, chat, vision = "speech", "Speech/audio model", False, False
    elif pipeline in {"feature-extraction", "sentence-similarity", "text-embeddings", "text-classification"} or "sentence-transformers" in library or "embeddings" in tags:
        kind, description, chat, vision = "embedding", "Embedding model", False, False
    elif pipeline in {"image-text-to-text", "image-to-text", "visual-question-answering", "image-classification", "object-detection"} or tags & {"vision", "multimodal", "image-text-to-text"}:
        kind, description, chat, vision = "vision", "Vision or multimodal model", False, True
    elif pipeline in {"text-generation", "text2text-generation", "conversational", "fill-mask"} or "text-generation" in tags:
        kind, description, chat, vision = "text", "Text generation", True, False
    else:
        kind, description, chat, vision = "unknown", "Model task was not declared by the Hub metadata", False, False
    return {"kind": kind, "description": description, "chat": chat, "vision": vision}


def _free_port(used: set[int]) -> int:
    for _ in range(40):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = int(sock.getsockname()[1])
        if port not in used and 1024 < port < 65536:
            return port
    raise HFAgentError("Could not allocate a free local model port.")


def _memory_need(file_bytes: int, context: int) -> int:
    # GGUF weights plus runtime metadata/KV cache.  This is intentionally an
    # estimate, not a claim of exact VRAM usage.
    return int(file_bytes * 1.12 + max(context, 512) * 4096 * 2)


class WindowsEngineFinder:
    """A Windows-aware engine detector with no shell or POSIX path fallback."""

    def __init__(self, paths: HFHostPaths, registry: EngineRegistry):
        self.paths, self.registry = paths, registry
        self.custom_paths: dict[str, str] = {}

    def set_custom_paths(self, values: dict[str, str]) -> None:
        self.custom_paths = dict(values)

    def _custom_candidates(self, engine: str, executable: str) -> list[str]:
        configured = self.custom_paths.get(engine)
        if not configured:
            return []
        path = Path(configured).expanduser()
        return [str(path / executable), str(path)] if path.is_dir() else [str(path)]

    @staticmethod
    def _version(path: str) -> tuple[str | None, str | None]:
        dll_context = None
        try:
            parent = str(Path(path).resolve().parent)
            add_dll_directory = getattr(os, "add_dll_directory", None)
            if add_dll_directory and os.name == "nt":
                try:
                    dll_context = add_dll_directory(parent)
                except OSError:
                    dll_context = None
            env = os.environ.copy()
            env["PATH"] = parent + os.pathsep + env.get("PATH", "")
            completed = subprocess.run([path, "--version"], capture_output=True, text=True,
                                       timeout=30, stdin=subprocess.DEVNULL, check=False, env=env)
        except (OSError, subprocess.TimeoutExpired) as error:
            return None, str(error)
        finally:
            if dll_context is not None:
                dll_context.close()
        if completed.returncode:
            return None, (completed.stderr or completed.stdout or "process failed").strip()[-200:]
        match = re.search(r"version:\s*([^\r\n]+)", completed.stdout or completed.stderr or "", re.I)
        return (match.group(1).strip() if match else "unknown"), None

    def llama_server(self) -> tuple[str | None, bool]:
        record = self.registry.get("llama.cpp") or {}
        managed = str(record.get("path") or "")
        if managed and Path(managed).is_file():
            return managed, True
        for candidate in (*self._custom_candidates("llama.cpp", "llama-server.exe"),
            shutil.which("llama-server.exe"), shutil.which("llama-server"),
            str(self.paths.engines / "llama.cpp" / "bin" / "llama-server.exe"),
            str(Path(os.environ.get("LOCALAPPDATA", "")) / "llama.cpp" / "llama-server.exe"),
        ):
            if candidate and Path(candidate).is_file():
                return candidate, False
        return None, False

    def detect(self) -> list[dict[str, Any]]:
        path, managed = self.llama_server()
        version, problem = self._version(path) if path else (None, None)
        llama = {"id": "llama.cpp", "name": "llama.cpp", "installed": version is not None,
                 "path": path, "version": version, "managed": managed,
                 "installed_by": "ModelMatrix" if managed else ("manual" if path else None),
                 "running": None, "note": problem}
        ollama = next((candidate for candidate in self._custom_candidates("ollama", "ollama.exe") if Path(candidate).is_file()), None)
        ollama = ollama or shutil.which("ollama.exe") or shutil.which("ollama")
        o_version = None
        if ollama:
            o_version, _ = self._version(ollama)
        return [{**llama, "applicable": True, "actions": ["install", "update", "uninstall"]},
                {"id": "ollama", "name": "Ollama", "applicable": True, "installed": bool(ollama),
                 "path": ollama, "version": o_version, "running": False, "managed": False,
                 "installed_by": "manual" if ollama else None,
                 "note": "Ollama is detected but its Windows installer is manual." if ollama else None,
                 "actions": []},
                {"id": "mlx-lm", "name": "MLX", "applicable": False, "installed": False,
                 "managed": False, "actions": [], "note": "MLX requires Apple Silicon and Metal; Windows cannot run it."}]


class WindowsProfileManager:
    """Generate and launch structured JSON profiles, never shell command strings."""

    def __init__(self, service: Any, finder: WindowsEngineFinder):
        self.service, self.finder = service, finder
        self.lock = threading.RLock()

    @property
    def directory(self) -> Path:
        return Path(self.service.configuration.profiles_directory).expanduser()

    def _profiles(self) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for path in self.directory.glob("*.json") if self.directory.exists() else []:
            try:
                result[path.stem] = parse_json_profile(path)
            except (OSError, ValueError):
                continue
        return result

    def _name(self, job: dict[str, Any]) -> str:
        base = _slug(f"{base_model_name(job['repo'])}-{job['label']}")
        existing = self._profiles()
        name, number = base, 2
        while name in existing or (self.directory / f"{name}.json").exists():
            name, number = f"{base}-{number}", number + 1
        return name

    def _write(self, job: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        server, _ = self.finder.llama_server()
        if not server:
            raise HFAgentError("llama.cpp is not installed on this Windows host.", 409, "engine_missing")
        name = job.get("profile") if isinstance(job.get("profile"), str) else self._name(job)
        model = job.get("model_hint") or base_model_name(job["repo"])
        folder = Path(job["folder"])
        used = {int(value["PORT"]) for value in self._profiles().values() if str(value.get("PORT", "")).isdigit()}
        config_port = int(getattr(self.service.configuration, "port", 0) or 0)
        port = int(job.get("port") or 0)
        if port <= 0 or port in used or port == config_port:
            port = _free_port(used | ({config_port} if config_port else set()))
        args = ["-m", str(folder / job["main_file"]), "--host", "127.0.0.1", "--port", str(port),
                "--alias", model, "-c", str(job["context"]), "-ngl", "999"]
        if job.get("projector_file"):
            args.extend(["--mmproj", str(folder / job["projector_file"])])
        full_argv = [str(server), *args]
        env_path = str(Path(server).parent) + os.pathsep + os.environ.get("PATH", "")
        # AgentService parses JSON profiles as flat uppercase string values.
        # START_COMMAND_JSON carries the executable; SERVER_ARGS_JSON is args only.
        profile = {"DISPLAY_NAME": f"{job['display_name']} ({job['label']})", "RUNTIME": "llama.cpp",
                   "HOST": "127.0.0.1", "PORT": str(port), "REQUEST_MODEL": model,
                   "SERVER_MODEL_ID": model, "MODEL_PATH": str(folder / job["main_file"]),
                   "CONTEXT_SIZE": str(job["context"]), "HF_REPO": job["repo"],
                   "HF_REVISION": job["revision"], "HF_VERSION": job["label"], "HF_DOWNLOAD_ID": job["id"],
                   "START_COMMAND_JSON": json.dumps(full_argv),
                   "SERVER_ARGS_JSON": json.dumps(args), "PATH": env_path,
                   "WORKING_DIRECTORY": str(Path(server).parent), "LOG_ALIAS": name}
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self.directory / f"{name}.json"
        temp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        temp.write_text(json.dumps(profile, indent=2) + "\n", encoding="utf-8")
        os.replace(temp, target)
        profile["port"] = port
        return name, profile

    @staticmethod
    def _ready(url: str, model: str) -> bool:
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                data = json.loads(response.read(1 << 20))
            models = data.get("data", []) if isinstance(data, dict) else []
            return any(isinstance(item, dict) and item.get("id") == model for item in models)
        except (OSError, ValueError):
            return False

    def _log_path(self, name: str, profile: dict[str, Any]) -> Path:
        try:
            return Path(Profile(name, {str(key): str(value) for key, value in profile.items()}).log_path)
        except Exception:  # noqa: BLE001 - log retrieval must never mask startup failure
            return self.directory / f"{name}.log"

    def _tail_log(self, path: Path, count: int = 80) -> list[str]:
        try:
            return path.read_text(encoding="utf-8-sig", errors="replace").splitlines()[-count:]
        except OSError:
            return []

    def run(self, manager: DownloadManager, job_id: str, explicit: bool) -> None:
        with manager.lock:
            job = manager.jobs.get(job_id)
            if not job or job.get("state") != "completed":
                return
            manager._set(job, profile_state="creating", profile_message="Creating the Windows profile")
        try:
            name, profile = self._write(job)
            job["profile"] = name
            if not job.get("start_after") and not explicit:
                manager._set(job, profile_state="created", profile_message=f"Profile {name} created. Start it when you want to use it.")
                manager.save()
                return
            manager._set(job, profile_state="starting", profile_message=f"Starting {name}")
            self.service.start(name)
            url = f"http://127.0.0.1:{profile['port']}/v1"
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                if self._ready(f"{url}/models", profile["REQUEST_MODEL"]):
                    ok, detail = self._chat(url, profile["REQUEST_MODEL"])
                    if ok:
                        manager._set(job, profile_state="ready", ready_model=profile["REQUEST_MODEL"],
                                     ready_url=url, profile_message=f"{name} is running and answered the test message.")
                        manager.save()
                        return
                    raise HFAgentError(f"{name} reported the model but the test chat failed: {detail}")
                time.sleep(0.5)
            raise HFAgentError(f"llama-server.exe did not expose model {profile['REQUEST_MODEL']} within 90 seconds.")
        except (HFAgentError, OSError) as error:
            manager._set(job, profile_state="start_failed", profile_message=str(error),
                         profile_log=self._tail_log(self._log_path(name, profile)))
            try:
                self.service.stop(name, force=False)
            except Exception:  # noqa: BLE001 - preserve the original startup failure
                pass
        manager.save()

    @staticmethod
    def _chat(url: str, model: str) -> tuple[bool, str]:
        body = json.dumps({"model": model, "messages": [{"role": "user", "content": "Reply with the word ready."}],
                           "max_tokens": 8, "temperature": 0}).encode()
        request = urllib.request.Request(f"{url.rstrip('/')}/chat/completions", data=body,
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                answer = json.loads(response.read(1 << 20))
            return bool(isinstance(answer, dict) and answer.get("choices")), "answered the test message"
        except urllib.error.HTTPError as error:
            return False, f"HTTP {error.code}"
        except (OSError, ValueError) as error:
            return False, str(error)

    def delete(self, job: dict[str, Any], delete_files: bool, ollama: Any) -> None:
        name = job.get("profile")
        if name:
            values = self._profiles().get(name)
            if values and values.get("HF_DOWNLOAD_ID") == job.get("id"):
                try:
                    self.service.stop(name, force=False)
                except Exception:  # noqa: BLE001 - the process may already be stopped
                    pass
                (self.directory / f"{name}.json").unlink(missing_ok=True)
        return None


class LocalHF:
    """The complete local Dashboard HF API for a Windows AgentService."""

    def __init__(self, service: Any):
        self.service = service
        root = Path(service.configuration.root).expanduser()
        self.paths = HFHostPaths(root)
        self.facts = WindowsHostFacts()
        self.registry = EngineRegistry(self.paths.state / "engines.json")
        self.finder = WindowsEngineFinder(self.paths, self.registry)
        self.secrets = SecretStore(root)
        self.profiles = WindowsProfileManager(service, self.finder)
        self.downloads = DownloadManager(self.paths, self.profiles, fetch=fetch_with_token,
                                         token_provider=self._token, start_worker=True)
        self.installer = WindowsEngineInstaller(self.paths, self.registry, self.finder,
                                                on_change=lambda _: self.downloads.engine_changed(),
                                                in_use=self._engine_in_use)
        self.settings_file = self.paths.state / "settings.json"
        try:
            saved_settings = json.loads(self.settings_file.read_text(encoding="utf-8-sig"))
            saved_models = saved_settings.get("models_folder") if isinstance(saved_settings, dict) else None
            if isinstance(saved_models, str) and Path(saved_models).is_absolute():
                self.paths.models = Path(saved_models)
            if isinstance(saved_settings, dict) and isinstance(saved_settings.get("engine_paths"), dict):
                self.finder.set_custom_paths({str(k): str(v) for k, v in saved_settings["engine_paths"].items()
                                              if isinstance(k, str) and isinstance(v, str)})
        except (OSError, ValueError):
            pass
        settings = self._settings()
        self.downloads.rate_limit_bytes_per_second = int(settings["speed_limit_mbps"] * 1024 * 1024 / 8) if settings["speed_limit_mbps"] else None
        self.plans: dict[str, dict[str, Any]] = {}
        self.plan_lock = threading.Lock()
        self._awake_stop = threading.Event()
        self._awake_thread = threading.Thread(target=self._keep_awake_loop, name="hf-keep-awake", daemon=True)
        self._awake_thread.start()

    @staticmethod
    def _set_keep_awake(enabled: bool) -> None:
        """Use the Windows execution-state API; never claim a no-op toggle."""
        if os.name != "nt":
            return
        try:
            kernel32 = __import__("ctypes").windll.kernel32
            api = kernel32.SetThreadExecutionState
            api.argtypes = [__import__("ctypes").wintypes.UINT]
            api.restype = __import__("ctypes").wintypes.UINT
            # ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED.
            api(0x80000000 | (0x00000001 | 0x00000040) if enabled else 0x80000000)
        except (AttributeError, OSError):
            pass

    def close(self) -> None:
        self._awake_stop.set()
        if self._awake_thread is not threading.current_thread():
            self._awake_thread.join(timeout=2)
        self.downloads.close()
        worker = getattr(self.downloads, "worker", None)
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=2)

    def _keep_awake_loop(self) -> None:
        try:
            while not self._awake_stop.wait(15):
                try:
                    settings = self._settings()
                    active = any(job.get("state") in {"queued", "downloading", "waiting", "verifying"} for job in self.downloads.list())
                    self._set_keep_awake(bool(settings.get("keep_awake") and active))
                except (OSError, ValueError):
                    # The data folder can be removed during an uninstall. Do
                    # not resurrect it or crash a background worker.
                    break
        finally:
            self._set_keep_awake(False)

    def _token(self) -> str | None:
        return self.secrets.get(HF_TOKEN_NAME) or self.secrets.get("hf_token")

    def _engine_in_use(self, _folder: str) -> str | None:
        for job in self.downloads.list():
            if job.get("engine") == "llama.cpp" and job.get("profile_state") in {"starting", "ready"}:
                return str(job.get("profile") or job.get("display_name") or "a model")
        return None

    def _settings(self) -> dict[str, Any]:
        try:
            value = json.loads(self.settings_file.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            value = {}
        if not isinstance(value, dict):
            value = {}
        endpoint = value.get("endpoint")
        try:
            free_disk = shutil.disk_usage(self.paths.models.parent if self.paths.models.parent.exists() else self.paths.models).free
        except OSError:
            free_disk = None
        return {"models_folder": str(self.paths.models), "models_folder_path": str(self.paths.models),
                "free_disk_bytes": free_disk,
                "endpoint": endpoint if isinstance(endpoint, str) else None,
                "endpoint_in_use": str(endpoint or "https://huggingface.co"),
                "speed_limit_mbps": float(value.get("speed_limit_mbps", 0) or 0),
                "connections_per_file": int(value.get("connections_per_file", 1) or 1),
                "keep_awake": bool(value.get("keep_awake", False)),
                "start_after_download": bool(value.get("start_after_download", True)),
                "token_saved": bool(self._token()), "engine_paths": value.get("engine_paths", {}) if isinstance(value.get("engine_paths", {}), dict) else {},
                "capabilities": {"speed_limit": True, "connections_per_file": False, "keep_awake": os.name == "nt"}}

    def _save_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        current = self._settings()
        allowed = {"endpoint", "speed_limit_mbps", "connections_per_file", "keep_awake", "start_after_download", "engine_paths", "models_folder"}
        for key, value in patch.items():
            if key not in allowed:
                continue
            if key == "endpoint":
                current[key] = None if value in (None, "") else _endpoint(value)
            elif key == "models_folder":
                if not isinstance(value, str) or not value.strip():
                    raise HFAgentError("Models folder must be a non-empty path.")
                candidate = Path(value).expanduser()
                if not candidate.is_absolute():
                    raise HFAgentError("Models folder must be an absolute Windows path.")
                current[key] = str(candidate)
                self.paths.models = candidate
            elif key == "engine_paths":
                if not isinstance(value, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in value.items()):
                    raise HFAgentError("Engine paths must be a map of engine names to absolute paths.")
                current[key] = value
                self.finder.set_custom_paths(value)
            elif key in ("speed_limit_mbps", "connections_per_file"):
                if not isinstance(value, (int, float)) or value < 0:
                    raise HFAgentError(f"Invalid {key}.")
                current[key] = value
            else:
                current[key] = bool(value)
        persisted = {key: current[key] for key in allowed if key in current}
        self.settings_file.parent.mkdir(parents=True, exist_ok=True)
        temp = self.settings_file.with_suffix(".tmp")
        temp.write_text(json.dumps(persisted, indent=2), encoding="utf-8")
        os.replace(temp, self.settings_file)
        if "speed_limit_mbps" in current:
            self.downloads.rate_limit_bytes_per_second = int(float(current["speed_limit_mbps"]) * 1024 * 1024 / 8) if current["speed_limit_mbps"] else None
        return self._settings()

    def _request_json(self, url: str, token: str | None = None, timeout: float = 20) -> Any:
        request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "ModelMatrix-Agent"})
        try:
            with fetch_with_token(request, token, timeout) as response:
                return json.loads(response.read(8 * 1024 * 1024))
        except urllib.error.HTTPError as error:
            if error.code in (401, 403):
                raise HFAgentError("Hugging Face requires a token for this model.", 401, "login_required") from error
            if error.code == 404:
                raise HFAgentError("Hugging Face could not find that model or revision.", 404, "not_found") from error
            raise HFAgentError(f"Hugging Face answered HTTP {error.code}.", 502, "upstream_error") from error
        except (OSError, ValueError) as error:
            raise HFAgentError(f"Could not reach Hugging Face: {error}", 502, "upstream_error") from error

    def _request_text(self, url: str, timeout: float = 20) -> str:
        request = urllib.request.Request(url, headers={"Accept": "text/plain", "User-Agent": "ModelMatrix-Agent"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read(4096).decode("utf-8", "replace").strip()
        except (OSError, urllib.error.HTTPError, ValueError) as error:
            raise HFAgentError("GitHub's official llama.cpp nightly marker could not be read.", 503, "artifact_unverified") from error

    def _repo_report(self, repo: str, revision: str, context: int) -> dict[str, Any]:
        settings = self._settings()
        endpoint = settings["endpoint_in_use"]
        token = self._token()
        metadata = self._request_json(f"{endpoint}/api/models/{urllib.parse.quote(repo, safe='/')}?blobs=true", token)
        metadata = metadata if isinstance(metadata, dict) else {}
        pinned = str(metadata.get("sha") or "")
        resolved_revision = pinned if re.fullmatch(r"[0-9a-fA-F]{40}", pinned) else revision
        tree_url = f"{endpoint}/api/models/{urllib.parse.quote(repo, safe='/')}/tree/{urllib.parse.quote(resolved_revision, safe='')}?recursive=true"
        tree = self._request_json(tree_url, token)
        if not isinstance(tree, list):
            tree = tree.get("siblings", []) if isinstance(tree, dict) else []
        files: list[dict[str, Any]] = []
        projectors: list[dict[str, Any]] = []
        for item in tree:
            if not isinstance(item, dict) or item.get("type", "file") != "file":
                continue
            path = item.get("path")
            if not isinstance(path, str) or not path.lower().endswith(".gguf"):
                continue
            size = _file_size(item)
            if not size and isinstance(item.get("lfs"), dict):
                size = _file_size(item["lfs"])
            record = {"path": path, "size": size, "sha256": _sha(item.get("sha256")) or _sha((item.get("lfs") or {}).get("sha256"))}
            if size > 0 and _is_projector(path):
                projectors.append(record)
            elif size > 0:
                files.append(record)
        gated_value = metadata.get("gated", False)
        gated = bool(gated_value and gated_value not in ("false", "auto"))
        variants: list[dict[str, Any]] = []
        notes: list[str] = ["Memory values are estimates; actual use depends on runtime settings."]
        machine = self.facts.payload(self.paths)
        budget = int(machine.get("budget_bytes") or 0)
        disk = machine.get("free_disk_bytes")
        grouped: dict[str, list[dict[str, Any]]] = {}
        for item in files:
            # Split GGUF shards are one downloadable variant, not independent
            # models.  Keep the original paths and checksums in the payload.
            key = re.sub(r"-\d{5}-of-\d{5}(?=\.gguf$)", "", item["path"], flags=re.I)
            grouped.setdefault(key, []).append(item)
        for group in grouped.values():
            group.sort(key=lambda item: item["path"])
            if any(not item.get("sha256") for item in group):
                notes.append(f"Skipped {group[0]['path']}: Hugging Face did not provide SHA-256 for every shard.")
                continue
            if not _complete_shard_group(group):
                notes.append(f"Skipped {group[0]['path']}: GGUF shard set is incomplete.")
                continue
            total_bytes = sum(item["size"] for item in group)
            need = _memory_need(total_bytes, context)
            level = "unknown" if budget <= 0 else ("good" if need <= budget * 8 // 10 else ("tight" if need <= budget else "too_big"))
            label = _variant_label(group[0]["path"])
            quality, bits, quality_score = _variant_quality(label)
            variants.append({"label": label, "files": group, "total_bytes": total_bytes,
                             "download_bytes": total_bytes, "bits_per_weight": bits, "quality": quality,
                             "full_precision": bits is not None and bits >= 16, "supported": True, "fit": {"context": context, "need_bytes": need,
                             "cache_bytes": max(0, need - total_bytes), "level": level},
                             "longest_good_context": context if level != "too_big" else 0,
                             "fits_on_disk": disk is None or disk > total_bytes + 512 * 1024 * 1024,
                             "recommended": False, "_quality_score": quality_score})
        task = _task_metadata(metadata)
        projector = min(projectors, key=lambda item: item["size"]) if projectors else None
        if projector:
            if not projector.get("sha256"):
                notes.append("The mmproj file has no SHA-256 and cannot be downloaded safely.")
                projector = None
            else:
                notes.append("The mmproj projector is an image adapter, not a main model variant.")
        supported_task = task["kind"] in {"text", "vision"} and (task["kind"] != "vision" or projector is not None)
        candidates = [item for item in variants if item["fits_on_disk"] and item["fit"]["level"] != "too_big"]
        chosen = max(candidates or variants, key=lambda item: (item["_quality_score"], -item["download_bytes"])) if variants else None
        if chosen:
            chosen["recommended"] = True
        for item in variants:
            item.pop("_quality_score", None)
        license_value = None
        card = metadata.get("cardData")
        if isinstance(card, dict):
            license_value = card.get("license")
        license_value = license_value or metadata.get("license")
        repo_info = {"id": repo, "url": f"{endpoint}/{repo}", "revision": resolved_revision, "sha": pinned or None,
                     "license": license_value, "gated": gated, "login_required": gated and not bool(token),
                     "downloads": metadata.get("downloads"), "likes": metadata.get("likes"),
                     "library": metadata.get("library_name"), "pipeline_tag": metadata.get("pipeline_tag"),
                     "last_modified": metadata.get("lastModified")}
        if not variants:
            verdict = {"code": "unsupported_format", "title": "No verified GGUF variant found", "detail": "No complete GGUF variant with size and SHA-256 metadata was published for this revision.", "runnable": False}
        elif gated and not token:
            verdict = {"code": "login_required", "title": "Hugging Face login required", "detail": "Save a token and accept this model's terms on Hugging Face before downloading.", "runnable": False}
        elif not pinned:
            verdict = {"code": "unverified_revision", "title": "Repository revision is not pinned", "detail": "Hugging Face did not return a commit SHA, so this model cannot be downloaded safely.", "runnable": False}
        elif not supported_task:
            verdict = {"code": "unsupported_task", "title": "Unsupported model task", "detail": f"The Hub metadata identifies this as a {task['kind']} model; Windows llama.cpp downloads are limited to text and vision GGUF models with a verified projector.", "runnable": False}
        else:
            verdict = {"code": "ready" if chosen and chosen["fit"]["level"] != "too_big" else "too_big", "title": "Verified GGUF model", "detail": "Complete GGUF files and SHA-256 checksums are pinned to this repository revision.", "runnable": bool(chosen and chosen["fit"]["level"] != "too_big")}
        recommendation = None
        if chosen:
            recommendation = {"label": chosen["label"], "context": context, "need_bytes": chosen["fit"]["need_bytes"],
                              "download_bytes": chosen["download_bytes"], "level": chosen["fit"]["level"], "reason": "Highest-quality verified variant that fits the reported memory and disk budget."}
        return {"schema_version": HF_API_VERSION, "input": repo, "kind": "model", "repo": repo_info,
                "format": {"id": "gguf", "name": "GGUF", "summary": "llama.cpp quantized model"},
                "task": task,
                "verdict": verdict, "machine": {**machine, "apple_silicon": False, "rosetta": False},
                "engine": {"needed": "llama.cpp", "needed_name": "llama.cpp", "ready": bool(self.finder.llama_server()[0]),
                           "engines": self.finder.detect(), "installer_available": True},
                "model": {"name": base_model_name(repo), "architecture": metadata.get("config", {}).get("model_type") if isinstance(metadata.get("config"), dict) else None, "parameters": None,
                           "max_context": context, "default_context": context, "context": context,
                           "layout_source": "GGUF file manifest", "cache_bytes_at_context": recommendation["need_bytes"] - recommendation["download_bytes"] if recommendation else 0},
                "variants": variants, "projector": projector, "recommendation": recommendation,
                "alternatives": [], "search_results": [], "notes": notes,
                "download_available": bool(variants) and bool(pinned) and supported_task and not (gated and not token), "download_folder": str(self.paths.models), "checked_at": now_text()}

    def _search(self, text: str) -> dict[str, Any]:
        if not text.strip():
            raise HFAgentError("Enter a model search term.")
        endpoint = self._settings()["endpoint_in_use"]
        data = self._request_json(f"{endpoint}/api/models?search={urllib.parse.quote(text)}&limit=20&sort=downloads&direction=-1", self._token())
        results = []
        for item in data if isinstance(data, list) else []:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                continue
            results.append({"repo": item["id"], "format": "gguf" if "gguf" in item["id"].lower() else "unknown",
                            "downloads": item.get("downloads"), "likes": item.get("likes"), "gated": bool(item.get("gated")), "same_model": False})
        return {"results": results, "search_results": results, "checked_at": now_text()}

    def _download(self, body: dict[str, Any]) -> dict[str, Any]:
        engine = str(body.get("engine") or "llama.cpp")
        input_name = body.get("name")
        if engine == "ollama":
            repo, input_variant = "ollama/model", None
        else:
            repo, input_variant = _model_input(input_name)
        # Dashboard's ``version`` is the selected GGUF variant label.  HF's
        # repository revision is an independent value and defaults to main.
        revision = _valid_revision(body.get("revision"))
        context = int(body.get("context") or 8192)
        if context < 512 or context > (1 << 20):
            raise HFAgentError("Context must be between 512 and 1048576 tokens.")
        if engine in ("mlx", "mlx-lm"):
            raise HFAgentError("MLX requires Apple Silicon and Metal; it is not available on Windows.", 400, "unsupported_platform")
        if engine == "ollama":
            model = body.get("ollama_model") or body.get("model") or body.get("name")
            if not isinstance(model, str) or not MODEL_ID_RE.match(model):
                raise HFAgentError("Enter a valid Ollama model name.")
            job_id = f"dl-{uuid.uuid4().hex[:12]}"
            ollama_repo = model.removeprefix("hf.co/").split(":", 1)[0]
            repo = ollama_repo if REPO_RE.match(ollama_repo) else f"ollama/{_slug(ollama_repo)}"
            raw = {"id": job_id, "kind": "ollama", "repo": repo, "revision": "main", "label": input_variant or "latest",
                   "engine": "ollama", "display_name": model, "files": [], "main_file": None, "ollama_model": model,
                   "context": context, "start_after": self._settings()["start_after_download"], "warnings": []}
            added = self.downloads.add({"job": raw, "folder_name": _slug(repo.replace("/", "-")),
                                        "endpoint": self._settings()["endpoint_in_use"], "token": self._token()})
            return {"download": added, "existing": False}
        if engine != "llama.cpp":
            raise HFAgentError("Windows downloads currently support GGUF through llama.cpp or Ollama.")
        report = self._repo_report(repo, revision, context)
        if not report["download_available"]:
            raise HFAgentError(report["verdict"]["detail"], 409, report["verdict"]["code"])
        selected = report["variants"][0]
        wanted = body.get("version") or input_variant
        for variant in report["variants"]:
            if isinstance(wanted, str) and wanted.lower() in variant["label"].lower():
                selected = variant
                break
        selected_files = list(selected["files"])
        projector = report.get("projector") if report.get("task", {}).get("vision") else None
        if projector:
            selected_files.append(projector)
        job_id = f"dl-{uuid.uuid4().hex[:12]}"
        pinned_revision = report["repo"].get("revision") or revision
        raw = {"id": job_id, "kind": "files", "repo": repo, "revision": pinned_revision, "label": selected["label"], "engine": engine,
               "display_name": repo.split("/", 1)[-1], "files": selected_files, "main_file": selected["files"][0]["path"],
               "projector_file": projector["path"] if projector else None,
               "context": context, "start_after": self._settings()["start_after_download"], "memory_need_bytes": selected["fit"]["need_bytes"],
               "gated": report["repo"]["gated"], "warnings": report["notes"]}
        folder_name = _safe_relative(_slug(repo.replace("/", "-")), "folder name")
        added = self.downloads.add({"job": raw, "folder_name": folder_name, "endpoint": self._settings()["endpoint_in_use"], "token": self._token()})
        return {"download": added, "existing": False}

    def _models(self) -> dict[str, Any]:
        models = []
        tracked_folders: set[str] = set()
        for job in self.downloads.list():
            if job.get("state") != "completed":
                continue
            folder = Path(job["folder"])
            tracked_folders.add(str(folder.resolve()))
            existing = [item["path"] for item in job.get("files", []) if (folder / item["path"]).is_file()]
            missing = len(job.get("files", [])) - len(existing)
            size = sum((folder / item["path"]).stat().st_size for item in job.get("files", []) if (folder / item["path"]).is_file())
            models.append({"id": job["id"], "repo": job["repo"], "label": job["label"], "display_name": job["display_name"],
                           "format": job.get("format", "gguf"), "engine": job["engine"], "kind": job.get("kind", "files"), "files": existing,
                           "folder": str(folder), "bytes_on_disk": size, "missing_files": missing, "completed_at": job.get("completed_at"),
                           "profile": job.get("profile"), "profile_state": job.get("profile_state"), "profile_message": job.get("profile_message"),
                           "ready_url": job.get("ready_url"), "ready_model": job.get("ready_model")})
        untracked = []
        if self.paths.models.exists():
            for folder in self.paths.models.iterdir():
                try:
                    resolved = str(folder.resolve())
                    if folder.is_dir() and resolved not in tracked_folders:
                        untracked.append({"folder": str(folder), "bytes_on_disk": sum(item.stat().st_size for item in folder.rglob("*") if item.is_file())})
                except OSError:
                    continue
        return {"models": models, "untracked": untracked}

    def _plan(self, body: dict[str, Any]) -> dict[str, Any]:
        engine = str(body.get("engine") or "")
        action = str(body.get("action") or "install")
        if engine in ("mlx", "mlx-lm"):
            raise HFAgentError("MLX requires Apple Silicon and Metal; Windows cannot install it.", 400, "unsupported_platform")
        if engine not in ("llama.cpp", "ollama") or action not in ("install", "update", "uninstall"):
            raise HFAgentError("Unsupported Windows engine action.")
        now = now_text()
        plan_id = f"plan-{uuid.uuid4().hex[:16]}"
        downloads: list[dict[str, Any]] = []
        notes: list[str] = []
        version = None
        if engine == "llama.cpp" and action != "uninstall":
            data = self._request_json("https://api.github.com/repos/ggml-org/llama.cpp/releases/latest", timeout=20)
            tag = data.get("tag_name") if isinstance(data, dict) else None
            assets = data.get("assets", []) if isinstance(data, dict) else []
            arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"
            def cpu_asset(asset: Any) -> bool:
                name = str(asset.get("name", "")) if isinstance(asset, dict) else ""
                lower = name.lower()
                return bool(re.search(r"(?:^|[-_])(?:win|windows)(?:[-_]|$)", lower)
                            and re.search(r"(?:^|[-_])" + arch + r"(?:[-_.]|$)", lower)
                            and lower.endswith(".zip")
                            and not re.search(r"(?:cuda|cudart|vulkan|rocm|hip)", lower))
            candidates = [asset for asset in assets if cpu_asset(asset)]
            # Recent llama.cpp releases (including v0.7.0) intentionally put
            # Windows binaries on a nightly release.  The documented marker
            # is authoritative; do not guess a bNNN tag or fabricate a URL.
            if not candidates and isinstance(tag, str) and tag:
                marker_url = f"https://github.com/ggml-org/llama.cpp/releases/download/{urllib.parse.quote(tag, safe='')}/nightly-tag.txt"
                nightly_tag = self._request_text(marker_url)
                if not re.fullmatch(r"b[0-9]+", nightly_tag):
                    raise HFAgentError("GitHub's nightly marker was not a valid llama.cpp release tag.", 503, "artifact_unverified")
                data = self._request_json(f"https://api.github.com/repos/ggml-org/llama.cpp/releases/tags/{nightly_tag}", timeout=20)
                tag = data.get("tag_name") if isinstance(data, dict) else nightly_tag
                assets = data.get("assets", []) if isinstance(data, dict) else []
                candidates = [asset for asset in assets if cpu_asset(asset)]
            chosen = next((asset for asset in candidates if re.search(rf"(?:^|[-_])(?:win|windows)-{arch}\.zip$", str(asset.get("name", "")), re.I)), None)
            chosen = chosen or (sorted(candidates, key=lambda item: str(item.get("name", "")))[0] if candidates else None)
            if not chosen or not isinstance(chosen.get("digest"), str) or not chosen["digest"].lower().startswith("sha256:"):
                raise HFAgentError("GitHub did not publish a SHA-256 digest for a Windows llama.cpp asset.", 503, "artifact_unverified")
            digest = chosen["digest"].split(":", 1)[1].lower()
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise HFAgentError("GitHub returned an invalid SHA-256 digest.", 503, "artifact_unverified")
            version = str(tag or "latest")
            downloads = [{"name": chosen["name"], "url": chosen.get("browser_download_url"), "size": int(chosen.get("size") or 0), "sha256": digest}]
            if not isinstance(downloads[0]["url"], str) or not downloads[0]["url"].startswith("https://github.com/ggml-org/llama.cpp/releases/download/"):
                raise HFAgentError("GitHub returned an invalid official asset URL.", 503, "artifact_unverified")
        elif engine == "ollama":
            notes = ["Ollama's official Windows installer is not downloaded or executed by ModelMatrix.", "Download and verify Ollama from ollama.com, then return to refresh engines."]
        plan = {"id": plan_id, "engine": engine, "engine_name": "llama.cpp" if engine == "llama.cpp" else "Ollama", "action": action,
                "method": "official", "version": version, "installed_version": next((x.get("version") for x in self.finder.detect() if x.get("id") == engine), None),
                "title": f"{'Install' if action == 'install' else action.title()} {engine} on Windows", "commands": [], "downloads": downloads,
                "download_size": gb_text(sum(item["size"] for item in downloads)), "location": str(self.paths.engines / "llama.cpp"),
                "needs_admin": False, "removal": "Remove only the ModelMatrix-managed llama.cpp folder.", "notes": notes,
                "created_at": now, "expires_at": _iso_after(PLAN_SECONDS)}
        with self.plan_lock:
            self.plans[plan_id] = {"plan": plan, "expires": time.time() + PLAN_SECONDS}
        return plan

    def _run_plan(self, body: dict[str, Any]) -> dict[str, Any]:
        plan_id = body.get("plan_id")
        if not isinstance(plan_id, str) or not PLAN_RE.match(plan_id):
            raise HFAgentError("Missing or invalid engine plan id.")
        if body.get("approved") is not True:
            raise HFAgentError("Engine changes require explicit approval.", 400, "approval_required")
        with self.plan_lock:
            record = self.plans.get(plan_id)
            if not record or record["expires"] < time.time():
                self.plans.pop(plan_id, None)
                raise HFAgentError("That engine plan expired or has already been used.", 409, "plan_expired")
            # Consume only after approval and expiry checks pass, atomically with
            # the lookup, so retries cannot execute a one-use plan twice.
            self.plans.pop(plan_id, None)
        plan = record["plan"]
        if plan["engine"] == "ollama":
            raise HFAgentError("Ollama installation is manual on Windows; no installer command was executed.", 409, "manual_install")
        if plan["action"] == "uninstall":
            return {"job": self.installer.uninstall()}
        return {"job": self.installer.start_install({"engine": "llama.cpp", "archives": plan["downloads"], "version": plan.get("version"), "tag": plan.get("version")})}

    def route(self, method: str, path: str, query: dict[str, Any], body: dict[str, Any]) -> tuple[int, dict[str, Any]] | None:
        method, path = method.upper(), path.rstrip("/") or "/"
        if path == "/api/hf/check" and method == "GET":
            name = query.get("name") or query.get("repo")
            if name:
                repo, input_revision = _model_input(str(name))
                return 200, self._repo_report(repo, _valid_revision(query.get("version") or input_revision), int(query.get("context") or 8192))
            return 200, self._search(str(query.get("search") or ""))
        if path in ("/api/hf/search", "/api/hf/check/search") and method == "GET":
            return 200, self._search(str(query.get("q") or query.get("search") or query.get("name") or ""))
        if path == "/api/hf/settings":
            return 200, {"settings": self._settings()} if method == "GET" else {"settings": self._save_settings(body)}
        if path == "/api/hf/token":
            if method == "POST":
                token = body.get("token")
                if not isinstance(token, str) or not token.strip() or len(token) > 4096 or any(ord(c) < 32 for c in token):
                    raise HFAgentError("Enter a valid Hugging Face token.")
                self.secrets.set(HF_TOKEN_NAME, token.strip())
                return 200, {"saved": True}
            if method == "DELETE":
                self.secrets.delete(HF_TOKEN_NAME)
                self.secrets.delete("hf_token")
                return 200, {"saved": False}
        if path == "/api/hf/downloads" and method == "GET":
            return 200, {"downloads": self.downloads.list(), "models_folder": str(self.paths.models)}
        if path == "/api/hf/downloads" and method == "POST":
            return 200, self._download(body)
        match = re.fullmatch(r"/api/hf/downloads/([^/]+)(?:/(pause|resume|cancel|start|delete))?", path)
        if match:
            job_id, action = urllib.parse.unquote(match.group(1)), match.group(2)
            if method == "GET" and not action:
                return 200, {"download": self.downloads.get(job_id)}
            if method == "DELETE" and not action:
                action = "delete"
            if method == "POST" and action:
                result = self.downloads.action({"id": job_id, "action": action, "delete_files": bool(body.get("delete_files"))})
                return 200, result
        if path == "/api/hf/models" and method == "GET":
            return 200, self._models()
        if path == "/api/hf/models/delete" and method == "POST":
            result = self.downloads.action({"id": body.get("id"), "action": "delete", "delete_files": bool(body.get("delete_files"))})
            return 200, {"removed": body.get("id"), "files_deleted": bool(body.get("delete_files")), "freed_bytes": 0,
                          "profile_deleted": None, "kept_in_ollama": result.get("kept_in_ollama") if isinstance(result, dict) else False}
        if path == "/api/hf/engines" and method == "GET":
            return 200, {"engines": self.finder.detect(), "engines_folder": str(self.paths.engines), "jobs": self.installer.list(), "host": "local"}
        if path == "/api/hf/engines/plan" and method == "POST":
            return 200, {"plan": self._plan(body)}
        if path == "/api/hf/engines/run" and method == "POST":
            return 200, self._run_plan(body)
        match = re.fullmatch(r"/api/hf/engines/jobs(?:/([^/]+))?(?:/cancel)?", path)
        if match and method == "GET":
            job_id = match.group(1)
            if job_id:
                since = int(query.get("since") or 0) if str(query.get("since") or "0").isdigit() else 0
                return 200, {"job": self.installer.public(self.installer.job(job_id), since)}
            return 200, {"jobs": self.installer.list()}
        match = re.fullmatch(r"/api/hf/engines/jobs/([^/]+)/cancel", path)
        if match and method == "POST":
            self.installer.cancel(match.group(1))
            return 200, {"job": self.installer.public(self.installer.job(match.group(1)))}
        return None
