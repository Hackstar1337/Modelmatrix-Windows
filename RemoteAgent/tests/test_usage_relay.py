"""Usage relay: token/timing extraction, client identity, tiers and the store."""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[1]
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from modelmatrix_agent import (  # noqa: E402
    UsageAnalyzer,
    UsageStore,
    usage_client_identity,
    usage_host_header_rejected,
    usage_is_cloud_model,
)


def _sse(payload: dict) -> bytes:
    return b"data: " + json.dumps(payload).encode() + b"\n\n"


class UsageAnalyzerTests(unittest.TestCase):
    def test_openai_stream_with_usage_chunk(self) -> None:
        analyzer = UsageAnalyzer("text/event-stream", time.time())
        analyzer.feed(_sse({"model": "m", "choices": [{"delta": {"content": "Hel"}}]}))
        analyzer.feed(_sse({"model": "m", "choices": [{"delta": {"content": "lo"}}]}))
        analyzer.feed(_sse({"model": "m", "choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 2}}))
        analyzer.feed(b"data: [DONE]\n\n")
        result = analyzer.finish()
        self.assertEqual(result["prompt_tokens"], 9)
        self.assertEqual(result["completion_tokens"], 2)
        self.assertEqual(result["tokens_estimated"], 0)
        self.assertIsNotNone(result["ttft_ms"])
        self.assertEqual(result["stream"], 1)

    def test_stream_without_usage_is_estimated_from_chunks(self) -> None:
        analyzer = UsageAnalyzer("text/event-stream; charset=utf-8", time.time())
        payload = b"".join(_sse({"choices": [{"delta": {"content": "x"}}]}) for _ in range(5))
        analyzer.feed(payload[:7])
        analyzer.feed(payload[7:])
        result = analyzer.finish()
        self.assertEqual(result["completion_tokens"], 5)
        self.assertEqual(result["tokens_estimated"], 1)

    def test_ollama_ndjson_uses_eval_counts(self) -> None:
        analyzer = UsageAnalyzer("application/x-ndjson", time.time())
        analyzer.feed(json.dumps({"model": "gemma3:270m", "message": {"content": "Hi"}, "done": False}).encode() + b"\n")
        analyzer.feed(json.dumps({
            "model": "gemma3:270m", "done": True, "prompt_eval_count": 14,
            "eval_count": 40, "eval_duration": 500_000_000,
        }).encode() + b"\n")
        result = analyzer.finish()
        self.assertEqual(result["prompt_tokens"], 14)
        self.assertEqual(result["completion_tokens"], 40)
        self.assertAlmostEqual(result["tokens_per_second"], 80.0)
        self.assertEqual(result["served_model"], "gemma3:270m")

    def test_plain_json_response(self) -> None:
        analyzer = UsageAnalyzer("application/json", time.time())
        body = json.dumps({"model": "m", "usage": {"prompt_tokens": 3, "completion_tokens": 4}}).encode()
        analyzer.feed(body[:10])
        analyzer.feed(body[10:])
        result = analyzer.finish()
        self.assertEqual((result["prompt_tokens"], result["completion_tokens"]), (3, 4))
        self.assertEqual(result["stream"], 0)

    def test_garbage_is_ignored(self) -> None:
        analyzer = UsageAnalyzer("text/event-stream", time.time())
        analyzer.feed(b"data: {not json\n\n: comment\n\n")
        result = analyzer.finish()
        self.assertIsNone(result["completion_tokens"])


class UsageIdentityTests(unittest.TestCase):
    def test_explicit_header_wins(self) -> None:
        self.assertEqual(usage_client_identity({"X-Client-Name": "Cursor", "User-Agent": "curl/8"}, "10.0.0.2"),
                         ("cursor", "Cursor"))

    def test_user_agent_product(self) -> None:
        client_id, label = usage_client_identity({"User-Agent": "OpenWebUI/0.6 (Linux)"}, "10.0.0.2")
        self.assertEqual(label, "OpenWebUI")
        self.assertEqual(client_id, "openwebui@10.0.0.2")

    def test_address_fallback(self) -> None:
        self.assertEqual(usage_client_identity({}, "10.0.0.9"), ("client@10.0.0.9", "10.0.0.9"))

    def test_rebinding_host_rejected_on_loopback_relay(self) -> None:
        self.assertTrue(usage_host_header_rejected("evil.example:7701", "127.0.0.1"))
        self.assertFalse(usage_host_header_rejected("127.0.0.1:7701", "127.0.0.1"))
        self.assertFalse(usage_host_header_rejected("localhost:7701", "127.0.0.1"))
        self.assertFalse(usage_host_header_rejected("[::1]:7701", "::1"))
        self.assertFalse(usage_host_header_rejected(None, "127.0.0.1"))
        self.assertFalse(usage_host_header_rejected("gpu.tailnet.ts.net:7701", "100.64.0.7"))

    def test_cloud_tier(self) -> None:
        self.assertTrue(usage_is_cloud_model("gpt-oss:120b-cloud"))
        self.assertTrue(usage_is_cloud_model("kimi-k2:1t-cloud"))
        self.assertFalse(usage_is_cloud_model("gemma3:270m"))
        self.assertFalse(usage_is_cloud_model("cloudy-model"))
        self.assertFalse(usage_is_cloud_model(None))


class UsageStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.store = UsageStore(Path(self.directory.name) / "usage.sqlite3")

    def tearDown(self) -> None:
        self.store._db.close()  # noqa: SLF001 - release SQLite's Windows file handle first.
        self.directory.cleanup()

    def _row(self, **overrides) -> dict:
        row = {
            "started_at": time.time(), "client_id": "cursor", "client_label": "Cursor", "model": "qwen",
            "tier": "local", "status": 200, "prompt_tokens": 10, "completion_tokens": 20,
            "duration_ms": 1000.0, "ttft_ms": 100.0, "tokens_per_second": 25.0,
        }
        row.update(overrides)
        return row

    def test_summary_models_clients_and_rename(self) -> None:
        self.store.record(self._row())
        self.store.record(self._row(model="gpt-oss:120b-cloud", tier="cloud", tokens_per_second=50.0))
        self.store.record(self._row(status=500, client_id="script", client_label="python-requests"))
        summary = self.store.summary("1h")
        self.assertEqual(summary["requests"], 3)
        self.assertEqual(summary["errors"], 1)
        self.assertEqual(summary["cloud_requests"], 1)
        self.assertEqual(summary["total_tokens"], 90)
        self.assertEqual(self.store.summary("1h", "cloud")["requests"], 1)
        self.assertEqual(self.store.models("1h")[0]["model"], "qwen")
        self.store.rename_client("cursor", "My Editor")
        names = {client["client_id"]: client["name"] for client in self.store.clients("1h")}
        self.assertEqual(names["cursor"], "My Editor")

    def test_retention_prunes_old_rows(self) -> None:
        self.store.record(self._row(started_at=time.time() - 40 * 86400))
        self.store.record(self._row())
        self.store.set_setting("retention_days", "30")
        self.store.prune()
        self.assertEqual(self.store.count(), 1)
        self.assertEqual(self.store.summary("all")["requests"], 1)


if __name__ == "__main__":
    unittest.main()
