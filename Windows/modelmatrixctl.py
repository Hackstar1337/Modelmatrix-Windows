"""Windows CLI for the ModelMatrix controller; intentionally stdlib-only."""
from __future__ import annotations
import argparse, json, os, sys
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener

APP = "modelmatrixctl"

def default_root() -> Path:
    return Path(os.environ.get("MODELMATRIX_DATA_ROOT") or (Path(os.environ.get("LOCALAPPDATA", Path.home())) / "ModelMatrix"))

def _secret(root: Path, token_file: Path | None = None) -> str | None:
    if token_file is not None:
        token = token_file.read_text(encoding="utf-8-sig").strip()
        if not token:
            raise ValueError("auth token file is empty")
        if any(char in token for char in "\r\n"):
            raise ValueError("auth token file contains an invalid newline")
        return token
    explicit = os.environ.get("MODELMATRIX_TOKEN")
    if explicit:
        if any(char in explicit for char in "\r\n"):
            raise ValueError("MODELMATRIX_TOKEN contains an invalid newline")
        return explicit
    try:
        from secrets_store import SecretStore  # type: ignore
        store = SecretStore(root)
        for name in ("controller", "controller-token", "auth-token"):
            value = store.get(name)
            if value:
                return value
    except Exception:
        # Linux CI has no DPAPI backend; an authenticated controller will still
        # reject the request and produce a nonzero result rather than a false success.
        pass
    return None

def request(endpoint: str, method: str, path: str, payload: dict | None, root: Path,
            timeout: float = 8.0, auth_token_file: Path | None = None) -> dict:
    query = ""
    if method == "GET" and payload:
        query = "?" + urlencode({k: str(v) for k, v in payload.items() if v is not None})
    body = None if method == "GET" else json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
    headers = {"Accept": "application/json"}
    token = _secret(root, auth_token_file)
    if token:
        headers["Authorization"] = "Bearer " + token
    endpoint_root = endpoint.rstrip("/")
    parsed_endpoint = urlsplit(endpoint_root)
    origin = f"{parsed_endpoint.scheme}://{parsed_endpoint.netloc}" if parsed_endpoint.netloc else endpoint_root
    if method != "GET":
        headers["Origin"] = origin
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = Request(endpoint.rstrip("/") + path + query, data=body, headers=headers, method=method)
    opener = build_opener(HTTPCookieProcessor(CookieJar()))
    try:
        # A controller without a configured Bearer token still requires a
        # same-origin browser session for mutations. Prime a cookie jar from
        # the loopback session route, then send that cookie on the operation.
        # Authenticated controllers use the validated Bearer path instead and
        # never need a token-bearing login request from the CLI.
        if method != "GET" and not token:
            session_req = Request(endpoint_root + "/api/dashboard/session", headers={"Accept": "application/json", "Origin": origin})
            with opener.open(session_req, timeout=timeout):
                pass
        with opener.open(req, timeout=timeout) as response:
            raw = response.read()
            parsed = json.loads(raw.decode("utf-8")) if raw else {}
            result = parsed if isinstance(parsed, dict) else {"value": parsed}
            if result.get("ok") is False or ("error" in result and method != "GET"):
                raise RuntimeError("controller reported an unsuccessful operation")
            return result
    except HTTPError as error:
        raw = error.read().decode("utf-8", "replace")
        try: details = json.loads(raw)
        except json.JSONDecodeError: details = {"error": raw or error.reason}
        safe = json.dumps(details, ensure_ascii=False)
        if token and token in safe:
            safe = safe.replace(token, "[redacted]")
        raise RuntimeError(f"controller returned HTTP {error.code}: {safe}") from error
    except (URLError, TimeoutError, OSError) as error:
        raise RuntimeError(f"controller is unreachable at {endpoint}: {error}") from error

def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=APP, description="Control a local ModelMatrix Windows controller over HTTP.")
    p.add_argument("--endpoint", default=os.environ.get("MODELMATRIX_CONTROLLER_URL", "http://127.0.0.1:7700"))
    p.add_argument("--root", type=Path, default=default_root(), help="per-user data root (default: %%LOCALAPPDATA%%\\ModelMatrix)")
    p.add_argument("--auth-token-file", type=Path, help="read the controller Bearer token from this file")
    subs = p.add_subparsers(dest="command", required=True)
    for name in ("status", "list", "doctor", "stop-all"):
        subs.add_parser(name, help={"status":"show controller status", "list":"list profiles and lifecycle state", "doctor":"run diagnostics", "stop-all":"stop all owned profiles"}[name])
    for name in ("start", "stop", "restart", "switch"):
        q = subs.add_parser(name, help=f"{name} a named profile")
        q.add_argument("profile")
        if name == "stop": q.add_argument("--force", action="store_true")
    u = subs.add_parser("usage", help="show usage overview"); u.add_argument("--range", default="24h")
    b = subs.add_parser("benchmark", help="inspect or start a benchmark"); b.add_argument("action", choices=("status", "start"), nargs="?", default="status")
    o = subs.add_parser("optimize", help="inspect or run safe optimization actions"); o.add_argument("action", choices=("plan", "run"), nargs="?", default="plan")
    for hf_name in ("HF", "hf"):
        h = subs.add_parser(hf_name, help="Hugging Face operations"); h.add_argument("action", choices=("check", "models", "engines"), nargs="?", default="models"); h.add_argument("query", nargs="?")
    c = subs.add_parser("connect", help="inspect connection profiles"); c.add_argument("action", choices=("profiles",), nargs="?", default="profiles")
    return p

def _operation(args: argparse.Namespace) -> tuple[str, str, dict]:
    name = args.command
    if name == "status": return "GET", "/api/status", {}
    if name == "list": return "GET", "/api/status", {}
    if name == "doctor": return "GET", "/api/doctor", {}
    if name == "stop-all": return "POST", "/api/stop-all", {"force": False}
    if name in ("start", "stop", "restart", "switch"):
        return "POST", "/api/" + name, {"profile": args.profile, **({"force": True} if getattr(args, "force", False) else {})}
    if name == "usage": return "GET", "/api/usage/overview", {"range": args.range}
    if name == "benchmark": return ("GET", "/api/benchmark/status", {}) if args.action == "status" else ("POST", "/api/benchmark/start", {})
    if name == "optimize": return ("GET", "/api/optimize/plan", {}) if args.action == "plan" else ("POST", "/api/optimize/run", {})
    if name in ("HF", "hf"):
        routes = {"check": "/api/hf/check", "models": "/api/hf/models", "engines": "/api/hf/engines"}
        return "GET", routes[args.action], ({"query": args.query} if args.query else {})
    if name == "connect": return "GET", "/api/connect/profiles", {}
    raise RuntimeError("unsupported command")

def _human(result: dict) -> None:
    if "error" in result and len(result) <= 2: print(f"error: {result['error']}", file=sys.stderr); return
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))

def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    json_mode = "--json" in argv; dry_run = "--dry-run" in argv
    argv = [item for item in argv if item not in ("--json", "--dry-run")]
    args = parser().parse_args(argv)
    try:
        method, path, payload = _operation(args)
        plan = {"method": method, "path": path, "payload": payload, "endpoint": args.endpoint}
        if dry_run: result = {"ok": True, "dry_run": True, **plan}
        else:
            response = request(args.endpoint, method, path, payload, args.root, auth_token_file=args.auth_token_file)
            if response.get("ok") is False or ("error" in response and method != "GET"):
                raise RuntimeError("controller reported an unsuccessful operation")
            result = {"ok": True, **response}
        if json_mode: print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        else: _human(result)
        return 0
    except (RuntimeError, ValueError, OSError) as error:
        if json_mode: print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        else: print(f"{APP}: {error}", file=sys.stderr)
        return 1

if __name__ == "__main__": sys.exit(main())
