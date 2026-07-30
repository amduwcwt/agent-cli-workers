#!/usr/bin/env python3
import argparse
import json
import os
from pathlib import Path
import runpy
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


SKILL_DIR = Path(__file__).resolve().parents[1]
RUNNER = SKILL_DIR / "scripts" / "agent_worker.py"
RUNNER_API = runpy.run_path(str(RUNNER))


FAKE_GROK = r'''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import subprocess
import sys
import time

args = sys.argv[1:]
prompt_path = Path(args[args.index("--prompt-file") + 1])
prompt = prompt_path.read_text(encoding="utf-8")
resume = args[args.index("--resume") + 1] if "--resume" in args else None
with open(os.environ["FAKE_AGENT_LOG"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps({"binary": "grok", "args": args, "prompt": prompt}) + "\n")
stop_reason = "EndTurn"
result_text = "grok result: " + prompt.splitlines()[-1]
for line in prompt.splitlines():
    if line.startswith("SPAWN_CHILD_PID="):
        child_pid_path = Path(line.split("=", 1)[1])
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        child_pid_path.write_text(str(child.pid), encoding="utf-8")
    if line.startswith("SLEEP="):
        time.sleep(float(line.split("=", 1)[1]))
    if line.startswith("STOP_REASON="):
        stop_reason = line.split("=", 1)[1]
    if line == "RETURN_CAPSULE":
        cwd = args[args.index("--cwd") + 1]
        result_text = """I inspected the requested scope.\n\nSTATUS: failed
WORKSPACE: pwd=/stale; root=/stale; base=deadbeef; head=deadbeef
SUMMARY: Stale intermediate result.
FILES: none
VERIFY: not run because the first attempt was incomplete
RISKS: stale

Retry completed.\n\nSTATUS: succeeded
WORKSPACE: pwd={cwd}; root=non-git; base=none; head=none
SUMMARY: Focused review completed.
FILES: none
VERIFY: not run because this was a read-only review
RISKS: none""".format(cwd=cwd)
payload = {
    "text": result_text,
    "thought": "PRIVATE_GROK_THOUGHT_MUST_NOT_LEAK",
    "stopReason": stop_reason,
    "sessionId": resume or "grok-session-001",
    "requestId": "grok-request-001",
    "usage": {"input_tokens": 10, "output_tokens": 5},
    "modelUsage": {"grok": {"input_tokens": 10, "output_tokens": 5}},
}
if "USAGE_CANARY" in prompt:
    payload["usage"].update({
        "secret_label": "USAGE_PRIVATE_CANARY",
        "negative_tokens": -7,
        "fractional_tokens": 1.5,
        "nested": {"provider_secret_key": 99},
    })
print(json.dumps(payload))
'''


FAKE_CODEX = r'''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys
import time

args = sys.argv[1:]
prompt = sys.stdin.read()
resume = "resume" in args
thread_id = "codex-thread-001"
if resume:
    candidates = [arg for arg in args if arg.startswith("codex-thread-")]
    if candidates:
        thread_id = candidates[0]
with open(os.environ["FAKE_AGENT_LOG"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps({"binary": "codex", "args": args, "prompt": prompt}) + "\n")
for line in prompt.splitlines():
    if line.startswith("SLEEP="):
        time.sleep(float(line.split("=", 1)[1]))
text = "codex result: " + prompt.splitlines()[-1]
if "-o" in args:
    Path(args[args.index("-o") + 1]).write_text(text, encoding="utf-8")
events = [
    {"type": "thread.started", "thread_id": thread_id},
    {"type": "turn.started"},
    {"type": "item.completed", "item": {"id": "item_1", "type": "agent_message", "text": text}},
    {"type": "turn.completed", "usage": {"input_tokens": 12, "cached_input_tokens": 4, "output_tokens": 3}},
]
for event in events:
    print(json.dumps(event), flush=True)
'''


class AgentWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self.write_executable("grok", FAKE_GROK)
        self.write_executable("codex", FAKE_CODEX)
        self.state_dir = self.root / "state"
        self.state_dir.mkdir(mode=0o700)
        self.history_dir = self.root / "history"
        self.work_dir = self.root / "work"
        self.work_dir.mkdir()
        self.log_path = self.root / "agents.log"
        self.env = os.environ.copy()
        self.env["PATH"] = str(self.bin_dir) + os.pathsep + self.env.get("PATH", "")
        self.env["AGENT_CLI_WORKERS_STATE_DIR"] = str(self.state_dir)
        self.env["AGENT_CLI_WORKERS_HISTORY_DIR"] = str(self.history_dir)
        self.env["FAKE_AGENT_LOG"] = str(self.log_path)

    def tearDown(self):
        if RUNNER.exists() and self.state_dir.exists():
            for metadata_path in self.state_dir.glob("*/metadata.json"):
                try:
                    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if metadata.get("state") in {"queued", "running", "orphaned"}:
                    self.run_cli("cancel", metadata["worker_id"], "--timeout", "1")
        self.temp.cleanup()

    def write_executable(self, name, content):
        path = self.bin_dir / name
        path.write_text(content, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

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
        payload = json.loads(proc.stdout) if proc.stdout.strip() else None
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

    def spawn(self, agent, prompt_path, *extra):
        return self.run_cli(
            "spawn",
            "--agent",
            agent,
            "--cwd",
            str(self.work_dir),
            "--prompt-file",
            str(prompt_path),
            *extra,
        )

    def invocations(self):
        if not self.log_path.exists():
            return []
        return [json.loads(line) for line in self.log_path.read_text().splitlines()]

    def history_path(self, worker_id):
        return self.history_dir / f"{worker_id}.json"

    def read_history(self, worker_id):
        return json.loads(self.history_path(worker_id).read_text(encoding="utf-8"))

    @staticmethod
    def process_is_alive(pid):
        proc = subprocess.run(
            ["ps", "-p", str(pid), "-o", "stat="],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return proc.returncode == 0 and not proc.stdout.strip().startswith("Z")

    def seed_worker(self, worker_id, *, agent="grok", state="succeeded", cwd=None):
        directory = self.state_dir / worker_id
        directory.mkdir(parents=True)
        result_name = "stdout.json" if agent == "grok" else "stdout.jsonl"
        metadata = {
            "schema_version": 2,
            "worker_id": worker_id,
            "agent": agent,
            "state": state,
            "cwd": str(cwd or self.work_dir),
            "result_path": str(directory / result_name),
            "stderr_path": str(directory / "stderr.log"),
        }
        (directory / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
        (directory / result_name).write_text("", encoding="utf-8")
        (directory / "stderr.log").write_text("", encoding="utf-8")
        return directory, metadata

    def test_grok_adapter_spawn_collect_and_list(self):
        prompt = self.write_prompt("grok.md", "SLEEP=2\ngrok task")
        started = time.monotonic()
        proc, spawned = self.spawn("grok", prompt, "--no-capsule-contract")
        elapsed = time.monotonic() - started
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertLess(elapsed, 1.5)
        self.assertEqual(spawned["agent"], "grok")

        list_proc, listing = self.run_cli("list", "--agent", "grok")
        self.assertEqual(list_proc.returncode, 0, list_proc.stderr)
        self.assertEqual([w["worker_id"] for w in listing["workers"]], [spawned["worker_id"]])

        finished = self.wait_state(spawned["worker_id"], {"succeeded"})
        self.assertEqual(finished["session_id"], "grok-session-001")
        collect_proc, collected = self.run_cli("collect", spawned["worker_id"])
        self.assertEqual(collect_proc.returncode, 0, collect_proc.stderr)
        self.assertEqual(collected["result"]["text"], "grok result: grok task")
        self.assertEqual(collected["result"]["sessionId"], "grok-session-001")
        self.assertNotIn("PRIVATE_GROK_THOUGHT_MUST_NOT_LEAK", json.dumps(collected))
        self.assertNotIn("--max-turns", self.invocations()[-1]["args"])

    def test_grok_turn_cap_is_only_added_when_explicit(self):
        prompt = self.write_prompt("grok-short-check.md", "short closed check")
        proc, spawned = self.spawn("grok", prompt, "--max-turns", "3")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.wait_state(spawned["worker_id"], {"succeeded"})

        invocation = self.invocations()[-1]
        cap_index = invocation["args"].index("--max-turns")
        self.assertEqual(invocation["args"][cap_index + 1], "3")

    def test_collect_capsule_extracts_normalized_handoff_after_preface(self):
        prompt = self.write_prompt("grok-capsule.md", "RETURN_CAPSULE")
        proc, spawned = self.spawn("grok", prompt)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.wait_state(spawned["worker_id"], {"succeeded"})

        collect_proc, collected = self.run_cli(
            "collect",
            spawned["worker_id"],
            "--capsule",
        )

        self.assertEqual(collect_proc.returncode, 0, collect_proc.stderr)
        self.assertEqual(
            set(collected),
            {
                "agent",
                "completion_capsule",
                "exit_code",
                "request_id",
                "session_id",
                "state",
                "worker_id",
            },
        )
        self.assertEqual(collected["request_id"], "grok-request-001")
        self.assertEqual(
            collected["completion_capsule"],
            "\n".join(
                (
                    "STATUS: succeeded",
                    f"WORKSPACE: pwd={self.work_dir.resolve()}; root=non-git; base=none; head=none",
                    "SUMMARY: Focused review completed.",
                    "FILES: none",
                    "VERIFY: not run because this was a read-only review",
                    "RISKS: none",
                )
            ),
        )
        serialized = json.dumps(collected)
        self.assertNotIn("I inspected the requested scope", serialized)
        self.assertNotIn("PRIVATE_GROK_THOUGHT_MUST_NOT_LEAK", serialized)
        self.assertNotIn("usage", serialized.casefold())

    def test_collect_capsule_rejects_success_without_valid_handoff(self):
        prompt = self.write_prompt("grok-invalid-capsule.md", "no capsule")
        proc, spawned = self.spawn("grok", prompt)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.wait_state(spawned["worker_id"], {"succeeded"})

        collect_proc, collected = self.run_cli(
            "collect",
            spawned["worker_id"],
            "--capsule",
        )

        self.assertEqual(collect_proc.returncode, 4, collect_proc.stderr)
        self.assertEqual(collected["error"], "completion_capsule_invalid")
        serialized = json.dumps(collected)
        self.assertNotIn("grok result: no capsule", serialized)
        self.assertNotIn("PRIVATE_GROK_THOUGHT_MUST_NOT_LEAK", serialized)

    def test_completion_capsule_rejects_oversized_handoff(self):
        oversized = "\n".join(
            (
                "STATUS: succeeded",
                "WORKSPACE: pwd=/tmp; root=non-git; base=none; head=none",
                "SUMMARY: " + "x" * (16 * 1024),
                "FILES: none",
                "VERIFY: not run because this was a read-only review",
                "RISKS: none",
            )
        )

        self.assertIsNone(RUNNER_API["extract_completion_capsule"](oversized))

    def test_completion_capsule_accepts_markdown_bold_labels(self):
        bolded = "\n".join(
            (
                "**STATUS:** succeeded",
                "**WORKSPACE**: pwd=/tmp; root=non-git; base=none; head=none",
                "**SUMMARY:** Bolded labels are still a valid handoff.",
                "FILES: none",
                "**VERIFY:** not run because this was a read-only review",
                "**RISKS**: none",
            )
        )

        capsule = RUNNER_API["extract_completion_capsule"](bolded)
        self.assertIsNotNone(capsule)
        self.assertTrue(capsule.startswith("STATUS: succeeded"))
        self.assertIn("SUMMARY: Bolded labels are still a valid handoff.", capsule)

    def test_completion_capsule_accepts_grok_progress_prefix_and_whole_line_bold(self):
        grok_text = (
            "I'll finish the focused review now.STATUS: succeeded\n\n"
            "**WORKSPACE: pwd=/tmp; root=non-git; base=none; head=none**  \n\n"
            "**SUMMARY: The requested evidence was checked.**  \n\n"
            "**FILES: none**  \n\n"
            "**VERIFY: not run because this was a read-only review**  \n\n"
            "**RISKS: none**\n"
        )

        capsule = RUNNER_API["extract_completion_capsule"](grok_text)

        self.assertEqual(
            capsule,
            "\n".join(
                (
                    "STATUS: succeeded",
                    "WORKSPACE: pwd=/tmp; root=non-git; base=none; head=none",
                    "SUMMARY: The requested evidence was checked.",
                    "FILES: none",
                    "VERIFY: not run because this was a read-only review",
                    "RISKS: none",
                )
            ),
        )

    def test_grok_root_prompt_receives_completion_contract(self):
        prompt = self.write_prompt("grok-contract.md", "review the parser")
        proc, spawned = self.spawn("grok", prompt)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.wait_state(spawned["worker_id"], {"succeeded"})

        sent = self.invocations()[-1]["prompt"]
        self.assertIn(RUNNER_API["CAPSULE_CONTRACT_MARKER"], sent)
        self.assertIn("review the parser", sent)
        self.assertIn(str(self.work_dir), sent)

    def test_grok_root_prompt_does_not_duplicate_an_existing_completion_contract(self):
        task = """Role: reviewer
Task: review the parser

Return only this final completion capsule:
STATUS: succeeded|blocked|failed
WORKSPACE: pwd=<path>; root=<path>; base=<launch-sha>; head=<final-sha>
SUMMARY: <one concise sentence>
FILES: <comma-separated paths or none>
VERIFY: <command and exit code>
RISKS: <remaining risks or none>
"""
        prompt = self.write_prompt("grok-existing-contract.md", task)

        proc, spawned = self.spawn("grok", prompt)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.wait_state(spawned["worker_id"], {"succeeded"})
        sent = self.invocations()[-1]["prompt"]
        self.assertEqual(sent, task)
        self.assertNotIn(RUNNER_API["CAPSULE_CONTRACT_MARKER"], sent)

    def test_capsule_contract_can_be_suppressed(self):
        prompt = self.write_prompt("grok-no-contract.md", "review the parser")
        proc, spawned = self.spawn("grok", prompt, "--no-capsule-contract")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.wait_state(spawned["worker_id"], {"succeeded"})

        sent = self.invocations()[-1]["prompt"]
        self.assertNotIn(RUNNER_API["CAPSULE_CONTRACT_MARKER"], sent)
        self.assertEqual(sent, "review the parser")
        self.assertFalse(spawned["capsule_contract_requested"])

    def test_telemetry_marks_explicit_non_capsule_result_not_requested(self):
        prompt = self.write_prompt("grok-detailed-review.md", "detailed review body")
        proc, spawned = self.spawn("grok", prompt, "--no-capsule-contract")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.wait_state(spawned["worker_id"], {"succeeded"})

        summary = self.read_history(spawned["worker_id"])
        self.assertEqual(summary["capsule"], {"status": "not-requested", "bytes": 0})
        report_proc, report = self.run_cli("report", "--since-days", "30")
        self.assertEqual(report_proc.returncode, 0, report_proc.stderr)
        self.assertEqual(report["capsules"]["not-requested"], 1)
        self.assertEqual(report["capsules"]["invalid"], 0)

    def test_worker_lock_reports_missing_state_directory(self):
        missing = self.state_dir / "grok-00000000000000-000000"
        with self.assertRaises(RUNNER_API["RunnerError"]) as raised:
            with RUNNER_API["worker_lock"](missing):
                pass
        self.assertEqual(raised.exception.error, "worker_not_found")

    def test_grok_sandbox_is_passed_through(self):
        prompt = self.write_prompt("grok-sandbox.md", "sandbox task")
        proc, spawned = self.spawn("grok", prompt, "--sandbox", "read-only")
        self.assertEqual(proc.returncode, 0, proc.stderr)

        self.wait_state(spawned["worker_id"], {"succeeded"})
        invocation = self.invocations()[-1]
        sandbox_index = invocation["args"].index("--sandbox")
        self.assertEqual(invocation["args"][sandbox_index + 1], "read-only")

    def test_grok_followup_requires_matching_dangerous_sandbox(self):
        first_prompt = self.write_prompt("grok-danger-first.md", "first dangerous task")
        proc, first = self.spawn(
            "grok",
            first_prompt,
            "--permission-mode",
            "bypassPermissions",
            "--sandbox",
            "danger-full-access",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.wait_state(first["worker_id"], {"succeeded"})

        follow_prompt = self.write_prompt("grok-danger-follow.md", "de-escalated follow-up")
        follow_proc, follow = self.run_cli(
            "followup",
            first["worker_id"],
            "--prompt-file",
            str(follow_prompt),
        )
        self.assertEqual(follow_proc.returncode, 4, follow_proc.stderr)
        self.assertEqual(follow["error"], "sandbox_reauthorization_required")

        follow_proc, follow = self.run_cli(
            "followup",
            first["worker_id"],
            "--sandbox",
            "danger-full-access",
            "--prompt-file",
            str(follow_prompt),
        )
        self.assertEqual(follow_proc.returncode, 0, follow_proc.stderr)
        self.wait_state(follow["worker_id"], {"succeeded"})

        invocation = self.invocations()[-1]
        self.assertNotIn("bypassPermissions", invocation["args"])
        sandbox_index = invocation["args"].index("--sandbox")
        self.assertEqual(invocation["args"][sandbox_index + 1], "danger-full-access")

    def test_grok_followup_rejects_legacy_session_without_sandbox_profile(self):
        directory, metadata = self.seed_worker("legacy-grok-session")
        metadata.update(
            session_id="legacy-session-id",
            binary=str((self.bin_dir / "grok").resolve()),
            permission_mode="default",
        )
        (directory / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
        prompt = self.write_prompt("legacy-follow.md", "legacy follow-up")

        follow_proc, follow = self.run_cli(
            "followup",
            "legacy-grok-session",
            "--prompt-file",
            str(prompt),
        )
        self.assertEqual(follow_proc.returncode, 4)
        self.assertEqual(follow["error"], "sandbox_profile_unknown")

    def test_grok_non_completion_stop_reason_is_failure(self):
        prompt = self.write_prompt("grok-cancelled.md", "STOP_REASON=Cancelled")
        proc, spawned = self.spawn("grok", prompt)
        self.assertEqual(proc.returncode, 0, proc.stderr)

        finished = self.wait_state(spawned["worker_id"], {"succeeded", "failed", "cancelled"})
        self.assertEqual(finished["state"], "failed")
        self.assertIn("stopReason=Cancelled", finished["runner_error"])
        collect_proc, collected = self.run_cli("collect", spawned["worker_id"])
        self.assertEqual(collect_proc.returncode, 1, collect_proc.stderr)
        self.assertEqual(collected["result"]["stopReason"], "Cancelled")

    def test_list_filters_workers_by_exact_cwd(self):
        prompt = self.write_prompt("cwd.md", "SLEEP=0.8\ncwd task")
        proc, spawned = self.spawn("grok", prompt)
        self.assertEqual(proc.returncode, 0, proc.stderr)

        list_proc, listing = self.run_cli("list", "--cwd", str(self.work_dir))
        self.assertEqual(list_proc.returncode, 0, list_proc.stderr)
        self.assertEqual([w["worker_id"] for w in listing["workers"]], [spawned["worker_id"]])

        other_cwd = self.root / "other-worktree"
        other_cwd.mkdir()
        list_proc, listing = self.run_cli("list", "--cwd", str(other_cwd))
        self.assertEqual(list_proc.returncode, 0, list_proc.stderr)
        self.assertEqual(listing["workers"], [])

    def test_scoped_list_does_not_refresh_unrelated_workers(self):
        other_cwd = self.root / "other-worktree"
        other_cwd.mkdir()
        directory, metadata = self.seed_worker(
            "unrelated-active",
            state="running",
            cwd=other_cwd,
        )
        metadata["wrapper_pid"] = 99999999
        metadata_path = directory / "metadata.json"
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

        list_proc, listing = self.run_cli("list", "--cwd", str(self.work_dir))
        self.assertEqual(list_proc.returncode, 0, list_proc.stderr)
        self.assertEqual(listing["workers"], [])
        self.assertEqual(json.loads(metadata_path.read_text())["state"], "running")

    def test_cleanup_refuses_worker_directory_symlink(self):
        victim, _ = self.seed_worker("victim-worker")
        alias = self.state_dir / "alias-worker"
        alias.symlink_to(victim, target_is_directory=True)

        cleanup_proc, cleanup = self.run_cli("cleanup", "alias-worker")
        self.assertEqual(cleanup_proc.returncode, 5)
        self.assertEqual(cleanup["error"], "unsafe_worker_path")
        self.assertTrue(victim.is_dir())

    def test_cleanup_rechecks_terminal_state_while_holding_worker_lock(self):
        directory, active = self.seed_worker("cleanup-race", state="running")
        stale_terminal = dict(active, state="succeeded")
        emitted = mock.Mock()
        cleanup = RUNNER_API["cmd_cleanup"]

        with (
            mock.patch.dict(os.environ, self.env),
            mock.patch.dict(
                cleanup.__globals__,
                {
                    "refreshed_metadata": mock.Mock(return_value=stale_terminal),
                    "emit": emitted,
                },
            ),
        ):
            result = cleanup(
                argparse.Namespace(worker_id="cleanup-race", discard_history=False)
            )

        self.assertEqual(result, 4)
        self.assertTrue(directory.is_dir())
        self.assertEqual(emitted.call_args.args[0]["error"], "worker_not_terminal")

    def test_cleanup_normalizes_directory_removal_failure(self):
        directory, _ = self.seed_worker("cleanup-rmtree-failure")
        cleanup = RUNNER_API["cmd_cleanup"]

        with (
            mock.patch.dict(os.environ, self.env),
            mock.patch.object(
                cleanup.__globals__["shutil"],
                "rmtree",
                side_effect=OSError("read-only filesystem"),
            ),
        ):
            with self.assertRaises(RUNNER_API["RunnerError"]) as raised:
                cleanup(
                    argparse.Namespace(
                        worker_id="cleanup-rmtree-failure",
                        discard_history=False,
                    )
                )

        self.assertEqual(raised.exception.error, "cleanup_failed")
        self.assertTrue(directory.is_dir())

    def test_inline_prompt_has_same_size_limit_as_other_sources(self):
        args = argparse.Namespace(
            prompt="x" * (RUNNER_API["MAX_PROMPT_BYTES"] + 1),
            prompt_file=None,
            prompt_stdin=False,
        )
        with self.assertRaises(RUNNER_API["RunnerError"]) as raised:
            RUNNER_API["read_prompt"](args)
        self.assertEqual(raised.exception.error, "prompt_too_large")

    def test_existing_insecure_state_root_is_rejected_without_chmod(self):
        shared = self.root / "shared-state"
        shared.mkdir(mode=0o755)
        shared.chmod(0o755)
        env = self.env.copy()
        env["AGENT_CLI_WORKERS_STATE_DIR"] = str(shared)

        proc = subprocess.run(
            [sys.executable, str(RUNNER), "list"],
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        payload = json.loads(proc.stdout)
        self.assertEqual(proc.returncode, 5)
        self.assertEqual(payload["error"], "insecure_state_dir")
        self.assertEqual(stat.S_IMODE(shared.stat().st_mode), 0o755)

    def test_collect_bounds_structured_result_parsing(self):
        directory, _ = self.seed_worker("large-result")
        result_path = directory / "stdout.json"
        result_path.write_text(
            json.dumps({"text": "x" * 4096, "sessionId": "large-session"}),
            encoding="utf-8",
        )

        collect_proc, collected = self.run_cli(
            "collect",
            "large-result",
            "--max-bytes",
            "128",
        )
        self.assertEqual(collect_proc.returncode, 0, collect_proc.stderr)
        self.assertIsNone(collected["result"])
        self.assertTrue(collected["stdout_truncated"])
        self.assertTrue(collected["result_parse_error"])
        self.assertNotIn("stdout", collected)

    def test_collect_never_falls_back_to_malformed_grok_stdout(self):
        directory, _ = self.seed_worker("malformed-grok", state="failed")
        private_marker = "PRIVATE_MALFORMED_GROK_THOUGHT"
        (directory / "stdout.json").write_text(
            '{"thought": "' + private_marker,
            encoding="utf-8",
        )

        collect_proc, collected = self.run_cli("collect", "malformed-grok")

        self.assertEqual(collect_proc.returncode, 1, collect_proc.stderr)
        self.assertIsNone(collected["result"])
        self.assertTrue(collected["result_parse_error"])
        self.assertNotIn(private_marker, json.dumps(collected))

    def test_non_finite_wait_and_timeout_are_rejected(self):
        self.seed_worker("finite-arguments")

        collect_proc, collect = self.run_cli(
            "collect",
            "finite-arguments",
            "--wait",
            "nan",
        )
        self.assertEqual(collect_proc.returncode, 2)
        self.assertEqual(collect["error"], "invalid_wait")

        cancel_proc, cancel = self.run_cli(
            "cancel",
            "finite-arguments",
            "--timeout",
            "nan",
        )
        self.assertEqual(cancel_proc.returncode, 2)
        self.assertEqual(cancel["error"], "invalid_timeout")

    def test_invalid_deadline_is_rejected_before_worker_creation(self):
        prompt = self.write_prompt("invalid-deadline.md", "deadline validation")

        for value in ("0", "-1", "nan", "inf"):
            proc, payload = self.spawn(
                "grok",
                prompt,
                "--deadline-seconds",
                value,
            )
            self.assertEqual(proc.returncode, 2, (value, proc.stderr))
            self.assertEqual(payload["error"], "invalid_deadline")

        self.assertEqual(list(self.state_dir.glob("*/metadata.json")), [])

    def test_codex_adapter_uses_cli_model_default_and_collects_jsonl(self):
        secret = "codex-secret-marker"
        prompt = self.write_prompt("codex.md", f"SLEEP=2\n{secret}")
        started = time.monotonic()
        proc, spawned = self.spawn("codex", prompt, "--sandbox", "read-only")
        elapsed = time.monotonic() - started
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertLess(elapsed, 1.5)
        self.assertEqual(spawned["agent"], "codex")
        self.assertNotIn("model", spawned)
        worker_dir = self.state_dir / spawned["worker_id"]
        metadata_path = worker_dir / "metadata.json"
        metadata_text = metadata_path.read_text()
        self.assertNotIn(secret, metadata_text)
        self.assertEqual(stat.S_IMODE(metadata_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((worker_dir / "prompt.md").stat().st_mode), 0o600)
        command = subprocess.check_output(
            ["ps", "-p", str(spawned["wrapper_pid"]), "-o", "command="], text=True
        )
        self.assertNotIn(secret, command)

        finished = self.wait_state(spawned["worker_id"], {"succeeded"})
        self.assertEqual(finished["session_id"], "codex-thread-001")
        collect_proc, collected = self.run_cli("collect", spawned["worker_id"])
        self.assertEqual(collect_proc.returncode, 0, collect_proc.stderr)
        self.assertEqual(collected["result"]["text"], "codex result: " + secret)
        self.assertEqual(collected["result"]["threadId"], "codex-thread-001")
        self.assertEqual(collected["result"]["usage"]["cached_input_tokens"], 4)
        for name in ("stdout.jsonl", "stderr.log", "final.txt"):
            self.assertEqual(stat.S_IMODE((worker_dir / name).stat().st_mode), 0o600)
        invocation = self.invocations()[-1]
        self.assertIn("exec", invocation["args"])
        self.assertIn("--json", invocation["args"])
        self.assertNotIn("-m", invocation["args"])
        self.assertNotIn(secret, " ".join(invocation["args"]))

    def test_configured_model_prefixes_are_disabled_before_worker_creation(self):
        prompt = self.write_prompt("disabled-model.md", "must not start")
        self.env["AGENT_CLI_WORKERS_DISABLED_MODEL_PREFIXES"] = "blocked-model, legacy-model"

        for model in ("blocked-model", "blocked-model-fast", "LEGACY-MODEL-custom"):
            with self.subTest(model=model):
                proc, payload = self.spawn("codex", prompt, "--model", model)
                self.assertEqual(proc.returncode, 4, proc.stderr)
                self.assertEqual(payload["error"], "model_disabled")

        self.assertEqual(list(self.state_dir.iterdir()), [])
        self.assertEqual(self.invocations(), [])

    def test_followup_cannot_resume_a_session_with_a_disabled_model(self):
        self.env["AGENT_CLI_WORKERS_DISABLED_MODEL_PREFIXES"] = "blocked-model"
        directory, metadata = self.seed_worker("legacy-blocked-model", agent="codex")
        metadata.update(
            session_id="codex-thread-legacy",
            binary=str((self.bin_dir / "codex").resolve()),
            model="blocked-model-old",
            sandbox="read-only",
        )
        (directory / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
        prompt = self.write_prompt("legacy-disabled-followup.md", "must not resume")

        proc, payload = self.run_cli(
            "followup",
            metadata["worker_id"],
            "--prompt-file",
            str(prompt),
        )

        self.assertEqual(proc.returncode, 4, proc.stderr)
        self.assertEqual(payload["error"], "model_disabled")
        worker_directories = sorted(path.name for path in self.state_dir.iterdir() if path.is_dir())
        self.assertEqual(worker_directories, [metadata["worker_id"]])
        self.assertEqual(self.invocations(), [])

    def test_codex_defaults_to_read_only_sandbox(self):
        prompt = self.write_prompt("codex-default-sandbox.md", "default sandbox")
        proc, spawned = self.spawn("codex", prompt)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.wait_state(spawned["worker_id"], {"succeeded"})

        invocation = self.invocations()[-1]
        sandbox_index = invocation["args"].index("--sandbox")
        self.assertEqual(invocation["args"][sandbox_index + 1], "read-only")

    def test_codex_adapter_uses_configured_default_and_resumes_same_thread(self):
        self.env["AGENT_CLI_WORKERS_CODEX_MODEL"] = "configured-fast-model"
        first_prompt = self.write_prompt("configured-first.md", "first configured task")
        proc, first = self.spawn("codex", first_prompt)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(first["agent"], "codex")
        self.assertEqual(first["model"], "configured-fast-model")
        self.wait_state(first["worker_id"], {"succeeded"})

        invocation = self.invocations()[-1]
        model_index = invocation["args"].index("-m")
        self.assertEqual(invocation["args"][model_index + 1], "configured-fast-model")

        list_proc, listing = self.run_cli("list", "--agent", "codex")
        self.assertEqual(list_proc.returncode, 0, list_proc.stderr)
        self.assertEqual([w["worker_id"] for w in listing["workers"]], [first["worker_id"]])

        follow_prompt = self.write_prompt("configured-follow.md", "follow configured task")
        follow_proc, follow = self.run_cli(
            "followup",
            first["worker_id"],
            "--prompt-file",
            str(follow_prompt),
        )
        self.assertEqual(follow_proc.returncode, 0, follow_proc.stderr)
        finished = self.wait_state(follow["worker_id"], {"succeeded"})
        self.assertEqual(finished["session_id"], "codex-thread-001")
        invocation = self.invocations()[-1]
        self.assertIn("resume", invocation["args"])
        model_index = invocation["args"].index("-m")
        self.assertEqual(invocation["args"][model_index + 1], "configured-fast-model")

    def test_codex_followup_places_sandbox_before_resume(self):
        first_prompt = self.write_prompt("first.md", "first codex task")
        proc, first = self.spawn("codex", first_prompt, "--sandbox", "read-only")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.wait_state(first["worker_id"], {"succeeded"})

        follow_prompt = self.write_prompt("follow.md", "follow codex task")
        follow_proc, follow = self.run_cli(
            "followup",
            first["worker_id"],
            "--prompt-file",
            str(follow_prompt),
        )
        self.assertEqual(follow_proc.returncode, 0, follow_proc.stderr)
        finished = self.wait_state(follow["worker_id"], {"succeeded"})
        self.assertEqual(finished["session_id"], "codex-thread-001")
        invocation = self.invocations()[-1]
        self.assertIn("resume", invocation["args"])
        self.assertIn("codex-thread-001", invocation["args"])
        self.assertLess(invocation["args"].index("--sandbox"), invocation["args"].index("resume"))

    def test_followup_rejects_concurrent_resume_of_same_session(self):
        first_prompt = self.write_prompt("session-first.md", "session parent")
        proc, first = self.spawn("codex", first_prompt)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.wait_state(first["worker_id"], {"succeeded"})

        active_prompt = self.write_prompt("session-active.md", "SLEEP=1.2\nactive resume")
        active_proc, active = self.run_cli(
            "followup",
            first["worker_id"],
            "--prompt-file",
            str(active_prompt),
        )
        self.assertEqual(active_proc.returncode, 0, active_proc.stderr)

        duplicate_prompt = self.write_prompt("session-duplicate.md", "duplicate resume")
        duplicate_proc, duplicate = self.run_cli(
            "followup",
            first["worker_id"],
            "--prompt-file",
            str(duplicate_prompt),
        )
        self.assertEqual(duplicate_proc.returncode, 3)
        self.assertEqual(duplicate["error"], "session_in_use")
        self.wait_state(active["worker_id"], {"succeeded"})

    def test_codex_followup_requires_explicit_dangerous_bypass(self):
        first_prompt = self.write_prompt("bypass-first.md", "first bypass task")
        proc, first = self.spawn(
            "codex",
            first_prompt,
            "--dangerously-bypass-approvals-and-sandbox",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.wait_state(first["worker_id"], {"succeeded"})

        follow_prompt = self.write_prompt("bypass-follow.md", "de-escalated follow-up")
        follow_proc, follow = self.run_cli(
            "followup",
            first["worker_id"],
            "--prompt-file",
            str(follow_prompt),
        )
        self.assertEqual(follow_proc.returncode, 0, follow_proc.stderr)
        self.wait_state(follow["worker_id"], {"succeeded"})

        invocation = self.invocations()[-1]
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", invocation["args"])
        sandbox_index = invocation["args"].index("--sandbox")
        self.assertEqual(invocation["args"][sandbox_index + 1], "read-only")

    def test_internal_run_is_single_instance(self):
        prompt = self.write_prompt("single-run.md", "SLEEP=1.2\nsingle instance")
        proc, spawned = self.spawn("grok", prompt)
        self.assertEqual(proc.returncode, 0, proc.stderr)

        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not self.invocations():
            time.sleep(0.05)
        self.assertTrue(self.invocations())

        duplicate_proc, duplicate = self.run_cli("_run", spawned["worker_id"], timeout=3)
        self.assertEqual(duplicate_proc.returncode, 2, duplicate_proc.stderr)
        self.assertEqual(duplicate["error"], "worker_already_running")
        self.wait_state(spawned["worker_id"], {"succeeded"})
        self.assertEqual(len(self.invocations()), 1)

    def test_codex_cancel_and_cleanup(self):
        prompt = self.write_prompt("cancel.md", "SLEEP=10\ncancel codex")
        proc, spawned = self.spawn("codex", prompt)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        worker_id = spawned["worker_id"]

        cancel_proc, cancelled = self.run_cli("cancel", worker_id, "--timeout", "3")
        self.assertEqual(cancel_proc.returncode, 0, cancel_proc.stderr)
        self.assertEqual(cancelled["state"], "cancelled")

        cleanup_proc, cleaned = self.run_cli("cleanup", worker_id)
        self.assertEqual(cleanup_proc.returncode, 0, cleanup_proc.stderr)
        self.assertEqual(cleaned["state"], "cleaned")

    def test_telemetry_summary_outcome_report_and_cleanup_are_private(self):
        canary = "SECRET_TELEMETRY_CANARY"
        prompt = self.write_prompt(
            "telemetry.md",
            f"{canary}\nUSAGE_CANARY\nRETURN_CAPSULE",
        )
        proc, spawned = self.spawn(
            "grok",
            prompt,
            "--task-class",
            "review",
            "--route-reason",
            "fast-readonly",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.wait_state(spawned["worker_id"], {"succeeded"})

        history_path = self.history_path(spawned["worker_id"])
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not history_path.exists():
            time.sleep(0.02)
        self.assertTrue(history_path.exists())
        self.assertEqual(stat.S_IMODE(self.history_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(history_path.stat().st_mode), 0o600)

        summary = self.read_history(spawned["worker_id"])
        self.assertEqual(summary["task_class"], "review")
        self.assertEqual(summary["route_reason"], "fast-readonly")
        self.assertEqual(summary["capsule"]["status"], "valid")
        self.assertGreater(summary["capsule"]["bytes"], 0)
        self.assertEqual(summary["usage"]["input_tokens"], 10)
        self.assertEqual(summary["usage"]["output_tokens"], 5)
        self.assertEqual(set(summary["usage"]), {"input_tokens", "output_tokens"})
        self.assertGreaterEqual(summary["duration_seconds"], 0)
        self.assertRegex(summary["skill_sha256"], r"^[0-9a-f]{64}$")
        serialized = json.dumps(summary)
        for forbidden in (
            canary,
            str(self.work_dir),
            "grok-session-001",
            "PRIVATE_GROK_THOUGHT_MUST_NOT_LEAK",
            "USAGE_PRIVATE_CANARY",
            "provider_secret_key",
            "prompt_sha256",
            "session_id",
            "result_path",
            "stderr_path",
            '"cwd"',
            '"text"',
        ):
            self.assertNotIn(forbidden, serialized)

        outcome_proc, outcome = self.run_cli(
            "record-outcome",
            spawned["worker_id"],
            "--outcome",
            "accepted",
            "--verification",
            "passed",
        )
        self.assertEqual(outcome_proc.returncode, 0, outcome_proc.stderr)
        self.assertEqual(outcome["controller"]["outcome"], "accepted")
        self.assertEqual(outcome["controller"]["verification"], "passed")
        self.assertEqual(outcome["controller"]["provenance"], "controller")

        report_proc, report = self.run_cli("report", "--since-days", "30")
        self.assertEqual(report_proc.returncode, 0, report_proc.stderr)
        self.assertEqual(report["runs"], 1)
        self.assertEqual(report["root_runs"], 1)
        self.assertEqual(report["followup_runs"], 0)
        self.assertEqual(report["by_agent"], {"grok": 1})
        self.assertEqual(report["by_outcome"], {"accepted": 1})
        self.assertEqual(report["by_verification"], {"passed": 1})
        self.assertEqual(report["feedback"]["recorded"], 1)
        self.assertEqual(report["feedback"]["missing"], 0)
        self.assertEqual(report["corrupt_summaries"], 0)
        self.assertEqual(report["usage_totals"]["input_tokens"], 10)
        self.assertNotIn(canary, json.dumps(report))

        cleanup_proc, cleaned = self.run_cli("cleanup", spawned["worker_id"])
        self.assertEqual(cleanup_proc.returncode, 0, cleanup_proc.stderr)
        self.assertEqual(cleaned["state"], "cleaned")
        self.assertFalse((self.state_dir / spawned["worker_id"]).exists())
        self.assertTrue(history_path.exists())

    def test_telemetry_records_invalid_capsule_failure_and_missing_usage(self):
        prompt = self.write_prompt("invalid-telemetry.md", "no capsule")
        proc, spawned = self.spawn("grok", prompt)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.wait_state(spawned["worker_id"], {"succeeded"})
        summary = self.read_history(spawned["worker_id"])
        self.assertEqual(summary["capsule"], {"status": "invalid", "bytes": 0})

        directory, metadata = self.seed_worker("missing-usage", state="failed")
        metadata.update(
            runner_version="legacy",
            started_at="2026-01-01T00:00:00Z",
            finished_at="2026-01-01T00:00:01Z",
            exit_code=7,
        )
        (directory / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
        cleanup_proc, _ = self.run_cli("cleanup", "missing-usage")
        self.assertEqual(cleanup_proc.returncode, 0, cleanup_proc.stderr)
        summary = self.read_history("missing-usage")
        self.assertEqual(summary["failure_class"], "exit_nonzero")
        self.assertIsNone(summary["usage"])
        self.assertEqual(summary["capsule"], {"status": "missing", "bytes": 0})

        directory, metadata = self.seed_worker("stderr-max-turns", state="failed")
        metadata.update(
            runner_version="legacy",
            finished_at="2026-01-01T00:00:01Z",
            exit_code=1,
        )
        (directory / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
        (directory / "stderr.log").write_text(
            "provider stopped after maximum turns reached", encoding="utf-8"
        )
        cleanup_proc, _ = self.run_cli("cleanup", "stderr-max-turns")
        self.assertEqual(cleanup_proc.returncode, 0, cleanup_proc.stderr)
        self.assertEqual(self.read_history("stderr-max-turns")["failure_class"], "max_turns")

    def test_telemetry_corruption_blocks_cleanup_unless_discarded(self):
        prompt = self.write_prompt("corrupt-history.md", "corrupt history")
        proc, spawned = self.spawn("grok", prompt)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.wait_state(spawned["worker_id"], {"succeeded"})
        self.history_path(spawned["worker_id"]).write_text("{broken", encoding="utf-8")

        outcome_proc, outcome = self.run_cli(
            "record-outcome",
            spawned["worker_id"],
            "--outcome",
            "rejected",
            "--verification",
            "failed",
        )
        self.assertEqual(outcome_proc.returncode, 5)
        self.assertEqual(outcome["error"], "history_corrupt")

        report_proc, report = self.run_cli("report")
        self.assertEqual(report_proc.returncode, 0, report_proc.stderr)
        self.assertEqual(report["runs"], 0)
        self.assertEqual(report["corrupt_summaries"], 1)

        cleanup_proc, cleanup = self.run_cli("cleanup", spawned["worker_id"])
        self.assertEqual(cleanup_proc.returncode, 5)
        self.assertEqual(cleanup["error"], "history_corrupt")
        self.assertTrue((self.state_dir / spawned["worker_id"]).exists())

        cleanup_proc, cleaned = self.run_cli(
            "cleanup",
            spawned["worker_id"],
            "--discard-history",
        )
        self.assertEqual(cleanup_proc.returncode, 0, cleanup_proc.stderr)
        self.assertEqual(cleaned["state"], "cleaned")
        self.assertFalse((self.state_dir / spawned["worker_id"]).exists())
        self.assertFalse(self.history_path(spawned["worker_id"]).exists())

    def test_report_normalizes_tampered_labels_without_echoing_them(self):
        prompt = self.write_prompt("tampered-report.md", "tampered report")
        proc, spawned = self.spawn("grok", prompt)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.wait_state(spawned["worker_id"], {"succeeded"})

        summary = self.read_history(spawned["worker_id"])
        canary = "REPORT_PRIVATE_CANARY"
        summary.update(
            agent=f"{canary}_AGENT",
            task_class=f"{canary}_TASK",
            route_reason=f"{canary}_ROUTE",
            state=f"{canary}_STATE",
            failure_class=f"{canary}_FAILURE",
            controller={
                "provenance": "controller",
                "outcome": f"{canary}_OUTCOME",
                "verification": f"{canary}_VERIFY",
                "reason_codes": [f"{canary}_REASON"],
            },
        )
        self.history_path(spawned["worker_id"]).write_text(
            json.dumps(summary), encoding="utf-8"
        )

        report_proc, report = self.run_cli("report")

        self.assertEqual(report_proc.returncode, 0, report_proc.stderr)
        self.assertNotIn(canary, json.dumps(report))
        for dimension in (
            "by_agent",
            "by_task_class",
            "by_route_reason",
            "by_state",
            "by_failure_class",
            "by_outcome",
            "by_verification",
            "by_reason_code",
        ):
            self.assertEqual(report[dimension], {"unknown": 1})

    def test_controller_label_corrections_survive_cleanup_snapshot(self):
        prompt = self.write_prompt("correct-labels.md", "correct labels")
        proc, spawned = self.spawn("grok", prompt)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.wait_state(spawned["worker_id"], {"succeeded"})

        outcome_proc, _ = self.run_cli(
            "record-outcome",
            spawned["worker_id"],
            "--outcome",
            "accepted",
            "--verification",
            "passed",
            "--task-class",
            "review",
            "--route-reason",
            "independent-review",
        )
        self.assertEqual(outcome_proc.returncode, 0, outcome_proc.stderr)
        cleanup_proc, _ = self.run_cli("cleanup", spawned["worker_id"])
        self.assertEqual(cleanup_proc.returncode, 0, cleanup_proc.stderr)

        summary = self.read_history(spawned["worker_id"])
        self.assertEqual(summary["task_class"], "review")
        self.assertEqual(summary["route_reason"], "independent-review")

    def test_record_outcome_distinguishes_delivery_evidence_and_claim_failures(self):
        prompt = self.write_prompt("reason-codes.md", "reason codes")
        proc, spawned = self.spawn("grok", prompt)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.wait_state(spawned["worker_id"], {"succeeded"})

        outcome_proc, outcome = self.run_cli(
            "record-outcome",
            spawned["worker_id"],
            "--outcome",
            "rejected",
            "--verification",
            "failed",
            "--reason-code",
            "deliverable-incomplete",
            "--reason-code",
            "evidence-missing",
            "--reason-code",
            "claim-conflict",
        )

        self.assertEqual(outcome_proc.returncode, 0, outcome_proc.stderr)
        self.assertEqual(
            outcome["controller"]["reason_codes"],
            ["deliverable-incomplete", "evidence-missing", "claim-conflict"],
        )
        report_proc, report = self.run_cli("report", "--since-days", "30")
        self.assertEqual(report_proc.returncode, 0, report_proc.stderr)
        self.assertEqual(
            report["by_reason_code"],
            {
                "claim-conflict": 1,
                "deliverable-incomplete": 1,
                "evidence-missing": 1,
            },
        )

    def test_record_outcome_drops_unknown_history_fields_without_echoing_them(self):
        prompt = self.write_prompt("outcome-canary.md", "outcome canary")
        proc, spawned = self.spawn("grok", prompt)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.wait_state(spawned["worker_id"], {"succeeded"})

        canary = "OUTCOME_PRIVATE_CANARY"
        summary = self.read_history(spawned["worker_id"])
        summary.update(
            prompt=canary,
            cwd=f"/{canary}",
            session_id=canary,
            raw_stderr=canary,
            unknown_nested={"secret": canary},
        )
        summary["artifacts"]["unknown_bytes"] = canary
        summary["capsule"]["unknown_status"] = canary
        summary["usage"]["unknown_tokens"] = canary
        self.history_path(spawned["worker_id"]).write_text(
            json.dumps(summary), encoding="utf-8"
        )

        outcome_proc, outcome = self.run_cli(
            "record-outcome",
            spawned["worker_id"],
            "--outcome",
            "accepted",
            "--verification",
            "passed",
        )

        self.assertEqual(outcome_proc.returncode, 0, outcome_proc.stderr)
        self.assertNotIn(canary, json.dumps(outcome))
        self.assertNotIn(canary, json.dumps(self.read_history(spawned["worker_id"])))

    def test_stale_snapshot_after_cleanup_preserves_derived_metrics(self):
        prompt = self.write_prompt("stale-snapshot.md", "RETURN_CAPSULE")
        proc, spawned = self.spawn("grok", prompt)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.wait_state(spawned["worker_id"], {"succeeded"})
        metadata = json.loads(
            (self.state_dir / spawned["worker_id"] / "metadata.json").read_text(
                encoding="utf-8"
            )
        )
        before = self.read_history(spawned["worker_id"])
        cleanup_proc, _ = self.run_cli("cleanup", spawned["worker_id"])
        self.assertEqual(cleanup_proc.returncode, 0, cleanup_proc.stderr)

        with mock.patch.dict(
            os.environ,
            {
                "AGENT_CLI_WORKERS_STATE_DIR": str(self.state_dir),
                "AGENT_CLI_WORKERS_HISTORY_DIR": str(self.history_dir),
            },
        ):
            RUNNER_API["snapshot_history"](metadata)

        after = self.read_history(spawned["worker_id"])
        for field in ("usage", "capsule", "artifacts", "duration_seconds"):
            self.assertEqual(after[field], before[field])

    def test_cleanup_requires_summary_when_terminal_metadata_lacks_result_path(self):
        directory, metadata = self.seed_worker("missing-result-path", state="failed")
        metadata.pop("result_path")
        (directory / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")

        cleanup_proc, cleanup = self.run_cli("cleanup", "missing-result-path")

        self.assertEqual(cleanup_proc.returncode, 5)
        self.assertEqual(cleanup["error"], "history_snapshot_unavailable")
        self.assertTrue(directory.exists())

        discard_proc, _ = self.run_cli(
            "cleanup", "missing-result-path", "--discard-history"
        )
        self.assertEqual(discard_proc.returncode, 0, discard_proc.stderr)
        self.assertFalse(directory.exists())

    def test_successful_worker_ignores_recovered_provider_error_stderr(self):
        directory, metadata = self.seed_worker("recovered-provider-error", state="succeeded")
        metadata.update(finished_at="2026-01-01T00:00:01Z", exit_code=0)
        (directory / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
        (directory / "stderr.log").write_text(
            "retry recovered after HTTP status 429", encoding="utf-8"
        )

        cleanup_proc, _ = self.run_cli("cleanup", "recovered-provider-error")

        self.assertEqual(cleanup_proc.returncode, 0, cleanup_proc.stderr)
        self.assertEqual(
            self.read_history("recovered-provider-error")["failure_class"], "none"
        )

    def test_telemetry_unsafe_history_root_preserves_raw_until_escape_hatch(self):
        victim = self.root / "history-victim"
        victim.mkdir(mode=0o700)
        self.history_dir.symlink_to(victim, target_is_directory=True)
        prompt = self.write_prompt("unsafe-history.md", "unsafe history")
        proc, spawned = self.spawn("grok", prompt)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.wait_state(spawned["worker_id"], {"succeeded"})

        cleanup_proc, cleanup = self.run_cli("cleanup", spawned["worker_id"])
        self.assertEqual(cleanup_proc.returncode, 5)
        self.assertEqual(cleanup["error"], "unsafe_history_dir")
        self.assertTrue((self.state_dir / spawned["worker_id"]).exists())

        cleanup_proc, cleaned = self.run_cli(
            "cleanup",
            spawned["worker_id"],
            "--discard-history",
        )
        self.assertEqual(cleanup_proc.returncode, 0, cleanup_proc.stderr)
        self.assertEqual(cleaned["state"], "cleaned")
        self.assertFalse((self.state_dir / spawned["worker_id"]).exists())

    def test_existing_insecure_history_root_is_rejected_without_chmod(self):
        self.history_dir.mkdir(mode=0o755)
        self.history_dir.chmod(0o755)
        prompt = self.write_prompt("insecure-history.md", "insecure history")
        proc, spawned = self.spawn("grok", prompt)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.wait_state(spawned["worker_id"], {"succeeded"})

        cleanup_proc, cleanup = self.run_cli("cleanup", spawned["worker_id"])
        self.assertEqual(cleanup_proc.returncode, 5)
        self.assertEqual(cleanup["error"], "insecure_history_dir")
        self.assertEqual(stat.S_IMODE(self.history_dir.stat().st_mode), 0o755)
        self.assertTrue((self.state_dir / spawned["worker_id"]).exists())

        cleanup_proc, _ = self.run_cli(
            "cleanup",
            spawned["worker_id"],
            "--discard-history",
        )
        self.assertEqual(cleanup_proc.returncode, 0, cleanup_proc.stderr)

    def test_history_root_cannot_be_an_ancestor_of_worker_state(self):
        self.env["AGENT_CLI_WORKERS_HISTORY_DIR"] = str(self.root)

        report_proc, report = self.run_cli("report")

        self.assertEqual(report_proc.returncode, 5)
        self.assertEqual(report["error"], "unsafe_history_dir")

    def test_telemetry_can_be_disabled_and_history_can_be_purged(self):
        self.env["AGENT_CLI_WORKERS_TELEMETRY"] = "0"
        prompt = self.write_prompt("disabled-telemetry.md", "disabled telemetry")
        proc, spawned = self.spawn("grok", prompt)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.wait_state(spawned["worker_id"], {"succeeded"})
        self.assertFalse(self.history_dir.exists())
        cleanup_proc, _ = self.run_cli("cleanup", spawned["worker_id"])
        self.assertEqual(cleanup_proc.returncode, 0, cleanup_proc.stderr)

        self.env.pop("AGENT_CLI_WORKERS_TELEMETRY")
        prompt = self.write_prompt("purge-telemetry.md", "purge telemetry")
        proc, spawned = self.spawn("grok", prompt)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.wait_state(spawned["worker_id"], {"succeeded"})
        summary = self.read_history(spawned["worker_id"])
        summary["recorded_at"] = "2000-01-01T00:00:00Z"
        self.history_path(spawned["worker_id"]).write_text(
            json.dumps(summary), encoding="utf-8"
        )
        purge_proc, purged = self.run_cli(
            "purge-history",
            "--older-than-days",
            "30",
        )
        self.assertEqual(purge_proc.returncode, 0, purge_proc.stderr)
        self.assertEqual(purged["removed"], 1)
        self.assertFalse(self.history_path(spawned["worker_id"]).exists())

    def test_outcome_and_cleanup_are_serialized_without_lost_update(self):
        prompt = self.write_prompt("concurrent-history.md", "concurrent history")
        proc, spawned = self.spawn("grok", prompt)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.wait_state(spawned["worker_id"], {"succeeded"})

        outcome = subprocess.Popen(
            [
                sys.executable,
                str(RUNNER),
                "record-outcome",
                spawned["worker_id"],
                "--outcome",
                "accepted",
                "--verification",
                "passed",
                "--reason-code",
                "none",
            ],
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        cleanup = subprocess.Popen(
            [sys.executable, str(RUNNER), "cleanup", spawned["worker_id"]],
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        outcome_stdout, outcome_stderr = outcome.communicate(timeout=5)
        cleanup_stdout, cleanup_stderr = cleanup.communicate(timeout=5)
        self.assertEqual(outcome.returncode, 0, (outcome_stdout, outcome_stderr))
        self.assertEqual(cleanup.returncode, 0, (cleanup_stdout, cleanup_stderr))
        summary = self.read_history(spawned["worker_id"])
        self.assertEqual(summary["controller"]["outcome"], "accepted")
        self.assertEqual(summary["controller"]["reason_codes"], ["none"])

    def test_deadline_terminates_agent_group_and_blocks_followup(self):
        child_pid_path = self.root / "deadline-child.pid"
        prompt = self.write_prompt(
            "deadline.md",
            f"SPAWN_CHILD_PID={child_pid_path}\nSLEEP=30\ndeadline task",
        )
        proc, spawned = self.spawn(
            "grok",
            prompt,
            "--deadline-seconds",
            "0.25",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(spawned["deadline_seconds"], 0.25)

        finished = self.wait_state(spawned["worker_id"], {"failed"}, timeout=6)
        self.assertEqual(finished["termination_reason"], "deadline_exceeded")
        self.assertEqual(finished["deadline_seconds"], 0.25)
        self.assertIn("deadline_at", finished)
        self.assertIn("timed_out_at", finished)
        self.assertIn("deadline exceeded", finished["runner_error"])
        self.assertFalse((self.state_dir / spawned["worker_id"] / "prompt.md").exists())

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not child_pid_path.exists():
            time.sleep(0.02)
        self.assertTrue(child_pid_path.exists())
        child_pid = int(child_pid_path.read_text())
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and self.process_is_alive(child_pid):
            time.sleep(0.05)
        self.assertFalse(self.process_is_alive(child_pid))

        collect_proc, collected = self.run_cli("collect", spawned["worker_id"])
        self.assertEqual(collect_proc.returncode, 1, collect_proc.stderr)
        self.assertEqual(collected["termination_reason"], "deadline_exceeded")

        follow_prompt = self.write_prompt("deadline-follow.md", "do not resume")
        follow_proc, follow = self.run_cli(
            "followup",
            spawned["worker_id"],
            "--prompt-file",
            str(follow_prompt),
        )
        self.assertEqual(follow_proc.returncode, 4)
        self.assertEqual(follow["error"], "deadline_session_not_resumable")

    def test_deadline_allows_fast_worker_and_is_not_inherited(self):
        prompt = self.write_prompt("deadline-fast.md", "fast deadline task")
        proc, first = self.spawn(
            "grok",
            prompt,
            "--deadline-seconds",
            "2",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        finished = self.wait_state(first["worker_id"], {"succeeded"})
        self.assertEqual(finished["deadline_seconds"], 2.0)
        self.assertIn("deadline_at", finished)
        self.assertNotIn("termination_reason", finished)

        follow_prompt = self.write_prompt("deadline-fast-follow.md", "fast follow-up")
        follow_proc, follow = self.run_cli(
            "followup",
            first["worker_id"],
            "--prompt-file",
            str(follow_prompt),
        )
        self.assertEqual(follow_proc.returncode, 0, follow_proc.stderr)
        self.assertNotIn("deadline_seconds", follow)
        self.assertEqual(
            self.wait_state(follow["worker_id"], {"succeeded"})["state"],
            "succeeded",
        )

    def test_deadline_applies_to_codex_adapter(self):
        prompt = self.write_prompt("deadline-codex.md", "SLEEP=30\ncodex deadline")
        proc, spawned = self.spawn(
            "codex",
            prompt,
            "--deadline-seconds",
            "0.2",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

        finished = self.wait_state(spawned["worker_id"], {"failed"}, timeout=6)
        self.assertEqual(finished["termination_reason"], "deadline_exceeded")
        self.assertEqual(finished["deadline_seconds"], 0.2)
        self.assertFalse((self.state_dir / spawned["worker_id"] / "prompt.md").exists())

        collect_proc, collected = self.run_cli("collect", spawned["worker_id"])
        self.assertEqual(collect_proc.returncode, 1, collect_proc.stderr)
        self.assertEqual(collected["termination_reason"], "deadline_exceeded")

    def test_process_group_stop_escalates_after_grace(self):
        ready_path = self.root / "sigterm-ignored.ready"
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    "import pathlib,signal,time; "
                    "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                    f"pathlib.Path({str(ready_path)!r}).touch(); "
                    "time.sleep(30)"
                ),
            ],
            start_new_session=True,
        )
        self.addCleanup(lambda: child.poll() is None and child.kill())
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not ready_path.exists():
            time.sleep(0.01)
        self.assertTrue(ready_path.exists())

        stopped = RUNNER_API["stop_process_group"](
            child.pid,
            grace_seconds=0.1,
            leader=child,
        )

        self.assertTrue(stopped)
        child.wait(timeout=2)
        self.assertEqual(child.returncode, -signal.SIGKILL)

    def test_wrapper_identity_allows_brief_process_table_grace(self):
        check = RUNNER_API["wrapper_identity_matches_with_grace"]
        identity = mock.Mock(side_effect=(False, False, True))
        with mock.patch.dict(
            check.__globals__,
            {
                "is_pid_alive": mock.Mock(return_value=True),
                "wrapper_identity_matches": identity,
            },
        ):
            self.assertTrue(check("queued-worker", 12345))
        self.assertEqual(identity.call_count, 3)

    def test_cancel_finishes_cleanly_when_wrapper_exits_during_identity_check(self):
        self.seed_worker("wrapper-exited-during-cancel", agent="codex", state="running")
        args = argparse.Namespace(worker_id="wrapper-exited-during-cancel", timeout=1)

        with mock.patch.dict(
            os.environ,
            {"AGENT_CLI_WORKERS_STATE_DIR": str(self.state_dir)},
        ):
            metadata = RUNNER_API["finish_cancel_after_wrapper_exit"](args)

        self.assertEqual(metadata["state"], "cancelled")

    def test_cancel_terminates_agent_descendants(self):
        child_pid_path = self.root / "descendant.pid"
        prompt = self.write_prompt(
            "descendant.md",
            f"SPAWN_CHILD_PID={child_pid_path}\nSLEEP=30\ncancel descendants",
        )
        proc, spawned = self.spawn("grok", prompt)
        self.assertEqual(proc.returncode, 0, proc.stderr)

        deadline = time.monotonic() + 4
        while time.monotonic() < deadline and not child_pid_path.exists():
            time.sleep(0.05)
        self.assertTrue(child_pid_path.exists())
        child_pid = int(child_pid_path.read_text())

        def kill_leaked_child():
            if self.process_is_alive(child_pid):
                os.kill(child_pid, signal.SIGKILL)

        self.addCleanup(kill_leaked_child)
        cancel_proc, cancelled = self.run_cli(
            "cancel",
            spawned["worker_id"],
            "--timeout",
            "3",
        )
        self.assertEqual(cancel_proc.returncode, 0, cancel_proc.stderr)
        self.assertEqual(cancelled["state"], "cancelled")

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and self.process_is_alive(child_pid):
            time.sleep(0.05)
        self.assertFalse(self.process_is_alive(child_pid))

    def test_cancel_refuses_pid_that_is_not_our_wrapper(self):
        worker_id = "identity-guard"
        worker_dir = self.state_dir / worker_id
        worker_dir.mkdir(parents=True)
        metadata = {
            "schema_version": 1,
            "worker_id": worker_id,
            "agent": "codex",
            "state": "running",
            "wrapper_pid": os.getpid(),
            "cwd": str(self.work_dir),
        }
        (worker_dir / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")

        status_proc, status = self.run_cli("status", worker_id)
        self.assertEqual(status_proc.returncode, 0)
        self.assertEqual(status["state"], "lost")
        self.assertTrue(status["wrapper_identity_mismatch"])

        proc, payload = self.run_cli("cancel", worker_id, "--timeout", "1")
        self.assertEqual(proc.returncode, 5)
        self.assertEqual(payload["error"], "process_identity_mismatch")

    def test_orphaned_codex_blocks_cleanup_and_can_be_cancelled(self):
        worker_id = "codex-orphan-guard"
        worker_dir = self.state_dir / worker_id
        worker_dir.mkdir(parents=True)
        final_path = worker_dir / "final.txt"
        codex = subprocess.Popen(
            [
                str((self.bin_dir / "codex").resolve()),
                "exec",
                "--json",
                "-o",
                str(final_path),
                "-",
            ],
            env=self.env,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        )
        self.addCleanup(lambda: codex.poll() is None and codex.kill())
        metadata = {
            "schema_version": 1,
            "worker_id": worker_id,
            "agent": "codex",
            "state": "running",
            "wrapper_pid": 99999999,
            "process_group_id": codex.pid,
            "agent_pid": codex.pid,
            "binary": str((self.bin_dir / "codex").resolve()),
            "cwd": str(self.work_dir),
            "result_path": str(worker_dir / "stdout.jsonl"),
            "stderr_path": str(worker_dir / "stderr.log"),
        }
        (worker_dir / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")

        status_proc, status = self.run_cli("status", worker_id)
        self.assertEqual(status_proc.returncode, 0, status_proc.stderr)
        self.assertEqual(status["state"], "orphaned")

        cleanup_proc, cleanup = self.run_cli("cleanup", worker_id)
        self.assertEqual(cleanup_proc.returncode, 4)
        self.assertEqual(cleanup["error"], "worker_not_terminal")

        cancel_proc, cancelled = self.run_cli("cancel", worker_id, "--timeout", "3")
        self.assertEqual(cancel_proc.returncode, 0, cancel_proc.stderr)
        self.assertEqual(cancelled["state"], "cancelled")
        codex.wait(timeout=3)
        codex.stdin.close()

    def test_adapter_specific_flags_are_rejected(self):
        prompt = self.write_prompt("flags.md", "flags")
        proc, payload = self.spawn("codex", prompt, "--permission-mode", "auto")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(payload["error"], "unsupported_option")

        proc, payload = self.spawn("codex-spark", prompt)
        self.assertEqual(proc.returncode, 2)
        self.assertIsNone(payload)

        proc, payload = self.spawn("grok", prompt, "--dangerously-bypass-approvals-and-sandbox")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(payload["error"], "unsupported_option")


if __name__ == "__main__":
    unittest.main()
