from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from us_equity_research.cli import main


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
                "market": "US",
                "workflow": "daily_report",
                "decision_at": "2026-08-16T08:30:00-04:00",
                "snapshot": {"selector": "demo"},
            },
            {
                "schema_version": "0.1",
                "market": "US",
                "workflow": "theme_research",
                "decision_at": "2026-08-16T08:30:00-04:00",
                "snapshot": {"selector": "demo"},
                "subject": "automation platform",
            },
            {
                "schema_version": "0.1",
                "market": "US",
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

    def test_filesystem_errors_do_not_expose_absolute_paths(self) -> None:
        private_path = self.workspace / "private" / "missing-request.json"

        run = self._run_cli("run", "--request-json", str(private_path))

        self.assertEqual(run.returncode, 2)
        self.assertEqual(run.stdout, "")
        error = json.loads(run.stderr)
        self.assertEqual(error["market"], "US")
        self.assertEqual(error["error"], "FileNotFoundError")
        self.assertEqual(error["message"], "filesystem operation failed")
        self.assertNotIn(str(private_path), run.stderr)
        self.assertNotIn(str(self.workspace), run.stderr)

    def test_collect_sec_snapshot_cli_forwards_explicit_inputs(self) -> None:
        seed_path = self.workspace / "seed.json"
        market_path = self.workspace / "market.json"
        seed_path.write_text("{}", encoding="utf-8")
        market_path.write_text("{}", encoding="utf-8")
        expected = {
            "schema_version": "0.1",
            "market": "US",
            "status": "published",
            "snapshot_id": "golden-us-1",
        }
        with (
            patch("us_equity_research.cli.collect_sec_snapshot", return_value=expected) as collect,
            patch("us_equity_research.cli._write_json") as write_json,
        ):
            status = main(
                [
                    "--workspace",
                    str(self.workspace),
                    "collect-sec-snapshot",
                    "--seed-json",
                    str(seed_path),
                    "--snapshot-id",
                    "golden-us-1",
                    "--retrieved-at",
                    "2026-08-18T12:00:00+00:00",
                    "--market-json",
                    str(market_path),
                ]
            )

        self.assertEqual(status, 0)
        collect.assert_called_once_with(
            workspace=self.workspace.resolve(),
            seed_path=seed_path,
            snapshot_id="golden-us-1",
            retrieved_at="2026-08-18T12:00:00+00:00",
            market_path=market_path,
        )
        write_json.assert_called_once_with(expected)

    def test_collect_sec_snapshot_requires_sec_user_agent(self) -> None:
        seed_path = self.workspace / "seed.json"
        seed_path.write_text(
            json.dumps(
                {
                    "schema_version": "0.1",
                    "market": "US",
                    "as_of": "2026-08-18T11:00:00+00:00",
                    "themes": [],
                }
            ),
            encoding="utf-8",
        )
        with patch.dict(os.environ, {"SEC_USER_AGENT": ""}):
            run = self._run_cli(
                "collect-sec-snapshot",
                "--seed-json",
                str(seed_path),
                "--snapshot-id",
                "missing-user-agent",
                "--retrieved-at",
                "2026-08-18T12:00:00+00:00",
            )
        self.assertEqual(run.returncode, 2)
        error = json.loads(run.stderr)
        self.assertIn("SEC_USER_AGENT", error["message"])

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
