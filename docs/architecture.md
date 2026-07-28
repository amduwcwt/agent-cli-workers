# Architecture

## Boundary

`agent_worker.py` is a local lifecycle adapter, not an orchestrator control plane. Every public command emits one JSON object. The controlling agent or human owns task decomposition, worktree creation, result review, verification, integration, and token or cost budgets. The runner can enforce an opt-in per-worker wall-clock deadline but does not add a turn cap unless the caller explicitly requests one.

## Lifecycle

```text
spawn -> queued -> running -> succeeded|failed|cancelled|lost
                              -> derived history summary
                              -> controller outcome
                              -> followup creates a new worker
                              -> cleanup removes raw terminal state
```

The detached wrapper owns an agent process group. Metadata records wrapper and child identities so cancellation does not signal a reused unrelated PID. If a wrapper disappears while its child remains identifiable, the record becomes `orphaned` and can still be cancelled safely.

An optional `--deadline-seconds` starts when the provider child starts. At expiry the wrapper records deadline metadata, sends `SIGTERM` to the agent process group, waits three seconds, and escalates to `SIGKILL`. The wrapper remains alive to clean the prompt, parse residual output, and commit terminal metadata. The result is `failed` with `termination_reason=deadline_exceeded`; it cannot be resumed, and follow-ups do not inherit the parent's deadline. This is process-group cancellation, not rollback: partial workspace writes remain, and descendants that deliberately leave the process group are outside the cancellation boundary.

Native continuation is adapter-specific:

- Grok stores `sessionId` and resumes with `--resume` under the same sandbox profile.
- Codex stores `thread_id` and resumes through `codex exec resume`.

Only one active descendant may resume a native session at a time.

## Trust boundaries

1. Prompt text is untrusted and may contain secrets. Prefer `--prompt-file` or stdin; prompt files are private and deleted after execution.
2. Provider output is untrusted. Grok JSON is parsed through a top-level allowlist. The provider `thought` field and malformed/truncated raw stdout are never emitted by the runner.
3. A completion capsule is a context boundary, not proof. Root Grok prompts receive the contract by default. `collect --capsule` extracts the last ordered six-field block, accepts plain or Markdown-bold labels, normalizes it, limits it to 16 KiB, and rejects a successful worker without a valid block.
4. Worker claims are untrusted. The caller compares workspace proof, inspects diffs, and reruns focused verification.
5. State paths are privileged local persistence. The state root must be owned, non-symlinked, and inaccessible to group/other users.
6. Telemetry is derived persistence, not a transcript archive. The sibling history root receives one atomic `0600` JSON summary per worker under an owned, non-symlinked `0700` directory. It excludes prompt/result text, thought, raw stderr, cwd, filenames, session ids, diffs, and arbitrary provider keys.
7. Task class, route reason, outcome, verification, and reason codes are controller-submitted allowlisted labels with explicit provenance. They are not inferred from prompts and are not provider self-evaluation.

## Telemetry lifecycle

The terminal wrapper creates or refreshes the derived summary. `cleanup` verifies or repairs it before deleting raw worker state; `--discard-history` is the explicit escape hatch when telemetry is broken and raw sensitive artifacts must still be removed. Outcome updates and cleanup serialize through the history lock, while atomic replacement prevents partial summaries.

`report` reads only supported summaries and aggregates fixed low-cardinality dimensions. It reports corrupt/unsupported counts, root/follow-up run counts, feedback missingness, token availability, capsule status, artifact bytes, and duration denominators. Unknown schemas are skipped rather than rewritten. `purge-history` supplies retention and rollback; `AGENT_CLI_WORKERS_TELEMETRY=0` disables new summaries. No network sink exists.

## Isolation

The runner defaults to `read-only`. An editing worker requires `workspace-write` or an explicitly authorized bypass. The runner does not create Git worktrees because worktree naming, base revisions, integration, and deletion belong to the caller.

The workflow contract keeps one writer per overlapping file set. A read-only reviewer may run after a writer finishes or at a fixed commit.

## Adapter contract

An adapter must provide:

- deterministic argv construction without prompt text in argv;
- a private structured capture format;
- parsing of final text and a native session identifier;
- explicit sandbox and dangerous-mode behavior;
- process identity evidence sufficient for safe cancellation;
- focused fake-binary tests for spawn, collection, resume, failure, and cancellation.

Adding an adapter should not expand the project into automatic planning or merge management.
