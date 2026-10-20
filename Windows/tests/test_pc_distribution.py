from __future__ import annotations

import os
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(os.name == "nt", "Windows distribution regression")
class PCDistributionRegressionTests(unittest.TestCase):
    def test_build_compile_step_handles_single_python_path_and_native_quoting(self) -> None:
        script = (ROOT / "Scripts" / "build-windows.ps1").read_text(encoding="utf-8")
        self.assertNotIn("$python[0]", script)
        self.assertIn("$pythonCommand = @($python)", script)
        self.assertIn("ModelMatrix-compile-", script)
        self.assertIn("Set-Content -LiteralPath $compileScript", script)
        self.assertIn("compile(path.read_text(encoding=\"utf-8\")", script)
        self.assertIn("Remove-Item -LiteralPath $compileScript", script)


if __name__ == "__main__":
    unittest.main()
