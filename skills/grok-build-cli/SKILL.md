---
name: grok-build-cli
description: Call Grok Build CLI directly or through the durable agent-cli-workers runner for research, review, coding, testing, native-session follow-up, caller-owned worktrees, deadlines, and privacy-minimized local outcome telemetry. Use when the user asks to use Grok, keep working while Grok runs, isolate a Grok edit, diagnose Grok/provider behavior, or improve delegation from recorded run evidence. Existing grok_worker.py commands remain supported; use agent-cli-workers for mixed delegation and model selection.
---

# Grok Build CLI

Call the configured Grok provider without changing its model, endpoint, or credentials:

```text
Codex -> Grok CLI -> configured Grok provider
```

Omit `--model` unless the user explicitly requests one. Never print, copy, or persist API keys.

## Choose direct or asynchronous execution

| Situation | Mode |
|---|---|
| Small task whose answer blocks further useful work | Direct headless call |
| Grok can run while Codex inspects, edits, or tests independently | Shared async runner |
| User explicitly requests an isolated Grok edit | Shared runner in a caller-created worktree |
| Mixed Grok and Codex delegation | `agent-cli-workers` skill |

Do not wait serially for an async worker while useful local work remains. Inspect `list --cwd <exact-cwd>` or `status` before starting a duplicate task. An unfiltered `list` is global and may contain workers from unrelated repositories.

For mixed delegation, use `--agent codex` for the Codex worker. Model selection defaults to each installed CLI and can be configured through the shared runner.

Use Grok as the primary worker for fast read-only investigation, bounded review, contract checking, and independent second opinions. Use Codex as the sole writer for implementation and test repair by default. If the active task constrains the writer to Grok, apply the same task-scoped authorization envelope to Grok, isolate the edit from every other writer, and never let Grok and Codex edit an overlapping file set.

## Apply task-scoped authorization

- Derive authorization from the active task's requested outcome, target scope, and operation class—not from trigger words or message phrasing. Treat a change/build/fix request as authority for ordinary reversible repository-local steps needed for that outcome; keep a review/explanation/diagnosis request read-only unless the requested outcome changes.
- Track that authority as a task-scoped authorization envelope. A continuation resumes the existing authorization envelope; it neither creates nor expands authority. Derive a new envelope when the user replaces or materially changes the task.
- Treat `workspace-write` as an execution profile within that envelope, not as a separate approval event. Select it only when the worker must perform repository-local writes already implied by the active task.
- Delegation changes who performs an action, not the action's authorization class. Route an implementation envelope to the normal Codex writer unless the task constrains provider choice; never convert a read-only envelope into write authority merely because an agent is requested.
- Compare each next action with the current envelope. Ask only when it adds a new target, operation class, external side-effect domain, dangerous bypass, destructive or irreversible behavior, or credential access. Reuse authority already present in the envelope until the action completes, the user revokes it, or the task is superseded.

Require Grok to end with the same compact handoff used by the shared runner:

```text
STATUS: succeeded|blocked|failed
WORKSPACE: pwd=<path>; root=<path>; base=<launch-sha>; head=<final-sha>
SUMMARY: <one or two concise sentences; include highest-signal file:line findings for reviews>
FILES: <comma-separated paths or none>
VERIFY: <command => exit code; or not run with reason>
RISKS: <none or concise unresolved risks>
```

For code-aware work, record the launch revision as `base` and the final revision as `head`; this remains unambiguous if a writer commits. For intentional non-Git research, use `root=non-git; base=none; head=none`. Reject mismatched workspace proof. Read full Grok output only to diagnose a failure; otherwise keep the main context to the completion capsule and independently verify any edits or claims.

## Run an asynchronous Grok worker

Use the shared lifecycle implementation:

```bash
RUNNER="${CODEX_HOME:-$HOME/.codex}/skills/agent-cli-workers/scripts/agent_worker.py"

python3 "$RUNNER" spawn \
  --agent grok \
  --cwd <absolute-cwd> \
  --task-class review \
  --route-reason fast-readonly \
  --prompt-file <absolute-task-file>
```

The command returns immediately. Prefer `--prompt-file` or `--prompt-stdin`; `--prompt` exposes task text in the caller's argv. Metadata stores no prompt body. Shared state lives under:

```text
${AGENT_CLI_WORKERS_STATE_DIR:-${CODEX_HOME:-~/.codex}/state/agent-cli-workers/workers}
```

The shared runner defaults Grok to `--sandbox read-only`. Pass `--sandbox workspace-write` only when the active authorization envelope includes repository-local writes. Add `--permission-mode bypassPermissions` only when that envelope explicitly includes non-interactive approval bypass. Scope both choices to the requested task and exact CWD; neither authorizes unrelated or destructive operations.

Omit `--max-turns` for multi-source research, repository-wide source scans, and other open-ended investigations. Use it only for a short, closed check that should finish in a few tool turns; never use `1`, because a reasoning event can consume the only turn.

For long work, split the task into bounded deliverables and add `--deadline-seconds <positive-seconds>` when the runner should enforce a wall-clock deadline. It starts the clock when the Grok child starts, sends `SIGTERM` to the agent process group at expiry, waits three seconds, and escalates to `SIGKILL` if needed. A deadline is opt-in, per worker, and never inherited by `followup`. A timed-out worker is failed and cannot be resumed; narrow the task and start fresh. Provider-aware token or cost budgets are not yet enforced. If Grok reports `max turns reached` or another non-completion stop reason, collect diagnostics once and do not `followup` that native session merely to ask it to finish. Start fresh only after narrowing the task or deliberately removing the cap.

### Observe and collect

```bash
python3 "$RUNNER" list --cwd "$PWD" --agent grok
python3 "$RUNNER" status <worker-id>
python3 "$RUNNER" collect <worker-id> --capsule
python3 "$RUNNER" collect <worker-id> --capsule --wait 30
# Failure diagnosis only:
python3 "$RUNNER" collect <worker-id>
```

Capsule collection exits `0` for success, `1` for a terminal failure/cancellation, `3` while active, and `4` when a successful worker omitted a valid capsule. Avoid a blocking wait longer than 60 seconds. The compact result excludes Grok `thought` and token-usage payloads. Use ordinary `collect` only for failure diagnosis; it still allowlists Grok's result fields and never emits `thought`. Independently inspect delegated code changes and run focused verification.

### Continue, cancel, and clean

```bash
python3 "$RUNNER" followup <worker-id> --prompt-file <absolute-follow-up-file>
python3 "$RUNNER" cancel <worker-id>
python3 "$RUNNER" cleanup <worker-id>
```

`followup` creates a new worker record and resumes the parent's native Grok `sessionId`. Grok requires the exact sandbox profile used to create that session. A `danger-full-access` parent therefore requires the flag again on every follow-up; use a fresh session to change profiles. Legacy sessions without a recorded sandbox profile and deadline-exceeded sessions are not resumed by the safe runner. `bypassPermissions` and deadlines are never inherited. `cancel` verifies process identity and terminates the owned process group. A surviving Grok child whose wrapper died becomes `orphaned`; cleanup remains blocked until it is safely cancelled or otherwise terminal.

Only one active worker may resume the same native session. Wait for or cancel it before starting another follow-up.

The async cancellation path is verified for Unix/macOS systems with `ps` and process groups. Use direct mode on Windows.

## Preserve legacy commands

The former runner path is now a thin compatibility entry point:

```bash
LEGACY_RUNNER="${CODEX_HOME:-$HOME/.codex}/skills/grok-build-cli/scripts/grok_worker.py"

python3 "$LEGACY_RUNNER" spawn \
  --cwd <absolute-cwd> \
  --prompt-file <absolute-task-file>
```

It injects `--agent grok`, filters legacy `list` output to Grok, maps `--grok-binary` to `--binary`, forwards `--deadline-seconds`, and gives an explicit `GROK_BUILD_CLI_STATE_DIR` precedence when mapping it to `AGENT_CLI_WORKERS_STATE_DIR`. Keep it for existing scripts; use the shared runner for new workflows.

The compatibility entry also forwards task/route labels and controller outcomes; its `report` command automatically filters to Grok:

```bash
python3 "$LEGACY_RUNNER" record-outcome <worker-id> \
  --outcome accepted \
  --verification passed
python3 "$LEGACY_RUNNER" report --since-days 30
```

The shared runner stores only derived, low-cardinality summaries in private local history. It never places prompt text, result text, provider thought, raw stderr, cwd, filenames, session ids, or arbitrary usage keys in telemetry. Record outcomes only after independent verification; use `purge-history` for retention and `cleanup --discard-history` only as the explicit raw-deletion escape hatch when history is unavailable or corrupt.

## Run a direct task

Use direct headless mode only when the answer is immediately required:

```bash
grok --cwd <absolute-cwd> --no-plan --no-memory --no-subagents \
  --output-format json -p '<self-contained task>'
```

Add `--permission-mode bypassPermissions` only with the authorization described above. Do not add `--check` before Grok produces the primary deliverable. If it returns only a plan or progress note, resume the saved session with a focused request for the completed result.

## Choose the working directory

| Task | Recommended CWD |
|---|---|
| Read, research, or review | Original repository |
| Small requested edit | Original repository when conflicts are unlikely |
| Parallel or isolated edit | Caller-created git worktree |

On verified Grok Build 0.2.93, headless `--worktree` accepted the flag but did not create or switch a worktree. Re-test before relying on a newer version. Otherwise create isolation with Git and pass the exact path:

```bash
git -C <repo> worktree add -b <branch> <absolute-worktree> <ref>

python3 "$RUNNER" spawn \
  --agent grok \
  --cwd <absolute-worktree> \
  --sandbox workspace-write \
  --permission-mode bypassPermissions \
  --prompt-file <absolute-task-file>
```

After collection, inspect and verify the worktree, integrate or discard deliberately, then remove it with `git worktree remove`.

## Diagnose failures

1. Run `grok --version` and `grok models` without changing model selection.
2. Run `grok inspect` in the target CWD.
3. Before code work, require Grok to report `pwd`, `git rev-parse --show-toplevel`, and `git rev-parse HEAD`. Reject output whose reported workspace differs from the exact expected worktree.
4. Never resume a native session after a workspace mismatch. Start fresh after checking the local worktree; two wrong filesystem views under the correct local CWD are a worker/provider workspace-view failure, not evidence about the target repository.
5. Inspect runner `status`, `collect`, and `stderr_path`; do not mistake a live worker for failure.
6. Distinguish a Grok provider failure from the outer Codex provider.
7. Let CC Switch manage the Grok URL and key.

Direct and async calls intentionally use the user's normal Grok configuration, including configured skills, hooks, plugins, and MCP servers.

## Finish cleanly

- collect Grok's final capsule and exit status with `--capsule`;
- verify changed files and focused tests from Codex;
- retain and inspect a timed-out write worker's worktree because process termination does not roll back partial edits;
- report worker ID, native session ID, and worktree when relevant;
- cancel abandoned workers;
- clean terminal worker state and temporary worktrees deliberately.
