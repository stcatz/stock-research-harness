from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from a_share_research.cli import main


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

    def test_collect_snapshot_dispatches_seed_and_workspace(self) -> None:
        seed_path = self.workspace / "seed.json"
        seed = {"schema_version": "0.1", "market": "CN", "evidence": [], "themes": []}
        seed_path.write_text(json.dumps(seed), encoding="utf-8")
        result = Mock()
        result.to_dict.return_value = {
            "schema_version": "0.1",
            "market": "CN",
            "snapshot_id": "cn-test",
        }
        stdout = io.StringIO()

        with (
            patch("a_share_research.cli.collect_cn_snapshot", return_value=result) as collect,
            redirect_stdout(stdout),
        ):
            exit_code = main(
                [
                    "--workspace",
                    str(self.workspace),
                    "collect-snapshot",
                    "--seed-json",
                    str(seed_path),
                    "--snapshot-id",
                    "cn-test",
                ]
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["snapshot_id"], "cn-test")
        call = collect.call_args
        self.assertEqual(call.args, (seed,))
        self.assertEqual(call.kwargs["workspace"], self.workspace.resolve())
        self.assertEqual(call.kwargs["snapshot_id"], "cn-test")
        self.assertEqual(call.kwargs["provider_kind"], "baostock")
        self.assertNotIn("retrieved_at", call.kwargs)

    def test_collect_snapshot_dispatches_explicit_hithink_provider_and_raw_store(self) -> None:
        seed_path = self.workspace / "seed.json"
        seed_path.write_text(
            json.dumps({"schema_version": "0.1", "market": "CN", "evidence": [], "themes": []}),
            encoding="utf-8",
        )
        raw_root = self.workspace / "raw"
        result = Mock()
        result.to_dict.return_value = {"snapshot_id": "cn-hithink"}
        stdout = io.StringIO()

        with (
            patch("a_share_research.cli.collect_cn_snapshot", return_value=result) as collect,
            redirect_stdout(stdout),
        ):
            exit_code = main(
                [
                    "--workspace",
                    str(self.workspace),
                    "collect-snapshot",
                    "--seed-json",
                    str(seed_path),
                    "--snapshot-id",
                    "cn-hithink",
                    "--provider",
                    "hithink",
                    "--raw-store-root",
                    str(raw_root),
                ]
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(collect.call_args.kwargs["provider_kind"], "hithink")
        self.assertEqual(collect.call_args.kwargs["raw_store_root"], raw_root)

    def test_provider_probe_is_an_explicit_network_command_with_safe_dispatch(self) -> None:
        result = {
            "schema_version": "0.1",
            "market": "CN",
            "provider": "hithink-financial-api",
            "status": "ok",
            "request_id": "request-123",
        }
        stdout = io.StringIO()
        raw_root = self.workspace / "raw"
        with (
            patch("a_share_research.cli.probe_hithink_provider", return_value=result) as probe,
            redirect_stdout(stdout),
        ):
            exit_code = main(
                [
                    "--workspace",
                    str(self.workspace),
                    "provider-probe",
                    "--provider",
                    "hithink",
                    "--symbol",
                    "600000.SH",
                    "--raw-store-root",
                    str(raw_root),
                ]
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), result)
        probe.assert_called_once_with(
            "600000.SH",
            workspace=self.workspace.resolve(),
            raw_store_root=raw_root,
        )

    def test_doctor_remains_offline_when_hithink_is_configured(self) -> None:
        stdout = io.StringIO()
        with (
            patch.dict(os.environ, {"HITHINK_FINANCE_API_KEY": "configured-secret"}),
            patch("a_share_research.cli.probe_hithink_provider") as probe,
            redirect_stdout(stdout),
        ):
            exit_code = main(["--workspace", str(self.workspace), "doctor"])

        self.assertEqual(exit_code, 0)
        probe.assert_not_called()

    def test_collect_snapshot_rejects_public_retrieved_at_override(self) -> None:
        seed_path = self.workspace / "seed.json"
        seed_path.write_text(
            json.dumps({"schema_version": "0.1", "market": "CN", "evidence": [], "themes": []}),
            encoding="utf-8",
        )
        stderr = io.StringIO()

        with (
            self.assertRaises(SystemExit) as error,
            patch("sys.stderr", stderr),
        ):
            main(
                [
                    "--workspace",
                    str(self.workspace),
                    "collect-snapshot",
                    "--seed-json",
                    str(seed_path),
                    "--snapshot-id",
                    "cn-test",
                    "--retrieved-at",
                    "2026-08-17T18:00:00+08:00",
                ]
            )

        self.assertEqual(error.exception.code, 2)
        self.assertIn("unrecognized arguments: --retrieved-at", stderr.getvalue())

    def test_cli_redacts_filesystem_error_paths(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()

        with (
            patch(
                "a_share_research.cli.collect_cn_snapshot",
                side_effect=OSError(f"cannot write {self.workspace}/private/snapshot.json"),
            ),
            redirect_stdout(stdout),
            patch("sys.stderr", stderr),
        ):
            exit_code = main(
                [
                    "--workspace",
                    str(self.workspace),
                    "collect-snapshot",
                    "--seed-json",
                    str(self.workspace / "missing-seed.json"),
                    "--snapshot-id",
                    "cn-test",
                ]
            )

        self.assertEqual(exit_code, 2)
        self.assertEqual(stdout.getvalue(), "")
        error = json.loads(stderr.getvalue())
        self.assertEqual(error["message"], "filesystem operation failed")
        self.assertNotIn(str(self.workspace), stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
