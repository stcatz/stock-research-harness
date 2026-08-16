from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


class PackagingTests(unittest.TestCase):
    def test_wheel_includes_demo_fixture_and_installed_demo_loads(self) -> None:
        project_root = Path(__file__).resolve().parents[1]

        with tempfile.TemporaryDirectory() as temporary:
            tempdir = Path(temporary)
            build_root = tempdir / "project-copy"
            dist_dir = tempdir / "dist"
            install_dir = tempdir / "site"
            workspace = tempdir / "workspace"
            shutil.copytree(
                project_root,
                build_root,
                ignore=shutil.ignore_patterns("__pycache__", "build", "dist", "*.egg-info"),
            )
            dist_dir.mkdir()
            install_dir.mkdir()
            workspace.mkdir()

            subprocess.run(
                ["uv", "build", "--wheel", "--out-dir", str(dist_dir)],
                cwd=build_root,
                check=True,
                capture_output=True,
                text=True,
            )

            wheels = sorted(dist_dir.glob("*.whl"))
            self.assertEqual(len(wheels), 1, wheels)
            wheel_path = wheels[0]

            with zipfile.ZipFile(wheel_path) as archive:
                names = set(archive.namelist())
            self.assertIn("us_equity_research/fixtures/demo_snapshot.json", names)

            subprocess.run(
                [
                    "uv",
                    "pip",
                    "install",
                    "--python",
                    sys.executable,
                    "--target",
                    str(install_dir),
                    str(wheel_path),
                ],
                cwd=tempdir,
                check=True,
                capture_output=True,
                text=True,
            )

            env = {
                **os.environ,
                "PYTHONPATH": str(install_dir),
                "PYTHONNOUSERSITE": "1",
            }
            script = """
from pathlib import Path
from us_equity_research.core.contracts import RunRequest
from us_equity_research.core.snapshot import load_snapshot

request = RunRequest.from_dict(
    {
        "schema_version": "0.1",
        "market": "US",
        "workflow": "daily_report",
        "decision_at": "2026-08-16T08:30:00-04:00",
        "snapshot": {"selector": "demo"},
    }
)
snapshot = load_snapshot(Path.cwd(), request)
print(snapshot.snapshot_id)
print(snapshot.data_mode)
"""
            result = subprocess.run(
                [sys.executable, "-c", script],
                cwd=workspace,
                check=True,
                capture_output=True,
                text=True,
                env=env,
            )

            self.assertEqual(result.stdout.strip().splitlines(), ["us-demo-20260816", "fixture"])


if __name__ == "__main__":
    unittest.main()
