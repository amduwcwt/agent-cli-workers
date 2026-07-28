#!/usr/bin/env python3
import json
from datetime import datetime, timedelta
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "skills" / "agent-cli-workers" / "scripts" / "controller_observation.py"


class ControllerObservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.observation_dir = self.root / "observations"
        self.env = os.environ.copy()
        self.env["AGENT_CLI_WORKERS_OBSERVATION_DIR"] = str(self.observation_dir)

    def tearDown(self):
        self.temp.cleanup()

    def run_cli(self, *args):
        proc = subprocess.run(
            [sys.executable, str(TOOL), *args],
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        payload = json.loads(proc.stdout) if proc.stdout.strip() else None
        return proc, payload

    def begin(self, decision_id, *extra):
        return self.run_cli(
            "begin",
            "--decision-id",
            decision_id,
            "--task-class",
            "review",
            "--predicted-route",
            "grok",
            "--wait-mode",
            "blocking",
            *extra,
        )

    def set_duration(self, decision_id, seconds):
        path = self.observation_dir / "observations.sqlite3"
        with sqlite3.connect(path) as connection:
            requested_text = connection.execute(
                "SELECT requested_at FROM observations WHERE decision_id = ?",
                (decision_id,),
            ).fetchone()[0]
            requested = datetime.fromisoformat(requested_text.replace("Z", "+00:00"))
            verified = (requested + timedelta(seconds=seconds)).isoformat().replace(
                "+00:00", "Z"
            )
            connection.execute(
                "UPDATE observations SET verified_at = ? WHERE decision_id = ?",
                (verified, decision_id),
            )

    def test_begin_finish_records_controller_end_to_end_time(self):
        begin_proc, begun = self.run_cli(
            "begin",
            "--decision-id",
            "decision-001",
            "--task-class",
            "review",
            "--predicted-route",
            "grok",
            "--wait-mode",
            "parallel",
            "--parallel-work-class",
            "implementation",
            "--predicted-direct-seconds",
            "120",
        )

        self.assertEqual(begin_proc.returncode, 0, begin_proc.stderr)
        self.assertEqual(begun["state"], "open")
        self.assertEqual(begun["wait_mode"], "parallel")
        self.assertEqual(begun["parallel_work_class"], "implementation")
        record_path = self.observation_dir / "observations.sqlite3"
        self.assertEqual(stat.S_IMODE(self.observation_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(record_path.stat().st_mode), 0o600)

        finish_proc, finished = self.run_cli(
            "finish",
            "decision-001",
            "--actual-route",
            "grok",
            "--verification",
            "passed",
            "--rework-count",
            "1",
        )

        self.assertEqual(finish_proc.returncode, 0, finish_proc.stderr)
        self.assertEqual(finished["state"], "complete")
        self.assertEqual(finished["verification"], "passed")
        self.assertEqual(finished["rework_count"], 1)
        self.assertGreaterEqual(finished["end_to_end_seconds"], 0)
        with sqlite3.connect(record_path) as connection:
            stored = connection.execute(
                "SELECT requested_at, verified_at FROM observations WHERE decision_id = ?",
                ("decision-001",),
            ).fetchone()
        self.assertIsNotNone(stored[0])
        self.assertIsNotNone(stored[1])

    def test_begin_enforces_parallel_work_contract(self):
        proc, payload = self.run_cli(
            "begin",
            "--decision-id",
            "direct-blocking",
            "--task-class",
            "review",
            "--predicted-route",
            "direct",
            "--wait-mode",
            "blocking",
        )
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(payload["error"], "invalid_wait_mode")

        proc, payload = self.run_cli(
            "begin",
            "--decision-id",
            "parallel-without-work",
            "--task-class",
            "review",
            "--predicted-route",
            "codex",
            "--wait-mode",
            "parallel",
        )
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(payload["error"], "parallel_work_required")

        proc, payload = self.run_cli(
            "begin",
            "--decision-id",
            "blocking-with-work",
            "--task-class",
            "review",
            "--predicted-route",
            "grok",
            "--wait-mode",
            "blocking",
            "--parallel-work-class",
            "testing",
        )
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(payload["error"], "unexpected_parallel_work")

    def test_duplicate_begin_and_finish_are_rejected(self):
        proc, _ = self.begin("decision-duplicate")
        self.assertEqual(proc.returncode, 0, proc.stderr)

        duplicate_proc, duplicate = self.begin("decision-duplicate")
        self.assertEqual(duplicate_proc.returncode, 4)
        self.assertEqual(duplicate["error"], "observation_exists")

        finish_args = (
            "finish",
            "decision-duplicate",
            "--actual-route",
            "grok",
            "--verification",
            "passed",
        )
        finish_proc, _ = self.run_cli(*finish_args)
        self.assertEqual(finish_proc.returncode, 0, finish_proc.stderr)
        repeated_proc, repeated = self.run_cli(*finish_args)
        self.assertEqual(repeated_proc.returncode, 4)
        self.assertEqual(repeated["error"], "observation_already_finished")

    def test_database_schema_contains_only_allowlisted_fields(self):
        proc, _ = self.begin("decision-private")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        path = self.observation_dir / "observations.sqlite3"
        with sqlite3.connect(path) as connection:
            columns = tuple(
                row[1] for row in connection.execute("PRAGMA table_info(observations)")
            )
        self.assertEqual(
            columns,
            (
                "decision_id",
                "requested_at",
                "task_class",
                "predicted_route",
                "predicted_direct_seconds",
                "wait_mode",
                "parallel_work_class",
                "verified_at",
                "actual_route",
                "verification",
                "rework_count",
            ),
        )
        for forbidden in ("prompt", "cwd", "filename", "diff", "session_id"):
            self.assertNotIn(forbidden, columns)

    def test_finish_rejects_tampered_wait_mode_contract(self):
        proc, _ = self.begin("decision-tampered")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        path = self.observation_dir / "observations.sqlite3"
        with sqlite3.connect(path) as connection:
            connection.execute("PRAGMA ignore_check_constraints = ON")
            connection.execute(
                "UPDATE observations SET wait_mode = 'parallel' WHERE decision_id = ?",
                ("decision-tampered",),
            )

        finish_proc, finished = self.run_cli(
            "finish",
            "decision-tampered",
            "--actual-route",
            "grok",
            "--verification",
            "passed",
        )

        self.assertEqual(finish_proc.returncode, 5)
        self.assertEqual(finished["error"], "observation_corrupt")

    def test_report_does_not_echo_tampered_labels(self):
        proc, _ = self.begin("decision-tampered-label")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        path = self.observation_dir / "observations.sqlite3"
        canary = "PRIVATE_OBSERVATION_CANARY"
        with sqlite3.connect(path) as connection:
            connection.execute("PRAGMA ignore_check_constraints = ON")
            connection.execute(
                "UPDATE observations SET task_class = ? WHERE decision_id = ?",
                (canary, "decision-tampered-label"),
            )

        report_proc, report = self.run_cli("report")

        self.assertEqual(report_proc.returncode, 0, report_proc.stderr)
        self.assertNotIn(canary, json.dumps(report))
        self.assertEqual(report["corrupt_observations"], 1)

    def test_report_uses_percentiles_instead_of_average(self):
        for index, duration in enumerate((10, 20, 30, 40), start=1):
            decision_id = f"decision-{index}"
            proc, _ = self.begin(decision_id)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            proc, _ = self.run_cli(
                "finish",
                decision_id,
                "--actual-route",
                "grok",
                "--verification",
                "passed",
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)

            self.set_duration(decision_id, duration)

        report_proc, report = self.run_cli("report", "--since-days", "30")

        self.assertEqual(report_proc.returncode, 0, report_proc.stderr)
        self.assertEqual(report["observations"], 4)
        self.assertEqual(report["completed"], 4)
        self.assertEqual(report["open"], 0)
        self.assertEqual(report["by_task_class"], {"review": 4})
        self.assertEqual(report["end_to_end"]["p50_seconds"], 25)
        self.assertEqual(report["end_to_end"]["p90_seconds"], 37)
        self.assertNotIn("average_seconds", report["end_to_end"])
        self.assertEqual(
            report["duration_by_actual_route"]["grok"],
            {"count": 4, "p50_seconds": 25, "p90_seconds": 37},
        )
        self.assertEqual(
            report["duration_by_wait_mode"]["blocking"],
            {"count": 4, "p50_seconds": 25, "p90_seconds": 37},
        )

    def test_report_calibrates_direct_runtime_predictions(self):
        begin_proc, _ = self.run_cli(
            "begin",
            "--decision-id",
            "decision-direct",
            "--task-class",
            "implementation",
            "--predicted-route",
            "direct",
            "--predicted-direct-seconds",
            "25",
            "--wait-mode",
            "none",
        )
        self.assertEqual(begin_proc.returncode, 0, begin_proc.stderr)
        finish_proc, _ = self.run_cli(
            "finish",
            "decision-direct",
            "--actual-route",
            "direct",
            "--verification",
            "passed",
        )
        self.assertEqual(finish_proc.returncode, 0, finish_proc.stderr)
        self.set_duration("decision-direct", 40)

        report_proc, report = self.run_cli("report")

        self.assertEqual(report_proc.returncode, 0, report_proc.stderr)
        self.assertEqual(
            report["direct_prediction_error"],
            {
                "count": 1,
                "p50_absolute_error_seconds": 15,
                "p90_absolute_error_seconds": 15,
            },
        )

    def test_insecure_observation_directory_is_rejected_without_chmod(self):
        self.observation_dir.mkdir(mode=0o755)
        self.observation_dir.chmod(0o755)

        proc, payload = self.run_cli("report")

        self.assertEqual(proc.returncode, 5)
        self.assertEqual(payload["error"], "insecure_observation_dir")
        self.assertEqual(stat.S_IMODE(self.observation_dir.stat().st_mode), 0o755)


if __name__ == "__main__":
    unittest.main()
