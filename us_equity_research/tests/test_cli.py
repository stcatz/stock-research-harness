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
                "snapshot": {"selector": "demo"},
                "subject": "automation platform",
            },
            {
                "schema_version": "0.1",
                "workflow": "stock_research",
                "decision_at": "2026-08-16T08:30:00-04:00",
                "snapshot": {"selector": "demo"},
                "symbol": "DEMOA",
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
                self.assertEqual(payload["status"], "completed")
                self.assertEqual(payload["workflow"], request["workflow"])
                self.assertEqual(payload["data_mode"], "fixture")

    def test_init_and_doctor_emit_canonical_json(self) -> None:
        initialized = self._run_cli("init")
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        init_payload = json.loads(initialized.stdout)
        self.assertEqual(init_payload["schema_version"], "0.1")
        self.assertEqual(init_payload["market"], "US")
        self.assertEqual(init_payload["status"], "initialized")
        self.assertEqual(init_payload["database"], "data/us_stock_research.sqlite3")
        self.assertFalse(Path(init_payload["database"]).is_absolute())
        self.assertFalse(init_payload["broker_capability"])

        checked = self._run_cli("doctor")
        self.assertEqual(checked.returncode, 0, checked.stderr)
        doctor_payload = json.loads(checked.stdout)
        self.assertEqual(doctor_payload["market"], "US")
        self.assertEqual(doctor_payload["status"], "ok")
        self.assertTrue(doctor_payload["database_initialized"])
        self.assertTrue(doctor_payload["demo_fixture_available"])
        self.assertFalse(doctor_payload["broker_capability"])

    def test_demo_emits_canonical_daily_report_request(self) -> None:
        run = self._run_cli("demo")
        self.assertEqual(run.returncode, 0, run.stderr)
        payload = json.loads(run.stdout)
        self.assertEqual(payload["market"], "US")
        self.assertEqual(payload["workflow"], "daily_report")
        self.assertEqual(payload["data_mode"], "fixture")
        self.assertEqual(payload["counts"], {"observe": 1, "continue_research": 1, "exclude": 1})

    def test_artifact_read_never_returns_absolute_path(self) -> None:
        created = self._run_cli("demo")
        self.assertEqual(created.returncode, 0, created.stderr)
        artifact_id = json.loads(created.stdout)["artifact_id"]

        run = self._run_cli(
            "artifact-read",
            "--request-json",
            "-",
            input_text=json.dumps(
                {"artifact_id": artifact_id, "section": "summary", "max_chars": 500}
            ),
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        payload = json.loads(run.stdout)
        self.assertEqual(payload["market"], "US")
        self.assertEqual(payload["artifact_id"], artifact_id)
        self.assertEqual(payload["section"], "summary")
        self.assertFalse(Path(payload["relative_path"]).is_absolute())
        self.assertNotIn(str(self.workspace), run.stdout)

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
            [
                sys.executable,
                "-m",
                "us_equity_research.cli",
                "--workspace",
                str(self.workspace),
                *args,
            ],
            input=input_text,
            check=False,
            capture_output=True,
            text=True,
            cwd=self.package_root,
        )


if __name__ == "__main__":
    unittest.main()
