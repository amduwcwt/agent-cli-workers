# agent-cli-workers

Lightweight, resumable background workers for Grok CLI and Codex CLI, packaged as Codex skills.

`agent-cli-workers` is intentionally smaller than an agent IDE or fleet orchestrator. It starts one bounded CLI worker, persists enough lifecycle state to inspect or resume it, and returns a compact completion capsule instead of pushing an entire provider transcript into the controlling agent's context.

## Why this exists

Most multi-agent tools optimize for many terminals, worktrees, dashboards, autonomous planning, or merge automation. This project optimizes for a narrower loop:

```text
Codex main session
  -> one Grok or Codex CLI worker
  -> durable local state + native session id
  -> bounded six-line completion capsule
  -> independent verification by the caller
```

The runner provides:

- detached `spawn`, `status`, `list`, `collect`, `followup`, `cancel`, and `cleanup` commands;
- Grok and Codex adapters without a daemon, database, tmux, or service account;
- native Grok session and Codex thread resume;
- read-only sandbox defaults and explicit per-worker escalation;
- private prompt files, process-group cancellation/deadlines, and owned state directories;
- privacy-minimized derived run summaries, controller-recorded outcomes, local aggregate reports, and retention controls;
- allowlisted Grok results that never emit the provider `thought` field;
- a default root-Grok contract plus `collect --capsule`, which normalizes plain or Markdown-bold six-line handoffs and rejects invalid or oversized successful results;
- workflow guidance for one writer per overlapping file set and caller-owned worktrees.

## Non-goals

This project does not plan task graphs, create worktrees, merge branches, watch CI, manage pull requests, provide a TUI, or run an autonomous agent fleet. The controlling Codex session keeps those decisions.

## Requirements

- Python 3.10+
- macOS or Linux; lifecycle ownership uses Unix process groups, `fcntl`, and `ps`
- Codex CLI and/or a Grok Build CLI compatible with the documented flags
- Git only when the delegated task needs repository proof or a caller-created worktree

No Python packages are required.

## Install as Codex skills

Clone the repository, then link the skills into your Codex home. `ln -s` fails safely when a destination already exists and is reversible by removing only the symlink.

```bash
git clone https://github.com/amduwcwt/agent-cli-workers.git
cd agent-cli-workers
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills"
ln -s "$PWD/skills/agent-cli-workers" "${CODEX_HOME:-$HOME/.codex}/skills/agent-cli-workers"
ln -s "$PWD/skills/grok-build-cli" "${CODEX_HOME:-$HOME/.codex}/skills/grok-build-cli"
```

The second skill is an optional Grok-focused compatibility entry point. The shared runner lives in the first skill.

## Quick start

Write a bounded task contract to a file:

```text
Role: reviewer
Task: Review the parser state machine for correctness.
CWD: /absolute/path/to/repository
Scope: read-only; do not edit files
Workspace proof: report pwd, repository root, launch HEAD, and final HEAD
Verification: explain why no command is appropriate for a read-only review

Return only this final completion capsule:
STATUS: succeeded|blocked|failed
WORKSPACE: pwd=<path>; root=<path>; base=<launch-sha>; head=<final-sha>
SUMMARY: <one or two concise sentences>
FILES: <comma-separated paths or none>
VERIFY: <command => exit code; or not run with reason>
RISKS: <none or concise unresolved risks>
```

Start one worker:

```bash
RUNNER="${CODEX_HOME:-$HOME/.codex}/skills/agent-cli-workers/scripts/agent_worker.py"

python3 "$RUNNER" spawn \
  --agent grok \
  --cwd /absolute/path/to/repository \
  --task-class review \
  --route-reason fast-readonly \
  --sandbox read-only \
  --prompt-file /absolute/path/to/task.md
```

Collect only the compact handoff:

```bash
python3 "$RUNNER" status <worker-id>
python3 "$RUNNER" collect <worker-id> --capsule --wait 30
```

Use ordinary `collect` only to diagnose a terminal failure. Even diagnostic collection filters Grok's top-level result fields and never falls back to malformed or truncated Grok stdout.

For an editing worker, choose `--agent codex --sandbox workspace-write`. Create and clean any isolation worktree yourself; the runner never changes Git topology.

Root Grok prompts receive the completion contract automatically; pass `--no-capsule-contract` only for intentional non-capsule output.

## Turn caps and long tasks

The runner does not set a turn cap by default. Omit `--max-turns` for multi-source research, repository-wide source scans, and other open-ended investigations; evidence gathering can exhaust the cap before the worker produces a completion capsule. Use the option only for a short, closed Grok check that should finish in a few tool turns.

Split long work into bounded deliverables. Add `--deadline-seconds <positive-seconds>` to `spawn` when the runner should enforce a per-worker wall-clock deadline. The clock starts when the provider child starts. At expiry the wrapper sends `SIGTERM` to the agent process group, waits three seconds, and escalates to `SIGKILL` if necessary. The worker finishes as `failed` with `termination_reason=deadline_exceeded`; that native session cannot be resumed. Deadlines are opt-in and never inherited by `followup`. The runner does not yet enforce provider-aware token or cost budgets. After a `max turns reached` failure, collect diagnostics once and do not resume the same native session merely to ask for the missing conclusion.

Deadline termination is not a rollback. A write-capable worker can leave a partial diff, so retain and inspect its worktree before deciding whether to integrate or discard it. Process-group cancellation covers descendants that remain in the agent's group; a descendant that deliberately creates a new session/process group is outside that guarantee.

## Local telemetry

The runner writes one derived summary per terminal worker to a private history directory. Supply versioned low-cardinality labels with `--task-class` and `--route-reason`; after independent verification, record the controller's outcome:

```bash
python3 "$RUNNER" record-outcome <worker-id> \
  --outcome accepted \
  --verification passed

python3 "$RUNNER" report --since-days 30
python3 "$RUNNER" purge-history --older-than-days 90
```

Summaries retain lifecycle state, runner/skill version, fixed token fields, byte counts, capsule status, controller provenance, and allowlisted enum labels. They do not retain prompt or result text, thought, raw stderr, cwd, filenames, session ids, diffs, or arbitrary provider usage keys. No telemetry is uploaded.

Set `AGENT_CLI_WORKERS_TELEMETRY=0` to disable new summaries or `AGENT_CLI_WORKERS_HISTORY_DIR` to choose another private root. Normal cleanup preserves the summary before deleting raw worker artifacts. `cleanup <worker-id> --discard-history` is the explicit escape hatch when history is unavailable or corrupt and raw sensitive artifacts must still be removed. Aggregate reports include sample and missing-feedback denominators; do not treat small samples or controller labels as proof that a routing rule is better.

## Model policy

By default the runner does not pass a model to either CLI. This preserves each user's existing CLI configuration.

```bash
export AGENT_CLI_WORKERS_CODEX_MODEL=<your-codex-model>
export AGENT_CLI_WORKERS_DISABLED_MODEL_PREFIXES=retired-model,experimental-model
```

An explicit `--model` overrides the Codex environment default unless it matches a disabled prefix. Grok model selection is also left unchanged unless `--model` is explicitly provided.

## State and privacy

Worker state defaults to:

```text
${AGENT_CLI_WORKERS_STATE_DIR:-${CODEX_HOME:-~/.codex}/state/agent-cli-workers/workers}
```

Derived telemetry history defaults to the sibling `history` directory and can be overridden with `AGENT_CLI_WORKERS_HISTORY_DIR`.

The runner requires the state root to be an owned directory with mode `0700`. Worker artifacts use mode `0600`. Prompt bodies are removed after execution and are never stored in metadata or detached-wrapper arguments. Raw provider captures remain local until `cleanup`; inspect them deliberately and do not publish the state directory.

See [docs/architecture.md](docs/architecture.md) for lifecycle and trust boundaries, [docs/related-projects.md](docs/related-projects.md) for the landscape comparison, and [SECURITY.md](SECURITY.md) for reporting and operational risks.

## Development

```bash
python3 skills/agent-cli-workers/tests/test_agent_worker.py
python3 skills/grok-build-cli/tests/test_grok_worker.py
python3 tests/test_public_tree.py
```

All tests use fake agent binaries. They do not call a model provider.

## License

MIT. See [LICENSE](LICENSE).
