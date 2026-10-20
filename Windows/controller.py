"""Windows local controller for ModelMatrix.

This module intentionally keeps the existing RemoteAgent service as the source of
truth for lifecycle and status operations.  The HTTP layer adds the browser
session, dashboard assets, profile editing, usage compatibility, and Windows
capability reporting without changing the macOS product.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import hmac
import http.client
import json
import mimetypes
import os
import platform
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from http import HTTPStatus
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

# RemoteAgent is deliberately imported from the repository rather than copied.
_REMOTE = Path(__file__).resolve().parents[1] / "RemoteAgent"
if str(_REMOTE) not in sys.path:
    sys.path.insert(0, str(_REMOTE))

from agent_core import (  # type: ignore
    AgentError,
    InvalidConfigurationError,
    InvalidProfileError,
    ProfileNotFoundError,
    UnsupportedError,
    UsageError,
)
from modelmatrix_agent import (  # type: ignore
    AGENT_VERSION,
    AgentConfiguration,
    AgentRequestHandler,
    AgentService,
    ProfileRepository,
    RUNTIME_SPECS,
    UsageRelay,
    UsageStore,
    host_metrics_payload,
    start_usage_relay,
)
from modelmatrix_agent import (  # type: ignore
    Profile,
    build_start_command,
    _popen_profile,
    _spawn_environment,
)
from agent_core import parse_json_profile  # type: ignore

try:
    from capture import start_capture
except ImportError:  # package-style import
    from .capture import start_capture  # type: ignore

# These files are owned by the HF/bridge workers.  Import them by name as part
# of the stable extension contract; a source checkout without those optional
# workers still starts and reports the capability as unavailable.
try:  # pragma: no cover - direct ``python Windows/controller.py`` execution
    from hf_local import LocalHF  # type: ignore
except ImportError:  # pragma: no cover - package-style ``import Windows.controller``
    try:
        from .hf_local import LocalHF  # type: ignore
    except ImportError:
        LocalHF = None  # type: ignore
try:  # pragma: no cover - direct ``python Windows/controller.py`` execution
    from bridge import LocalBridge  # type: ignore
except ImportError:  # pragma: no cover - package-style ``import Windows.controller``
    try:
        from .bridge import LocalBridge  # type: ignore
    except ImportError:
        LocalBridge = None  # type: ignore


SESSION_COOKIE = "modelmatrix_session"
PROFILE_NAME_RE = __import__("re").compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,63}$")
PROFILE_KEY_RE = __import__("re").compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MAX_BODY = 2 * 1024 * 1024
SAFE_HEADER = __import__("re").compile(r"^[\x20-\x7e\t]*$")


class UnauthorizedError(AgentError):
    http_status = 401
    code = "unauthorized"
    public_message = "unauthorized"


def _json_error(status: int, code: str, message: str) -> tuple[int, dict[str, Any]]:
    return status, {"error": code, "message": message}


def _profile_name(value: Any) -> str:
    if not isinstance(value, str) or not PROFILE_NAME_RE.fullmatch(value.strip()):
        raise UsageError("profile must be 1-64 characters: letters, numbers, space, '.', '_' or '-'")
    if value.strip() != value or value.endswith((".", " ")) or ".." in value or "\\" in value or "/" in value:
        raise UsageError("profile contains an unsafe path character")
    reserved = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
    if value.casefold() in reserved:
        raise UsageError("profile uses a reserved Windows device name")
    return value


def _string_map(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise UsageError("values must be an object")
    result: dict[str, str] = {}
    for key, raw in value.items():
        if not isinstance(key, str) or not PROFILE_KEY_RE.fullmatch(key):
            raise UsageError(f"invalid profile key: {key!r}")
        if isinstance(raw, (dict, list, tuple)) or raw is None:
            raise UsageError(f"profile value {key} must be a string")
        text = str(raw)
        if "\r" in text:
            raise UsageError(f"profile value {key} contains CR")
        result[key] = text
    return result


def _env_quote(value: str) -> str:
    if value and __import__("re").fullmatch(r"[A-Za-z0-9_./:@%+,=-]+", value):
        return value
    if "'" not in value and "\n" not in value and "\t" not in value and "\r" not in value:
        return "'" + value + "'"
    return json.dumps(value, ensure_ascii=False)


def _parse_env(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if not PROFILE_KEY_RE.fullmatch(key.strip()):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] == "'":
            value = value[1:-1]
        elif len(value) >= 2 and value[0] == value[-1] == '"':
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                value = value[1:-1]
        result[key.strip()] = value
    return result

def _parse_env_text(text: str) -> dict[str, str]:
    # Windows editors conventionally submit CRLF.  Normalize line endings but
    # continue rejecting a bare carriage return embedded in a value.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", delete=False) as stream:
        stream.write(text)
        temporary = Path(stream.name)
    try:
        return _parse_env(temporary)
    finally:
        temporary.unlink(missing_ok=True)


def _write_env(path: Path, values: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(values, key=lambda key: (0 if key in {"DISPLAY_NAME", "RUNTIME", "BASE_URL", "REQUEST_MODEL"} else 1, key))
    text = "".join(f"{key}={_env_quote(values[key])}\n" for key in ordered)
    fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        try:
            os.unlink(temp)
        except FileNotFoundError:
            pass


def _profile_file(directory: Path, name: str) -> Path:
    return directory / f"{name}.env"


def _profile_source_file(directory: Path, name: str) -> Path:
    """Return the on-disk profile source, preserving JSON adapter profiles."""
    env_path = _profile_file(directory, name)
    json_path = env_path.with_suffix(".json")
    if env_path.is_file() and json_path.is_file():
        raise UsageError("profile has both .env and .json files")
    if env_path.is_symlink() or json_path.is_symlink():
        raise UsageError("profile file must not be a symlink or junction")
    if env_path.is_file():
        return env_path
    if json_path.is_file():
        return json_path
    raise ProfileNotFoundError(name)


def _profile_values(service: AgentService, name: str) -> dict[str, str]:
    path = _profile_source_file(service.configuration.profiles_directory, name)
    if path.suffix.lower() == ".env":
        return _parse_env(path)
    return parse_json_profile(path)


def parse_json_profile_text(text: str, name: str) -> dict[str, str]:
    """Use the original parser's normalization rules for raw JSON documents."""
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=f"-{name}.json", delete=False) as stream:
        stream.write(text)
        temporary = Path(stream.name)
    try:
        return parse_json_profile(temporary)
    finally:
        temporary.unlink(missing_ok=True)


def _profile_running(service: AgentService, name: str) -> bool:
    try:
        rows = service.status_payload([name]).get("statuses", [])
        return bool(rows and (rows[0].get("running") or rows[0].get("ready")))
    except AgentError:
        return False


def _profile_path(service: AgentService, name: str, suffix: str) -> Path:
    _profile_name(name)
    directory = service.configuration.profiles_directory
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / f"{name}{suffix}"
    try:
        if candidate.exists() and candidate.is_symlink():
            raise UsageError("profile file must not be a symlink or junction")
        candidate.resolve().parent.relative_to(directory.resolve())
    except ValueError as error:
        raise UsageError("profile path escapes profiles directory") from error
    return candidate


def _check_profile_collision(service: AgentService, name: str, *, ignore: str | None = None) -> None:
    directory = service.configuration.profiles_directory
    folded = name.casefold()
    for path in directory.glob("*"):
        if path.suffix.lower() not in {".env", ".json"}:
            continue
        stem = path.stem
        if ignore is not None and stem.casefold() == ignore.casefold():
            continue
        if stem.casefold() == folded:
            raise UsageError("profile name collides with an existing profile")


def _profile_catalog(service: AgentService) -> list[dict[str, Any]]:
    loaded = service.profiles.load()
    rows = []
    for name, profile in sorted(loaded.items(), key=lambda item: item[0].casefold()):
        rows.append({
            "name": name,
            "profile": name,
            "display_name": getattr(profile, "display_name", name),
            "runtime": getattr(profile, "runtime", "unknown"),
            "values": dict(getattr(profile, "values", {})),
        })
    return rows


def _ollama_json(path: str, method: str = "GET", body: bytes | None = None) -> dict[str, Any] | None:
    try:
        request = urllib.request.Request(f"http://127.0.0.1:11434{path}", data=body, method=method,
                                         headers={"Accept": "application/json", "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=1.5) as response:
            parsed = json.loads(response.read(4 * 1024 * 1024))
        return parsed if isinstance(parsed, dict) else None
    except (OSError, ValueError, json.JSONDecodeError):
        return None


class SessionManager:
    def __init__(self, token: str | None) -> None:
        self.auth_token = token
        self._sessions: dict[str, float] = {}
        self._lock = threading.Lock()

    def issue(self) -> str:
        value = secrets.token_urlsafe(32)
        with self._lock:
            now = time.time()
            self._sessions = {key: stamp for key, stamp in self._sessions.items() if now - stamp < 86400}
            self._sessions[value] = now
        return value

    def valid(self, value: str | None) -> bool:
        if not value:
            return False
        with self._lock:
            stamp = self._sessions.get(value)
            if stamp is None or time.time() - stamp >= 86400:
                self._sessions.pop(value, None)
                return False
            self._sessions[value] = time.time()
            return True

    def revoke(self, value: str | None) -> None:
        if value:
            with self._lock:
                self._sessions.pop(value, None)

    def token_valid(self, value: Any) -> bool:
        return self.auth_token is None or (isinstance(value, str) and hmac.compare_digest(value, self.auth_token))


class ProfileModule:
    """Dashboard profile/catalog API.  Every route returns (status, body)."""
    def __init__(self, service: "LocalService") -> None:
        self.service = service

    def route(self, method: str, path: str, query: dict[str, str], body: dict[str, Any]) -> tuple[int, dict[str, Any]] | None:
        if path == "/api/profiles" and method == "GET":
            return 200, {"profiles": _profile_catalog(self.service), "profiles_dir": str(self.service.configuration.profiles_directory)}
        if path == "/api/profiles/runtimes" and method == "GET":
            runtimes = [
                {"id": runtime, "label": label, "tags": tags, "launch_mode": launch_mode}
                for runtime, (label, tags, launch_mode) in sorted(RUNTIME_SPECS.items())
            ]
            templates = [
                {"id": "external", "label": "OpenAI-compatible endpoint",
                 "values": {"RUNTIME": "external", "LAUNCH_MODE": "external", "HOST": "127.0.0.1", "PORT": "8080", "REQUEST_MODEL": "model"}},
                {"id": "llama.cpp", "label": "llama.cpp",
                 "values": {"RUNTIME": "llama.cpp", "LAUNCH_MODE": "adapter", "HOST": "127.0.0.1", "PORT": "8080", "REQUEST_MODEL": "model", "MODEL_FILE": ""}},
                {"id": "ollama", "label": "Ollama",
                 "values": {"RUNTIME": "ollama", "LAUNCH_MODE": "external", "BASE_URL": "http://127.0.0.1:11434/v1", "REQUEST_MODEL": "llama3.2"}},
            ]
            return 200, {"runtimes": runtimes, "templates": templates}
        if path == "/api/profiles/document" and method == "GET":
            name = _profile_name(query.get("profile", ""))
            file = _profile_source_file(self.service.configuration.profiles_directory, name)
            return 200, {"profile": name, "file": str(file), "format": file.suffix.lower().removeprefix("."),
                         "values": _profile_values(self.service, name),
                         "text": file.read_text(encoding="utf-8-sig"), "running": _profile_running(self.service, name)}
        if path == "/api/profiles/save" and method == "POST":
            name = _profile_name(body.get("profile", body.get("name", "")))
            original = body.get("original_profile") or body.get("original_name")
            if original is not None:
                original = _profile_name(original)
            creating = bool(body.get("create", False)) or original is None and not any(
                p.casefold() == name.casefold() for p in {x["name"] for x in _profile_catalog(self.service)}
            )
            source = None if creating else _profile_source_file(self.service.configuration.profiles_directory, original or name)
            if original and original.casefold() != name.casefold():
                if _profile_running(self.service, original):
                    raise UsageError("cannot rename a running profile")
                _check_profile_collision(self.service, name, ignore=original)
            elif creating:
                _check_profile_collision(self.service, name)
            elif _profile_running(self.service, original or name):
                raise UsageError("cannot edit a running profile")
            raw_text = body.get("text")
            format_name = source.suffix.lower() if source else ".env"
            if isinstance(raw_text, str):
                normalized = raw_text.replace("\r\n", "\n").replace("\r", "\n")
                if format_name == ".json" or normalized.lstrip().startswith("{"):
                    try:
                        parsed = json.loads(normalized)
                    except json.JSONDecodeError as error:
                        raise UsageError(f"profile JSON is invalid: {error}") from error
                    if not isinstance(parsed, dict): raise UsageError("profile JSON must be an object")
                    values = parse_json_profile_text(normalized, name)
                    format_name = ".json"
                else:
                    values = _parse_env_text(normalized)
            else:
                values = _string_map(body.get("values", body.get("config", {})))
            values.pop("PROFILE_NAME", None); values.setdefault("DISPLAY_NAME", name)
            self.service.save_profile_document(name, values, raw_text if format_name == ".json" and isinstance(raw_text, str) else None,
                                               source=source, create_only=creating, format_name=format_name)
            if original and original.casefold() != name.casefold():
                old = _profile_source_file(self.service.configuration.profiles_directory, original)
                old.unlink(missing_ok=True)
            return 200, {"profile": name, "name": name, "values": values, "saved": True}
        if path == "/api/profiles/create" and method == "POST":
            name = _profile_name(body.get("profile", body.get("name", "")))
            values = _string_map(body.get("values", {}))
            values.setdefault("DISPLAY_NAME", name)
            self.service.save_profile_document(name, values, None, source=None, create_only=True, format_name=".env")
            return 200, {"profile": name, "name": name, "values": values, "created": True}
        if path == "/api/profiles/duplicate" and method == "POST":
            source = _profile_name(body.get("profile", ""))
            target = _profile_name(body.get("new_profile", body.get("newName", "")))
            values = _profile_values(self.service, source)
            _check_profile_collision(self.service, target)
            if _profile_running(self.service, source): raise UsageError("cannot duplicate a running profile")
            values["DISPLAY_NAME"] = f"{values.get('DISPLAY_NAME', source)} copy"
            source_file = _profile_source_file(self.service.configuration.profiles_directory, source)
            self.service.save_profile_document(target, values, None, source=source_file, create_only=True, format_name=source_file.suffix.lower())
            return 200, {"profile": target, "name": target, "values": values, "duplicated_from": source}
        if path == "/api/profiles/delete" and method == "POST":
            name = _profile_name(body.get("profile", ""))
            if _profile_running(self.service, name): raise UsageError("cannot delete a running profile")
            self.service.delete_profile(name)
            return 200, {"profile": name, "deleted": True}
        if path == "/api/profiles/capture" and method == "POST":
            name = _profile_name(body.get("profile", ""))
            enabled = bool(body.get("enabled", False))
            return 200, self.service.capture(name, enabled)
        return None


class UsageModule:
    """Dashboard usage adapter backed by the original UsageStore SQLite connection."""
    def __init__(self, service: "LocalService") -> None: self.service = service
    def _relay(self) -> UsageRelay:
        relay = getattr(self.service, "usage", None)
        if relay is None: raise UsageError("usage tracking is not running on this host")
        return relay
    def _rows(self, range_key="24h", tier=None, host="all", search=None, limit=100000, offset=0):
        store = self._relay().store; seconds = {"1h":3600,"24h":86400,"7d":604800,"30d":2592000,"all":None}.get(range_key,86400)
        clauses=[]; params=[]
        if seconds is not None: clauses.append("r.started_at >= ?"); params.append(time.time()-seconds)
        if tier in ("local","cloud"): clauses.append("r.tier = ?"); params.append(tier)
        if host and host != "all": clauses.append("r.host = ?"); params.append(host)
        if search:
            clauses.append("(r.model LIKE ? OR r.model_requested LIKE ? OR r.client_label LIKE ? OR n.name LIKE ? OR r.profile LIKE ?)")
            params.extend([f"%{search}%"]*5)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        sql = ("SELECT r.*, n.name AS custom_name FROM requests r LEFT JOIN client_names n "
               "ON n.client_id=r.client_id" + where + " ORDER BY r.id DESC LIMIT ? OFFSET ?")
        params.extend([max(1,min(100000,int(limit))), max(0,int(offset))])
        with store._lock:
            return [dict(row) for row in store._db.execute(sql, params).fetchall()]
    @staticmethod
    def _norm(row):
        row = dict(row); row["client_name"] = row.get("custom_name") or row.get("client_label") or row.get("client_id") or "unknown"
        row["model"] = row.get("model") or row.get("model_served") or row.get("model_requested")
        row["stream"] = bool(row.get("stream")); row["tokens_estimated"] = bool(row.get("tokens_estimated")); return row
    def _summary(self, rows, range_key):
        prompt=sum(int(r.get("prompt_tokens") or 0) for r in rows); completion=sum(int(r.get("completion_tokens") or 0) for r in rows)
        speeds=[float(r["tokens_per_second"]) for r in rows if r.get("tokens_per_second") is not None]
        ttft=[float(r["ttft_ms"]) for r in rows if r.get("ttft_ms") is not None]; durations=[float(r["duration_ms"]) for r in rows if r.get("duration_ms") is not None]
        pct=lambda vals,f: sorted(vals)[min(len(vals)-1,max(0,round(f*(len(vals)-1))))] if vals else None
        local=[r for r in rows if r.get("tier") != "cloud"]; cloud=[r for r in rows if r.get("tier") == "cloud"]
        return {"range":range_key,"requests":len(rows),"prompt_tokens":prompt,"completion_tokens":completion,"total_tokens":prompt+completion,
          "errors":sum(1 for r in rows if int(r.get("status") or 0)>=400 or r.get("error")),"local_requests":len(local),"cloud_requests":len(cloud),
          "local_completion_tokens":sum(int(r.get("completion_tokens") or 0) for r in local),"cloud_completion_tokens":sum(int(r.get("completion_tokens") or 0) for r in cloud),
          "inference_ms":sum(durations),"estimated_rows":sum(1 for r in rows if r.get("tokens_estimated")),"clients":len({r.get("client_id") for r in rows}),"models":len({r.get("model") or r.get("model_requested") for r in rows}),
          "avg_tps":sum(speeds)/len(speeds) if speeds else None,"avg_ttft_ms":sum(ttft)/len(ttft) if ttft else None,"avg_duration_ms":sum(durations)/len(durations) if durations else None,
          "avg_prompt_tokens":prompt/len(rows) if rows else 0,"p50_ttft_ms":pct(ttft,.5),"p95_ttft_ms":pct(ttft,.95),"p50_tps":pct(speeds,.5),"peak_tps":max(speeds) if speeds else None,
          "previous":{"requests":0,"tokens":0,"avg_tps":None,"avg_ttft_ms":None}}
    def _models(self, rows):
        groups={}
        for r in rows: groups.setdefault(r.get("model") or r.get("model_requested") or "unknown",[]).append(r)
        total=max(1,len(rows)); out=[]
        for model,items in groups.items():
            speeds=[r["tokens_per_second"] for r in items if r.get("tokens_per_second") is not None]; tt=[r["ttft_ms"] for r in items if r.get("ttft_ms") is not None]
            out.append({"model":model,"alias":items[0].get("model_requested"),"profile":items[0].get("profile"),"runtime":items[0].get("runtime"),"tier":items[0].get("tier"),"host":items[0].get("host"),"requests":len(items),"prompt_tokens":sum(int(r.get("prompt_tokens") or 0) for r in items),"completion_tokens":sum(int(r.get("completion_tokens") or 0) for r in items),"avg_tps":sum(speeds)/len(speeds) if speeds else None,"avg_ttft_ms":sum(tt)/len(tt) if tt else None,"last_used":max(r.get("started_at") or 0 for r in items),"share":len(items)/total})
        return sorted(out,key=lambda x:x["requests"],reverse=True)[:50]
    def _clients(self, rows):
        groups={}
        for r in rows: groups.setdefault(r.get("client_id") or "unknown",[]).append(r)
        out=[]
        for cid,items in groups.items():
            counts={}; [counts.__setitem__(r.get("model") or "unknown",counts.get(r.get("model") or "unknown",0)+1) for r in items]
            speeds=[r["tokens_per_second"] for r in items if r.get("tokens_per_second") is not None]
            out.append({"client_id":cid,"name":items[0].get("custom_name") or items[0].get("client_label") or cid,"detected":items[0].get("client_label"),"process":items[0].get("client_process"),"custom_name":items[0].get("custom_name"),"requests":len(items),"tokens":sum(int(r.get("prompt_tokens") or 0)+int(r.get("completion_tokens") or 0) for r in items),"avg_tps":sum(speeds)/len(speeds) if speeds else None,"last_seen":max(r.get("started_at") or 0 for r in items),"first_seen":min(r.get("started_at") or 0 for r in items),"top_model":max(counts,key=counts.get)})
        return sorted(out,key=lambda x:x["requests"],reverse=True)[:100]
    def _timeseries(self, rows, range_key):
        width={"1h":300,"24h":3600,"7d":86400,"30d":86400}.get(range_key,86400); buckets={}
        for r in rows:
            t=int(float(r.get("started_at") or 0)//width*width); b=buckets.setdefault(t,{"t":t,"requests":0,"local":0,"cloud":0,"prompt_tokens":0,"completion_tokens":0,"errors":0,"_speeds":[],"_ttft":[]}); b["requests"]+=1; b[r.get("tier") if r.get("tier") in ("local","cloud") else "local"]+=1; b["prompt_tokens"]+=int(r.get("prompt_tokens") or 0); b["completion_tokens"]+=int(r.get("completion_tokens") or 0); b["errors"]+=int(int(r.get("status") or 0)>=400); b["_speeds"] += [r["tokens_per_second"]] if r.get("tokens_per_second") is not None else []; b["_ttft"] += [r["ttft_ms"]] if r.get("ttft_ms") is not None else []
        out=[]
        for b in buckets.values():
            speeds, ttft = b.pop("_speeds"), b.pop("_ttft")
            b["avg_tps"] = sum(speeds) / len(speeds) if speeds else None
            b["avg_ttft_ms"] = sum(ttft) / len(ttft) if ttft else None
            out.append(b)
        return sorted(out,key=lambda x:x["t"])
    def route(self, method, path, query, body):
        relay=self._relay(); range_key=query.get("range","24h"); tier=query.get("tier"); host=query.get("host","all"); rows=[self._norm(r) for r in self._rows(range_key,tier,host)]
        if path=="/api/usage/overview" and method=="GET":
            allrows=[self._norm(r) for r in self._rows("all",tier,host)]
            hs={}
            for r in rows: hs.setdefault(r.get("host") or "local",[]).append(r)
            return 200,{"summary":self._summary(rows,range_key),"totals":self._summary(allrows,"all")|{"requests_24h":self._summary([self._norm(x) for x in self._rows("24h",tier,host)],"24h")["requests"],"tokens_24h":self._summary([self._norm(x) for x in self._rows("24h",tier,host)],"24h")["total_tokens"],"active_clients":len({x.get("client_id") for x in allrows if (x.get("started_at") or 0)>time.time()-300}),"requests_per_minute":self._summary([self._norm(x) for x in self._rows("1h",tier,host)],"1h")["requests"]/60},"host_stats":[{"host":h,"requests":len(v),"last_request":max(x.get("started_at") or 0 for x in v)} for h,v in hs.items()],"timeseries":self._timeseries(rows,range_key),"models":self._models(rows),"clients":self._clients(rows)[:12],"recent":[self._norm(x) for x in self._rows(range_key,tier,host,limit=30)],"in_flight":relay.inflight_rows(),"last_id":relay.store.count(),"hosts":sorted(hs),"relay":relay.status(),"generated_at":time.time()}
        if path=="/api/usage/summary" and method=="GET": return 200,self._summary(rows,range_key)
        if path=="/api/usage/timeseries" and method=="GET": return 200,{"buckets":self._timeseries(rows,range_key)}
        if path=="/api/usage/models" and method=="GET": return 200,{"models":self._models(rows)}
        if path=="/api/usage/clients" and method=="GET": return 200,{"clients":self._clients(rows)}
        if path=="/api/usage/requests" and method=="GET":
            limit=max(1,min(500,int(query.get("limit","100")))); offset=max(0,min(100000,int(query.get("offset","0")))); search=query.get("q",query.get("search","")); selected=[self._norm(r) for r in self._rows(range_key,tier,host,search,limit,offset)]
            total=len(self._rows(range_key,tier,host,search,100000,0)); return 200,{"requests":selected,"limit":limit,"offset":offset,"total":total,"search":search}
        if path=="/api/usage/live" and method=="GET": return 200,{"in_flight":relay.inflight_rows(),"recent":[self._norm(x) for x in self._rows("all",tier,host,limit=30)],"last_id":relay.store.count()}
        if path in {"/api/usage/settings","/api/usage/settings/"} and method=="GET":
            value=relay.status(); value["capture"]=self.service.capture_state(); return 200,value
        if path=="/api/usage/settings" and method=="POST":
            if "retention_days" in body:
                days=body["retention_days"]
                if not isinstance(days,int) or not 1<=days<=3650: raise UsageError("retention_days must be 1-3650")
                relay.store.set_setting("retention_days",str(days)); relay.store.prune()
            value=relay.status(); value["capture"]=self.service.capture_state(); return 200,value
        if path=="/api/usage/clients/rename" and method=="POST": relay.store.rename_client(str(body.get("client_id") or ""),str(body.get("name") or "")); return 200,{"renamed":True}
        if path=="/api/usage/reset" and method=="POST":
            if body.get("confirm") is not True: raise UsageError("confirm must be true")
            relay.store.reset(); return 200,{"reset":True}
        return None
    def csv(self,query):
        rows=[self._norm(x) for x in self._rows(query.get("range","all"),query.get("tier"),query.get("host","all"),query.get("q",query.get("search","")),100000,0)]
        fields=["id","started_at","host","profile","runtime","endpoint","model_requested","model","client_name","client_process","status","stream","prompt_tokens","completion_tokens","tokens_estimated","tokens_per_second","ttft_ms","duration_ms","tier","error"]; out=__import__("io").StringIO(newline=""); w=csv.DictWriter(out,fieldnames=fields,extrasaction="ignore"); w.writeheader(); w.writerows(rows); return 200,out.getvalue().encode(),{"Content-Type":"text/csv; charset=utf-8","Content-Disposition":'attachment; filename="modelmatrix-usage.csv"'}


class OptimizeModule:
    def __init__(self, service: "LocalService") -> None: self.service = service
    def _memory(self) -> dict[str, Any]:
        try: raw=host_metrics_payload().get("memory") or {}
        except Exception: raw={}
        mb=lambda key: int(float(raw.get(key) or 0)*1024*1024)
        total,used,free=mb("total_mb"),mb("used_mb"),mb("available_mb")
        return {"total_bytes":total,"used_bytes":used,"app_bytes":used,"wired_bytes":0,"compressed_bytes":0,"cached_bytes":0,"free_bytes":free,"swap_used_bytes":0,"swap_total_bytes":0,"pressure":"critical" if raw.get("percent",0)>=90 else "warning" if raw.get("percent",0)>=75 else "normal"}
    def _busy(self):
        usage=self.service.usage
        active={r.get("profile") for r in usage.inflight_rows()} if usage else set()
        recent=[]
        if usage:
            recent=usage.store.recent(100,"1h")
        last={r.get("profile"):float(r.get("started_at") or 0) for r in recent}
        return active,last
    def _plan(self):
        items=[]; ollama=self.service.ollama_status(); active,last=self._busy(); now=time.time(); idle=900; inuse=30
        for model in ollama.get("loaded",[]):
            name=model.get("name") or model.get("model")
            if name:
                busy=name in active or now-float(last.get(name,0))<inuse
                size=int(model.get("size") or 0)
                items.append({"id":f"ollama:{name}","kind":"ollama_model","title":f"Unload {name}","detail":"Ollama model in memory" if not busy else "Answering a request recently","bytes":size,"in_use":busy,"needs_password":False,"presets":[] if busy else ["recommended","maximum"],"last_used_at":last.get(name)})
        for row in self.service.status_payload().get("statuses",[]):
            if row.get("running"):
                name=row.get("profile") or row.get("name"); busy=name in active or now-float(last.get(name,0))<inuse
                size=int(float(row.get("rss_mb") or 0)*1024*1024)
                items.append({"id":f"profile:{name}","kind":"model_server","title":f"Stop {name}","detail":"Model server" if not busy else "In-flight or recently used","bytes":size,"in_use":busy,"needs_password":False,"presets":[] if busy else ["recommended","maximum"],"last_used_at":last.get(name)})
        return {"memory":self._memory(),"items":items,"apps":[],"presets":{"recommended":[x["id"] for x in items if "recommended" in x["presets"]],"maximum":[x["id"] for x in items if "maximum" in x["presets"]]},"estimates":{"recommended":sum(x["bytes"] for x in items if "recommended" in x["presets"]),"maximum":sum(x["bytes"] for x in items if "maximum" in x["presets"])},"thresholds":{"in_use_seconds":inuse,"idle_seconds":idle},"generated_at":now}
    def route(self,method,path,query,body):
        if path=="/api/optimize/plan" and method=="GET": return 200,self._plan()
        if path=="/api/optimize/run" and method=="POST":
            ids=body.get("items",[])
            if not isinstance(ids,list): raise UsageError("items must be an array")
            plan={x["id"]:x for x in self._plan()["items"]}; results=[]
            for ident in ids:
                if not isinstance(ident,str) or ident not in plan: results.append({"id":ident,"ok":False,"message":"unknown optimization item"}); continue
                item=plan[ident]
                if item["in_use"]: results.append({"id":ident,"ok":True,"skipped":True,"message":"request in flight or recently used"}); continue
                try:
                    if ident.startswith("ollama:"):
                        if not self.service.ollama_status().get("reachable"): raise UnsupportedError("Ollama unavailable")
                        ok=_ollama_json("/api/generate","POST",json.dumps({"name":ident[7:],"keep_alive":0}).encode()) is not None
                        results.append({"id":ident,"ok":ok,"message":"unloaded" if ok else "Ollama unload failed"})
                    else:
                        self.service.stop(ident[8:]); results.append({"id":ident,"ok":True,"message":"server stopped"})
                except AgentError as e: results.append({"id":ident,"ok":False,"message":str(e)})
            after=self._memory(); return 200,{"results":results,"before":self._memory(),"after":after,"freed_bytes":0}
        if path=="/api/optimize/purge" and method=="POST": return 409,{"error":"unsupported_capability","message":"Windows file-cache purge is unavailable"}
        return None


class LocalHFModule:
    """Adapter for the companion LocalHF route module's required contract."""
    def __init__(self, service: "LocalService") -> None:
        self.service = service
        self.error: str | None = None
        if LocalHF is None:
            self.impl = None
            self.error = "LocalHF adapter is not installed"
        else:
            try:
                self.impl = LocalHF(service)
            except Exception as error:
                self.impl = None
                self.error = str(error)
    def route(self, method: str, path: str, query: dict[str, str], body: dict[str, Any]) -> tuple[int, dict[str, Any]] | None:
        if self.impl is None and path.startswith("/api/hf/"):
            return 501, {"error": "unsupported_capability", "message": self.error or "HF adapter unavailable"}
        if self.impl is None: return None
        result = self.impl.route(method, path, query, body)
        return result


class LocalBridgeModule:
    def __init__(self, service: "LocalService") -> None:
        self.service = service
        self.error: str | None = None
        if LocalBridge is None:
            self.impl = None
            self.error = "LocalBridge adapter is not installed"
        else:
            try:
                self.impl = LocalBridge(service)
            except Exception as error:
                self.impl = None
                self.error = str(error)
    def route(self, method: str, path: str, query: dict[str, str], body: dict[str, Any]) -> tuple[int, dict[str, Any]] | None:
        if self.impl is None and path.startswith("/api/bridge/"):
            return 501, {"error": "unsupported_capability", "message": self.error or "bridge adapter unavailable"}
        if self.impl is None: return None
        return self.impl.route(method, path, query, body)


class LocalService(AgentService):
    def __init__(self, configuration: AgentConfiguration) -> None:
        super().__init__(configuration)
        self.usage: UsageRelay | None = None
        self.hf_local = LocalHFModule(self)
        self.bridge_local = LocalBridgeModule(self)
        self.profiles_local = ProfileModule(self)
        self.usage_local = UsageModule(self)
        self.optimize_local = OptimizeModule(self)
        self.capture_ports: dict[str, int] = {}
        self.capture_relays: dict[str, Any] = {}
        self._shutdown_requested = threading.Event()

    def bootstrap(self) -> None:
        self.configuration.profiles_directory.mkdir(parents=True, exist_ok=True)
        self.configuration.run_directory.mkdir(parents=True, exist_ok=True)
        requested_relay_port = getattr(self.configuration, "relay_port", None)
        if requested_relay_port is None:
            self.usage = start_usage_relay(self)
        else:
            self.usage = UsageRelay(self, self.configuration.host, requested_relay_port)
            self.usage.start()
        self.hf = getattr(self, "hf", None)
        self.start_watchdog()

    def stop_all_managed(self) -> None:
        # Close public listeners first: AgentService must see the original
        # profile port as free while it verifies and terminates the owned PID.
        for name in list(self.capture_relays):
            self._stop_capture(name)
        try:
            self.stop_all(force=False)
        except AgentError:
            try:
                self.stop_all(force=True)
            except AgentError:
                pass
        hf_local = getattr(self.hf_local, "impl", None)
        if hf_local is not None:
            hf_local.close()
        bridge = getattr(self.bridge_local, "impl", None)
        if bridge is not None:
            for gateway_id in list(bridge._tunnels):
                bridge._stop_tunnel(gateway_id)
        usage = self.usage
        if usage is not None:
            try: usage.stop()
            except Exception: pass
        try: self.stop_watchdog()
        except Exception: pass

    @staticmethod
    def _capture_flag(values: dict[str, str]) -> bool:
        return str(values.get("CAPTURE_MODE", values.get("CAPTURE", ""))).strip().lower() in {"1", "true", "yes"}

    def _capture_supported(self, values: dict[str, str]) -> bool:
        try: int(values.get("PORT", ""))
        except (TypeError, ValueError): return False
        runtime = str(values.get("RUNTIME", "unknown")).lower()
        spec = RUNTIME_SPECS.get(runtime)
        return bool(spec and spec[2] != "external" and runtime != "ollama")

    def _validate_capture_command(self, values: dict[str, str]) -> None:
        # Structured argv is deterministic: PORT is supplied through the
        # environment and known placeholders can be rewritten. A shell string
        # has no safe cross-platform port transformation, so reject it rather
        # than claiming capture succeeded while the child binds the public port.
        if values.get("START_COMMAND") and not values.get("START_COMMAND_JSON") and not values.get("SERVER_ARGS_JSON"):
            raise InvalidProfileError("capture requires START_COMMAND_JSON/SERVER_ARGS_JSON or an adapter launch; custom shell commands cannot be safely rewritten")

    def _free_port(self) -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((self.configuration.host.strip("[]"), 0))
            return int(sock.getsockname()[1])

    def _captured_profile(self, profile: Profile) -> Profile:
        internal = self.capture_ports.get(profile.name)
        if not internal: return profile
        values = dict(profile.values); values["PORT"] = str(internal); values["HOST"] = "127.0.0.1"
        for key in ("START_COMMAND_JSON", "SERVER_ARGS_JSON"):
            raw = values.get(key)
            if raw:
                try:
                    argv = json.loads(raw)
                    if isinstance(argv, list) and all(isinstance(x, str) for x in argv):
                        values[key] = json.dumps([x.replace("{port}", str(internal)).replace("{PORT}", str(internal)).replace("${PORT}", str(internal)) for x in argv])
                except json.JSONDecodeError as error:
                    raise InvalidProfileError(f"{profile.name}: {key} must be valid JSON") from error
        return Profile(profile.name, values)

    def _spawn_profile(self, profile: Profile, canonical: str, command: str) -> None:
        actual = self._captured_profile(profile)
        if actual is not profile:
            # Rebuild from structured argv/profile fields after PORT replacement.
            command = build_start_command(actual)
        super()._spawn_profile(actual, canonical, command)

    def start(self, name: str) -> None:
        profile = self.resolve_profile(name)
        enabled = self._capture_flag(profile.values) and self._capture_supported(profile.values)
        if enabled:
            self._validate_capture_command(profile.values)
            # A stale listener would make AgentService classify its own public
            # port as foreign. It is safe to stop because it is controller-owned.
            self._stop_capture(profile.name)
            self.capture_ports.setdefault(profile.name, self._free_port())
        super().start(name)
        if enabled and self.usage is not None and profile.name not in self.capture_relays:
            target = self._captured_profile(profile)
            models = [str(x) for x in (target.request_model, target.server_model_id, target.name) if x]
            relay = start_capture(self.configuration.host, int(profile.endpoint_port), self.capture_ports[profile.name], profile.name, target.runtime, models, self.usage)
            if relay is None:
                # Never leave a successful-looking capture setting with no listener.
                super().stop(name, force=True)
                self.capture_ports.pop(profile.name, None)
                raise InvalidConfigurationError("capture public port is unavailable")
            self.capture_relays[profile.name] = relay

    def _stop_capture(self, name: str) -> None:
        relay = self.capture_relays.pop(name, None)
        if relay is not None:
            relay.stop()

    def stop(self, name: str, force: bool = False) -> None:
        self._stop_capture(name)
        pid = self._read_pid(name)
        try:
            super().stop(name, force=force)
        finally:
            self.capture_ports.pop(name, None)
            if pid and hasattr(os, "WNOHANG") and hasattr(os, "waitpid"):
                try: os.waitpid(pid, os.WNOHANG)
                except (ChildProcessError, OSError): pass

    def status(self, profile: Profile, allow_port_fallback: bool = True, *, listeners=None):
        row = super().status(self._captured_profile(profile), allow_port_fallback, listeners=listeners)
        if profile.name in self.capture_ports:
            public = str(profile.endpoint_port)
            internal = str(self.capture_ports[profile.name])
            row["port"] = public
            if isinstance(row.get("base_url"), str):
                row["base_url"] = row["base_url"].replace(f":{internal}", f":{public}")
        return row

    def capture(self, name: str, enabled: bool) -> dict[str, Any]:
        profile = self.resolve_profile(name)
        if enabled and not self._capture_supported(profile.values):
            raise UnsupportedError(f"{name}: capture requires a launched non-external profile with PORT")
        if enabled: self._validate_capture_command(profile.values)
        values = dict(_profile_values(self, name))
        if enabled: values["CAPTURE_MODE"] = "1"
        else: values.pop("CAPTURE_MODE", None)
        source = _profile_source_file(self.configuration.profiles_directory, name)
        self.save_profile_document(name, values, None, source=source, create_only=False, format_name=source.suffix.lower())
        if not enabled:
            self._stop_capture(name)
            self.capture_ports.pop(name, None)
        elif _profile_running(self, name):
            # Apply immediately while preserving the AgentService supervision path.
            self.restart(name)
        return {"profile": name, "capture": enabled, "capture_state": self.capture_state()}

    def capture_state(self) -> list[dict[str, Any]]:
        rows = []
        for item in _profile_catalog(self):
            values = item["values"]
            enabled = self._capture_flag(values) and self._capture_supported(values)
            row = {"profile": item["name"], "display_name": item["display_name"], "supported": self._capture_supported(values), "enabled": enabled}
            try: row["port"] = int(values["PORT"])
            except (KeyError, ValueError): pass
            if enabled:
                row["internal_port"] = self.capture_ports.get(item["name"])
                relay = self.capture_relays.get(item["name"])
                row["listening"] = bool(relay and relay.listening)
                if relay and relay.error: row["error"] = relay.error
            rows.append(row)
        return rows

    def save_profile(self, name: str, values: dict[str, str], create_only: bool) -> None:
        self.save_profile_document(name, values, None, source=None, create_only=create_only, format_name=".env")

    def save_profile_document(self, name: str, values: dict[str, str], raw_json: str | None,
                              *, source: Path | None, create_only: bool, format_name: str) -> None:
        _profile_name(name)
        if create_only: _check_profile_collision(self, name)
        suffix = ".json" if format_name.lower() in {".json", "json"} else ".env"
        path = _profile_path(self, name, suffix)
        other = path.with_suffix(".json" if suffix == ".env" else ".env")
        if create_only and (path.exists() or other.exists()): raise UsageError("profile already exists")
        if suffix == ".json":
            payload = raw_json if raw_json is not None else json.dumps(values, ensure_ascii=False, indent=2) + "\n"
            path.write_text(payload.replace("\r\n", "\n").replace("\r", "\n"), encoding="utf-8", newline="\n")
        else: _write_env(path, values)
        self.profiles = ProfileRepository(self.configuration.profiles_directory)

    def delete_profile(self, name: str) -> None:
        path = _profile_source_file(self.configuration.profiles_directory, _profile_name(name))
        path.unlink()
        self.profiles = ProfileRepository(self.configuration.profiles_directory)

    def ollama_status(self) -> dict[str, Any]:
        tags = _ollama_json("/api/tags")
        ps = _ollama_json("/api/ps")
        version = _ollama_json("/api/version")
        installed = (tags or {}).get("models", [])
        result: dict[str, Any] = {"reachable": tags is not None or ps is not None, "base_url": "http://127.0.0.1:11434", "installed": installed, "loaded": (ps or {}).get("models", [])}
        if version and version.get("version"): result["version"] = version["version"]
        return result

    def module_route(self, method: str, path: str, query: dict[str, str], body: dict[str, Any]) -> tuple[int, dict[str, Any]] | None:
        for module in (self.profiles_local, self.usage_local, self.optimize_local, self.hf_local, self.bridge_local):
            result = module.route(method, path, query, body)
            if result is not None: return result
        if method == "GET" and path == "/api/ollama/status": return 200, self.ollama_status()
        if method == "GET" and path == "/api/dashboard/session/info":
            return 200, {"platform": "windows", "platform_name": platform.platform(), "capabilities": {"native_memory_metrics": True, "gpu_metrics": bool(shutil.which("nvidia-smi")), "mlx": False, "metal": False, "ollama": True, "capture": any(x.get("supported") for x in self.capture_state())}}
        if method == "GET" and path == "/api/dashboard/info":
            host = self.configuration.host.strip("[]")
            url_host = f"[{host}]" if ":" in host else host
            relay = self.usage.status() if self.usage is not None else {"enabled": False, "listening": False, "port": None, "url": None, "error": "usage relay unavailable"}
            return 200, {
                "name": "ModelMatrix",
                "version": AGENT_VERSION,
                "controller_url": f"http://{url_host}:{self.configuration.port}",
                "token_required": bool(self.configuration.auth_token),
                "profiles_dir": str(self.configuration.profiles_directory),
                "controller_root": str(self.configuration.root),
                "relay": relay,
                "platform": {"os": "windows", "display_name": "Windows", "local_host_label": socket.gethostname(), "credential_store": "Windows Credential Manager"},
                "capabilities": {
                    "open_folders": False, "quit_apps": False, "purge_file_cache": False,
                    "keep_awake": False, "remote_gateways": self.bridge_local.impl is not None,
                    "capture": any(x.get("supported") for x in self.capture_state()), "connect_apps": self.bridge_local.impl is not None,
                    "huggingface": self.hf_local.impl is not None,
                },
            }
        return None


class DashboardHandler(AgentRequestHandler):
    server: "LocalHTTPServer"

    def setup(self) -> None:
        super().setup()
        # AgentRequestHandler predates the Windows wrapper and reads an
        # instance ``service`` attribute. Populate it from this server only;
        # never mutate DashboardHandler class state (multiple controllers may
        # be alive in one process during tests and embedded use).
        self.service = self.server.service

    def _session_cookie(self) -> str | None:
        raw = self.headers.get("Cookie", "")
        for item in raw.split(";"):
            key, _, value = item.strip().partition("=")
            if key == SESSION_COOKIE: return value
        return None

    def _host_allowed(self) -> bool:
        raw = self.headers.get("Host", "")
        if not raw or any(char in raw for char in "\r\n"):
            return False
        host = raw.strip().lower()
        configured = self.server.server_address[0].strip("[]").lower()
        if ":" in host and host.startswith("["):
            hostname = host[1:host.find("]")]
        elif host.count(":") == 1:
            hostname = host.rsplit(":", 1)[0]
        else:
            hostname = host
        allowed = {configured, "localhost", "127.0.0.1", "::1"}
        if configured in {"0.0.0.0", "::"}:
            allowed |= {"localhost", "127.0.0.1", "::1"}
        return hostname in allowed

    def _authorize(self) -> None:  # type: ignore[override]
        token = self.server.auth_token
        if token is None: return
        authorization = self.headers.get("Authorization", "")
        bearer = authorization[7:].strip() if authorization.lower().startswith("bearer ") else None
        if bearer and hmac.compare_digest(bearer, token): return
        if self.server.session_manager.valid(self._session_cookie()): return
        raise UnauthorizedError("unauthorized")

    def _csrf_ok(self, method: str, path: str) -> bool:
        if method in {"GET", "HEAD", "OPTIONS"}: return True
        bearer = self.headers.get("Authorization", "")
        is_bearer = bool(self.server.auth_token and bearer.lower().startswith("bearer ") and hmac.compare_digest(bearer[7:].strip(), self.server.auth_token))
        if path == "/api/dashboard/session" and method == "POST":
            # Login is the one mutation without a session, but it is still
            # origin-bound to prevent a cross-site token submission.
            is_bearer = True
        if self.server.auth_token is not None and not is_bearer and not self.server.session_manager.valid(self._session_cookie()): return False
        fetch = (self.headers.get("Sec-Fetch-Site") or "").lower()
        if fetch in {"cross-site", "cross-origin"}: return False
        origin = self.headers.get("Origin")
        if origin:
            host = self.headers.get("Host", "")
            try:
                parsed = urllib.parse.urlparse(origin)
                if parsed.netloc and parsed.netloc.lower() != host.lower(): return False
            except ValueError: return False
        return True

    def _set_session_cookie(self, value: str) -> None:
        self._new_cookie = f"{SESSION_COOKIE}={value}; Path=/; HttpOnly; SameSite=Strict"

    def _clear_session_cookie(self) -> None:
        self.server.session_manager.revoke(self._session_cookie())
        self._new_cookie = f"{SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"

    def _send_json_local(self, status: int, payload: dict[str, Any], headers: dict[str, str] | None = None) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Security-Policy", "default-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'")
        if getattr(self, "_new_cookie", None): self.send_header("Set-Cookie", self._new_cookie)
        for key, value in (headers or {}).items(): self.send_header(key, value)
        self.end_headers(); self.wfile.write(data)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length < 0 or length > MAX_BODY: raise UsageError("request body too large")
        if not length: return {}
        try: value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError): raise UsageError("request body must be JSON")
        if not isinstance(value, dict): raise UsageError("request body must be an object")
        return value

    def _static(self, path: str) -> bool:
        relative = urllib.parse.unquote(path.removeprefix("/"))
        if not relative: relative = "index.html"
        dashboard_dir = self.server.dashboard_dir
        candidate = (dashboard_dir / relative).resolve()
        try: candidate.relative_to(dashboard_dir.resolve())
        except ValueError: self._send_json_local(404, {"error": "not_found", "message": "not found"}); return True
        if not candidate.is_file():
            candidate = dashboard_dir / "index.html"
        if not candidate.is_file(): return False
        content = candidate.read_bytes()
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self.send_response(200); self.send_header("Content-Type", content_type); self.send_header("Content-Length", str(len(content))); self.send_header("Cache-Control", "no-cache" if candidate.name == "index.html" else "public, max-age=3600"); self.send_header("Content-Security-Policy", "default-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'"); self.end_headers(); self.wfile.write(content); return True

    def _handle_local(self, method: str) -> None:
        self._new_cookie = None
        parsed = urllib.parse.urlsplit(self.path); path = parsed.path; query = {key: values[-1] for key, values in urllib.parse.parse_qs(parsed.query, keep_blank_values=True).items()}
        if re.fullmatch(r"/api/connect/[^/]+/v1/(models|chat/completions)", path):
            # Third-party OpenAI clients possess only their scoped connection
            # key, never the dashboard/controller credential or browser cookie.
            # LocalBridge revalidates the key, profile identity and model.
            bridge = getattr(self.server.service.bridge_local, "impl", None)
            if bridge is not None and bridge.proxy(self, path):
                return
            self._send_json_local(503, {"error": "bridge_unavailable", "message": "connection bridge unavailable"})
            return
        if path == "/api/dashboard/session":
            if not self._csrf_ok(method, path):
                self._send_json_local(403, {"error": "csrf_failed", "message": "browser session or same-origin validation failed"}); return
            if method == "GET":
                authenticated = self.server.auth_token is None or self.server.session_manager.valid(self._session_cookie())
                self._send_json_local(200, {"authenticated": authenticated, "token_required": self.server.auth_token is not None}); return
            if method == "POST":
                body = self._body()
                if not self.server.session_manager.token_valid(body.get("token")): self._send_json_local(401, {"error": "unauthorized", "message": "invalid token"}); return
                self._set_session_cookie(self.server.session_manager.issue()); self._send_json_local(200, {"authenticated": True, "token_required": self.server.auth_token is not None}); return
            if method == "DELETE":
                self._clear_session_cookie(); self._send_json_local(200, {"authenticated": False}); return
        if not self._csrf_ok(method, path):
            self._send_json_local(403, {"error": "csrf_failed", "message": "browser session or same-origin validation failed"}); return
        self._authorize()
        if path == "/api/shutdown" and method == "POST":
            self._send_json_local(200, {"ok": True, "shutting_down": True})
            threading.Thread(target=self.server.graceful_shutdown, name="controller-shutdown", daemon=True).start()
            return
        # Streaming bridge routes must consume the request body exactly once.
        # Dispatch them before the JSON-only local modules; otherwise the body
        # is consumed by ``_body`` and LocalBridge.proxy sees an empty stream.
        if path == "/api/connect/inference" or path.startswith("/api/remote/"):
            bridge = getattr(self.server.service.bridge_local, "impl", None)
            if bridge is not None and bridge.proxy(self, path):
                return
        if path == "/api/usage/export.csv" and method == "GET":
            status, data, headers = self.server.service.usage_local.csv(query); self.send_response(status); [self.send_header(k, v) for k, v in headers.items()]; self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data); return
        raw_body = b""
        body: dict[str, Any] = {}
        if method not in {"GET", "HEAD"}:
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length < 0 or length > MAX_BODY: raise UsageError("request body too large")
            raw_body = self.rfile.read(length)
            if raw_body:
                try: body = json.loads(raw_body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as error: raise UsageError("request body must be JSON") from error
                if not isinstance(body, dict): raise UsageError("request body must be an object")
        result = self.server.service.module_route(method, path, query, body)
        if result is not None:
            status, payload = result; self._send_json_local(status, payload); return
        # Original AgentRequestHandler owns lifecycle/status/HF routes.
        if method in {"GET", "POST"}:
            super()._dispatch_routed(method, path, raw_body); return
        self._send_json_local(405, {"error": "method_not_allowed", "message": "method not allowed"})

    def do_GET(self) -> None:  # noqa: N802
        if not self._host_allowed(): self._send_json_local(421, {"error": "invalid_host", "message": "invalid Host header"}); return
        path = urllib.parse.urlsplit(self.path).path
        if not path.startswith("/api/"):
            if not self._csrf_ok("GET", path): self._send_json_local(403, {"error": "csrf_failed", "message": "forbidden"}); return
            if not self._static(path): self._send_json_local(404, {"error": "not_found", "message": "dashboard assets not found"})
            return
        try: self._handle_local("GET")
        except AgentError as error: self._send_json_local(error.http_status, {"error": error.code, "message": error.public_message})
        except (ValueError, OSError) as error: self._send_json_local(500, {"error": "internal_error", "message": str(error)})

    def do_POST(self) -> None:  # noqa: N802
        if not self._host_allowed(): self._send_json_local(421, {"error": "invalid_host", "message": "invalid Host header"}); return
        try: self._handle_local("POST")
        except AgentError as error: self._send_json_local(error.http_status, {"error": error.code, "message": error.public_message})
        except (ValueError, OSError) as error: self._send_json_local(500, {"error": "internal_error", "message": str(error)})

    def do_DELETE(self) -> None:  # noqa: N802
        if not self._host_allowed(): self._send_json_local(421, {"error": "invalid_host", "message": "invalid Host header"}); return
        try: self._handle_local("DELETE")
        except AgentError as error: self._send_json_local(error.http_status, {"error": error.code, "message": error.public_message})


class LocalHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    def __init__(self, address: tuple[str, int], service: LocalService, token: str | None, dashboard_dir: Path):
        super().__init__(address, DashboardHandler)
        self.service = service; self.auth_token = token; self.dashboard_dir = dashboard_dir
        service.configuration.port = self.server_port
        self.session_manager = SessionManager(token)
        self._shutdown_started = False
        service.shutdown = lambda: threading.Thread(target=self.graceful_shutdown, name="bridge-shutdown", daemon=True).start()

    def graceful_shutdown(self) -> None:
        if self._shutdown_started: return
        self._shutdown_started = True
        try: self.service.stop_all_managed()
        finally:
            self.shutdown()
            self.server_close()


def dashboard_dir(root: Path) -> Path:
    configured = os.environ.get("MODELMATRIX_DASHBOARD_DIR")
    candidates = [Path(configured).expanduser()] if configured else []
    candidates += [root / "Dashboard" / "dist", Path(__file__).resolve().parents[1] / "Dashboard" / "dist"]
    for candidate in candidates:
        if (candidate / "index.html").is_file(): return candidate.resolve()
    return candidates[-1].resolve()


def _read_token(path: str | None) -> str | None:
    if not path: return None
    token = Path(path).read_text(encoding="utf-8-sig").strip()
    if len(token.encode()) < 16: raise InvalidConfigurationError("auth token must be at least 16 bytes")
    return token


def make_service(root: Path, profiles_dir: Path | None, host: str, port: int, token: str | None,
                 *, unsafe_bind: bool = False, relay_port: int | None = None) -> LocalService:
    config = AgentConfiguration(root=root, host=host, port=port, auth_token=token, unsafe_bind=unsafe_bind, profiles_dir=profiles_dir)
    service = LocalService(config)
    if relay_port is not None:
        if not 0 < relay_port < 65536: raise InvalidConfigurationError("relay port must be 1-65535")
        service.configuration.relay_port = relay_port  # consumed by bootstrap below
    service.bootstrap(); return service


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ModelMatrix Windows local dashboard controller")
    parser.add_argument("--root", type=Path, default=Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ModelMatrix")
    parser.add_argument("--profiles-dir", type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7700)
    parser.add_argument("--auth-token-file", type=Path)
    parser.add_argument("--unsafe-bind", action="store_true", help="allow a non-loopback bind (requires a strong token)")
    parser.add_argument("--relay-port", type=int, help="usage relay listener port (default: controller port + 1)")
    parser.add_argument("--no-browser", action="store_true")
    return parser


def _controller_record(root: Path, port: int) -> Path | None:
    if os.name != "nt":
        return None
    from datetime import datetime, timezone
    from platform_support import process_identity
    identity = process_identity(os.getpid())
    if identity is None or identity.created_ns is None or not identity.executable:
        raise InvalidConfigurationError("cannot record the native controller process identity")
    started = datetime.fromtimestamp(identity.created_ns / 1_000_000_000 - 11644473600, timezone.utc)
    path = root / "run" / "controller.pid"
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"schema": 1, "pid": os.getpid(), "executable": identity.executable,
              "controller": str(Path(__file__).resolve()), "root": str(root.resolve()),
              "command_line": subprocess.list2cmdline([sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]]),
              "started_utc": started.isoformat(), "port": port,
              "shutdown_url": f"http://127.0.0.1:{port}/api/shutdown"}
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(record), encoding="utf-8")
    os.replace(temporary, path)
    return path


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    token = _read_token(str(args.auth_token_file) if args.auth_token_file else None)
    service = make_service(args.root, args.profiles_dir, args.host, args.port, token, unsafe_bind=args.unsafe_bind, relay_port=args.relay_port)
    server = LocalHTTPServer((args.host, args.port), service, token, dashboard_dir(args.root))
    record = _controller_record(args.root, server.server_port)
    print(f"controller=http://{args.host}:{server.server_port}", flush=True)
    try:
        if not args.no_browser:
            import webbrowser
            webbrowser.open(f"http://127.0.0.1:{server.server_port}/")
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown(); server.server_close(); service.stop_all_managed()
        if record is not None:
            try:
                if json.loads(record.read_text(encoding="utf-8-sig")).get("pid") == os.getpid():
                    record.unlink(missing_ok=True)
            except (OSError, ValueError):
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
