"""Regression tests for the Windows distribution layer.

These tests are intentionally runnable on non-Windows hosts: PowerShell files are
validated as source contracts while CLI/tray Python code is imported and exercised.
The Windows workflow additionally runs the same suite on native PowerShell.
"""
from __future__ import annotations

import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "Windows") not in sys.path:
    sys.path.insert(0, str(ROOT / "Windows"))
import modelmatrixctl  # noqa: E402
import tray  # noqa: E402


class DistributionScriptTests(unittest.TestCase):
    def read(self, name: str) -> str:
        return (ROOT / "Scripts" / name).read_text(encoding="utf-8")

    def test_install_and_package_preserve_dashboard_dist_parent(self):
        install = self.read("install-windows.ps1")
        package = self.read("package-windows.ps1")
        self.assertIn("Join-Path $InstallRoot 'Dashboard'", install)
        self.assertIn("Dashboard\\dist\\index.html", install)
        self.assertIn("$destination = Join-Path $stage $relative", package)
        self.assertIn("'LICENSE'", package)
        self.assertIn("'Windows\\README.md'", package)

    def test_build_accepts_higher_node_major_and_requires_static_build_for_skip(self):
        build = self.read("build-windows.ps1")
        self.assertIn("$nodeNumeric.Major -lt 22", build)
        self.assertIn("$nodeNumeric.Major -eq 22 -and $nodeNumeric.Minor -lt 12", build)
        self.assertIn("Dashboard/dist/index.html is required with -SkipDashboard", build)
        self.assertIn("corepack", build)

    def test_stop_never_uses_automatic_pid_variable_or_pid_only_kill(self):
        stop = self.read("stop-windows.ps1")
        self.assertNotIn("$pid =", stop.lower())
        for marker in ("record.executable", "record.controller", "record.started_utc", "Get-CimInstance Win32_Process", "CloseMainWindow"):
            self.assertIn(marker, stop)
        self.assertNotIn("Stop-Process -Id $pid", stop)

    def test_verify_escapes_uri_colon_and_start_does_not_unconditionally_pass_relay(self):
        verify = self.read("verify-windows.ps1")
        start = self.read("start-windows.ps1")
        self.assertIn("${uri}:", verify)
        self.assertIn("--relay-port", start)
        self.assertIn("$help -match", start)
        self.assertIn("ConvertTo-Json", start)

    def test_tray_uses_pointer_sized_prototypes_and_statuses(self):
        source = (ROOT / "Windows" / "tray.py").read_text(encoding="utf-8")
        self.assertIn("UINT_PTR = ctypes.c_size_t", source)
        self.assertIn("CreateWindowExW.restype = wintypes.HWND", source)
        self.assertIn('status.get("statuses")', source)
        self.assertIn('row.get("profile")', source)


class CLIDistributionTests(unittest.TestCase):
    def test_dry_run_has_no_request_side_effect(self):
        with patch.object(modelmatrixctl, "request", side_effect=AssertionError("dry-run connected")):
            out = io.StringIO()
            with patch("sys.stdout", out):
                self.assertEqual(modelmatrixctl.main(["--dry-run", "--json", "start", "demo"]), 0)
        payload = json.loads(out.getvalue())
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["path"], "/api/start")

    def test_dry_run_preserves_useful_commands(self):
        with patch.object(modelmatrixctl, "request", side_effect=AssertionError("dry-run connected")):
            out = io.StringIO()
            with patch("sys.stdout", out):
                self.assertEqual(modelmatrixctl.main(["--dry-run", "--json", "usage", "--range", "7d"]), 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["path"], "/api/usage/overview")
        self.assertEqual(payload["payload"], {"range": "7d"})


if __name__ == "__main__":
    unittest.main()
