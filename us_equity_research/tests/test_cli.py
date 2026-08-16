from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name)
        self.package_root = Path(__file__).resolve().parents[1]

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_run_accepts_all_three_workflows(self) -> None:
        requests = [
            {
                "schema_version": "0.1",
                "workflow": "daily_report",
                "decision_at": "2026-08-16T08:30:00-04:00",
                "snapshot": {"selector": "demo"},
            },
            {
                "schema_version": "0.1",
                "workflow": "theme_research",
                "decision_at": "2026-08-16T08:30:00-04:00",
                "snapshot": {"selector": "latest"},
                "subject": "cloud",
            },
            {
                "schema_version": "0.1",
                "workflow": "stock_research",
                "decision_at": "2026-08-16T08:30:00-04:00",
                "snapshot": {"selector": "id", "snapshot_id": "us-example-20260816"},
                "symbol": "AAPL",
            },
        ]

        for request in requests:
            with self.subTest(workflow=request["workflow"]):
                run = self._run_cli("run", "--request-json", "-", input_text=json.dumps(request))
                self.assertEqual(run.returncode, 0, run.stderr)
                self.assertEqual(run.stderr, "")
                self.assertEqual(run.stdout.strip().count("\n"), 0)
                payload = json.loads(run.stdout)
                self.assertEqual(payload["schema_version"], "0.1")
                self.assertEqual(payload["market"], "US")
                self.assertEqual(payload["status"], "not_implemented")
                self.assertEqual(payload["request"]["workflow"], request["workflow"])

    def test_init_and_doctor_emit_canonical_json(self) -> None:
        for command in ("init", "doctor"):
            with self.subTest(command=command):
                run = self._run_cli(command)
                self.assertEqual(run.returncode, 0, run.stderr)
                self.assertEqual(run.stderr, "")
                self.assertEqual(run.stdout.strip().count("\n"), 0)
                payload = json.loads(run.stdout)
                self.assertEqual(payload["schema_version"], "0.1")
                self.assertEqual(payload["market"], "US")
                self.assertEqual(payload["command"], command)

    def test_demo_emits_canonical_daily_report_request(self) -> None:
        run = self._run_cli("demo")
        self.assertEqual(run.returncode, 0, run.stderr)
        payload = json.loads(run.stdout)
        self.assertEqual(payload["market"], "US")
        self.assertEqual(payload["request"]["workflow"], "daily_report")
        self.assertEqual(payload["request"]["snapshot"]["selector"], "demo")

    def test_artifact_read_never_returns_absolute_path(self) -> None:
        run = self._run_cli(
            "artifact-read",
            "--request-json",
            "-",
            input_text=json.dumps({"artifact_id": "us-artifact-demo", "section": "summary"}),
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        payload = json.loads(run.stdout)
        self.assertEqual(payload["status"], "not_implemented")
        if "relative_path" in payload:
            self.assertFalse(Path(payload["relative_path"]).is_absolute())

    def test_cli_errors_are_structured_and_use_market_us(self) -> None:
        request = {
            "schema_version": "0.1",
            "market": "CN",
            "workflow": "daily_report",
            "decision_at": "2026-08-16T08:30:00-04:00",
            "snapshot": {"selector": "demo"},
        }
        run = self._run_cli("run", "--request-json", "-", input_text=json.dumps(request))
        self.assertEqual(run.returncode, 2)
        self.assertEqual(run.stdout, "")
        error = json.loads(run.stderr)
        self.assertEqual(error["market"], "US")
        self.assertIn("permanently bound", error["message"])

    def _run_cli(
        self, *args: str, input_text: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "us_equity_research.cli", *args],
            input=input_text,
            check=False,
            capture_output=True,
            text=True,
            cwd=self.package_root,
        )


if __name__ == "__main__":
    unittest.main()
