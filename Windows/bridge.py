"""Windows-local Dashboard bridge and protected connection controller.

The class is deliberately transport-neutral: ``route`` handles ordinary JSON
requests and ``proxy`` is used by the HTTP handler for streaming responses.
It preserves the RemoteAgent's route contract while providing a real local
controller (the menu-bar heartbeat queue is not used as a fake success path).
"""
from __future__ import annotations

import copy
import hashlib
import ipaddress
import json
import os
import platform
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

try:
    from agent_core import AgentError, OperationFailedError, ProfileConflictError, ProfileNotFoundError, UnsupportedError, UsageError
except ImportError:  # package import during embedding
    from RemoteAgent.agent_core import AgentError, OperationFailedError, ProfileConflictError, ProfileNotFoundError, UnsupportedError, UsageError
try:
    from client_connections import connection_profiles, forward_connection
except ImportError:
    from RemoteAgent.client_connections import connection_profiles, forward_connection
try:
    from secrets_store import SecretStore, SecretStoreError
except ImportError:  # package import as ``Windows.bridge``
    from .secrets_store import SecretStore, SecretStoreError  # type: ignore
try:
    from native_windows import close_apps as native_close_apps, inventory_apps as native_inventory_apps
except ImportError:  # package import as ``Windows.bridge``
    from .native_windows import close_apps as native_close_apps, inventory_apps as native_inventory_apps  # type: ignore


MAX_BODY = 64 * 1024 * 1024
REMOTE_GET = {"/api/status", "/api/host/metrics", "/api/doctor", "/api/benchmark/status", "/api/integrations", "/api/version", "/api/ollama/status", "/api/usage/overview", "/api/usage/summary", "/api/usage/timeseries", "/api/usage/models", "/api/usage/clients", "/api/usage/requests", "/api/usage/live", "/api/usage/settings", "/api/usage/export.csv", "/api/hf/check", "/api/hf/check/search", "/api/hf/search", "/api/hf/settings", "/api/hf/downloads", "/api/hf/models", "/api/hf/engines", "/api/hf/engines/jobs"}
REMOTE_POST = {"/api/start", "/api/stop", "/api/restart", "/api/switch", "/api/stop-all", "/api/benchmark/start", "/api/usage/clients/rename", "/api/hf/settings", "/api/hf/token", "/api/hf/downloads", "/api/hf/models/delete", "/api/hf/engines/plan", "/api/hf/engines/run"}
REMOTE_QUERY_KEYS = {"name", "repo", "version", "context", "search", "q", "range", "tier", "limit", "offset", "since", "refresh"}
TARGETS = ("kilo", "opencode")
REQUIRED_TARGET_VERSIONS = {"kilo": "7.8.12", "opencode": "1.16.2"}


def _now() -> float:
    return time.time()


def _nonempty(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise UsageError(f"missing required string field: {field}")
    return value.strip()


def _bool(value: Any, default: bool = False) -> bool:
    return value if isinstance(value, bool) else default


def _safe_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", value):
        raise UsageError("invalid gateway or connection id")
    return value


def _json_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _url(value: str, *, allow_loopback: bool = True) -> urllib.parse.SplitResult:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise UsageError("gateway address must be an HTTP(S) URL without credentials")
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise UsageError("invalid gateway port")
    return parsed


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


def _opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


@dataclass
class _Tunnel:
    process: subprocess.Popen[bytes]
    local_port: int


class LocalBridge:
    """Controller-side bridge used by ``Windows/controller.py``."""

    def __init__(self, service: Any):
        self.service = service
        self.root = Path(service.configuration.root).expanduser()
        self.bridge_root = self.root / "bridge"
        self.bridge_root.mkdir(parents=True, exist_ok=True)
        self.gateway_file = self.bridge_root / "gateways.json"
        self.secret_store = SecretStore(self.root, test_mode=os.environ.get("MODELMATRIX_TEST_SECRET_STORE") == "1")
        self._lock = threading.RLock()
        self._commands: dict[str, dict[str, Any]] = {}
        self._command_order: list[str] = []
        self._heartbeat_at = _now()
        self._app: dict[str, Any] = {"name": "ModelMatrix Windows controller", "platform": "windows" if os.name == "nt" else sys.platform, "local": True}
        self._tunnels: dict[str, _Tunnel] = {}
        self._plans: dict[str, dict[str, Any]] = {}
        self._connections: dict[str, dict[str, Any]] = self._load_connections()
        self._gateways: dict[str, dict[str, Any]] = self._load_gateways()

    # ---- persistence -------------------------------------------------
    def _load_gateways(self) -> dict[str, dict[str, Any]]:
        try:
            raw = json.loads(self.gateway_file.read_text(encoding="utf-8-sig")) if self.gateway_file.exists() else {}
            items = raw.get("gateways", raw) if isinstance(raw, dict) else {}
            return {str(k): dict(v) for k, v in items.items() if isinstance(v, dict) and isinstance(k, str)}
        except (OSError, ValueError):
            return {}

    def _load_connections(self) -> dict[str, dict[str, Any]]:
        path = self.bridge_root / "connections.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
            return {str(k): dict(v) for k, v in raw.get("connections", {}).items()} if isinstance(raw, dict) else {}
        except (OSError, ValueError):
            return {}

    def _atomic_json(self, path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as handle:
                json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except OSError as error:
            try: temporary.unlink(missing_ok=True)
            except OSError: pass
            raise OperationFailedError(f"cannot safely write {path.name}") from error

    def _save_gateways(self) -> None:
        public = {k: {key: value for key, value in v.items() if key not in {"token", "key"}} for k, v in self._gateways.items()}
        self._atomic_json(self.gateway_file, {"version": 1, "gateways": public})

    def _save_connections(self) -> None:
        public = {k: {key: value for key, value in v.items() if key not in {"key", "token"}} for k, v in self._connections.items()}
        self._atomic_json(self.bridge_root / "connections.json", {"version": 1, "connections": public})

    # ---- bridge state/commands --------------------------------------
    def _public_gateway(self, gateway: dict[str, Any]) -> dict[str, Any]:
        result = {k: v for k, v in gateway.items() if k not in {"token", "key", "private_key"}}
        if gateway.get("kind") == "ssh" and gateway.get("ssh_host"):
            host = str(gateway["ssh_host"])
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            destination = f"{gateway.get('ssh_user')}@{host}" if gateway.get("ssh_user") else host
            port = int(gateway.get("ssh_port", 22))
            result["ssh_target"] = destination if port == 22 else f"{destination}:{port}"
        result["has_token"] = bool(self.secret_store.get(f"gateway-{gateway['id']}"))
        result["status"] = self._gateway_status(gateway, probe=False)
        return result

    def _public_command(self, command: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in command.items() if k not in {"token", "key"}}

    def _state(self) -> dict[str, Any]:
        with self._lock:
            state = {"app_connected": True, "gateways": [self._public_gateway(g) for g in self._gateways.values() if g.get("enabled", True)], "app": dict(self._app), "last_heartbeat": self._heartbeat_at}
        if os.name == "nt":
            try:
                state["app"]["windows_apps"] = native_inventory_apps()
            except Exception:
                state["app"]["windows_apps"] = []
        return state

    def _command(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        command = {"id": str(uuid4()), "type": kind, "status": "queued", "created_at": _now(), "updated_at": _now(), "payload": copy.deepcopy(payload)}
        try:
            result = self._execute_command(kind, payload)
            command.update(status="done", result=result, message="completed")
        except AgentError as error:
            command.update(status="failed", message=error.public_message, result={"error": error.code})
            raise
        except Exception as error:
            command.update(status="failed", message="native action failed")
            raise OperationFailedError(f"native bridge action failed: {error}") from error
        with self._lock:
            self._commands[command["id"]] = command
            self._command_order.append(command["id"])
            if len(self._command_order) > 200:
                del self._commands[self._command_order.pop(0)]
        return self._public_command(command)

    def _execute_command(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        if kind == "gateway.pair": return self._gateway_pair(payload)
        if kind == "gateway.add": return self._gateway_add(payload)
        if kind == "gateway.update": return self._gateway_update(payload)
        if kind == "gateway.toggle": return self._gateway_toggle(payload)
        if kind == "gateway.remove": return self._gateway_remove(payload)
        if kind == "gateway.refresh": return self._gateway_refresh(payload)
        if kind in {"gateway.install_agent", "gateway.update_agent"}: return self._gateway_install(payload, update=kind.endswith("update_agent"))
        if kind == "app.refresh": return {"state": self._state()}
        if kind == "app.open_settings": return self._open_path(self.root)
        if kind in {"app.open_profiles_folder", "app.open_profiles"}: return self._open_path(Path(self.service.configuration.profiles_directory))
        if kind == "app.open_logs": return self._open_path(self.root / "logs")
        if kind == "app.open_usage": return self._open_path(self.root)
        if kind == "app.open_optimize": return {"opened": False, "reason": "optimize is a Dashboard view"}
        if kind == "app.open_downloads": return self._open_path(self.root / "downloads")
        if kind in {"app.open_url", "app.open_browser"}: return self._open_url(payload)
        if kind in {"app.copy", "app.copy_text", "clipboard.copy"}: return self._copy_text(payload)
        if kind == "app.quit_app": return self._quit_controller()
        if kind == "app.quit_apps": return self._quit_user_apps(payload)
        raise UsageError(f"unknown command type: {kind}")

    def _open_path(self, path: Path) -> dict[str, Any]:
        path = path.expanduser()
        if not path.exists():
            raise OperationFailedError(f"folder does not exist: {path}")
        if os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif shutil.which("xdg-open"):
            subprocess.Popen([shutil.which("xdg-open") or "xdg-open", str(path)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            raise UnsupportedError("Explorer/folder reveal is unavailable on this host")
        return {"opened": True, "path": str(path)}

    def _open_url(self, payload: dict[str, Any]) -> dict[str, Any]:
        value = _nonempty(payload.get("url"), "url")
        parsed = urllib.parse.urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise UsageError("browser URL must be an HTTP(S) URL without credentials")
        if not webbrowser.open(value, new=0, autoraise=True):
            raise OperationFailedError("default browser refused the URL")
        return {"opened": True, "url": value}

    def _copy_text(self, payload: dict[str, Any]) -> dict[str, Any]:
        text = payload.get("text")
        if not isinstance(text, str) or len(text) > 1_048_576:
            raise UsageError("clipboard text must be a string of at most 1 MiB")
        commands = [["clip.exe"]] if os.name == "nt" else [["wl-copy"], ["xclip", "-selection", "clipboard"]]
        for command in commands:
            executable = shutil.which(command[0])
            if not executable:
                continue
            try:
                # clip.exe consumes UTF-16LE text.  Sending UTF-8 makes every
                # non-ASCII clipboard operation silently produce mojibake.
                encoding = "utf-16le" if os.name == "nt" and command[0].lower() == "clip.exe" else "utf-8"
                completed = subprocess.run([executable, *command[1:]], input=text.encode(encoding), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=5, check=False)
            except (OSError, subprocess.TimeoutExpired):
                continue
            if completed.returncode == 0:
                return {"copied": True}
        raise UnsupportedError("clipboard is unavailable on this host")

    def _quit_controller(self) -> dict[str, Any]:
        shutdown = getattr(self.service, "shutdown", None)
        if callable(shutdown):
            shutdown()
            return {"requested": True}
        return {"requested": False, "reason": "controller lifecycle is owned by its host"}

    def _quit_user_apps(self, payload: dict[str, Any]) -> dict[str, Any]:
        requested = payload.get("apps")
        if not isinstance(requested, list) or len(requested) > 40 or any(not isinstance(item, dict) for item in requested):
            raise UnsupportedError("safe user-app close requires a current native window inventory")
        if os.name != "nt":
            raise UnsupportedError("gentle WM_CLOSE is only available on Windows")
        current = native_inventory_apps()
        selected: list[dict[str, Any]] = []
        for item in requested:
            path = item.get("path")
            name = item.get("name")
            if not isinstance(path, str) or not path:
                raise UsageError("each app must include its current executable path")
            match = next((row for row in current if str(row.get("path", "")).casefold() == path.casefold() and (not name or row.get("name") == name)), None)
            if match is None:
                raise UsageError("requested app is not in the current native window inventory")
            selected.append(match)
        return native_close_apps(selected) if selected else []

    # ---- gateway management -----------------------------------------
    def _gateway_from_payload(self, payload: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
        old = existing or {}
        mode = str(payload.get("kind", payload.get("mode", old.get("kind", "ssh")))).lower()
        if mode not in {"ssh", "direct"}: raise UsageError("gateway kind must be ssh or direct")
        name = str(payload.get("name", old.get("name", "gateway"))).strip() or "gateway"
        gateway = {"id": _safe_id(str(payload.get("id", old.get("id", re.sub(r"[^A-Za-z0-9_.-]+", "-", name.lower())[:64] or uuid4().hex[:12])))), "name": name, "kind": mode, "enabled": _bool(payload.get("enabled"), old.get("enabled", True))}
        if mode == "direct":
            base = str(payload.get("base_url", old.get("base_url", ""))).strip()
            parsed_base = _url(base)
            host = parsed_base.hostname or ""
            try:
                loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                loopback = host.lower() in {"localhost"} or host.lower().endswith(".localhost")
            existing_secret = bool(old.get("id") and self.secret_store.has(f"gateway-{old['id']}"))
            if not loopback and not payload.get("token") and not old.get("token") and not existing_secret and payload.get("protected_tailnet") is not True and old.get("protected_tailnet") is not True:
                raise UsageError("non-loopback direct gateways require a bearer token or recognized protected tailnet")
            gateway["base_url"] = base.rstrip("/")
        else:
            target = payload.get("ssh_target", old.get("ssh_target"))
            parsed_target = self._parse_ssh_target(target) if target is not None else {}
            host = _nonempty(payload.get("ssh_host", payload.get("host", parsed_target.get("host", old.get("ssh_host", old.get("host", ""))))), "ssh_host")
            user = str(payload.get("ssh_user", payload.get("user", parsed_target.get("user", old.get("ssh_user", old.get("user", "")))))).strip()
            if host.startswith("-") or user.startswith("-") or any(c in host for c in "\r\n") or any(c in user for c in "\r\n"):
                raise UsageError("unsafe SSH destination")
            port = int(payload.get("ssh_port", payload.get("port", parsed_target.get("port", old.get("ssh_port", old.get("port", 22))))))
            remote_port = int(payload.get("remote_port", old.get("remote_port", 7700)))
            local_port = int(payload.get("local_port", old.get("local_port", 0)))
            if not 1 <= port <= 65535 or not 1 <= remote_port <= 65535 or not 0 <= local_port <= 65535: raise UsageError("invalid gateway port")
            gateway.update(ssh_host=host, ssh_user=user, ssh_port=port, remote_port=remote_port, local_port=local_port)
            if old.get("base_url") and not payload.get("local_port"):
                gateway["base_url"] = old["base_url"]
        for key in ("platform", "known_hosts", "ssh_path"):
            if key in payload and payload[key] is not None: gateway[key] = str(payload[key])
        return gateway

    @staticmethod
    def _parse_ssh_target(value: Any) -> dict[str, Any]:
        """Parse the frontend's ssh_target (user@host, IPv6-safe, optional port)."""
        target = _nonempty(value, "ssh_target")
        if any(c in target for c in "\r\n") or target.startswith("-"):
            raise UsageError("unsafe SSH destination")
        user = ""
        if "@" in target:
            user, target = target.rsplit("@", 1)
        if not target:
            raise UsageError("SSH target host is required")
        host, port = target, 22
        if target.startswith("["):
            end = target.find("]")
            if end < 0:
                raise UsageError("invalid IPv6 SSH target")
            host = target[1:end]
            suffix = target[end + 1:]
            if suffix:
                if not suffix.startswith(":"):
                    raise UsageError("invalid SSH target port")
                port = int(suffix[1:])
        elif target.count(":") == 1:
            candidate_host, candidate_port = target.rsplit(":", 1)
            if candidate_port.isdigit():
                host, port = candidate_host, int(candidate_port)
        if not host or host.startswith("-") or any(c in host for c in "\r\n"):
            raise UsageError("unsafe SSH destination")
        if user.startswith("-") or any(c in user for c in "\r\n"):
            raise UsageError("unsafe SSH destination")
        if not 1 <= port <= 65535:
            raise UsageError("invalid gateway port")
        return {"user": user, "host": host, "port": port}

    def _gateway_add(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            gateway = self._gateway_from_payload(payload)
            if gateway["id"] in self._gateways: raise UsageError("gateway already exists")
            token = payload.get("token")
            if token is not None:
                if not isinstance(token, str) or not token: raise UsageError("token must be non-empty")
                self.secret_store.set(f"gateway-{gateway['id']}", token)
            self._gateways[gateway["id"]] = gateway
            self._save_gateways()
        self._ensure_tunnel(gateway)
        return {"gateway": self._public_gateway(gateway)}

    def _gateway_update(self, payload: dict[str, Any]) -> dict[str, Any]:
        gid = _safe_id(_nonempty(payload.get("id"), "id"))
        with self._lock:
            if gid not in self._gateways: raise UsageError("gateway not found")
            gateway = self._gateway_from_payload({**payload, "id": gid}, self._gateways[gid])
            self._gateways[gid] = gateway
            if isinstance(payload.get("token"), str): self.secret_store.set(f"gateway-{gid}", payload["token"])
            self._save_gateways()
        self._stop_tunnel(gid)
        self._ensure_tunnel(gateway)
        return {"gateway": self._public_gateway(gateway)}

    def _gateway_toggle(self, payload: dict[str, Any]) -> dict[str, Any]:
        gid = _safe_id(_nonempty(payload.get("id"), "id"))
        with self._lock:
            gateway = self._gateways.get(gid)
            if not gateway: raise UsageError("gateway not found")
            gateway["enabled"] = _bool(payload.get("enabled"), not bool(gateway.get("enabled", True)))
            self._save_gateways()
        if gateway["enabled"]: self._ensure_tunnel(gateway)
        else: self._stop_tunnel(gid)
        return {"gateway": self._public_gateway(gateway)}

    def _gateway_remove(self, payload: dict[str, Any]) -> dict[str, Any]:
        gid = _safe_id(_nonempty(payload.get("id"), "id"))
        with self._lock:
            if gid not in self._gateways: raise UsageError("gateway not found")
            del self._gateways[gid]
            self.secret_store.delete(f"gateway-{gid}")
            self._save_gateways()
        self._stop_tunnel(gid)
        return {"removed": True, "id": gid}

    def _gateway_refresh(self, payload: dict[str, Any]) -> dict[str, Any]:
        gid = _safe_id(_nonempty(payload.get("id"), "id"))
        gateway = self._gateways.get(gid)
        if not gateway: raise UsageError("gateway not found")
        if gateway.get("kind") == "ssh":
            self._stop_tunnel(gid)
            self._ensure_tunnel(gateway)
        status = self._gateway_status(gateway, probe=True)
        result = self._public_gateway(gateway)
        result["status"] = status
        return {"gateway": result}

    def _parse_pair(self, link: str) -> dict[str, Any]:
        parsed = urllib.parse.urlsplit(link.strip())
        if parsed.scheme != "modelmatrix-gateway" or not parsed.hostname: raise UsageError("invalid gateway pairing link")
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        mode = query.get("mode", ["ssh"])[-1]
        if mode not in {"ssh", "direct"}: raise UsageError("invalid gateway pairing mode")
        name = query.get("name", [parsed.hostname])[-1]
        port_raw = query.get("agent_port", ["7700"])[-1]
        try: agent_port = int(port_raw)
        except ValueError: raise UsageError("invalid agent port")
        if not 1 <= agent_port <= 65535: raise UsageError("invalid agent port")
        payload: dict[str, Any] = {"name": name, "kind": mode, "remote_port": agent_port}
        if mode == "direct": payload["base_url"] = f"http://{parsed.hostname}:{agent_port}"
        else:
            payload.update(ssh_host=parsed.hostname, ssh_user=urllib.parse.unquote(parsed.username or ""), ssh_port=parsed.port or 22)
        return payload

    def _gateway_pair(self, payload: dict[str, Any]) -> dict[str, Any]:
        link = _nonempty(payload.get("link"), "link")
        fields = self._parse_pair(link)
        fields.update({k: v for k, v in payload.items() if k in {"id", "name", "token", "known_hosts", "ssh_path"}})
        return self._gateway_add(fields)

    def _ssh_executable(self, gateway: dict[str, Any]) -> str:
        candidate = gateway.get("ssh_path") or shutil.which("ssh")
        if not candidate or not Path(candidate).exists() and not shutil.which(str(candidate)):
            raise UnsupportedError("OpenSSH executable was not found on PATH")
        return str(candidate)

    def _known_hosts(self, gateway: dict[str, Any]) -> str:
        configured = gateway.get("known_hosts")
        candidates = [Path(configured).expanduser()] if configured else [Path.home() / ".ssh" / "known_hosts"]
        for path in candidates:
            if path.is_file(): return str(path)
        raise OperationFailedError("SSH known_hosts file is required; refusing trust-on-first-use")

    def _ensure_tunnel(self, gateway: dict[str, Any]) -> None:
        if not gateway.get("enabled", True) or gateway.get("kind") != "ssh": return
        if gateway["id"] in self._tunnels and self._tunnels[gateway["id"]].process.poll() is None: return
        ssh = self._ssh_executable(gateway)
        known = self._known_hosts(gateway)
        local = int(gateway.get("local_port") or 0)
        if local == 0:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.bind(("127.0.0.1", 0))
                local = int(probe.getsockname()[1])
        destination = f"{gateway.get('ssh_user')}@{gateway['ssh_host']}" if gateway.get("ssh_user") else gateway["ssh_host"]
        if destination.startswith("-"): raise UsageError("unsafe SSH destination")
        args = [ssh, "-N", "-T", "-o", "BatchMode=yes", "-o", "ControlMaster=no", "-o", "ControlPath=none", "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={known}", "-o", "ExitOnForwardFailure=yes", "-o", "ConnectTimeout=10", "-p", str(gateway.get("ssh_port", 22)), "-L", f"127.0.0.1:{local}:127.0.0.1:{gateway['remote_port']}", destination]
        try:
            process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as error: raise OperationFailedError("could not start OpenSSH tunnel") from error
        self._tunnels[gateway["id"]] = _Tunnel(process, local)
        gateway["local_port"] = local
        gateway["base_url"] = f"http://127.0.0.1:{local}"
        if process.stderr is not None:
            threading.Thread(target=lambda: process.stderr.read(), name=f"modelmatrix-ssh-{gateway['id']}", daemon=True).start()
        # A tunnel that exits immediately is not a successful gateway.
        time.sleep(0.03)
        if process.poll() is not None:
            self._tunnels.pop(gateway["id"], None)
            raise OperationFailedError("OpenSSH tunnel exited before forwarding was established")
        self._save_gateways()

    def _stop_tunnel(self, gid: str) -> None:
        tunnel = self._tunnels.pop(gid, None)
        if tunnel and tunnel.process.poll() is None:
            tunnel.process.terminate()
            try: tunnel.process.wait(timeout=3)
            except subprocess.TimeoutExpired: tunnel.process.kill()

    def _gateway_status(self, gateway: dict[str, Any], *, probe: bool = True) -> str:
        if not gateway.get("enabled", True): return "disabled"
        if gateway.get("kind") == "ssh" and gateway["id"] not in self._tunnels:
            try: self._ensure_tunnel(gateway)
            except AgentError: return "unavailable"
        base = gateway.get("base_url")
        if not base: return "unavailable"
        if not probe: return "configured"
        try:
            parsed = _url(str(base))
            request = urllib.request.Request(urllib.parse.urlunsplit(parsed._replace(path=parsed.path.rstrip("/") + "/api/status", query="", fragment="")), headers={"Authorization": f"Bearer {self.secret_store.get('gateway-' + gateway['id'])}"} if self.secret_store.get("gateway-" + gateway["id"]) else {})
            with _opener().open(request, timeout=5) as response:
                return "ready" if 200 <= response.status < 300 else "unavailable"
        except (OSError, urllib.error.URLError, ValueError): return "unavailable"

    def _gateway_install(self, payload: dict[str, Any], *, update: bool) -> dict[str, Any]:
        gid = _safe_id(_nonempty(payload.get("id"), "id"))
        gateway = self._gateways.get(gid)
        if not gateway or gateway.get("kind") != "ssh": raise UnsupportedError("agent deployment requires an SSH gateway")
        ssh = self._ssh_executable(gateway); known = self._known_hosts(gateway)
        destination = f"{gateway.get('ssh_user')}@{gateway['ssh_host']}" if gateway.get("ssh_user") else gateway["ssh_host"]
        windows_remote = str(gateway.get("platform", "")).lower() == "windows"
        scripts = [Path(__file__).resolve().parents[1] / "RemoteAgent" / "install-remote-agent.ps1"] if windows_remote else [Path(__file__).resolve().parents[1] / "RemoteAgent" / "install-remote-agent.sh"]
        script = next((p for p in scripts if p.is_file()), None)
        if script is None: raise UnsupportedError("no verified installer exists for the remote platform")
        if windows_remote:
            scp = gateway.get("scp_path") or shutil.which("scp")
            if not scp:
                raise UnsupportedError("OpenSSH scp executable was not found; Windows deployment requires a staged local payload")
            source_root = script.parent
            payload = [source_root / name for name in ("agent_core.py", "discovery.py", "hf_agent.py", "client_connections.py", "platform_support.py", "modelmatrix_agent.py", "install-remote-agent.ps1")]
            if any(not item.is_file() for item in payload):
                raise UnsupportedError("Windows deployment payload is incomplete; refusing an unverified upstream download")
            stage = ".modelmatrix-agent-stage"
            prep_command = [ssh, "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={known}", destination, "powershell.exe", "-NoProfile", "-NonInteractive", "-Command", f"New-Item -ItemType Directory -Force -Path (Join-Path $env:USERPROFILE '{stage}') | Out-Null"]
            try:
                prepared = subprocess.run(prep_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=False)
            except (OSError, subprocess.TimeoutExpired) as error: raise OperationFailedError("remote Windows staging directory could not be created") from error
            if prepared.returncode != 0:
                raise OperationFailedError("remote Windows staging directory could not be created")
            copy_command = [scp, "-q", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={known}", *[str(item) for item in payload], f"{destination}:{stage}/"]
            try:
                copied = subprocess.run(copy_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180, check=False)
            except (OSError, subprocess.TimeoutExpired) as error: raise OperationFailedError("remote Windows agent payload upload failed") from error
            if copied.returncode != 0:
                raise OperationFailedError("remote Windows agent payload upload failed")
            command = [ssh, "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={known}", destination, "powershell.exe", "-NoProfile", "-NonInteractive", "-Command", f"Set-Location $env:USERPROFILE; & '.\\{stage}\\install-remote-agent.ps1' -SourceRoot '.\\{stage}' -Port {int(gateway.get('remote_port', 7700))}"]
            script_input = None
        else:
            command = [ssh, "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={known}", destination, "bash", "-s", "--"]
            script_input = script.read_bytes()
        try:
            completed = subprocess.run(command, input=script_input, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180, check=False)
        except (OSError, subprocess.TimeoutExpired) as error: raise OperationFailedError("remote agent deployment failed") from error
        if completed.returncode != 0:
            raise OperationFailedError("remote agent deployment failed")
        return {"installed": True, "updated": update, "gateway": self._public_gateway(gateway)}

    # ---- ConnectApps -------------------------------------------------
    def _profile_rows(self) -> list[dict[str, Any]]:
        rows = connection_profiles(self.service).get("profiles", [])
        result = []
        for row in rows:
            row = dict(row); row["key"] = row.get("profile"); row.setdefault("host", None); result.append(row)
        return result

    def _profile(self, key: str) -> tuple[str, Any, dict[str, Any]]:
        profiles = self.service.profiles.load()
        name = key.split(":", 1)[-1] if ":" in key else key
        profile = profiles.get(name)
        if profile is None:
            try: profile = self.service.profiles.profile(name)
            except Exception as error: raise ProfileNotFoundError(name) from error
        row = next((r for r in self._profile_rows() if r.get("key") == key or r.get("profile") == name), None)
        if row is None: raise ProfileNotFoundError(name)
        return name, profile, row

    def _target(self, target: str) -> dict[str, Any]:
        if target not in TARGETS: raise UsageError("target must be kilo, opencode, or manual")
        required_version = REQUIRED_TARGET_VERSIONS[target]
        env = "MODELMATRIX_KILO_PATH" if target == "kilo" else "MODELMATRIX_OPENCODE_PATH"
        candidates = [os.environ.get(env, "")] + ([shutil.which(target)] if shutil.which(target) else [])
        executable = next((p for p in candidates if p and Path(p).exists()), None)
        version = None; qualified = False; reason = "not installed"
        if executable:
            try:
                result = subprocess.run([executable, "--version"], capture_output=True, text=True, timeout=5, check=False)
                match = re.search(r"(?<!\d)(\d+\.\d+(?:\.\d+)?(?:[-+][0-9A-Za-z.-]+)?)", result.stdout + result.stderr)
                if match:
                    version = match.group(1)
                    qualified = version == required_version
                    reason = "qualified" if qualified else f"only exact {required_version} is supported"
                else: reason = "version could not be verified"
            except (OSError, subprocess.TimeoutExpired): reason = "version could not be verified"
        config_env = "MODELMATRIX_KILO_CONFIG" if target == "kilo" else "MODELMATRIX_OPENCODE_CONFIG"
        config = os.environ.get(config_env)
        if not config:
            appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData/Roaming")
            config = str(Path(appdata) / ("Kilo" if target == "kilo" else "opencode") / "config.json")
        return {"id": target, "name": "Kilo" if target == "kilo" else "OpenCode", "installed": bool(executable), "qualified": qualified, "version": version, "path": executable, "config_path": config, "required_version": required_version, "reason": reason, "reload_note": "reload the client after applying the protected change" if qualified else "manual setup only until a version-qualified build is installed"}

    def _connection_public(self, row: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in row.items() if k not in {"key", "token", "fingerprint", "config_hash"}}

    def _details(self, key: str, connection_id: str | None = None) -> dict[str, Any]:
        name, profile, row = self._profile(key)
        identity = row.get("identity_token", "")
        endpoint = self._relay_url(name, identity, connection_id)
        ready = row.get("readiness", "stopped")
        model = profile.request_model
        return {"profile": name, "display_name": profile.display_name, "model_id": model, "provider": "OpenAI-compatible", "base_url": endpoint, "server_root": endpoint.rsplit("/api/connect/", 1)[0] if "/api/connect/" in endpoint else endpoint, "model_list_url": endpoint + "/models", "chat_url": endpoint + "/chat/completions", "api_key_requirement": "required", "credential_kind": "protected_file", "readiness": "ready" if ready in {"running", "ready"} else ready, "context": row.get("context"), "capabilities": {"chat": True, "tools": "unknown", "images": "unknown", "embeddings": "unknown"}, "connection_id": connection_id, "summary": f"{profile.display_name} via ModelMatrix ({model})", "request_example": json.dumps({"model": model, "messages": [{"role": "user", "content": "Hello"}]}, separators=(",", ":")), "notes": ["The generated key is scoped to this connection and is never included here."]}

    def _relay_url(self, name: str, identity: str, connection_id: str | None) -> str:
        host = getattr(self.service.configuration, "host", "127.0.0.1")
        port = getattr(self.service.configuration, "port", 7700)
        if connection_id:
            return f"http://{host}:{port}/api/connect/{urllib.parse.quote(connection_id, safe='')}/v1"
        return f"http://{host}:{port}/v1"

    @staticmethod
    def _strip_jsonc_comments(text: str) -> str:
        """Remove JSONC comments without treating // inside a JSON string as syntax."""
        output: list[str] = []
        i = 0
        in_string = False
        escaped = False
        while i < len(text):
            char = text[i]
            if in_string:
                output.append(char)
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                i += 1
                continue
            if char == '"':
                in_string = True
                output.append(char)
                i += 1
            elif text.startswith("//", i):
                end = text.find("\n", i)
                if end < 0:
                    break
                output.append("\n")
                i = end + 1
            elif text.startswith("/*", i):
                end = text.find("*/", i + 2)
                if end < 0:
                    raise OperationFailedError("client configuration has an unterminated comment")
                segment = text[i:end + 2]
                output.extend("\n" if c == "\n" else " " for c in segment)
                i = end + 2
            else:
                output.append(char)
                i += 1
        return "".join(output)

    def _jsonc_load(self, path: Path) -> dict[str, Any]:
        if not path.exists(): return {}
        text = path.read_text(encoding="utf-8-sig")
        text = self._strip_jsonc_comments(text)
        text = re.sub(r",\s*([}\]])", r"\1", text)
        try: value = json.loads(text)
        except (ValueError, UnicodeError) as error: raise OperationFailedError("client configuration is not valid JSON/JSONC") from error
        if not isinstance(value, dict): raise OperationFailedError("client configuration must be an object")
        return value

    def _write_client(self, path: Path, value: dict[str, Any], *, connection_id: str, expected_hash: str | None) -> str:
        current = self._jsonc_load(path); current_hash = _json_hash(current)
        if expected_hash is not None and current_hash != expected_hash: raise ProfileConflictError("client configuration changed; review the plan")
        backup_dir = self.bridge_root / "connections" / "backups"; backup_dir.mkdir(parents=True, exist_ok=True)
        if path.exists(): shutil.copy2(path, backup_dir / f"{connection_id}.json.bak")
        self._atomic_json(path, value)
        return _json_hash(value)

    def _key_file(self, connection_id: str, key: str) -> Path:
        path = self.secret_store.directory / f"{connection_id}.key"
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="ascii", newline="\n") as handle:
                handle.write(key)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            os.replace(temporary, path)
            return path
        except OSError as error:
            try: temporary.unlink(missing_ok=True)
            except OSError: pass
            raise OperationFailedError("cannot write the protected client key file") from error

    @staticmethod
    def _model_key(profile_key: str, model_id: str) -> str:
        value = re.sub(r"[^a-z0-9_-]+", "-", f"{profile_key}-{model_id}".lower())
        return value.strip("-_")[:80] or "model"

    def _provider_document(self, provider_id: str, model_key: str, model_id: str, base_url: str, key_file: Path, context: Any) -> dict[str, Any]:
        model: dict[str, Any] = {"id": model_id}
        if isinstance(context, int) and context > 0:
            model["limit"] = {"context": context, "output": min(context, 8192)}
        return {"npm": "@ai-sdk/openai-compatible", "options": {"baseURL": base_url, "apiKey": "{file:" + str(key_file) + "}"}, "models": {model_key: model}}

    def _plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        key = _nonempty(payload.get("profile"), "profile"); target = str(payload.get("target", "manual"))
        name, profile, row = self._profile(key); identity = row.get("identity_token", "")
        target_info = {"id": "manual", "name": "Other app", "installed": False, "qualified": True, "reason": "manual setup"} if target == "manual" else self._target(target)
        if target != "manual" and not target_info["qualified"]: raise UnsupportedError(str(target_info.get("reason") or "target is not qualified"))
        connection_id = next((cid for cid, c in self._connections.items() if c.get("profile_key") == key and c.get("target") == target), None)
        config_path = target_info.get("config_path") if target != "manual" else None
        existing_config = self._jsonc_load(Path(config_path)) if config_path else {}
        plan_id = str(uuid4()); expires = _now() + 600
        base = self._relay_url(name, identity, connection_id or plan_id)
        model_key = re.sub(r"[^A-Za-z0-9_.-]+", "-", profile.request_model).strip("-")[:80]
        connection_id = connection_id or plan_id
        provider_id = next((c.get("provider_id") for c in self._connections.values() if c.get("profile_key") == key and c.get("target") == target), f"mmx-{connection_id}")
        model_key = next((c.get("model_key") for c in self._connections.values() if c.get("profile_key") == key and c.get("target") == target), self._model_key(name, profile.request_model))
        base = self._relay_url(name, identity, connection_id)
        plan = {"id": plan_id, "expires_at": expires, "profile_key": key, "target": target, "title": f"Connect {target_info['name']} to {profile.display_name}", "config_path": config_path, "target_version": target_info.get("version"), "provider_id": provider_id, "model_key": model_key, "base_url": base, "model_id": profile.request_model, "make_default": _bool(payload.get("make_default")), "credential_storage": "Windows DPAPI protected scoped key file", "changes": [f"add owned provider.{provider_id} with model {model_key}"] if target != "manual" else ["no config file is changed; use the protected endpoint details manually"], "notes": ["No config or credential is written until this exact expiring plan is approved.", "The client receives a private {file:path} key reference; the bearer key is never embedded in JSON."], "recovery": "Disconnect or Undo removes only the owned provider when its fingerprint still matches.", "_connection_id": connection_id, "_profile_identity": identity, "_target_path": target_info.get("path"), "_config_hash": _json_hash(existing_config), "_base": base}
        with self._lock: self._plans[plan_id] = plan
        return {k: v for k, v in plan.items() if not k.startswith("_")}

    def _apply(self, payload: dict[str, Any]) -> dict[str, Any]:
        pid = _nonempty(payload.get("plan_id"), "plan_id")
        if payload.get("approved") is not True: raise UsageError("explicit approval is required")
        plan = self._plans.get(pid)
        if not plan or plan["expires_at"] < _now(): raise ProfileConflictError("connection plan expired; create a new plan")
        name, profile, row = self._profile(plan["profile_key"])
        if row.get("identity_token") != plan["_profile_identity"]: raise ProfileConflictError("bound profile changed; create a new plan")
        target = plan["target"]; existing = next((k for k, v in self._connections.items() if v.get("profile_key") == plan["profile_key"] and v.get("target") == target), None); cid = existing or plan.get("_connection_id") or plan["id"]
        key = self.secret_store.get(f"connection-{cid}") or secrets.token_hex(32)
        config_hash = plan["_config_hash"]; config_path = Path(plan["config_path"]) if plan.get("config_path") else None
        original_bytes = config_path.read_bytes() if config_path and config_path.exists() else None
        key_file = self.secret_store.directory / f"{cid}.key"
        if target != "manual":
            info = self._target(target)
            if not info.get("qualified") or info.get("version") != plan.get("target_version") or info.get("path") != plan.get("_target_path"): raise ProfileConflictError("target executable changed; create a new plan")
            if config_path is None: raise OperationFailedError("qualified target has no config path")
            document = self._jsonc_load(config_path)
            providers = document.setdefault("provider", {})
            if not isinstance(providers, dict):
                raise ProfileConflictError("client provider root is not an object")
            provider_id = str(plan["provider_id"])
            if provider_id in providers and providers[provider_id] != self._provider_document(provider_id, plan["model_key"], profile.request_model, plan["base_url"], key_file, getattr(profile, "context", None)):
                raise ProfileConflictError("owned provider namespace already exists and differs")
            key_file = self._key_file(cid, key)
            providers[provider_id] = self._provider_document(provider_id, plan["model_key"], profile.request_model, plan["base_url"], key_file, getattr(profile, "context", None))
            try:
                self._write_client(config_path, document, connection_id=cid, expected_hash=config_hash)
                config_hash = _json_hash(document)
                self.secret_store.set(f"connection-{cid}", key)
            except Exception:
                if original_bytes is None:
                    try: config_path.unlink(missing_ok=True)
                    except OSError: pass
                else:
                    temporary = config_path.with_name(f".{config_path.name}.{secrets.token_hex(8)}.rollback")
                    temporary.write_bytes(original_bytes); os.replace(temporary, config_path)
                if not self.secret_store.get(f"connection-{cid}"):
                    try: key_file.unlink(missing_ok=True)
                    except OSError: pass
                raise
        else:
            self.secret_store.set(f"connection-{cid}", key)
            key_file = self._key_file(cid, key)
        base_url = self._relay_url(name, row.get("identity_token", ""), cid)
        ownership = _json_hash({"id": cid, "profile_identity": row.get("identity_token"), "config_hash": config_hash})
        ownership_fingerprint = _json_hash(self._jsonc_load(config_path).get("provider", {}).get(plan["provider_id"])) if config_path else ownership
        row_out = {"id": cid, "profile_key": plan["profile_key"], "profile": name, "display_name": profile.display_name, "model_id": profile.request_model, "identity_token": row.get("identity_token", ""), "target": target, "provider_id": plan["provider_id"], "model_key": plan["model_key"], "base_url": base_url, "state": "configured", "configured": True, "api_tested": False, "client_verified": False, "reload_required": target != "manual", "readiness": "ready" if row.get("readiness") in {"running", "ready"} else row.get("readiness", "stopped"), "config_path": str(config_path) if config_path else None, "message": "configuration written atomically" if config_path else "manual connection details ready", "created_at": _now(), "fingerprint": ownership, "ownership_fingerprint": ownership_fingerprint, "key_file": str(key_file)}
        self._connections[cid] = row_out; self._save_connections(); self._plans.pop(pid, None)
        return {"connection": self._connection_public(row_out), "existing": existing is not None}

    def _verify(self, cid: str) -> dict[str, Any]:
        conn = self._connections.get(cid)
        if not conn: raise UsageError("connection not found")
        key = self.secret_store.get(f"connection-{cid}")
        if not key: raise OperationFailedError("connection key is unavailable")
        details = self._details(conn["profile_key"], cid)
        # Verify the actual protected chat endpoint, never the unscoped /v1/models
        # endpoint (which could succeed while the bound model is unavailable).
        try:
            _, profile, row = self._profile(conn["profile_key"])
            if row.get("identity_token") != conn.get("identity_token", row.get("identity_token")) or row.get("readiness") not in {"running", "ready"}:
                raise OSError("bound profile is not ready")
            request = urllib.request.Request(details["chat_url"], data=json.dumps({"model": profile.request_model, "messages": [{"role": "user", "content": "ModelMatrix connection verification"}], "max_tokens": 1}).encode("utf-8"), method="POST", headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
            with _opener().open(request, timeout=20) as response:
                if response.status < 200 or response.status >= 300: raise OSError("model endpoint rejected request")
        except Exception as error:
            conn["api_tested"] = False; conn["state"] = "configured"; self._save_connections(); raise OperationFailedError("model API test failed") from error
        conn["api_tested"] = True; conn["state"] = "verified"; conn["message"] = "model API test succeeded"; self._save_connections()
        return {"connection": self._connection_public(conn), "message": conn["message"]}

    def _disconnect(self, cid: str, *, undo: bool = False) -> dict[str, Any]:
        conn = self._connections.get(cid)
        if not conn: raise UsageError("connection not found")
        if conn.get("config_path"):
            path = Path(conn["config_path"]); document = self._jsonc_load(path)
            providers = document.get("provider")
            provider_id = conn.get("provider_id")
            owned = providers.get(provider_id) if isinstance(providers, dict) else None
            if not isinstance(owned, dict) or _json_hash(owned) != conn.get("ownership_fingerprint"):
                raise ProfileConflictError("client configuration is no longer owned by this connection")
            providers.pop(provider_id, None)
            if not providers:
                document.pop("provider", None)
            self._atomic_json(Path(conn["config_path"]), document)
        self.secret_store.delete(f"connection-{cid}")
        try:
            (self.secret_store.directory / f"{cid}.key").unlink(missing_ok=True)
        except OSError as error:
            raise OperationFailedError("connection key file could not be revoked") from error
        conn["configured"] = False; conn["state"] = "disconnected"; conn["api_tested"] = False; conn["client_verified"] = False; conn["message"] = "connection key revoked"
        self._save_connections()
        return {"connection": self._connection_public(conn), "message": "connection disconnected" if not undo else "connection undone"}

    # ---- routes ------------------------------------------------------
    def route(self, method: str, path: str, query: dict[str, Any] | None = None, body: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]] | None:
        method = method.upper(); query = query or {}; body = body or {}
        if method == "GET" and path == "/api/bridge/state": return 200, self._state()
        if method == "POST" and path == "/api/bridge/heartbeat":
            with self._lock:
                self._heartbeat_at = _now(); self._app.update(body.get("app") if isinstance(body.get("app"), dict) else {})
            return 200, {"commands": [], "status_revision": int(self._heartbeat_at)}
        if method == "POST" and path == "/api/bridge/commands":
            return 202, self._command(_nonempty(body.get("type"), "type"), body.get("payload") if isinstance(body.get("payload"), dict) else {})
        if method == "POST" and path == "/api/bridge/commands/result":
            cid = _nonempty(body.get("id"), "id")
            command = self._commands.get(cid)
            if command is None: return 404, {"error": "not_found", "message": "command not found"}
            command.update(status="done" if _bool(body.get("ok")) else "failed", message=body.get("message"), result=body.get("result"), updated_at=_now())
            return 200, {"recorded": True}
        if method == "GET" and path.startswith("/api/bridge/commands/"):
            command = self._commands.get(path.rsplit("/", 1)[-1]); return (200, self._public_command(command)) if command else (404, {"error": "not_found", "message": "command not found"})
        if path == "/api/connect/inference": return None
        if path == "/api/connect/profiles" and method == "GET":
            host = getattr(self.service.configuration, "host", "127.0.0.1"); port = getattr(self.service.configuration, "port", 7700)
            return 200, {"profiles": self._profile_rows(), "relay": {"base_url": f"http://{host}:{port}", "server_root": f"http://{host}:{port}", "listening": True}}
        if path == "/api/connect/targets" and method == "GET": return 200, {"targets": [self._target(t) for t in TARGETS]}
        if path == "/api/connect/list" and method == "GET": return 200, {"connections": [self._connection_public(c) for c in self._connections.values()], "operations": []}
        if path == "/api/connect/details" and method == "GET": return 200, self._details(_nonempty(query.get("profile"), "profile"), query.get("connection"))
        if path == "/api/connect/plan" and method == "POST": return 200, {"plan": self._plan(body), "existing": False}
        if path == "/api/connect/apply" and method == "POST": return 200, self._apply(body)
        if path == "/api/connect/verify" and method == "POST": return 200, self._verify(_nonempty(body.get("id"), "id"))
        if path == "/api/connect/disconnect" and method == "POST":
            if body.get("approved") is not True: raise UsageError("explicit approval is required")
            return 200, self._disconnect(_nonempty(body.get("id"), "id"))
        if path == "/api/connect/undo" and method == "POST":
            if body.get("approved") is not True: raise UsageError("explicit approval is required")
            return 200, self._disconnect(_nonempty(body.get("id"), "id"), undo=True)
        if path == "/api/connect/key" and method == "POST":
            if body.get("approved") is not True: raise UsageError("explicit approval is required")
            key = self.secret_store.get(f"connection-{_nonempty(body.get('id'), 'id')}")
            if not key: raise UsageError("connection key not found")
            return 200, {"key": key, "warning": "Keep this scoped key private; it is returned only after explicit approval."}
        return None

    def proxy(self, handler: Any, path: str) -> bool:
        """Handle streaming ``/api/connect/inference`` or ``/api/remote``.

        Returns True when the handler was fully answered.  Returning False lets
        the caller continue with the original AgentRequestHandler routes.
        """
        parsed_path = urllib.parse.urlsplit(handler.path)
        direct_match = re.fullmatch(r"/api/connect/([^/]+)/v1/(models|chat/completions)", path)
        if direct_match:
            connection_id = urllib.parse.unquote(direct_match.group(1))
            expected = self.secret_store.get(f"connection-{connection_id}")
            supplied = handler.headers.get("Authorization", "")
            if not expected or supplied != f"Bearer {expected}":
                handler._send_json(401, {"error": "unauthorized", "message": "connection key required"}); return True
            conn = self._connections.get(connection_id)
            if not conn:
                handler._send_json(404, {"error": "not_found", "message": "connection not found"}); return True
            expected_method = "GET" if direct_match.group(2) == "models" else "POST"
            if handler.command != expected_method:
                handler._send_json(405, {"error": "method_not_allowed", "message": f"use {expected_method} for this endpoint"}); return True
            try:
                _, _, current = self._profile(conn["profile_key"])
                if current.get("identity_token") != conn.get("identity_token") or current.get("readiness") not in {"running", "ready"}:
                    handler._send_json(409, {"error": "model_unavailable", "message": "bound profile is not ready"}); return True
            except AgentError:
                handler._send_json(409, {"error": "profile_changed", "message": "bound profile is no longer available"}); return True
            if direct_match.group(2) == "models":
                # A scoped connection lists only its bound model, not every
                # other server or controller profile on this computer.
                handler._send_json(200, {"object": "list", "data": [{"id": conn["model_id"], "object": "model", "owned_by": "ModelMatrix"}]})
                return True
            raw_body = handler._read_body() if handler.command == "POST" and hasattr(handler, "_read_body") else b"{}"
            if raw_body is None:
                return True
            try:
                request = json.loads(raw_body or b"{}")
            except (ValueError, TypeError):
                handler._send_json(400, {"error": "invalid_request", "message": "request body must be JSON"}); return True
            if direct_match.group(2) == "chat/completions":
                if not isinstance(request, dict) or request.get("model") != conn.get("model_id"):
                    handler._send_json(409, {"error": "model_mismatch", "message": "requested model is not bound to this connection"}); return True
                try:
                    _, _, current = self._profile(conn["profile_key"])
                    if current.get("identity_token") != conn.get("identity_token") or current.get("readiness") not in {"running", "ready"}:
                        handler._send_json(409, {"error": "model_unavailable", "message": "bound profile is not ready"}); return True
                except AgentError:
                    handler._send_json(409, {"error": "profile_changed", "message": "bound profile is no longer available"}); return True
            current_identity = str(conn.get("identity_token", ""))
            handler.path = "/api/connect/inference?" + urllib.parse.urlencode({"profile": conn["profile_key"], "identity": current_identity, "endpoint": "/v1/chat/completions"})
            try:
                forward_connection(handler, raw_body)
            except AgentError:
                raise
            return True
        if path == "/api/connect/inference":
            query = urllib.parse.parse_qs(parsed_path.query)
            connection_id = (query.get("connection", [""])[-1])
            if connection_id:
                expected = self.secret_store.get(f"connection-{connection_id}")
                supplied = handler.headers.get("Authorization", "")
                if not expected or supplied != f"Bearer {expected}":
                    handler._send_json(401, {"error": "unauthorized", "message": "connection key required"}); return True
            try:
                raw_body = handler._read_body() if hasattr(handler, "_read_body") else b""
                if raw_body is None:
                    return True
                forward_connection(handler, raw_body)
            except AgentError:
                raise
            return True
        if not path.startswith("/api/remote/"): return False
        rest = path[len("/api/remote/"):]; slash = rest.find("/")
        if slash <= 0: handler._send_json(404, {"error": "not_found", "message": "not found"}); return True
        gid, remote_path = rest[:slash], rest[slash:]
        allowed_exact = {("GET", p) for p in REMOTE_GET} | {("POST", p) for p in REMOTE_POST} | {("DELETE", "/api/hf/token")}
        dynamic_hf = bool(re.fullmatch(r"/api/hf/(?:downloads/[^/]+(?:/(?:pause|resume|cancel|start|delete))?|engines/jobs/[^/]+(?:/cancel)?)", remote_path))
        if (handler.command, remote_path) not in allowed_exact and not (dynamic_hf and handler.command in {"GET", "POST", "DELETE"}):
            handler._send_json(404, {"error": "not_found", "message": "remote route not available"}); return True
        query = urllib.parse.parse_qs(parsed_path.query, keep_blank_values=True)
        if any(key.lower() in {"token", "authorization", "password", "secret", "key"} or key not in REMOTE_QUERY_KEYS for key in query):
            handler._send_json(400, {"error": "unsafe_query", "message": "remote query contains an unsupported or credential field"}); return True
        gateway = self._gateways.get(gid)
        if not gateway or not gateway.get("enabled", True): handler._send_json(503, {"error": "gateway_unavailable", "message": "gateway is not enabled"}); return True
        self._ensure_tunnel(gateway)
        base = str(gateway.get("base_url", "")).rstrip("/"); parsed = _url(base)
        outgoing_url = urllib.parse.urlunsplit(parsed._replace(path=parsed.path.rstrip("/") + remote_path, query=urllib.parse.urlencode([(key, value) for key, values in query.items() for value in values]), fragment=""))
        headers = {"User-Agent": "ModelMatrix-Windows-Bridge", "Accept-Encoding": "identity"}; token = self.secret_store.get(f"gateway-{gid}")
        if token: headers["Authorization"] = f"Bearer {token}"
        data = handler._read_body() if handler.command == "POST" else None
        if handler.command == "POST" and data is None:
            return True
        request = urllib.request.Request(outgoing_url, data=data, method=handler.command, headers=headers)
        try:
            response = _opener().open(request, timeout=120 if handler.command == "POST" else 20)
        except urllib.error.HTTPError as error: response = error
        except (OSError, urllib.error.URLError): handler._send_json(502, {"error": "remote_unreachable", "message": "could not reach remote gateway"}); return True
        handler.close_connection = True
        with response:
            handler.send_response(response.status); handler.send_header("Content-Type", response.headers.get("Content-Type", "application/json")); handler.send_header("Cache-Control", "no-store"); handler.send_header("Connection", "close"); handler.end_headers()
            while True:
                chunk = response.read(64 * 1024)
                if not chunk: break
                handler.wfile.write(chunk); handler.wfile.flush()
        return True
