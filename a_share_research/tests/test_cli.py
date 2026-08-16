from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name)
        self.environment = {
            **os.environ,
            "STOCK_RESEARCH_WORKSPACE": str(self.workspace),
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_cli_json_round_trip(self) -> None:
        run = subprocess.run(
            [sys.executable, "-m", "a_share_research.cli", "demo"],
            check=False,
            capture_output=True,
            text=True,
            env=self.environment,
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        summary = json.loads(run.stdout)
        self.assertFalse(
            any(str(self.workspace) in json.dumps(value) for value in summary.values())
        )

        read = subprocess.run(
            [
                sys.executable,
                "-m",
                "a_share_research.cli",
                "artifact-read",
                "--request-json",
                "-",
            ],
            input=json.dumps(
                {
                    "artifact_id": summary["artifact_id"],
                    "section": "summary",
                }
            ),
            check=False,
            capture_output=True,
            text=True,
            env=self.environment,
        )
        self.assertEqual(read.returncode, 0, read.stderr)
        artifact = json.loads(read.stdout)
        self.assertEqual(artifact["artifact_id"], summary["artifact_id"])
        self.assertFalse(Path(artifact["relative_path"]).is_absolute())

    def test_cli_returns_structured_error_on_wrong_market(self) -> None:
        request = {
            "schema_version": "0.1",
            "market": "US",
            "workflow": "daily_report",
            "decision_at": "2026-08-16T08:30:00+08:00",
            "snapshot": {"selector": "demo"},
        }
        run = subprocess.run(
            [
                sys.executable,
                "-m",
                "a_share_research.cli",
                "run",
                "--request-json",
                "-",
            ],
            input=json.dumps(request),
            check=False,
            capture_output=True,
            text=True,
            env=self.environment,
        )
        self.assertEqual(run.returncode, 2)
        error = json.loads(run.stderr)
        self.assertEqual(error["market"], "CN")
        self.assertIn("permanently bound", error["message"])


if __name__ == "__main__":
    unittest.main()
