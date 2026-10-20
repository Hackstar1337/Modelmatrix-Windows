"""Authenticated, profile-bound inference bridge for Mac-local client connections.

This module does not install/configure clients or expose a new public listener.
It runs behind the remote agent's existing /api authorization guard.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from agent_core import InvalidJSONError, ProfileConflictError, UsageError

_ENDPOINT_METHODS = {
    "/v1/models": "GET",
    "/v1/chat/completions": "POST",
    "/v1/completions": "POST",
    "/v1/responses": "POST",
}
_MAXIMUM_BODY = 64 * 1024 * 1024


def connection_profiles(service: Any) -> dict[str, Any]:
    profiles = service.profiles.load()
    rows = {row["profile"]: row for row in service.status_payload().get("statuses", [])}
    result = []
    for name, profile in sorted(profiles.items()):
        source = next((service.configuration.profiles_directory / (name + suffix)
                       for suffix in (".env", ".json")
                       if (service.configuration.profiles_directory / (name + suffix)).is_file()), None)
        if source is None or source.is_symlink():
            continue
        info = source.stat()
        status = rows.get(name, {})
        raw_context = profile.values.get("CONTEXT_SIZE", "")
        context = int(raw_context) if raw_context.isdigit() and "llama" in profile.runtime.lower() else None
        result.append({
            "profile": name, "display_name": profile.display_name,
            "model_id": profile.request_model, "runtime": profile.runtime,
            "identity_token": f"{info.st_dev}:{info.st_ino}:{info.st_ctime_ns}",
            "readiness": "running" if status.get("ready") else "starting" if status.get("running") else "stopped",
            "context": context,
        })
    return {"profiles": result, "inference_proxy": True}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


def forward_connection(handler: Any, raw_body: bytes) -> None:
    """Stream only the selected configured profile; never use relay fallback."""
    if len(raw_body) > _MAXIMUM_BODY:
        raise UsageError("connection request is too large")
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(handler.path).query)
    name = query.get("profile", [""])[-1]
    endpoint = query.get("endpoint", ["/v1/chat/completions"])[-1]
    identity = query.get("identity", [""])[-1]
    method = _ENDPOINT_METHODS.get(endpoint)
    if method is None:
        raise UsageError("this connection supports models and OpenAI inference endpoints only")
    body: dict[str, Any] = {}
    if method == "POST":
        try:
            body = json.loads(raw_body)
        except (ValueError, UnicodeDecodeError) as error:
            raise InvalidJSONError("invalid JSON") from error
        if not isinstance(body, dict):
            raise UsageError("request body must be an object")
    profile = handler.service.profiles.profile(name)
    metadata = next((row for row in connection_profiles(handler.service)["profiles"] if row["profile"] == name), None)
    if metadata is None or not identity or metadata["identity_token"] != identity:
        raise ProfileConflictError("the bound profile changed; review the connection")
    if method == "POST" and body.get("model") != profile.request_model:
        raise UsageError("the request model does not match the bound profile")
    if metadata["readiness"] != "running":
        raise ProfileConflictError("the bound model is not ready; start it in ModelMatrix")
    base = profile.base_url.rstrip("/")
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme not in ("http", "https") or parsed.username or parsed.password or not parsed.hostname:
        raise UsageError("the profile has an unsupported inference endpoint")
    suffix = endpoint.removeprefix("/v1")
    url = base + suffix
    upstream_model = profile.values.get("UPSTREAM_MODEL")
    if upstream_model and method == "POST":
        body["model"] = upstream_model
    data = json.dumps(body).encode("utf-8") if method == "POST" else None
    headers = {"Accept-Encoding": "identity", "User-Agent": "ModelMatrix-Connection"}
    if method == "POST":
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    # Gateway/controller credentials are never forwarded to the model server.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        response = opener.open(request, timeout=1800)
    except urllib.error.HTTPError as error:
        response = error
    except (OSError, urllib.error.URLError):
        handler._send_json(502, {"error": "model_unreachable", "message": "could not reach the bound model server"})
        return
    handler.close_connection = True
    try:
        with response:
            handler.send_response(response.status)
            handler.send_header("Content-Type", response.headers.get("Content-Type", "application/json"))
            handler.send_header("Cache-Control", "no-store")
            handler.send_header("Connection", "close")
            handler.end_headers()
            while True:
                reader = getattr(response, "read1", None) or getattr(response, "read")
                chunk = reader(64 * 1024)
                if not chunk:
                    break
                handler.wfile.write(chunk)
                handler.wfile.flush()
    except (BrokenPipeError, ConnectionResetError, OSError):
        # Client cancellation closes this upstream request, with no second HTTP reply.
        return
