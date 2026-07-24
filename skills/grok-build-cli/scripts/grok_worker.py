#!/usr/bin/env python3
"""Compatibility entry point for the shared agent-cli-workers runner.

Existing Grok commands keep their original shape while lifecycle ownership
lives in agent-cli-workers/scripts/agent_worker.py.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys


RUNNER = (
    Path(__file__).resolve().parents[2]
    / "agent-cli-workers"
    / "scripts"
    / "agent_worker.py"
)

VALUE_OPTIONS = {
    "--binary",
    "--cwd",
    "--deadline-seconds",
    "--grok-binary",
    "--max-bytes",
    "--max-turns",
    "--model",
    "--permission-mode",
    "--prompt",
    "--prompt-file",
    "--sandbox",
    "--timeout",
    "--wait",
}


def mapped_args(argv: list[str]) -> list[str]:
    if not argv:
        return []
    mapped = [argv[0]]
    index = 1
    while index < len(argv):
        value = argv[index]
        if value in VALUE_OPTIONS:
            mapped.append("--binary" if value == "--grok-binary" else value)
            if index + 1 < len(argv):
                mapped.append(argv[index + 1])
                index += 2
                continue
        elif value.startswith("--grok-binary="):
            mapped.append("--binary=" + value.split("=", 1)[1])
        elif value == "--agent" or value.startswith("--agent="):
            raise SystemExit("the grok-build-cli compatibility entry point only supports Grok")
        else:
            mapped.append(value)
        index += 1
    if mapped[0] in {"spawn", "list"}:
        mapped[1:1] = ["--agent", "grok"]
    return mapped


def main() -> None:
    if not RUNNER.is_file():
        raise SystemExit(f"shared agent worker runner not found: {RUNNER}")
    env = os.environ.copy()
    legacy_state = env.get("GROK_BUILD_CLI_STATE_DIR")
    if legacy_state:
        env["AGENT_CLI_WORKERS_STATE_DIR"] = legacy_state
    os.execve(
        sys.executable,
        [sys.executable, str(RUNNER), *mapped_args(sys.argv[1:])],
        env,
    )


if __name__ == "__main__":
    main()
