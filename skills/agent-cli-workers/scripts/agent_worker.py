#!/usr/bin/env python3
"""Durable asynchronous workers for Grok Build CLI and Codex CLI.

Public commands emit one JSON object on stdout. Prompt bodies live only in a
private worker file and are passed to agent CLIs by file or stdin, never in
worker metadata or detached-wrapper argv.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
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
RUNNER_VERSION = "0.1.0"
DEFAULT_SANDBOX = "read-only"
AGENTS = ("grok", "codex")
CODEX_AGENTS = frozenset(("codex",))
TERMINAL_STATES = {"succeeded", "failed", "cancelled", "lost"}
ACTIVE_STATES = {"queued", "running", "orphaned"}
WORKER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
MAX_PROMPT_BYTES = 1024 * 1024
DEFAULT_COLLECT_BYTES = 2 * 1024 * 1024
MAX_COMPLETION_CAPSULE_BYTES = 16 * 1024
SCRIPT_PATH = Path(__file__).resolve()
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
    r"^STATUS:[ \t]*(succeeded|blocked|failed)[ \t]*\r?\n"
    r"WORKSPACE:[ \t]*(.+?)[ \t]*\r?\n"
    r"SUMMARY:[ \t]*(.+?)[ \t]*\r?\n"
    r"FILES:[ \t]*(.+?)[ \t]*\r?\n"
    r"VERIFY:[ \t]*(.+?)[ \t]*\r?\n"
    r"RISKS:[ \t]*(.+?)[ \t]*(?=\r?$)",
    re.MULTILINE,
)


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
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
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
        "binary": resolved_binary,
        "permission_mode": permission_mode or "default",
        "max_turns": max_turns,
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
    return payload


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


def stop_process_group(group_id: int | None, grace_seconds: float = 1.0) -> bool:
    if not process_group_alive(group_id):
        return True
    signal_process_group(group_id, signal.SIGTERM)
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline and process_group_alive(group_id):
        time.sleep(0.05)
    if process_group_alive(group_id):
        signal_process_group(group_id, signal.SIGKILL)
        deadline = time.monotonic() + grace_seconds
        while time.monotonic() < deadline and process_group_alive(group_id):
            time.sleep(0.05)
    return not process_group_alive(group_id)


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
            pid_changes["grok_pid" if agent == "grok" else "codex_pid"] = child.pid
            update_metadata(worker_id, **pid_changes)
            if cancelled or cancel_marker.exists():
                signal_process_group(child_group_id, signal.SIGTERM)
            exit_code = child.wait()
    except Exception as exc:  # Wrapper failures must remain inspectable.
        runner_error = f"{type(exc).__name__}: {exc}"
        traceback.print_exc()
    finally:
        if not stop_process_group(child_group_id) and runner_error is None:
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
    elif runner_error is not None:
        state = "failed"
    elif (
        infer_agent(metadata) == "grok"
        and result is not None
        and result.get("stopReason") not in (None, "EndTurn")
    ):
        state = "failed"
        runner_error = f"Grok reported stopReason={result.get('stopReason')}"
    else:
        state = "succeeded" if exit_code == 0 else "failed"
    update_metadata(
        worker_id,
        state=state,
        exit_code=exit_code,
        finished_at=utc_now(),
        session_id=session_id,
        result_truncated=result_truncated,
        runner_error=runner_error,
    )
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
        wrapper_matches = wrapper_alive and wrapper_identity_matches(worker_id, wrapper_pid)
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
    metadata = create_worker(
        agent=args.agent,
        prompt=prompt,
        cwd=args.cwd,
        binary=args.binary,
        model=args.model,
        permission_mode=args.permission_mode,
        max_turns=args.max_turns,
        sandbox=args.sandbox,
        dangerously_bypass_approvals_and_sandbox=args.dangerously_bypass_approvals_and_sandbox,
    )
    emit(public_metadata(metadata))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    emit(public_metadata(refreshed_metadata(args.worker_id)))
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
        metadata = refreshed_metadata(args.worker_id)
        if metadata.get("state") == "orphaned":
            metadata = stop_orphan(args, metadata)
        elif metadata.get("state") not in TERMINAL_STATES:
            metadata = update_metadata(args.worker_id, state="cancelled", finished_at=utc_now())
        emit(public_metadata(metadata))
        return 0
    if not wrapper_identity_matches(args.worker_id, pid):
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

    if is_pid_alive(pid):
        if not wrapper_identity_matches(args.worker_id, pid):
            raise RunnerError("process_identity_mismatch", "wrapper identity changed before forced cancellation", 5)
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        latest = load_metadata(args.worker_id)
        if agent_identity_matches(latest):
            signal_agent(latest, signal.SIGKILL)
    else:
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
    shutil.rmtree(directory)
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
    parser.add_argument("--max-turns", type=int, help="Optional Grok agent turn cap; must be >= 2")
    parser.add_argument("--sandbox", choices=("read-only", "workspace-write", "danger-full-access"))
    parser.add_argument("--dangerously-bypass-approvals-and-sandbox", action="store_true")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Durable asynchronous Grok and Codex CLI workers")
    root.add_argument("--version", action="version", version=RUNNER_VERSION)
    commands = root.add_subparsers(dest="command", required=True)

    spawn = commands.add_parser("spawn", help="Start a detached CLI worker")
    spawn.add_argument("--agent", required=True, choices=AGENTS)
    spawn.add_argument("--cwd", required=True, help="Exact working directory for the agent")
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

    cleanup = commands.add_parser("cleanup", help="Remove one terminal worker's state directory")
    cleanup.add_argument("worker_id")
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
