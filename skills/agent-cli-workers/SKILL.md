---
name: agent-cli-workers
description: Unified durable async worker workflow for Grok CLI and Codex CLI. Routes fast read-only investigation and review to Grok, routes implementation to Codex, enforces one-writer isolation and bounded completion capsules, and supports collection, native-session resume, status, cancellation, and cleanup. Use when Codex needs a lightweight headless worker instead of a daemon, TUI, or fleet orchestrator.
---

# Agent CLI Workers (Grok + Codex)

This skill provides a unified process for running **Grok** and **Codex** workers from one runner.

Core adapter:

- `grok`: Grok CLI
- `codex`: Codex CLI; model selection defaults to the user's Codex configuration

Runner:

- `${CODEX_HOME:-$HOME/.codex}/skills/agent-cli-workers/scripts/agent_worker.py`

State directory:

- `${AGENT_CLI_WORKERS_STATE_DIR:-${CODEX_HOME:-~/.codex}/state/agent-cli-workers/workers}`
- Use a dedicated directory owned by the current user with mode `0700`. The runner rejects symlinked or group/other-accessible state roots instead of changing an existing directory's permissions.

## Why use this

- Run long/independent work in background.
- Keep main turn free for continued work.
- Resume native session later (`followup`) using same context.
- Collect output, inspect lifecycle state, and cleanup deterministically.

## Route the task

Choose one primary worker by default. Do not fan out merely because both adapters are available.

| Task shape | Primary worker | Recommended sandbox |
|---|---|---|
| Repository investigation, bounded research, contract/state-machine review, independent second opinion | `grok` | `read-only` |
| Implementation, test repair, mechanical edits, focused refactor | `codex` | `workspace-write` |
| Coupled architecture, shared foundations, or unclear ownership | Main session first | No worker until scoped |

Use both adapters only when tasks are independent or in a deliberate writer-reviewer sequence:

- Codex is the sole writer for an exact file set.
- Grok reviews read-only after the writer is terminal or at a fixed commit.
- Two workers may receive the same contract only for an intentional read-only comparison.
- The main session owns task decomposition, shared foundations, final diff review, verification, and integration.

## Turn caps and long tasks

- Omit `--max-turns` for multi-source research, repository-wide source scans, and other open-ended investigations. These tasks can consume the cap during evidence gathering before producing a completion capsule.
- Use `--max-turns` only for a short, closed check that should finish in a few tool turns. Treat it as an explicit per-task limit, never a default safety setting.
- Before starting a long task, split it into bounded deliverables and set a concrete wall-clock deadline. The caller owns that deadline by polling `status` and cancelling when it is reached; state a token or cost budget in the task or provider configuration when one is available. Do not start uncapped long work when the caller cannot monitor and cancel it. The runner does not currently enforce a wall-clock or token budget automatically.
- If a worker fails with `max turns reached` or a corresponding non-completion stop reason, collect diagnostics once and do not `followup` that native session merely to request the missing conclusion. Start a fresh worker only after narrowing the task or deliberately removing the cap.

## Isolation policy

- Use the original repository for read-only work and for one low-conflict writer.
- Create a caller-owned git worktree before spawning simultaneous writers, competing implementations, or explicitly isolated work.
- Pass the exact repository or worktree path through `--cwd`; never infer coverage from a worker in another worktree.
- Keep one writer per overlapping file set. A reviewer stays read-only and never repairs the writer's files.
- The runner does not create or remove worktrees. The caller records the expected root/revision and cleans the worktree deliberately after integration or discard.

## Task and result contract

Every delegated prompt must state the role, concrete deliverable, exact CWD, allowed scope, forbidden overlap, and requested verification. Require workspace proof before inspection or editing.

Use this compact contract:

```text
Role: <investigator|reviewer|implementer>
Task: <one bounded deliverable>
CWD: <exact absolute repository or worktree path>
Scope: <owned files/modules; forbidden edits or read-only>
Workspace proof: first run pwd and git rev-parse --show-toplevel; record launch HEAD as base and final HEAD as head
Verification: <specific commands, or explain why none are appropriate>

Return only this final completion capsule:
STATUS: succeeded|blocked|failed
WORKSPACE: pwd=<path>; root=<path>; base=<launch-sha>; head=<final-sha>
SUMMARY: <one or two concise sentences; include highest-signal file:line findings for reviews>
FILES: <comma-separated paths or none>
VERIFY: <command => exit code; or not run with reason>
RISKS: <none or concise unresolved risks>
```

The worker may reason and use tools freely, but its final response stays in the capsule. For an intentional non-Git task, use `root=non-git; base=none; head=none`. On collection:

- Reject a mismatched workspace or a vague claim such as "tests passed" without the command. Require an exit code when a command was requested; accept `not run` only with a concrete reason.
- If a successful worker omitted the capsule, use one focused `followup` to request it; do not resume repeatedly just to reformat output.
- Use `collect --capsule` for a normal successful handoff. Use ordinary `collect` only to diagnose failure; Grok results are allowlisted and never emit the provider's `thought` field.
- Treat the capsule as a handoff, not proof of correctness. Independently inspect edits and rerun the smallest relevant verification.

## Quick start

Choose one primary example:

```bash
RUNNER="${CODEX_HOME:-$HOME/.codex}/skills/agent-cli-workers/scripts/agent_worker.py"
WORKDIR=/absolute/path/to/project
PROMPT=/absolute/path/to/task.md

# Grok
python3 "$RUNNER" spawn \
  --agent grok \
  --cwd "$WORKDIR" \
  --sandbox read-only \
  --prompt-file "$PROMPT"

# Codex (uses the CLI's configured model by default)
python3 "$RUNNER" spawn \
  --agent codex \
  --cwd "$WORKDIR" \
  --sandbox workspace-write \
  --prompt-file "$PROMPT"
```

## Adapter/option compatibility

| Option | Grok | Codex |
|---|---|---|
| `--permission-mode` | ✅ | ⚠️ unsupported |
| `--max-turns` | ✅ short checks only; omit for research/source scans | ⚠️ unsupported |
| `--sandbox` | pass-through | ✅ |
| `--dangerously-bypass-approvals-and-sandbox` | ⚠️ unsupported | ✅ |
| `--model` | pass-through except disabled models | pass-through except disabled models |

### Model control

- Omit `--model` to use the model selected by the installed Codex or Grok CLI.
- Set `AGENT_CLI_WORKERS_CODEX_MODEL` to define a deployment-wide Codex default without editing the skill.
- Set comma-separated `AGENT_CLI_WORKERS_DISABLED_MODEL_PREFIXES` to reject local model families before worker creation and again before execution.
- An explicit `--model` overrides `AGENT_CLI_WORKERS_CODEX_MODEL` unless the model matches a disabled prefix.
- Both adapters default to the `read-only` sandbox. Request `workspace-write` explicitly for an editing worker.
- Treat `danger-full-access`, Grok `bypassPermissions`, and Codex `--dangerously-bypass-approvals-and-sandbox` as per-worker authorization, never a reusable session default.
- For Grok, omit `--model` by default so the configured provider/model remains authoritative.

### Grok-specific launch example

```bash
python3 "$RUNNER" spawn \
  --agent grok \
  --cwd "$WORKDIR" \
  --permission-mode auto \
  --sandbox read-only \
  --prompt-file "$PROMPT"
```

### Codex-specific launch example

```bash
python3 "$RUNNER" spawn \
  --agent codex \
  --cwd "$WORKDIR" \
  --sandbox workspace-write \
  --prompt-file "$PROMPT"
```

## Operations

```bash
python3 "$RUNNER" list --cwd "$WORKDIR"
python3 "$RUNNER" list --cwd "$WORKDIR" --agent codex
python3 "$RUNNER" list  # global diagnostic across repositories
python3 "$RUNNER" status <worker-id>
python3 "$RUNNER" collect <worker-id> --capsule
python3 "$RUNNER" collect <worker-id> --capsule --wait 30
# Failure diagnosis only:
python3 "$RUNNER" collect <worker-id>
python3 "$RUNNER" followup <worker-id> --prompt-file <followup-prompt>
python3 "$RUNNER" cancel <worker-id>
python3 "$RUNNER" cleanup <worker-id>
```

`collect` returns:

- `0` success
- `1` terminal failure/cancel
- `3` still running (when `--wait` times out)
- `4` successful worker returned an invalid completion capsule in `--capsule` mode

`followup` only works for terminal workers with captured native session/thread id.

`followup` inherits ordinary adapter settings. Codex bypass and `danger-full-access` are de-escalated unless explicitly requested again. Grok requires the exact sandbox profile used to create its native session: `danger-full-access` must be explicitly repeated, a different profile requires a fresh session, and legacy sessions with no recorded profile are not resumed. Grok `bypassPermissions` is never inherited.

Only one active worker may resume a native session at a time. Wait for or cancel the active descendant before starting another `followup`.

For Grok, an explicit `stopReason` other than `EndTurn` is a terminal failure even when the CLI exits `0`; `collect` still returns the structured partial result for diagnosis.

Use the exact repository or worktree path with `list --cwd` before deciding whether the current task already has a worker. Worker state is global; an unfiltered `list` can contain unrelated repositories such as Invite and must not be treated as coverage for the current repository.

## Recommended usage pattern for hardening workflows

1. For code work, require the worker to run and report `pwd`, `git rev-parse --show-toplevel`, and `git rev-parse HEAD` before inspecting or editing files.
2. Compare that workspace proof with the exact `--cwd` and expected revision. Treat metadata and launch arguments as routing evidence, not proof of the provider's filesystem view.
3. If any value mismatches, reject the result, cancel the worker, and never `followup` that native session. Start a fresh session only after rechecking the local worktree.
4. If a fresh Grok session sees the wrong filesystem twice under the correct local CWD, stop retrying and report a worker/provider workspace-view failure.
5. If Grok reaches a turn cap without a completion capsule, diagnose once and do not resume that session just to ask it to finish. Narrow the work and start fresh only when another attempt is justified.
6. Use one primary worker by default. For writer-reviewer work, wait for the Codex writer to finish, then give Grok a read-only review at the fixed worktree or commit.
7. Collect with `--capsule`, review diffs, and independently rerun focused verification before integration.
8. `cleanup` workers and remove temporary worktrees if created.

## Notes

- `prompt` text is written to private worker files, not metadata.
- Use `--prompt-file` or `--prompt-stdin` for secret-bearing tasks. The compatibility-only `--prompt` form exposes text in the caller's argv.
- Prompt sources are limited to 1 MiB. `collect --max-bytes` also bounds structured-result parsing. Truncated Grok JSON is never echoed as raw stdout.
- Normalized completion capsules are limited to 16 KiB; an oversized handoff is invalid.
- Cancellation owns the full agent process group and does not mark the worker terminal until spawned descendants have been terminated.
- Status detects a reused/mismatched wrapper PID, marks the record `lost`, and refuses to signal the unrelated process.
- Result parsing is memory-bounded, but the worker capture files do not have an execution-time disk quota. Inspect unexpectedly large state directories before collection and clean terminal workers deliberately.
- Keep unrelated editing tasks out of the same files across multiple active workers.
