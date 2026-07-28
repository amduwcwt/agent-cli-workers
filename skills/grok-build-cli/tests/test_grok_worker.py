#!/usr/bin/env python3
import json
import os
from pathlib import Path
import runpy
import stat
import subprocess
import sys
import tempfile
import time
import unittest


SKILL_DIR = Path(__file__).resolve().parents[1]
RUNNER = SKILL_DIR / "scripts" / "grok_worker.py"
RUNNER_API = runpy.run_path(str(RUNNER))


FAKE_GROK = r'''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys
import time

args = sys.argv[1:]
prompt_path = Path(args[args.index("--prompt-file") + 1])
prompt = prompt_path.read_text(encoding="utf-8")
task_prompt = prompt.split("\n\n--- agent-cli-workers completion contract ---", 1)[0].rstrip()
resume = args[args.index("--resume") + 1] if "--resume" in args else None
log_path = os.environ.get("FAKE_GROK_LOG")
if log_path:
    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"args": args, "prompt": prompt}) + "\n")

for line in prompt.splitlines():
    if line.startswith("SLEEP="):
        time.sleep(float(line.split("=", 1)[1]))

payload = {
    "text": "fake result: " + task_prompt.splitlines()[-1],
    "thought": "PRIVATE_GROK_THOUGHT_MUST_NOT_LEAK",
    "stopReason": "EndTurn",
    "sessionId": resume or "sess-fake-001",
    "requestId": "req-fake-001",
    "usage": {"input_tokens": 10, "output_tokens": 5},
}
print(json.dumps(payload))
if "FAIL" in prompt:
    sys.exit(7)
'''


class GrokWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self.fake_grok = self.bin_dir / "grok"
        self.fake_grok.write_text(FAKE_GROK, encoding="utf-8")
        self.fake_grok.chmod(self.fake_grok.stat().st_mode | stat.S_IXUSR)
        self.state_dir = self.root / "state"
        self.state_dir.mkdir(mode=0o700)
        self.work_dir = self.root / "work"
        self.work_dir.mkdir()
        self.log_path = self.root / "grok.log"
        self.env = os.environ.copy()
        self.env["PATH"] = str(self.bin_dir) + os.pathsep + self.env.get("PATH", "")
        self.env["GROK_BUILD_CLI_STATE_DIR"] = str(self.state_dir)
        self.env["FAKE_GROK_LOG"] = str(self.log_path)

    def tearDown(self):
        if RUNNER.exists() and self.state_dir.exists():
            for metadata_path in self.state_dir.glob("*/metadata.json"):
                try:
                    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if metadata.get("state") in {"queued", "running"}:
                    self.run_cli("cancel", metadata["worker_id"], "--timeout", "1")
        self.temp.cleanup()

    def run_cli(self, *args, timeout=10):
        proc = subprocess.run(
            [sys.executable, str(RUNNER), *args],
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
        payload = None
        if proc.stdout.strip():
            payload = json.loads(proc.stdout)
        return proc, payload

    def write_prompt(self, name, content):
        path = self.root / name
        path.write_text(content, encoding="utf-8")
        return path

    def wait_state(self, worker_id, expected, timeout=8):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            proc, payload = self.run_cli("status", worker_id)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            last = payload
            if payload["state"] in expected:
                return payload
            time.sleep(0.05)
        self.fail(f"worker {worker_id} did not reach {expected}; last={last}")

    def spawn(self, prompt_path, *extra):
        return self.run_cli(
            "spawn",
            "--cwd",
            str(self.work_dir),
            "--prompt-file",
            str(prompt_path),
            *extra,
        )

    def test_spawn_returns_before_grok_finishes_and_keeps_prompt_private(self):
        secret = "secret-marker-must-not-leak"
        prompt = self.write_prompt("task.md", f"SLEEP=2\n{secret}")

        started = time.monotonic()
        proc, spawned = self.spawn(prompt)
        elapsed = time.monotonic() - started

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertLess(elapsed, 1.5)
        self.assertIn(spawned["state"], {"queued", "running"})
        worker_id = spawned["worker_id"]
        metadata_path = self.state_dir / worker_id / "metadata.json"
        self.assertNotIn(secret, metadata_path.read_text(encoding="utf-8"))

        wrapper_pid = spawned["wrapper_pid"]
        command = subprocess.check_output(
            ["ps", "-p", str(wrapper_pid), "-o", "command="], text=True
        )
        self.assertNotIn(secret, command)

        finished = self.wait_state(worker_id, {"succeeded"})
        self.assertEqual(finished["exit_code"], 0)
        self.assertFalse((self.state_dir / worker_id / "prompt.md").exists())
        mode = stat.S_IMODE(self.state_dir.stat().st_mode)
        self.assertEqual(mode, 0o700)

        collect_proc, collected = self.run_cli("collect", worker_id)
        self.assertEqual(collect_proc.returncode, 0, collect_proc.stderr)
        self.assertEqual(collected["result"]["sessionId"], "sess-fake-001")
        self.assertEqual(collected["session_id"], "sess-fake-001")
        self.assertNotIn("PRIVATE_GROK_THOUGHT_MUST_NOT_LEAK", json.dumps(collected))

    def test_legacy_state_dir_wins_over_ambient_unified_state(self):
        ambient_state = self.root / "ambient-unified-state"
        self.env["AGENT_CLI_WORKERS_STATE_DIR"] = str(ambient_state)
        prompt = self.write_prompt("legacy-state.md", "legacy state")

        proc, spawned = self.spawn(prompt)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.wait_state(spawned["worker_id"], {"succeeded"})
        self.assertTrue((self.state_dir / spawned["worker_id"]).is_dir())
        self.assertFalse((ambient_state / spawned["worker_id"]).exists())

    def test_prompt_value_is_not_rewritten_as_a_legacy_option(self):
        self.assertEqual(
            RUNNER_API["mapped_args"](
                ["spawn", "--cwd", str(self.work_dir), "--prompt", "--grok-binary"]
            ),
            [
                "spawn",
                "--agent",
                "grok",
                "--cwd",
                str(self.work_dir),
                "--prompt",
                "--grok-binary",
            ],
        )

    def test_legacy_list_filters_out_codex_workers(self):
        worker_id = "codex-worker"
        worker_dir = self.state_dir / worker_id
        worker_dir.mkdir(parents=True)
        metadata = {
            "schema_version": 2,
            "worker_id": worker_id,
            "agent": "codex",
            "state": "succeeded",
            "cwd": str(self.work_dir),
            "result_path": str(worker_dir / "stdout.jsonl"),
            "stderr_path": str(worker_dir / "stderr.log"),
        }
        (worker_dir / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")

        proc, listing = self.run_cli("list")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(listing["workers"], [])

    def test_legacy_entry_rejects_explicit_agent_override(self):
        with self.assertRaises(SystemExit):
            RUNNER_API["mapped_args"](["spawn", "--agent", "codex"])

    def test_failure_preserves_exit_code_and_result(self):
        prompt = self.write_prompt("fail.md", "FAIL")
        proc, spawned = self.spawn(prompt)
        self.assertEqual(proc.returncode, 0, proc.stderr)

        finished = self.wait_state(spawned["worker_id"], {"failed"})
        self.assertEqual(finished["exit_code"], 7)

        collect_proc, collected = self.run_cli("collect", spawned["worker_id"])
        self.assertEqual(collect_proc.returncode, 1)
        self.assertEqual(collected["state"], "failed")
        self.assertEqual(collected["exit_code"], 7)
        self.assertEqual(collected["result"]["text"], "fake result: FAIL")

    def test_legacy_entry_forwards_deadline(self):
        prompt = self.write_prompt("deadline.md", "SLEEP=30\ndeadline task")
        proc, spawned = self.spawn(prompt, "--deadline-seconds", "0.2")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(spawned["deadline_seconds"], 0.2)

        finished = self.wait_state(spawned["worker_id"], {"failed"}, timeout=6)
        self.assertEqual(finished["termination_reason"], "deadline_exceeded")
        self.assertIn("deadline_at", finished)
        self.assertIn("timed_out_at", finished)

    def test_legacy_entry_forwards_telemetry_labels_and_reports_grok(self):
        prompt = self.write_prompt("telemetry.md", "telemetry task")
        proc, spawned = self.spawn(
            prompt,
            "--task-class",
            "review",
            "--route-reason",
            "fast-readonly",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.wait_state(spawned["worker_id"], {"succeeded"})

        outcome_proc, outcome = self.run_cli(
            "record-outcome",
            spawned["worker_id"],
            "--outcome",
            "accepted",
            "--verification",
            "passed",
        )
        self.assertEqual(outcome_proc.returncode, 0, outcome_proc.stderr)
        self.assertEqual(outcome["task_class"], "review")

        report_proc, report = self.run_cli("report", "--since-days", "30")
        self.assertEqual(report_proc.returncode, 0, report_proc.stderr)
        self.assertEqual(report["runs"], 1)
        self.assertEqual(report["by_agent"], {"grok": 1})
        self.assertEqual(report["by_outcome"], {"accepted": 1})

    def test_followup_resumes_native_session(self):
        first_prompt = self.write_prompt("first.md", "first task")
        proc, first = self.spawn(first_prompt)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.wait_state(first["worker_id"], {"succeeded"})

        follow_prompt = self.write_prompt("follow.md", "follow-up task")
        follow_proc, follow = self.run_cli(
            "followup",
            first["worker_id"],
            "--prompt-file",
            str(follow_prompt),
        )
        self.assertEqual(follow_proc.returncode, 0, follow_proc.stderr)
        finished = self.wait_state(follow["worker_id"], {"succeeded"})
        self.assertEqual(finished["session_id"], "sess-fake-001")

        invocations = [json.loads(line) for line in self.log_path.read_text().splitlines()]
        follow_args = invocations[-1]["args"]
        self.assertIn("--resume", follow_args)
        self.assertEqual(follow_args[follow_args.index("--resume") + 1], "sess-fake-001")
        self.assertNotEqual(first["worker_id"], follow["worker_id"])

    def test_cancel_and_cleanup_are_bounded(self):
        prompt = self.write_prompt("slow.md", "SLEEP=10\nslow task")
        proc, spawned = self.spawn(prompt)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        worker_id = spawned["worker_id"]

        cleanup_proc, cleanup = self.run_cli("cleanup", worker_id)
        self.assertEqual(cleanup_proc.returncode, 4)
        self.assertEqual(cleanup["error"], "worker_not_terminal")

        cancel_proc, cancelled = self.run_cli("cancel", worker_id, "--timeout", "3")
        self.assertEqual(cancel_proc.returncode, 0, cancel_proc.stderr)
        self.assertEqual(cancelled["state"], "cancelled")
        self.wait_state(worker_id, {"cancelled"})

        cleanup_proc, cleanup = self.run_cli("cleanup", worker_id)
        self.assertEqual(cleanup_proc.returncode, 0, cleanup_proc.stderr)
        self.assertEqual(cleanup["state"], "cleaned")
        self.assertFalse((self.state_dir / worker_id).exists())

    def test_cancel_refuses_pid_that_is_not_our_wrapper(self):
        worker_id = "identity-guard"
        worker_dir = self.state_dir / worker_id
        worker_dir.mkdir(parents=True)
        metadata = {
            "schema_version": 1,
            "worker_id": worker_id,
            "state": "running",
            "wrapper_pid": os.getpid(),
            "cwd": str(self.work_dir),
        }
        (worker_dir / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")

        proc, payload = self.run_cli("cancel", worker_id, "--timeout", "1")
        self.assertEqual(proc.returncode, 5)
        self.assertEqual(payload["error"], "process_identity_mismatch")

    def test_orphaned_grok_blocks_cleanup_and_can_be_cancelled(self):
        worker_id = "orphan-guard"
        worker_dir = self.state_dir / worker_id
        worker_dir.mkdir(parents=True)
        prompt_path = worker_dir / "prompt.md"
        prompt_path.write_text("SLEEP=10\norphan task", encoding="utf-8")
        grok = subprocess.Popen(
            [
                str(self.fake_grok),
                "--cwd",
                str(self.work_dir),
                "--no-plan",
                "--prompt-file",
                str(prompt_path),
            ],
            env=self.env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        def stop_grok():
            if grok.poll() is None:
                grok.kill()
            grok.wait(timeout=3)

        self.addCleanup(stop_grok)
        metadata = {
            "schema_version": 1,
            "worker_id": worker_id,
            "state": "running",
            "wrapper_pid": 99999999,
            "process_group_id": grok.pid,
            "grok_pid": grok.pid,
            "grok_binary": str(self.fake_grok.resolve()),
            "cwd": str(self.work_dir),
            "result_path": str(worker_dir / "stdout.json"),
            "stderr_path": str(worker_dir / "stderr.log"),
        }
        (worker_dir / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")

        status_proc, status = self.run_cli("status", worker_id)
        self.assertEqual(status_proc.returncode, 0, status_proc.stderr)
        command = subprocess.run(
            ["ps", "-ww", "-p", str(grok.pid), "-o", "command="],
            text=True,
            stdout=subprocess.PIPE,
            check=False,
        ).stdout.strip()
        self.assertEqual(status["state"], "orphaned", command)

        cleanup_proc, cleanup = self.run_cli("cleanup", worker_id)
        self.assertEqual(cleanup_proc.returncode, 4)
        self.assertEqual(cleanup["error"], "worker_not_terminal")

        cancel_proc, cancelled = self.run_cli("cancel", worker_id, "--timeout", "3")
        self.assertEqual(cancel_proc.returncode, 0, cancel_proc.stderr)
        self.assertEqual(cancelled["state"], "cancelled")
        grok.wait(timeout=3)

    def test_worker_id_rejects_path_traversal(self):
        proc, payload = self.run_cli("status", "../../outside")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(payload["error"], "invalid_worker_id")


if __name__ == "__main__":
    unittest.main()
