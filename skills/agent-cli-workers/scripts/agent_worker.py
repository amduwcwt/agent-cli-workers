#!/usr/bin/env python3
"""Durable asynchronous workers for Grok Build CLI and Codex CLI.

Public commands emit one JSON object on stdout. Prompt bodies live only in a
private worker file and are passed to agent CLIs by file or stdin, never in
worker metadata or detached-wrapper argv.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import time
import traceback
import uuid

try:
    import fcntl
except ImportError:  # pragma: no cover - Unix lifecycle is the supported path.
    fcntl = None


SCHEMA_VERSION = 2
RUNNER_VERSION = "0.4.0"
HISTORY_SCHEMA_VERSION = 1
DEFAULT_SANDBOX = "read-only"
DEADLINE_TERMINATION_GRACE_SECONDS = 3.0
AGENTS = ("grok", "codex")
CODEX_AGENTS = frozenset(("codex",))
TERMINAL_STATES = {"succeeded", "failed", "cancelled", "lost"}
ACTIVE_STATES = {"queued", "running", "orphaned"}
WORKER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
MAX_PROMPT_BYTES = 1024 * 1024
DEFAULT_COLLECT_BYTES = 2 * 1024 * 1024
MAX_COMPLETION_CAPSULE_BYTES = 16 * 1024
MAX_TELEMETRY_CLASSIFY_BYTES = 64 * 1024
SCRIPT_PATH = Path(__file__).resolve()
TASK_CLASSES = (
    "unknown",
    "research",
    "investigation",
    "review",
    "implementation",
    "test-repair",
    "refactor",
    "other",
)
ROUTE_REASONS = (
    "unknown",
    "fast-readonly",
    "default-writer",
    "user-provider-choice",
    "independent-review",
    "followup",
    "other",
)
CONTROLLER_OUTCOMES = ("accepted", "partial", "rejected")
VERIFICATION_OUTCOMES = ("passed", "failed", "not-run")
OUTCOME_REASON_CODES = (
    "none",
    "wrong-provider",
    "workspace-mismatch",
    "verification-failed",
    "completion-capsule-invalid",
    "provider-error",
    "deadline-exceeded",
    "max-turns",
    "too-slow",
    "redundant-authorization-prompt",
    "scope-mismatch",
    "other",
)
FAILURE_CLASSES = (
    "none",
    "deadline_exceeded",
    "cancelled",
    "lost",
    "max_turns",
    "provider_noncompletion",
    "provider_http_403",
    "provider_http_429",
    "provider_http_503",
    "runner_error",
    "exit_nonzero",
    "failed_unknown",
)
USAGE_FIELDS = ("input_tokens", "output_tokens", "cached_input_tokens", "total_tokens")
GROK_RESULT_FIELDS = (
    "text",
    "stopReason",
    "sessionId",
    "requestId",
    "usage",
    "modelUsage",
)
CAPSULE_FIELDS = ("STATUS", "WORKSPACE", "SUMMARY", "FILES", "VERIFY", "RISKS")
COMPLETION_CAPSULE_RE = re.compile(
    r"^(?:STATUS:|\*\*STATUS:\*\*|\*\*STATUS\*\*:)[ \t]*(succeeded|blocked|failed)[ \t]*\r?\n"
    r"(?:WORKSPACE:|\*\*WORKSPACE:\*\*|\*\*WORKSPACE\*\*:)[ \t]*(.+?)[ \t]*\r?\n"
    r"(?:SUMMARY:|\*\*SUMMARY:\*\*|\*\*SUMMARY\*\*:)[ \t]*(.+?)[ \t]*\r?\n"
    r"(?:FILES:|\*\*FILES:\*\*|\*\*FILES\*\*:)[ \t]*(.+?)[ \t]*\r?\n"
    r"(?:VERIFY:|\*\*VERIFY:\*\*|\*\*VERIFY\*\*:)[ \t]*(.+?)[ \t]*\r?\n"
    r"(?:RISKS:|\*\*RISKS:\*\*|\*\*RISKS\*\*:)[ \t]*(.+?)[ \t]*(?=\r?$)",
    re.MULTILINE,
)
CAPSULE_CONTRACT_MARKER = "--- agent-cli-workers completion contract ---"


class RunnerError(Exception):
    def __init__(self, error: str, message: str, exit_code: int = 2):
        super().__init__(message)
        self.error = error
        self.message = message
        self.exit_code = exit_code


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def emit(payload: dict) -> None:
    json.dump(payload, sys.stdout, ensure_ascii=False, sort_keys=True)
    sys.stdout.write("\n")


def state_root() -> Path:
    override = os.environ.get("AGENT_CLI_WORKERS_STATE_DIR")
    if override:
        candidate = Path(override).expanduser()
    else:
        codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        candidate = codex_home / "state" / "agent-cli-workers" / "workers"
    if candidate.is_symlink():
        raise RunnerError("unsafe_state_dir", "state directory must not be a symlink", 5)
    try:
        candidate.mkdir(parents=True, exist_ok=True, mode=0o700)
        root = candidate.resolve()
        root_stat = root.stat()
    except OSError as exc:
        raise RunnerError("state_dir_unavailable", f"cannot initialize state directory: {exc}", 5) from exc
    if not stat.S_ISDIR(root_stat.st_mode) or root_stat.st_uid != os.getuid():
        raise RunnerError("unsafe_state_dir", "state directory must be an owned directory", 5)
    if stat.S_IMODE(root_stat.st_mode) & 0o077:
        raise RunnerError(
            "insecure_state_dir",
            "state directory must not grant group or other access",
            5,
        )
    return root


def telemetry_enabled() -> bool:
    value = os.environ.get("AGENT_CLI_WORKERS_TELEMETRY", "1").strip().casefold()
    return value not in {"0", "false", "no", "off"}


def history_root() -> Path:
    override = os.environ.get("AGENT_CLI_WORKERS_HISTORY_DIR")
    if override:
        candidate = Path(override).expanduser()
    else:
        candidate = state_root().parent / "history"
    if candidate.is_symlink():
        raise RunnerError("unsafe_history_dir", "history directory must not be a symlink", 5)
    try:
        candidate.mkdir(parents=True, exist_ok=True, mode=0o700)
        root = candidate.resolve()
        root_stat = root.stat()
    except OSError as exc:
        raise RunnerError("history_dir_unavailable", f"cannot initialize history directory: {exc}", 5) from exc
    workers = state_root()
    if root == workers or workers in root.parents or root in workers.parents:
        raise RunnerError("unsafe_history_dir", "history directory must be separate from worker state", 5)
    if not stat.S_ISDIR(root_stat.st_mode) or root_stat.st_uid != os.getuid():
        raise RunnerError("unsafe_history_dir", "history directory must be an owned directory", 5)
    if stat.S_IMODE(root_stat.st_mode) & 0o077:
        raise RunnerError(
            "insecure_history_dir",
            "history directory must not grant group or other access",
            5,
        )
    return root


def history_path(worker_id: str) -> Path:
    validate_worker_id(worker_id)
    root = history_root()
    path = root / f"{worker_id}.json"
    if path.is_symlink() or path.parent != root:
        raise RunnerError("unsafe_history_path", "history summary path is unsafe", 5)
    return path


def skill_sha256() -> str | None:
    path = SCRIPT_PATH.parents[1] / "SKILL.md"
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def validate_worker_id(worker_id: str) -> None:
    if not WORKER_ID_RE.fullmatch(worker_id):
        raise RunnerError("invalid_worker_id", "worker id contains unsafe characters")


def worker_dir(worker_id: str, *, require_exists: bool = True) -> Path:
    validate_worker_id(worker_id)
    root = state_root()
    candidate = root / worker_id
    if candidate.is_symlink():
        raise RunnerError("unsafe_worker_path", "worker directory must not be a symlink", 5)
    if candidate.parent != root:
        raise RunnerError("invalid_worker_id", "worker id escapes the state directory")
    if require_exists and not candidate.is_dir():
        raise RunnerError("worker_not_found", f"worker {worker_id!r} was not found")
    return candidate


@contextmanager
def worker_lock(directory: Path):
    lock_path = directory / ".lock"
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    except FileNotFoundError as exc:
        raise RunnerError("worker_not_found", f"state directory {directory.name!r} was not found") from exc
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def atomic_write_json(path: Path, payload: dict) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load_metadata(worker_id: str) -> dict:
    directory = worker_dir(worker_id)
    path = directory / "metadata.json"
    try:
        metadata = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RunnerError("worker_not_found", f"worker {worker_id!r} was not found") from exc
    except json.JSONDecodeError as exc:
        raise RunnerError("metadata_corrupt", f"worker {worker_id!r} metadata is invalid", 5) from exc
    if metadata.get("worker_id") != worker_id:
        raise RunnerError("metadata_mismatch", "metadata worker id does not match its directory", 5)
    for key in ("result_path", "stderr_path", "final_output_path", "wrapper_log_path"):
        value = metadata.get(key)
        if not value:
            continue
        artifact = Path(value).expanduser()
        if artifact.is_symlink() or artifact.resolve().parent != directory:
            raise RunnerError("unsafe_metadata_path", f"metadata {key} escapes its worker directory", 5)
    return metadata


def update_metadata(worker_id: str, **changes) -> dict:
    directory = worker_dir(worker_id)
    with worker_lock(directory):
        metadata = load_metadata(worker_id)
        metadata.update(changes)
        metadata["updated_at"] = utc_now()
        atomic_write_json(directory / "metadata.json", metadata)
    return metadata


def write_private_text(path: Path, content: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def open_private(path: Path, mode: str):
    flags = os.O_WRONLY | os.O_CREAT
    flags |= os.O_APPEND if "a" in mode else os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    return os.fdopen(fd, mode, encoding="utf-8")


def read_prompt(args: argparse.Namespace) -> str:
    if getattr(args, "prompt", None) is not None:
        prompt = args.prompt
    elif getattr(args, "prompt_file", None) is not None:
        source = Path(args.prompt_file).expanduser()
        try:
            raw = source.read_bytes()
        except OSError as exc:
            raise RunnerError("prompt_read_failed", f"cannot read prompt file: {exc}") from exc
        if len(raw) > MAX_PROMPT_BYTES:
            raise RunnerError("prompt_too_large", "prompt exceeds 1 MiB")
        try:
            prompt = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RunnerError("prompt_not_utf8", "prompt file must be UTF-8") from exc
    else:
        raw = sys.stdin.buffer.read(MAX_PROMPT_BYTES + 1)
        if len(raw) > MAX_PROMPT_BYTES:
            raise RunnerError("prompt_too_large", "prompt exceeds 1 MiB")
        try:
            prompt = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RunnerError("prompt_not_utf8", "stdin prompt must be UTF-8") from exc
    try:
        prompt_bytes = prompt.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise RunnerError("prompt_not_utf8", "prompt must be valid UTF-8") from exc
    if len(prompt_bytes) > MAX_PROMPT_BYTES:
        raise RunnerError("prompt_too_large", "prompt exceeds 1 MiB")
    if not prompt.strip():
        raise RunnerError("prompt_empty", "prompt must not be empty")
    return prompt


def inject_completion_contract(prompt: str, cwd: str) -> str:
    contract = f"""{CAPSULE_CONTRACT_MARKER}
Before inspecting the task, run `pwd` and `git rev-parse --show-toplevel` and record the launch
HEAD. If this is intentionally non-Git work, use root=non-git; base=none; head=none.

Return only this final completion capsule:
STATUS: succeeded|blocked|failed
WORKSPACE: pwd=<path>; root=<path>; base=<launch-sha>; head=<final-sha>
SUMMARY: <one or two concise sentences; reviews should include high-signal file:line findings>
FILES: <comma-separated paths or none>
VERIFY: <command => exit code; or not run with reason>
RISKS: <none or concise unresolved risks>

The requested working directory is {cwd}.
"""
    return f"{prompt.rstrip()}\n\n{contract}"


def resolve_binary(agent: str, value: str | None) -> str:
    name = "grok" if agent == "grok" else "codex"
    if value:
        candidate = Path(value).expanduser()
        resolved = str(candidate.resolve()) if candidate.parent != Path(".") else shutil.which(value)
    else:
        resolved = shutil.which(name)
    if not resolved or not os.path.isfile(resolved) or not os.access(resolved, os.X_OK):
        raise RunnerError(f"{name}_not_found", f"cannot find an executable {name} binary")
    return str(Path(resolved).resolve())


def infer_agent(metadata: dict) -> str:
    agent = metadata.get("agent")
    if agent in AGENTS:
        return agent
    if metadata.get("codex_pid") or metadata.get("codex_binary"):
        return "codex"
    return "grok"  # Schema-v1 records were Grok-only.


def is_codex_agent(agent: str) -> bool:
    return agent in CODEX_AGENTS


def disabled_model_prefixes() -> tuple[str, ...]:
    raw = os.environ.get("AGENT_CLI_WORKERS_DISABLED_MODEL_PREFIXES", "")
    return tuple(value.strip().casefold() for value in raw.split(",") if value.strip())


def validated_model(model: str | None) -> str | None:
    effective_model = model.strip() if model and model.strip() else None
    if effective_model and effective_model.casefold().startswith(disabled_model_prefixes()):
        raise RunnerError(
            "model_disabled",
            "the requested model is disabled by AGENT_CLI_WORKERS_DISABLED_MODEL_PREFIXES",
            4,
        )
    return effective_model


def configured_codex_model() -> str | None:
    return validated_model(os.environ.get("AGENT_CLI_WORKERS_CODEX_MODEL"))


def agent_pid(metadata: dict) -> int | None:
    return metadata.get("agent_pid") or metadata.get("grok_pid") or metadata.get("codex_pid")


def agent_binary(metadata: dict) -> str:
    return metadata.get("binary") or metadata.get("grok_binary") or metadata.get("codex_binary") or ""


def new_worker_id(agent: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"{agent}-{stamp}-{uuid.uuid4().hex[:8]}"


def public_metadata(metadata: dict) -> dict:
    keys = (
        "schema_version",
        "worker_id",
        "agent",
        "state",
        "cwd",
        "created_at",
        "started_at",
        "finished_at",
        "updated_at",
        "wrapper_pid",
        "wrapper_identity_mismatch",
        "process_group_id",
        "agent_pid",
        "agent_process_group_id",
        "grok_pid",
        "codex_pid",
        "exit_code",
        "session_id",
        "parent_worker_id",
        "task_class",
        "route_reason",
        "deadline_seconds",
        "deadline_at",
        "timed_out_at",
        "termination_reason",
        "model",
        "prompt_sha256",
        "result_path",
        "stderr_path",
        "result_truncated",
        "runner_error",
    )
    payload = {key: metadata.get(key) for key in keys if metadata.get(key) is not None}
    payload.setdefault("agent", infer_agent(metadata))
    return payload


def ensure_supported_options(agent: str, args: argparse.Namespace) -> None:
    if is_codex_agent(agent):
        if getattr(args, "permission_mode", None) is not None:
            raise RunnerError("unsupported_option", "--permission-mode is only supported by Grok")
        if getattr(args, "max_turns", None) is not None:
            raise RunnerError("unsupported_option", "--max-turns is only supported by Grok")
    elif agent == "grok":
        if getattr(args, "dangerously_bypass_approvals_and_sandbox", False):
            raise RunnerError(
                "unsupported_option",
                "--dangerously-bypass-approvals-and-sandbox is only supported by Codex",
            )


def create_worker(
    *,
    agent: str,
    prompt: str,
    cwd: str,
    binary: str | None,
    model: str | None,
    permission_mode: str | None,
    max_turns: int | None,
    deadline_seconds: float | None,
    task_class: str,
    route_reason: str,
    sandbox: str | None,
    dangerously_bypass_approvals_and_sandbox: bool,
    resume_session_id: str | None = None,
    parent_worker_id: str | None = None,
) -> dict:
    working_directory = Path(cwd).expanduser().resolve()
    if not working_directory.is_dir():
        raise RunnerError("cwd_not_found", f"working directory does not exist: {working_directory}")
    if max_turns is not None and max_turns < 2:
        raise RunnerError("max_turns_too_low", "max-turns must be at least 2; omit it for the default")
    if deadline_seconds is not None and (
        not math.isfinite(deadline_seconds) or deadline_seconds <= 0
    ):
        raise RunnerError("invalid_deadline", "deadline-seconds must be finite and greater than zero")
    if dangerously_bypass_approvals_and_sandbox and sandbox:
        raise RunnerError("conflicting_options", "dangerous bypass cannot be combined with --sandbox")

    effective_model = validated_model(model)
    if is_codex_agent(agent) and effective_model is None:
        effective_model = configured_codex_model()

    resolved_binary = resolve_binary(agent, binary)
    worker_id = new_worker_id(agent)
    directory = worker_dir(worker_id, require_exists=False)
    directory.mkdir(mode=0o700)
    prompt_path = directory / "prompt.md"
    write_private_text(prompt_path, prompt)

    created_at = utc_now()
    result_name = "stdout.json" if agent == "grok" else "stdout.jsonl"
    effective_sandbox = sandbox
    if not dangerously_bypass_approvals_and_sandbox and sandbox is None:
        effective_sandbox = DEFAULT_SANDBOX
    metadata = {
        "schema_version": SCHEMA_VERSION,
        "runner_version": RUNNER_VERSION,
        "worker_id": worker_id,
        "agent": agent,
        "state": "queued",
        "cwd": str(working_directory),
        "created_at": created_at,
        "updated_at": created_at,
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "skill_sha256": skill_sha256(),
        "binary": resolved_binary,
        "permission_mode": permission_mode or "default",
        "max_turns": max_turns,
        "deadline_seconds": deadline_seconds,
        "task_class": task_class,
        "route_reason": route_reason,
        "model": effective_model,
        "sandbox": effective_sandbox,
        "dangerously_bypass_approvals_and_sandbox": dangerously_bypass_approvals_and_sandbox,
        "resume_session_id": resume_session_id,
        "parent_worker_id": parent_worker_id,
        "result_path": str(directory / result_name),
        "final_output_path": str(directory / "final.txt") if is_codex_agent(agent) else None,
        "stderr_path": str(directory / "stderr.log"),
        "wrapper_log_path": str(directory / "wrapper.log"),
    }
    atomic_write_json(directory / "metadata.json", metadata)

    env = os.environ.copy()
    env["AGENT_CLI_WORKERS_STATE_DIR"] = str(state_root())
    wrapper_log = open_private(directory / "wrapper.log", "w")
    try:
        wrapper = subprocess.Popen(
            [sys.executable, str(SCRIPT_PATH), "_run", worker_id],
            stdin=subprocess.DEVNULL,
            stdout=wrapper_log,
            stderr=subprocess.STDOUT,
            env=env,
            cwd=str(working_directory),
            start_new_session=True,
            close_fds=True,
        )
    except Exception:
        prompt_path.unlink(missing_ok=True)
        shutil.rmtree(directory, ignore_errors=True)
        raise
    finally:
        wrapper_log.close()

    return update_metadata(
        worker_id,
        wrapper_pid=wrapper.pid,
        process_group_id=wrapper.pid,
        wrapper_started_at=utc_now(),
    )


def grok_argv(metadata: dict, prompt_path: Path) -> list[str]:
    argv = [
        agent_binary(metadata),
        "--cwd",
        metadata["cwd"],
        "--no-plan",
        "--no-memory",
        "--no-subagents",
        "--output-format",
        "json",
    ]
    permission_mode = metadata.get("permission_mode")
    if permission_mode and permission_mode != "default":
        argv.extend(["--permission-mode", permission_mode])
    if metadata.get("max_turns") is not None:
        argv.extend(["--max-turns", str(metadata["max_turns"])])
    if metadata.get("sandbox"):
        argv.extend(["--sandbox", metadata["sandbox"]])
    model = validated_model(metadata.get("model"))
    if model:
        argv.extend(["--model", model])
    if metadata.get("resume_session_id"):
        argv.extend(["--resume", metadata["resume_session_id"]])
    argv.extend(["--prompt-file", str(prompt_path)])
    return argv


def codex_argv(metadata: dict) -> list[str]:
    directory = worker_dir(metadata["worker_id"])
    final_path = Path(metadata.get("final_output_path") or directory / "final.txt")
    session_id = metadata.get("resume_session_id")
    argv = [agent_binary(metadata), "exec"]
    # Sandbox is an `exec` option in Codex 0.144.x, not an `exec resume`
    # option, so it must precede the resume subcommand.
    if metadata.get("dangerously_bypass_approvals_and_sandbox"):
        argv.append("--dangerously-bypass-approvals-and-sandbox")
    elif metadata.get("sandbox"):
        argv.extend(["--sandbox", metadata["sandbox"]])
    if session_id:
        argv.extend(["resume", "--skip-git-repo-check"])
    else:
        argv.extend(["--skip-git-repo-check", "-C", metadata["cwd"]])
    model = validated_model(metadata.get("model"))
    if model:
        argv.extend(["-m", model])
    argv.extend(["--json", "-o", str(final_path)])
    if session_id:
        argv.append(session_id)
    argv.append("-")
    return argv


def parse_grok_result(path: Path, max_bytes: int) -> tuple[dict | None, bool]:
    raw, truncated = read_limited(path, max_bytes)
    if truncated:
        return None, True
    raw = raw.strip()
    if not raw:
        return None, False
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None, False
    if not isinstance(value, dict):
        return None, False
    return {key: value[key] for key in GROK_RESULT_FIELDS if key in value}, False


def parse_codex_result(metadata: dict, max_bytes: int) -> tuple[dict | None, bool]:
    thread_id = None
    message = None
    usage = None
    path = Path(metadata["result_path"])
    raw, truncated = read_limited(path, max_bytes)
    lines = raw.splitlines()
    if truncated and raw and not raw.endswith(("\n", "\r")):
        lines = lines[:-1]
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        event_type = event.get("type")
        if event_type == "thread.started":
            thread_id = event.get("thread_id") or thread_id
        elif event_type == "item.completed":
            item = event.get("item") or {}
            if item.get("type") == "agent_message":
                message = item.get("text") or message
        elif event_type == "turn.completed":
            usage = event.get("usage") or usage
    final_path = Path(metadata.get("final_output_path") or worker_dir(metadata["worker_id"]) / "final.txt")
    if message is None:
        message, final_truncated = read_limited(final_path, max_bytes)
        truncated = truncated or final_truncated
        if not message:
            message = None
    if thread_id is None and message is None and usage is None:
        return None, truncated
    result = {"text": message, "threadId": thread_id}
    if usage is not None:
        result["usage"] = usage
    return result, truncated


def parsed_result(metadata: dict, max_bytes: int = DEFAULT_COLLECT_BYTES) -> tuple[dict | None, bool]:
    if infer_agent(metadata) == "grok":
        return parse_grok_result(Path(metadata["result_path"]), max_bytes)
    return parse_codex_result(metadata, max_bytes)


def extract_completion_capsule(text: object) -> str | None:
    if not isinstance(text, str):
        return None
    matches = list(COMPLETION_CAPSULE_RE.finditer(text))
    if not matches:
        return None
    values = tuple(value.strip() for value in matches[-1].groups())
    if any(not value for value in values):
        return None
    capsule = "\n".join(f"{field}: {value}" for field, value in zip(CAPSULE_FIELDS, values))
    return capsule if len(capsule.encode("utf-8")) <= MAX_COMPLETION_CAPSULE_BYTES else None


def compact_capsule_payload(metadata: dict, result: dict | None) -> dict:
    payload = {
        "worker_id": metadata["worker_id"],
        "agent": infer_agent(metadata),
        "state": metadata.get("state"),
        "exit_code": metadata.get("exit_code"),
        "session_id": metadata.get("session_id"),
    }
    if result and result.get("requestId") is not None:
        payload["request_id"] = result["requestId"]
    for key in (
        "deadline_seconds",
        "deadline_at",
        "timed_out_at",
        "termination_reason",
        "runner_error",
    ):
        if metadata.get(key) is not None:
            payload[key] = metadata[key]
    return payload


def parse_utc(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def file_size(value: object) -> int:
    if not isinstance(value, str) or not value:
        return 0
    try:
        size = Path(value).stat().st_size
    except OSError:
        return 0
    return max(0, int(size))


def canonical_usage(result: dict | None) -> dict | None:
    usage = result.get("usage") if isinstance(result, dict) else None
    if not isinstance(usage, dict):
        return None
    normalized = {}
    for key in USAGE_FIELDS:
        value = usage.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            continue
        normalized[key] = value
    return normalized or None


def capsule_metrics(result: dict | None) -> dict:
    text = result.get("text") if isinstance(result, dict) else None
    capsule = extract_completion_capsule(text)
    if capsule is not None:
        return {"status": "valid", "bytes": len(capsule.encode("utf-8"))}
    if isinstance(text, str) and text:
        return {"status": "invalid", "bytes": 0}
    return {"status": "missing", "bytes": 0}


def failure_class(metadata: dict, result: dict | None) -> str:
    if metadata.get("termination_reason") == "deadline_exceeded":
        return "deadline_exceeded"
    state = metadata.get("state")
    if state == "cancelled":
        return "cancelled"
    if state == "lost":
        return "lost"
    stop_reason = result.get("stopReason") if isinstance(result, dict) else None
    if isinstance(stop_reason, str) and stop_reason not in ("", "EndTurn"):
        normalized = stop_reason.casefold()
        if "turn" in normalized and ("max" in normalized or "limit" in normalized):
            return "max_turns"
        return "provider_noncompletion"
    exit_code = metadata.get("exit_code")
    is_failure = (
        state == "failed"
        or bool(metadata.get("runner_error"))
        or (isinstance(exit_code, int) and not isinstance(exit_code, bool) and exit_code != 0)
    )
    if not is_failure:
        return "none"
    stderr, _ = read_limited(Path(metadata.get("stderr_path") or ""), MAX_TELEMETRY_CLASSIFY_BYTES)
    lowered = stderr.casefold()
    if re.search(r"\bmax(?:imum)?[-_ ]*turns?\b|\bturns?[-_ ]*limit\b", lowered):
        return "max_turns"
    for status in (403, 429, 503):
        if f"status {status}" in lowered or f"http_status\": {status}" in lowered:
            return f"provider_http_{status}"
    if metadata.get("runner_error"):
        return "runner_error"
    if isinstance(exit_code, int) and exit_code != 0:
        return "exit_nonzero"
    if state == "failed":
        return "failed_unknown"
    return "none"


def load_history_summary(worker_id: str, *, require_exists: bool = True) -> dict | None:
    path = history_path(worker_id)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        if require_exists:
            raise RunnerError("history_not_found", f"history for worker {worker_id!r} was not found", 4)
        return None
    except OSError as exc:
        raise RunnerError("history_read_failed", f"cannot read worker history: {exc}", 5) from exc
    try:
        summary = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RunnerError("history_corrupt", f"history for worker {worker_id!r} is invalid", 5) from exc
    if not isinstance(summary, dict) or summary.get("worker_id") != worker_id:
        raise RunnerError("history_corrupt", f"history for worker {worker_id!r} is invalid", 5)
    if summary.get("history_schema_version") != HISTORY_SCHEMA_VERSION:
        raise RunnerError(
            "history_schema_unsupported",
            f"history for worker {worker_id!r} uses an unsupported schema",
            5,
        )
    return summary


def build_history_summary(metadata: dict, existing: dict | None = None) -> dict:
    result, result_truncated = parsed_result(metadata)
    started_at = parse_utc(metadata.get("started_at"))
    finished_at = parse_utc(metadata.get("finished_at"))
    duration_seconds = None
    if started_at is not None and finished_at is not None:
        duration_seconds = max(0.0, round((finished_at - started_at).total_seconds(), 6))
    recorded_at = (existing or {}).get("recorded_at") or utc_now()
    existing_controller = (existing or {}).get("controller")
    preserve_controller_labels = (
        isinstance(existing_controller, dict)
        and existing_controller.get("provenance") == "controller"
    )
    task_class = metadata.get("task_class") or "unknown"
    route_reason = metadata.get("route_reason") or "unknown"
    if preserve_controller_labels and (existing or {}).get("task_class") in TASK_CLASSES:
        task_class = existing["task_class"]
    if preserve_controller_labels and (existing or {}).get("route_reason") in ROUTE_REASONS:
        route_reason = existing["route_reason"]
    summary = {
        "history_schema_version": HISTORY_SCHEMA_VERSION,
        "worker_id": metadata["worker_id"],
        "recorded_at": recorded_at,
        "updated_at": utc_now(),
        "finished_at": metadata.get("finished_at"),
        "runner_version": metadata.get("runner_version"),
        "skill_sha256": metadata.get("skill_sha256") or skill_sha256(),
        "agent": infer_agent(metadata),
        "model": metadata.get("model"),
        "sandbox": metadata.get("sandbox"),
        "task_class": task_class,
        "route_reason": route_reason,
        "state": metadata.get("state"),
        "failure_class": failure_class(metadata, result),
        "exit_code": metadata.get("exit_code"),
        "duration_seconds": duration_seconds,
        "parent_worker_id": metadata.get("parent_worker_id"),
        "usage": canonical_usage(result),
        "artifacts": {
            "stdout_bytes": file_size(metadata.get("result_path")),
            "stderr_bytes": file_size(metadata.get("stderr_path")),
            "wrapper_log_bytes": file_size(metadata.get("wrapper_log_path")),
            "final_output_bytes": file_size(metadata.get("final_output_path")),
        },
        "capsule": capsule_metrics(result),
        "result_truncated": bool(metadata.get("result_truncated") or result_truncated),
        "controller": existing_controller,
    }
    return summary


def history_source_directory(metadata: dict) -> Path | None:
    result_path = metadata.get("result_path")
    if not isinstance(result_path, str) or not result_path:
        return None
    return Path(result_path).parent


def snapshot_history_locked(metadata: dict) -> dict | None:
    existing = load_history_summary(metadata["worker_id"], require_exists=False)
    source_directory = history_source_directory(metadata)
    if source_directory is None or not source_directory.is_dir():
        return existing
    result_path = metadata.get("result_path")
    stderr_path = metadata.get("stderr_path")
    if existing is not None and (
        not isinstance(result_path, str)
        or not Path(result_path).is_file()
        or not isinstance(stderr_path, str)
        or not Path(stderr_path).is_file()
    ):
        return existing
    summary = build_history_summary(metadata, existing)
    atomic_write_json(history_path(metadata["worker_id"]), summary)
    return summary


def snapshot_history(metadata: dict) -> dict | None:
    if not telemetry_enabled():
        return None
    root = history_root()
    with worker_lock(root):
        return snapshot_history_locked(metadata)


def best_effort_snapshot_history(metadata: dict) -> None:
    if not telemetry_enabled():
        return
    try:
        snapshot_history(metadata)
    except Exception:
        traceback.print_exc()


def discard_history(worker_id: str) -> None:
    try:
        path = history_path(worker_id)
    except RunnerError:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def canonical_history_summary(summary: dict) -> dict:
    def safe_string(value: object, *, maximum: int = 128) -> str | None:
        if not isinstance(value, str) or not value or len(value) > maximum:
            return None
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/+@-]*", value):
            return None
        return value

    def safe_timestamp(value: object) -> str | None:
        return value if isinstance(value, str) and parse_utc(value) is not None else None

    def safe_nonnegative_number(value: object) -> int | float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        if not math.isfinite(value) or value < 0:
            return None
        return value

    def safe_nonnegative_int(value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return 0
        return value

    worker_id = summary["worker_id"]
    artifacts = summary.get("artifacts") if isinstance(summary.get("artifacts"), dict) else {}
    capsule = summary.get("capsule") if isinstance(summary.get("capsule"), dict) else {}
    capsule_status = capsule.get("status")
    if capsule_status not in {"valid", "invalid", "missing"}:
        capsule_status = "missing"
    parent_worker_id = summary.get("parent_worker_id")
    if not isinstance(parent_worker_id, str) or not WORKER_ID_RE.fullmatch(parent_worker_id):
        parent_worker_id = None
    exit_code = summary.get("exit_code")
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        exit_code = None
    skill_hash = summary.get("skill_sha256")
    if not isinstance(skill_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", skill_hash):
        skill_hash = None
    return {
        "history_schema_version": HISTORY_SCHEMA_VERSION,
        "worker_id": worker_id,
        "recorded_at": safe_timestamp(summary.get("recorded_at")) or utc_now(),
        "updated_at": utc_now(),
        "finished_at": safe_timestamp(summary.get("finished_at")),
        "runner_version": safe_string(summary.get("runner_version")),
        "skill_sha256": skill_hash,
        "agent": summary.get("agent") if summary.get("agent") in AGENTS else "unknown",
        "model": safe_string(summary.get("model")),
        "sandbox": (
            summary.get("sandbox")
            if summary.get("sandbox") in {"read-only", "workspace-write", "danger-full-access"}
            else None
        ),
        "task_class": (
            summary.get("task_class") if summary.get("task_class") in TASK_CLASSES else "unknown"
        ),
        "route_reason": (
            summary.get("route_reason")
            if summary.get("route_reason") in ROUTE_REASONS
            else "unknown"
        ),
        "state": (
            summary.get("state")
            if summary.get("state") in TERMINAL_STATES | ACTIVE_STATES
            else "unknown"
        ),
        "failure_class": (
            summary.get("failure_class")
            if summary.get("failure_class") in FAILURE_CLASSES
            else "unknown"
        ),
        "exit_code": exit_code,
        "duration_seconds": safe_nonnegative_number(summary.get("duration_seconds")),
        "parent_worker_id": parent_worker_id,
        "usage": canonical_usage({"usage": summary.get("usage")}),
        "artifacts": {
            key: safe_nonnegative_int(artifacts.get(key))
            for key in (
                "stdout_bytes",
                "stderr_bytes",
                "wrapper_log_bytes",
                "final_output_bytes",
            )
        },
        "capsule": {
            "status": capsule_status,
            "bytes": safe_nonnegative_int(capsule.get("bytes")),
        },
        "result_truncated": bool(summary.get("result_truncated")),
        "controller": None,
    }


def process_group_alive(group_id: int | None) -> bool:
    if not group_id or group_id <= 0:
        return False
    try:
        os.killpg(group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def signal_process_group(group_id: int | None, sig: signal.Signals) -> None:
    if not group_id or group_id <= 0 or group_id == os.getpgrp():
        return
    try:
        os.killpg(group_id, sig)
    except ProcessLookupError:
        pass


def stop_process_group(
    group_id: int | None,
    grace_seconds: float = 1.0,
    leader: subprocess.Popen | None = None,
) -> bool:
    def group_alive() -> bool:
        if leader is not None:
            leader.poll()
        return process_group_alive(group_id)

    if not group_alive():
        return True
    signal_process_group(group_id, signal.SIGTERM)
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline and group_alive():
        time.sleep(0.05)
    if group_alive():
        signal_process_group(group_id, signal.SIGKILL)
        deadline = time.monotonic() + grace_seconds
        while time.monotonic() < deadline and group_alive():
            time.sleep(0.05)
    return not group_alive()


def claim_worker(worker_id: str) -> tuple[Path, dict]:
    directory = worker_dir(worker_id)
    with worker_lock(directory):
        metadata = load_metadata(worker_id)
        if metadata.get("state") != "queued":
            raise RunnerError(
                "worker_already_running",
                f"worker {worker_id!r} has already been claimed",
            )
        metadata.update(
            state="running",
            started_at=utc_now(),
            updated_at=utc_now(),
            wrapper_pid=os.getpid(),
            process_group_id=os.getpgrp(),
        )
        atomic_write_json(directory / "metadata.json", metadata)
    return directory, metadata


def run_worker(worker_id: str) -> int:
    directory, metadata = claim_worker(worker_id)
    prompt_path = directory / "prompt.md"
    stdout_path = Path(metadata["result_path"])
    stderr_path = Path(metadata["stderr_path"])
    cancel_marker = directory / "cancel.requested"

    child: subprocess.Popen | None = None
    child_group_id: int | None = None
    cancelled = False
    deadline_exceeded = False
    timed_out_at = None

    def request_cancel(_signum, _frame):
        nonlocal cancelled
        cancelled = True
        signal_process_group(child_group_id, signal.SIGTERM)

    signal.signal(signal.SIGTERM, request_cancel)
    signal.signal(signal.SIGINT, request_cancel)

    exit_code = 1
    runner_error = None
    prompt_handle = None
    try:
        agent = infer_agent(metadata)
        argv = grok_argv(metadata, prompt_path) if agent == "grok" else codex_argv(metadata)
        if is_codex_agent(agent):
            prompt_handle = prompt_path.open("r", encoding="utf-8")
        with open_private(stdout_path, "w") as stdout_handle, open_private(stderr_path, "w") as stderr_handle:
            child = subprocess.Popen(
                argv,
                cwd=metadata["cwd"],
                stdin=prompt_handle if prompt_handle is not None else subprocess.DEVNULL,
                stdout=stdout_handle,
                stderr=stderr_handle,
                start_new_session=True,
                close_fds=True,
            )
            child_group_id = child.pid
            pid_changes = {
                "agent_pid": child.pid,
                "agent_process_group_id": child_group_id,
            }
            deadline_seconds = metadata.get("deadline_seconds")
            deadline_monotonic = None
            if deadline_seconds is not None:
                deadline_started_at = datetime.now(timezone.utc)
                deadline_monotonic = time.monotonic() + deadline_seconds
                pid_changes["deadline_at"] = (
                    deadline_started_at + timedelta(seconds=deadline_seconds)
                ).isoformat().replace("+00:00", "Z")
            pid_changes["grok_pid" if agent == "grok" else "codex_pid"] = child.pid
            update_metadata(worker_id, **pid_changes)
            if cancelled or cancel_marker.exists():
                signal_process_group(child_group_id, signal.SIGTERM)
            try:
                wait_timeout = (
                    max(0.0, deadline_monotonic - time.monotonic())
                    if deadline_monotonic is not None
                    else None
                )
                exit_code = child.wait(timeout=wait_timeout)
            except subprocess.TimeoutExpired:
                deadline_exceeded = True
                timed_out_at = utc_now()
                update_metadata(
                    worker_id,
                    timed_out_at=timed_out_at,
                    termination_reason="deadline_exceeded",
                )
                stopped = stop_process_group(
                    child_group_id,
                    grace_seconds=DEADLINE_TERMINATION_GRACE_SECONDS,
                    leader=child,
                )
                if not stopped:
                    runner_error = (
                        f"deadline exceeded after {deadline_seconds:g} seconds; "
                        "agent process group did not terminate"
                    )
                else:
                    runner_error = f"deadline exceeded after {deadline_seconds:g} seconds"
                if child.returncode is None:
                    try:
                        child.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        pass
                if child.returncode is not None:
                    exit_code = child.returncode
    except Exception as exc:  # Wrapper failures must remain inspectable.
        runner_error = f"{type(exc).__name__}: {exc}"
        traceback.print_exc()
    finally:
        if not stop_process_group(child_group_id, leader=child) and runner_error is None:
            runner_error = "agent process group did not terminate"
        if prompt_handle is not None:
            prompt_handle.close()
        prompt_path.unlink(missing_ok=True)

    result, result_truncated = parsed_result(metadata)
    if infer_agent(metadata) == "grok":
        session_id = result.get("sessionId") if result else None
    else:
        session_id = result.get("threadId") if result else None
    was_cancelled = cancelled or cancel_marker.exists()
    if was_cancelled:
        state = "cancelled"
        termination_reason = None
    elif deadline_exceeded:
        state = "failed"
        termination_reason = "deadline_exceeded"
    elif runner_error is not None:
        state = "failed"
        termination_reason = None
    elif (
        infer_agent(metadata) == "grok"
        and result is not None
        and result.get("stopReason") not in (None, "EndTurn")
    ):
        state = "failed"
        runner_error = f"Grok reported stopReason={result.get('stopReason')}"
        termination_reason = None
    else:
        state = "succeeded" if exit_code == 0 else "failed"
        termination_reason = None
    final_metadata = update_metadata(
        worker_id,
        state=state,
        exit_code=exit_code,
        finished_at=utc_now(),
        session_id=session_id,
        result_truncated=result_truncated,
        runner_error=runner_error,
        timed_out_at=timed_out_at,
        termination_reason=termination_reason,
    )
    best_effort_snapshot_history(final_metadata)
    return 0 if state == "succeeded" else 1


def is_pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    state = subprocess.run(
        ["ps", "-p", str(pid), "-o", "stat="],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if state.returncode == 0 and state.stdout.strip().startswith("Z"):
        return False
    return True


def process_command(pid: int) -> str:
    proc = subprocess.run(
        ["ps", "-ww", "-p", str(pid), "-o", "command="],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return proc.stdout.strip() if proc.returncode == 0 else ""


def wrapper_identity_matches(worker_id: str, pid: int) -> bool:
    command = process_command(pid)
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    return str(SCRIPT_PATH) in argv and any(
        argv[index : index + 2] == ["_run", worker_id]
        for index in range(len(argv) - 1)
    )


def wrapper_identity_matches_with_grace(worker_id: str, pid: int) -> bool:
    if wrapper_identity_matches(worker_id, pid):
        return True
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        if not is_pid_alive(pid):
            return False
        time.sleep(0.025)
        if wrapper_identity_matches(worker_id, pid):
            return True
    return False


def binary_identity_matches(argv: list[str], expected_binary: str) -> bool:
    if not expected_binary:
        return False
    expected = Path(expected_binary).resolve()
    for token in argv:
        if not token.startswith("/"):
            continue
        try:
            if Path(token).resolve() == expected:
                return True
        except OSError:
            continue
    return False


def agent_identity_matches(metadata: dict) -> bool:
    pid = agent_pid(metadata)
    if not is_pid_alive(pid):
        return False
    try:
        argv = shlex.split(process_command(pid))
    except ValueError:
        return False
    if not binary_identity_matches(argv, agent_binary(metadata)):
        return False
    directory = worker_dir(metadata["worker_id"])
    if infer_agent(metadata) == "grok":
        expected_prompt = directory / "prompt.md"
        try:
            prompt_value = argv[argv.index("--prompt-file") + 1]
        except (ValueError, IndexError):
            return False
        return Path(prompt_value).resolve() == expected_prompt.resolve()
    expected_final = Path(metadata.get("final_output_path") or directory / "final.txt")
    try:
        output_value = argv[argv.index("-o") + 1]
    except (ValueError, IndexError):
        return False
    return Path(output_value).resolve() == expected_final.resolve()


def refreshed_metadata(worker_id: str) -> dict:
    directory = worker_dir(worker_id)
    with worker_lock(directory):
        metadata = load_metadata(worker_id)
        if metadata.get("state") not in ACTIVE_STATES:
            return metadata
        wrapper_pid = metadata.get("wrapper_pid")
        wrapper_alive = is_pid_alive(wrapper_pid)
        wrapper_matches = wrapper_alive and wrapper_identity_matches_with_grace(worker_id, wrapper_pid)
        if wrapper_matches:
            return metadata
        identity_mismatch = wrapper_alive and not wrapper_matches
        if agent_identity_matches(metadata):
            metadata.update(
                state="orphaned",
                wrapper_identity_mismatch=identity_mismatch,
                runner_error=(
                    f"wrapper identity changed while its {infer_agent(metadata)} process is still running"
                    if identity_mismatch
                    else f"detached wrapper exited while its {infer_agent(metadata)} process is still running"
                ),
            )
        else:
            metadata.update(
                state="lost",
                finished_at=utc_now(),
                wrapper_identity_mismatch=identity_mismatch,
                runner_error=(
                    "wrapper PID is alive but no longer belongs to this worker"
                    if identity_mismatch
                    else "detached wrapper is no longer running and did not record completion"
                ),
            )
        metadata["updated_at"] = utc_now()
        atomic_write_json(directory / "metadata.json", metadata)
    return metadata


def active_session_worker(agent: str, session_id: str) -> dict | None:
    for path in state_root().glob("*/metadata.json"):
        worker_id = path.parent.name
        try:
            metadata = load_metadata(worker_id)
            if infer_agent(metadata) != agent or metadata.get("resume_session_id") != session_id:
                continue
            metadata = refreshed_metadata(worker_id)
            if metadata.get("state") in ACTIVE_STATES:
                return metadata
        except RunnerError:
            continue
    return None


def read_limited(path: Path, max_bytes: int) -> tuple[str, bool]:
    try:
        with path.open("rb") as handle:
            raw = handle.read(max_bytes + 1)
    except OSError:
        return "", False
    truncated = len(raw) > max_bytes
    return raw[:max_bytes].decode("utf-8", errors="replace"), truncated


def cmd_spawn(args: argparse.Namespace) -> int:
    ensure_supported_options(args.agent, args)
    prompt = read_prompt(args)
    if args.agent == "grok" and not args.no_capsule_contract:
        prompt = inject_completion_contract(
            prompt,
            str(Path(args.cwd).expanduser().resolve()),
        )
    metadata = create_worker(
        agent=args.agent,
        prompt=prompt,
        cwd=args.cwd,
        binary=args.binary,
        model=args.model,
        permission_mode=args.permission_mode,
        max_turns=args.max_turns,
        deadline_seconds=args.deadline_seconds,
        task_class=args.task_class,
        route_reason=args.route_reason,
        sandbox=args.sandbox,
        dangerously_bypass_approvals_and_sandbox=args.dangerously_bypass_approvals_and_sandbox,
    )
    emit(public_metadata(metadata))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    metadata = refreshed_metadata(args.worker_id)
    if metadata.get("state") in TERMINAL_STATES:
        best_effort_snapshot_history(metadata)
    emit(public_metadata(metadata))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    workers = []
    cwd_filter = str(Path(args.cwd).expanduser().resolve()) if args.cwd else None
    for path in sorted(state_root().glob("*/metadata.json"), reverse=True):
        worker_id = path.parent.name
        try:
            metadata = load_metadata(worker_id)
            agent_matches = args.agent is None or infer_agent(metadata) == args.agent
            cwd_matches = cwd_filter is None or metadata.get("cwd") == cwd_filter
            if agent_matches and cwd_matches:
                metadata = refreshed_metadata(worker_id)
                workers.append(public_metadata(metadata))
        except RunnerError:
            continue
    emit({"workers": workers})
    return 0


def cmd_collect(args: argparse.Namespace) -> int:
    deadline = time.monotonic() + args.wait
    while True:
        metadata = refreshed_metadata(args.worker_id)
        if metadata.get("state") not in ACTIVE_STATES or time.monotonic() >= deadline:
            break
        time.sleep(0.1)
    if metadata.get("state") in ACTIVE_STATES:
        payload = public_metadata(metadata)
        payload["error"] = "worker_running"
        emit(payload)
        return 3

    result, result_truncated = parsed_result(metadata, args.max_bytes)
    best_effort_snapshot_history(metadata)
    if args.capsule:
        payload = compact_capsule_payload(metadata, result)
        capsule = extract_completion_capsule(result.get("text") if result else None)
        if capsule is not None:
            payload["completion_capsule"] = capsule
            emit(payload)
            return 0 if metadata.get("state") == "succeeded" else 1
        if metadata.get("state") == "succeeded":
            payload.update(
                error="completion_capsule_invalid",
                message="successful worker did not return a valid six-line completion capsule",
            )
            emit(payload)
            return 4
        payload.update(
            completion_capsule=None,
            error="completion_capsule_unavailable",
        )
        emit(payload)
        return 1

    stdout, stdout_truncated = read_limited(Path(metadata["result_path"]), args.max_bytes)
    stderr, stderr_truncated = read_limited(Path(metadata["stderr_path"]), args.max_bytes)
    payload = public_metadata(metadata)
    payload["result"] = result
    if payload["result"] is None and stdout and infer_agent(metadata) != "grok":
        payload["stdout"] = stdout
    elif payload["result"] is None and stdout:
        payload["result_parse_error"] = True
    if stderr:
        payload["stderr"] = stderr
    if stdout_truncated:
        payload["stdout_truncated"] = True
    if stderr_truncated:
        payload["stderr_truncated"] = True
    if result_truncated:
        payload["result_truncated"] = True
    emit(payload)
    return 0 if metadata.get("state") == "succeeded" else 1


def cmd_followup(args: argparse.Namespace) -> int:
    parent = refreshed_metadata(args.worker_id)
    if parent.get("state") in ACTIVE_STATES:
        raise RunnerError("worker_running", "cannot follow up while the parent worker is active", 3)
    if parent.get("termination_reason") == "deadline_exceeded":
        raise RunnerError(
            "deadline_session_not_resumable",
            "a deadline-exceeded worker must not be resumed; narrow the task and start fresh",
            4,
        )
    session_id = parent.get("session_id")
    if not session_id:
        raise RunnerError("session_id_missing", "parent worker has no native session id", 4)
    agent = infer_agent(parent)
    ensure_supported_options(agent, args)
    prompt = read_prompt(args)
    dangerous_bypass = args.dangerously_bypass_approvals_and_sandbox
    parent_sandbox = parent.get("sandbox")
    if agent == "grok":
        if parent_sandbox is None:
            raise RunnerError(
                "sandbox_profile_unknown",
                "legacy Grok session has no recorded sandbox profile; start a fresh session",
                4,
            )
        if args.sandbox is not None and args.sandbox != parent_sandbox:
            raise RunnerError(
                "sandbox_profile_mismatch",
                "Grok resume requires the parent session's exact sandbox profile",
                4,
            )
        if parent_sandbox == "danger-full-access" and args.sandbox != parent_sandbox:
            raise RunnerError(
                "sandbox_reauthorization_required",
                "repeat --sandbox danger-full-access to authorize this Grok follow-up",
                4,
            )
        followup_sandbox = parent_sandbox
    elif dangerous_bypass:
        followup_sandbox = args.sandbox
    elif args.sandbox is not None:
        followup_sandbox = args.sandbox
    elif (
        parent.get("dangerously_bypass_approvals_and_sandbox", False)
        or parent_sandbox == "danger-full-access"
    ):
        followup_sandbox = None
    else:
        followup_sandbox = parent_sandbox
    parent_permission_mode = parent.get("permission_mode")
    if args.permission_mode is not None:
        followup_permission_mode = args.permission_mode
    elif parent_permission_mode == "bypassPermissions":
        followup_permission_mode = "default"
    else:
        followup_permission_mode = parent_permission_mode
    with worker_lock(state_root()):
        active = active_session_worker(agent, session_id)
        if active is not None:
            raise RunnerError(
                "session_in_use",
                f"native session is already being resumed by {active['worker_id']}",
                3,
            )
        metadata = create_worker(
            agent=agent,
            prompt=prompt,
            cwd=parent["cwd"],
            binary=args.binary or agent_binary(parent),
            model=args.model if args.model is not None else parent.get("model"),
            permission_mode=followup_permission_mode,
            max_turns=args.max_turns if args.max_turns is not None else parent.get("max_turns"),
            deadline_seconds=args.deadline_seconds,
            task_class=args.task_class,
            route_reason=args.route_reason,
            sandbox=followup_sandbox,
            dangerously_bypass_approvals_and_sandbox=dangerous_bypass,
            resume_session_id=session_id,
            parent_worker_id=parent["worker_id"],
        )
    emit(public_metadata(metadata))
    return 0


def signal_agent(metadata: dict, sig: signal.Signals) -> None:
    pid = agent_pid(metadata)
    if not pid:
        return
    group_id = metadata.get("agent_process_group_id")
    if not group_id:
        try:
            group_id = os.getpgid(pid)
        except ProcessLookupError:
            return
    try:
        if group_id > 0 and group_id != os.getpgrp():
            os.killpg(group_id, sig)
        else:
            os.kill(pid, sig)
    except ProcessLookupError:
        pass


def stop_orphan(args: argparse.Namespace, metadata: dict) -> dict:
    if not agent_identity_matches(metadata):
        return update_metadata(
            args.worker_id,
            state="lost",
            finished_at=utc_now(),
            runner_error="orphaned agent process disappeared before cancellation",
        )
    pid = agent_pid(metadata)
    signal_agent(metadata, signal.SIGTERM)
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline and is_pid_alive(pid):
        time.sleep(0.05)
    if is_pid_alive(pid):
        if not agent_identity_matches(metadata):
            raise RunnerError(
                "process_identity_mismatch",
                "orphaned agent identity changed before forced cancellation",
                5,
            )
        signal_agent(metadata, signal.SIGKILL)
    return update_metadata(
        args.worker_id,
        state="cancelled",
        finished_at=utc_now(),
        exit_code=-signal.SIGTERM,
        runner_error="cancelled after detached wrapper exited",
    )


def finish_cancel_after_wrapper_exit(args: argparse.Namespace) -> dict:
    metadata = load_metadata(args.worker_id)
    if metadata.get("state") in TERMINAL_STATES:
        return metadata
    if agent_identity_matches(metadata):
        return stop_orphan(args, metadata)
    return update_metadata(args.worker_id, state="cancelled", finished_at=utc_now())


def cmd_cancel(args: argparse.Namespace) -> int:
    metadata = refreshed_metadata(args.worker_id)
    if metadata.get("wrapper_identity_mismatch") and metadata.get("state") == "lost":
        raise RunnerError(
            "process_identity_mismatch",
            "refusing to signal a PID that is not this worker's detached wrapper",
            5,
        )
    if metadata.get("state") in TERMINAL_STATES:
        emit(public_metadata(metadata))
        return 0
    directory = worker_dir(args.worker_id)
    marker = directory / "cancel.requested"
    if not marker.exists():
        try:
            write_private_text(marker, utc_now() + "\n")
        except FileExistsError:
            pass

    if metadata.get("state") == "orphaned":
        emit(public_metadata(stop_orphan(args, metadata)))
        return 0

    pid = metadata.get("wrapper_pid")
    if not is_pid_alive(pid):
        emit(public_metadata(finish_cancel_after_wrapper_exit(args)))
        return 0
    if not wrapper_identity_matches(args.worker_id, pid):
        if not is_pid_alive(pid):
            emit(public_metadata(finish_cancel_after_wrapper_exit(args)))
            return 0
        raise RunnerError(
            "process_identity_mismatch",
            "refusing to signal a PID that is not this worker's detached wrapper",
            5,
        )

    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        metadata = load_metadata(args.worker_id)
        if metadata.get("state") in TERMINAL_STATES:
            emit(public_metadata(metadata))
            return 0
        if not is_pid_alive(pid):
            break
        time.sleep(0.05)

    wrapper_alive = is_pid_alive(pid)
    wrapper_matches = wrapper_alive and wrapper_identity_matches(args.worker_id, pid)
    if wrapper_alive and not wrapper_matches and is_pid_alive(pid):
        raise RunnerError("process_identity_mismatch", "wrapper identity changed before forced cancellation", 5)
    if wrapper_matches:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    latest = load_metadata(args.worker_id)
    if agent_identity_matches(latest):
        signal_agent(latest, signal.SIGKILL)
    metadata = update_metadata(
        args.worker_id,
        state="cancelled",
        finished_at=utc_now(),
        runner_error="forced cancellation after wrapper did not stop cleanly",
    )
    emit(public_metadata(metadata))
    return 0


def cmd_record_outcome(args: argparse.Namespace) -> int:
    if not telemetry_enabled():
        raise RunnerError("telemetry_disabled", "telemetry is disabled", 4)
    try:
        summary = load_history_summary(args.worker_id)
    except RunnerError as exc:
        if exc.error != "history_not_found":
            raise
        metadata = refreshed_metadata(args.worker_id)
        if metadata.get("state") in ACTIVE_STATES:
            raise RunnerError("worker_running", "cannot record outcome while worker is active", 3)
        snapshot_history(metadata)
        summary = load_history_summary(args.worker_id)
    reason_codes = list(dict.fromkeys(args.reason_code or []))
    if "none" in reason_codes and len(reason_codes) > 1:
        raise RunnerError("conflicting_reason_codes", "reason code 'none' cannot be combined", 2)
    root = history_root()
    with worker_lock(root):
        summary = canonical_history_summary(load_history_summary(args.worker_id))
        if args.task_class is not None:
            summary["task_class"] = args.task_class
        if args.route_reason is not None:
            summary["route_reason"] = args.route_reason
        summary["controller"] = {
            "provenance": "controller",
            "recorded_at": utc_now(),
            "outcome": args.outcome,
            "verification": args.verification,
            "reason_codes": reason_codes,
        }
        summary["updated_at"] = utc_now()
        atomic_write_json(history_path(args.worker_id), summary)
    emit(
        {
            "history_schema_version": HISTORY_SCHEMA_VERSION,
            "worker_id": summary["worker_id"],
            "task_class": summary["task_class"],
            "route_reason": summary["route_reason"],
            "controller": summary["controller"],
        }
    )
    return 0


def increment_count(mapping: dict, key: object, allowed: object = None) -> None:
    normalized = key if isinstance(key, str) and key else "unknown"
    if allowed is not None and normalized not in allowed:
        normalized = "unknown"
    mapping[normalized] = mapping.get(normalized, 0) + 1


def cmd_report(args: argparse.Namespace) -> int:
    if not telemetry_enabled():
        emit({"history_schema_version": HISTORY_SCHEMA_VERSION, "telemetry": "disabled", "runs": 0})
        return 0
    root = history_root()
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.since_days)
    summaries = []
    corrupt = 0
    unsupported = 0
    with worker_lock(root):
        for path in sorted(root.glob("*.json")):
            if path.is_symlink():
                corrupt += 1
                continue
            worker_id = path.stem
            try:
                summary = load_history_summary(worker_id)
            except RunnerError as exc:
                if exc.error == "history_schema_unsupported":
                    unsupported += 1
                else:
                    corrupt += 1
                continue
            recorded_at = parse_utc(summary.get("recorded_at"))
            if recorded_at is None or recorded_at < cutoff:
                continue
            if args.agent is not None and summary.get("agent") != args.agent:
                continue
            summaries.append(summary)

    report = {
        "history_schema_version": HISTORY_SCHEMA_VERSION,
        "generated_at": utc_now(),
        "since_days": args.since_days,
        "runs": len(summaries),
        "root_runs": 0,
        "followup_runs": 0,
        "corrupt_summaries": corrupt,
        "unsupported_summaries": unsupported,
        "by_agent": {},
        "by_task_class": {},
        "by_route_reason": {},
        "by_state": {},
        "by_failure_class": {},
        "by_outcome": {},
        "by_verification": {},
        "by_reason_code": {},
        "feedback": {"denominator": "runs", "recorded": 0, "missing": 0, "missing_rate": 0.0},
        "usage": {"denominator": "runs", "present": 0, "missing": 0},
        "usage_totals": {key: 0 for key in USAGE_FIELDS},
        "capsules": {
            "denominator": "runs",
            "valid": 0,
            "invalid": 0,
            "missing": 0,
        },
        "artifact_bytes": {
            "stdout_bytes": 0,
            "stderr_bytes": 0,
            "wrapper_log_bytes": 0,
            "final_output_bytes": 0,
            "capsule_bytes": 0,
        },
        "duration": {"denominator": "runs_with_duration", "count": 0, "total_seconds": 0.0, "average_seconds": None},
    }
    for summary in summaries:
        if summary.get("parent_worker_id"):
            report["followup_runs"] += 1
        else:
            report["root_runs"] += 1
        increment_count(report["by_agent"], summary.get("agent"), AGENTS)
        increment_count(report["by_task_class"], summary.get("task_class"), TASK_CLASSES)
        increment_count(report["by_route_reason"], summary.get("route_reason"), ROUTE_REASONS)
        increment_count(
            report["by_state"],
            summary.get("state"),
            TERMINAL_STATES | ACTIVE_STATES,
        )
        increment_count(
            report["by_failure_class"],
            summary.get("failure_class"),
            FAILURE_CLASSES,
        )
        controller = summary.get("controller")
        if isinstance(controller, dict) and controller.get("provenance") == "controller":
            report["feedback"]["recorded"] += 1
            increment_count(
                report["by_outcome"], controller.get("outcome"), CONTROLLER_OUTCOMES
            )
            increment_count(
                report["by_verification"],
                controller.get("verification"),
                VERIFICATION_OUTCOMES,
            )
            reasons = controller.get("reason_codes")
            if isinstance(reasons, list):
                for reason in reasons:
                    increment_count(report["by_reason_code"], reason, OUTCOME_REASON_CODES)
            elif reasons is not None:
                increment_count(report["by_reason_code"], "unknown", OUTCOME_REASON_CODES)
        else:
            report["feedback"]["missing"] += 1
        usage = summary.get("usage")
        if isinstance(usage, dict):
            report["usage"]["present"] += 1
            for key in USAGE_FIELDS:
                value = usage.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    report["usage_totals"][key] += value
        else:
            report["usage"]["missing"] += 1
        capsule = summary.get("capsule") if isinstance(summary.get("capsule"), dict) else {}
        capsule_status = capsule.get("status")
        if capsule_status not in {"valid", "invalid", "missing"}:
            capsule_status = "missing"
        report["capsules"][capsule_status] += 1
        capsule_bytes = capsule.get("bytes")
        if isinstance(capsule_bytes, int) and not isinstance(capsule_bytes, bool) and capsule_bytes >= 0:
            report["artifact_bytes"]["capsule_bytes"] += capsule_bytes
        artifacts = summary.get("artifacts") if isinstance(summary.get("artifacts"), dict) else {}
        for key in ("stdout_bytes", "stderr_bytes", "wrapper_log_bytes", "final_output_bytes"):
            value = artifacts.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                report["artifact_bytes"][key] += value
        duration = summary.get("duration_seconds")
        if isinstance(duration, (int, float)) and not isinstance(duration, bool) and math.isfinite(duration) and duration >= 0:
            report["duration"]["count"] += 1
            report["duration"]["total_seconds"] += duration
    runs = report["runs"]
    if runs:
        report["feedback"]["missing_rate"] = round(report["feedback"]["missing"] / runs, 6)
    duration_count = report["duration"]["count"]
    report["duration"]["total_seconds"] = round(report["duration"]["total_seconds"], 6)
    if duration_count:
        report["duration"]["average_seconds"] = round(
            report["duration"]["total_seconds"] / duration_count,
            6,
        )
    emit(report)
    return 0


def cmd_purge_history(args: argparse.Namespace) -> int:
    root = history_root()
    cutoff = (
        None
        if args.all
        else datetime.now(timezone.utc) - timedelta(days=args.older_than_days)
    )
    removed = 0
    skipped = 0
    with worker_lock(root):
        for path in sorted(root.glob("*.json")):
            if path.is_symlink():
                skipped += 1
                continue
            if cutoff is not None:
                try:
                    summary = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    skipped += 1
                    continue
                recorded_at = parse_utc(summary.get("recorded_at") if isinstance(summary, dict) else None)
                if recorded_at is None or recorded_at >= cutoff:
                    continue
            try:
                path.unlink()
            except OSError:
                skipped += 1
            else:
                removed += 1
    emit({"removed": removed, "skipped": skipped})
    return 0


def cmd_cleanup(args: argparse.Namespace) -> int:
    metadata = refreshed_metadata(args.worker_id)
    if metadata.get("state") in ACTIVE_STATES:
        payload = public_metadata(metadata)
        payload["error"] = "worker_not_terminal"
        emit(payload)
        return 4
    directory = worker_dir(args.worker_id)
    root = state_root()
    if directory.parent != root or directory == root:
        raise RunnerError("unsafe_cleanup_path", "refusing unsafe cleanup path", 5)

    def remove_directory() -> None:
        try:
            shutil.rmtree(directory)
        except OSError as exc:
            raise RunnerError("cleanup_failed", f"cannot remove worker state: {exc}", 5) from exc

    with worker_lock(directory):
        metadata = load_metadata(args.worker_id)
        if metadata.get("state") in ACTIVE_STATES:
            payload = public_metadata(metadata)
            payload["error"] = "worker_not_terminal"
            emit(payload)
            return 4
        if args.discard_history:
            remove_directory()
            discard_history(args.worker_id)
        elif telemetry_enabled():
            history = history_root()
            with worker_lock(history):
                summary = snapshot_history_locked(metadata)
                if summary is None:
                    raise RunnerError(
                        "history_snapshot_unavailable",
                        "cannot preserve telemetry summary from terminal worker metadata",
                        5,
                    )
                remove_directory()
        else:
            remove_directory()
    emit({"worker_id": args.worker_id, "state": "cleaned"})
    return 0


def add_prompt_source(parser: argparse.ArgumentParser) -> None:
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--prompt", help="Prompt text (visible in the caller command line)")
    source.add_argument("--prompt-file", help="Read the prompt from a UTF-8 file")
    source.add_argument("--prompt-stdin", action="store_true", help="Read the prompt from stdin")


def add_launch_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--binary", help="Explicit agent executable path")
    parser.add_argument("--model", help="Explicit model override")
    parser.add_argument(
        "--permission-mode",
        choices=("default", "acceptEdits", "auto", "dontAsk", "bypassPermissions", "plan"),
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        help="Optional Grok cap for short closed checks; omit for research/source scans; must be >= 2",
    )
    parser.add_argument(
        "--deadline-seconds",
        type=float,
        help="Optional per-worker wall-clock deadline; finite and greater than zero; never inherited",
    )
    parser.add_argument("--task-class", choices=TASK_CLASSES, default="unknown")
    parser.add_argument("--route-reason", choices=ROUTE_REASONS, default="unknown")
    parser.add_argument("--sandbox", choices=("read-only", "workspace-write", "danger-full-access"))
    parser.add_argument("--dangerously-bypass-approvals-and-sandbox", action="store_true")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Durable asynchronous Grok and Codex CLI workers")
    root.add_argument("--version", action="version", version=RUNNER_VERSION)
    commands = root.add_subparsers(dest="command", required=True)

    spawn = commands.add_parser("spawn", help="Start a detached CLI worker")
    spawn.add_argument("--agent", required=True, choices=AGENTS)
    spawn.add_argument("--cwd", required=True, help="Exact working directory for the agent")
    spawn.add_argument(
        "--no-capsule-contract",
        action="store_true",
        help="Do not append the default completion capsule contract to a root Grok prompt",
    )
    add_prompt_source(spawn)
    add_launch_options(spawn)
    spawn.set_defaults(func=cmd_spawn)

    status = commands.add_parser("status", help="Read one worker's current state")
    status.add_argument("worker_id")
    status.set_defaults(func=cmd_status)

    listing = commands.add_parser("list", help="List known workers")
    listing.add_argument("--agent", choices=AGENTS)
    listing.add_argument("--cwd", help="List only workers for this exact resolved working directory")
    listing.set_defaults(func=cmd_list)

    collect = commands.add_parser("collect", help="Collect a terminal worker result")
    collect.add_argument("worker_id")
    collect.add_argument("--wait", type=float, default=0, help="Wait up to this many seconds")
    collect.add_argument("--max-bytes", type=int, default=DEFAULT_COLLECT_BYTES)
    collect.add_argument(
        "--capsule",
        action="store_true",
        help="Return only a normalized completion capsule and compact session identifiers",
    )
    collect.set_defaults(func=cmd_collect)

    followup = commands.add_parser("followup", help="Resume a completed worker's native session")
    followup.add_argument("worker_id")
    add_prompt_source(followup)
    add_launch_options(followup)
    followup.set_defaults(func=cmd_followup)

    cancel = commands.add_parser("cancel", help="Stop one active worker after identity verification")
    cancel.add_argument("worker_id")
    cancel.add_argument("--timeout", type=float, default=5)
    cancel.set_defaults(func=cmd_cancel)

    outcome = commands.add_parser(
        "record-outcome",
        help="Record controller-verified low-cardinality outcome labels",
    )
    outcome.add_argument("worker_id")
    outcome.add_argument("--outcome", required=True, choices=CONTROLLER_OUTCOMES)
    outcome.add_argument("--verification", required=True, choices=VERIFICATION_OUTCOMES)
    outcome.add_argument("--reason-code", action="append", choices=OUTCOME_REASON_CODES)
    outcome.add_argument("--task-class", choices=TASK_CLASSES)
    outcome.add_argument("--route-reason", choices=ROUTE_REASONS)
    outcome.set_defaults(func=cmd_record_outcome)

    report = commands.add_parser("report", help="Aggregate privacy-minimized local worker summaries")
    report.add_argument("--since-days", type=float, default=30)
    report.add_argument("--agent", choices=AGENTS)
    report.set_defaults(func=cmd_report)

    purge = commands.add_parser("purge-history", help="Delete retained telemetry summaries")
    purge_scope = purge.add_mutually_exclusive_group(required=True)
    purge_scope.add_argument("--older-than-days", type=float)
    purge_scope.add_argument("--all", action="store_true")
    purge.set_defaults(func=cmd_purge_history)

    cleanup = commands.add_parser("cleanup", help="Remove one terminal worker's state directory")
    cleanup.add_argument("worker_id")
    cleanup.add_argument(
        "--discard-history",
        action="store_true",
        help="Delete raw worker state even when telemetry cannot be preserved",
    )
    cleanup.set_defaults(func=cmd_cleanup)

    internal = commands.add_parser("_run", help=argparse.SUPPRESS)
    internal.add_argument("worker_id")
    internal.set_defaults(func=lambda args: run_worker(args.worker_id))
    return root


def main() -> int:
    os.umask(0o077)
    try:
        args = parser().parse_args()
        if hasattr(args, "wait") and (not math.isfinite(args.wait) or args.wait < 0):
            raise RunnerError("invalid_wait", "wait must be finite and non-negative")
        if hasattr(args, "timeout") and (not math.isfinite(args.timeout) or args.timeout < 0):
            raise RunnerError("invalid_timeout", "timeout must be finite and non-negative")
        if hasattr(args, "since_days") and (
            not math.isfinite(args.since_days) or args.since_days < 0
        ):
            raise RunnerError("invalid_since_days", "since-days must be finite and non-negative")
        if (
            hasattr(args, "older_than_days")
            and args.older_than_days is not None
            and (not math.isfinite(args.older_than_days) or args.older_than_days < 0)
        ):
            raise RunnerError(
                "invalid_older_than_days",
                "older-than-days must be finite and non-negative",
            )
        if hasattr(args, "deadline_seconds") and args.deadline_seconds is not None and (
            not math.isfinite(args.deadline_seconds) or args.deadline_seconds <= 0
        ):
            raise RunnerError(
                "invalid_deadline",
                "deadline-seconds must be finite and greater than zero",
            )
        if hasattr(args, "max_bytes") and args.max_bytes <= 0:
            raise RunnerError("invalid_max_bytes", "max-bytes must be positive")
        return args.func(args)
    except RunnerError as exc:
        emit({"error": exc.error, "message": exc.message})
        return exc.exit_code
    except KeyboardInterrupt:
        emit({"error": "interrupted", "message": "operation interrupted"})
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
