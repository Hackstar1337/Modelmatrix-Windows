"""Dashboard contract tests for the local Windows HF adapter."""
from __future__ import annotations

import os
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
for entry in (ROOT / "RemoteAgent", ROOT / "Windows"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))
os.environ.setdefault("MODELMATRIX_TEST_SECRET_STORE", "1")

from hf_agent import HFAgentError  # noqa: E402
from hf_local import LocalHF, WindowsProfileManager  # noqa: E402


class LocalHFTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        configuration = SimpleNamespace(root=root, profiles_directory=root / "profiles")
        self.local = LocalHF(SimpleNamespace(configuration=configuration))

    def tearDown(self):
        self.temp.cleanup()

    def test_settings_round_trip_has_dashboard_fields(self):
        status, result = self.local.route("GET", "/api/hf/settings", {}, {})
        self.assertEqual(status, 200)
        self.assertIn("models_folder_path", result["settings"])
        status, result = self.local.route("PUT", "/api/hf/settings", {}, {"start_after_download": False})
        self.assertEqual(status, 200)
        self.assertFalse(result["settings"]["start_after_download"])
        self.assertEqual(self.local.route("GET", "/api/hf/settings", {}, {})[1]["settings"]["start_after_download"], False)

    def test_token_is_stored_outside_download_state_and_can_be_deleted(self):
        status, result = self.local.route("POST", "/api/hf/token", {}, {"token": "hf_test_token"})
        self.assertEqual((status, result), (200, {"saved": True}))
        self.assertEqual(self.local._token(), "hf_test_token")
        self.assertNotIn("hf_test_token", self.local.settings_file.read_text(encoding="utf-8") if self.local.settings_file.exists() else "")
        status, result = self.local.route("DELETE", "/api/hf/token", {}, {})
        self.assertEqual((status, result), (200, {"saved": False}))
        self.assertIsNone(self.local._token())

    def test_mlx_is_an_explicit_unsupported_platform_error(self):
        with self.assertRaises(HFAgentError) as raised:
            self.local.route("POST", "/api/hf/engines/plan", {}, {"engine": "mlx-lm", "action": "install"})
        self.assertEqual(raised.exception.code, "unsupported_platform")
        self.assertIn("Apple Silicon", raised.exception.message)

    def test_download_version_is_a_variant_label_not_an_hf_revision(self):
        report = {
            "download_available": True,
            "repo": {"gated": False},
            "notes": [],
            "variants": [{"label": "Q4_K_M", "files": [{"path": "model.Q4_K_M.gguf", "size": 4, "sha256": "a" * 64}],
                          "fit": {"need_bytes": 8}},
                         {"label": "Q8_0", "files": [{"path": "model.Q8_0.gguf", "size": 8, "sha256": "b" * 64}],
                          "fit": {"need_bytes": 12}}],
            "verdict": {"detail": "ok"},
        }
        self.local._repo_report = lambda repo, revision, context: (self.assertEqual(revision, "main") or report)
        captured = {}
        self.local.downloads.add = lambda payload: captured.setdefault("payload", payload) or {"id": "dl-test"}
        result = self.local.route("POST", "/api/hf/downloads", {}, {"name": "owner/model", "version": "Q8_0"})
        self.assertEqual(result[0], 200)
        self.assertEqual(captured["payload"]["job"]["revision"], "main")
        self.assertEqual(captured["payload"]["job"]["files"][0]["path"], "model.Q8_0.gguf")

    def test_route_dispatch_leaves_unowned_paths_for_original_handler(self):
        self.assertIsNone(self.local.route("GET", "/api/status", {}, {}))

    def test_profile_uses_original_uppercase_json_and_service_lifecycle(self):
        server = Path(self.temp.name) / "llama-server.exe"
        server.write_bytes(b"server")
        self.local.finder.llama_server = lambda: (str(server), True)
        name, profile = self.local.profiles._write({
            "repo": "owner/model", "label": "Q4_K_M", "display_name": "model", "folder": self.temp.name,
            "main_file": "model.gguf", "context": 4096, "id": "dl-profile123", "revision": "a" * 40,
        })
        parsed = json.loads((Path(self.temp.name) / "profiles" / f"{name}.json").read_text(encoding="utf-8"))
        self.assertEqual(parsed["RUNTIME"], "llama.cpp")
        self.assertEqual(json.loads(parsed["START_COMMAND_JSON"])[0], str(server))
        self.assertEqual(json.loads(parsed["SERVER_ARGS_JSON"])[0], "-m")
        self.assertNotIn(str(server), json.loads(parsed["SERVER_ARGS_JSON"]))
        self.assertEqual(parsed["REQUEST_MODEL"], profile["REQUEST_MODEL"])

    def test_report_uses_hub_task_and_excludes_projector_from_variants(self):
        sha = "a" * 40
        self.local.facts.payload = lambda _paths: {"budget_bytes": 10_000_000_000, "free_disk_bytes": 10_000_000_000}
        def request(url, *_args, **_kwargs):
            if "/tree/" in url:
                return [{"path": "model.Q4_K_M.gguf", "size": 100, "lfs": {"sha256": "b" * 64}},
                        {"path": "model.mmproj-f16.gguf", "size": 20, "lfs": {"sha256": "c" * 64}}]
            return {"sha": sha, "pipeline_tag": "feature-extraction", "gated": "auto", "cardData": {"license": "mit"}}
        self.local._request_json = request
        report = self.local._repo_report("owner/model", "main", 4096)
        self.assertFalse(report["task"]["chat"])
        self.assertEqual(report["task"]["kind"], "embedding")
        self.assertEqual(len(report["variants"]), 1)
        self.assertEqual(report["projector"]["path"], "model.mmproj-f16.gguf")
        self.assertFalse(report["download_available"])

    def test_engine_plan_prefers_cpu_architecture_and_requires_approval(self):
        digest = "d" * 64
        self.local._request_json = lambda url, **_kwargs: {"tag_name": "b123", "assets": [
            {"name": "llama-b123-bin-win-x64-cuda.zip", "digest": f"sha256:{digest}", "size": 10,
             "browser_download_url": "https://github.com/ggml-org/llama.cpp/releases/download/b123/llama-b123-bin-win-x64-cuda.zip"},
            {"name": "llama-b123-bin-win-x64.zip", "digest": f"sha256:{digest}", "size": 11,
             "browser_download_url": "https://github.com/ggml-org/llama.cpp/releases/download/b123/llama-b123-bin-win-x64.zip"},
        ]}
        self.local.finder.detect = lambda: []
        plan = self.local._plan({"engine": "llama.cpp", "action": "install"})
        self.assertTrue(plan["downloads"][0]["name"].endswith("win-x64.zip"))
        with self.assertRaises(HFAgentError) as raised:
            self.local._run_plan({"plan_id": plan["id"]})
        self.assertEqual(raised.exception.code, "approval_required")
        self.local.installer.start_install = lambda payload: {"state": "succeeded", "payload": payload}
        result = self.local._run_plan({"plan_id": plan["id"], "approved": True})
        self.assertEqual(result["job"]["state"], "succeeded")
        with self.assertRaises(HFAgentError) as raised:
            self.local._run_plan({"plan_id": plan["id"], "approved": True})
        self.assertEqual(raised.exception.code, "plan_expired")

    def test_ollama_download_reuses_shared_kind(self):
        captured = {}
        def add(payload):
            captured["payload"] = payload
            return {"id": "dl-ollama123"}
        self.local.downloads.add = add
        result = self.local._download({"name": "hf.co/owner/model:Q4_K_M", "engine": "ollama"})
        self.assertEqual(result["download"]["id"], "dl-ollama123")
        self.assertEqual(captured["payload"]["job"]["kind"], "ollama")
        self.assertEqual(captured["payload"]["job"]["ollama_model"], "hf.co/owner/model:Q4_K_M")


if __name__ == "__main__":
    unittest.main()
