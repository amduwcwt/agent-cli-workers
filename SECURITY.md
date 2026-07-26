# Security

## Reporting

After the GitHub repository is published, report suspected vulnerabilities through GitHub private vulnerability reporting when available. Do not include secrets, raw provider transcripts, or local state files in a public issue.

## Operational risks

- Agent CLIs execute with the current user's credentials. Start with `read-only` and grant write or bypass modes per task.
- Raw stdout/stderr captures remain in the private worker directory until cleanup. `collect --capsule` minimizes what enters the controlling context but does not erase local captures.
- Ordinary `collect` is for terminal failure diagnosis and may return allowlisted final text plus stderr. Treat both as potentially sensitive.
- Derived telemetry is local and intentionally excludes prompt/result text, thought, raw stderr, cwd, filenames, session ids, diffs, and arbitrary provider usage keys. Controller labels remain potentially sensitive operational metadata; retain and purge them deliberately.
- Telemetry history must remain an owned, non-symlinked `0700` directory with atomic `0600` summaries. A corrupt or unavailable history blocks normal cleanup so raw evidence is not silently discarded; use `--discard-history` only when deleting raw sensitive artifacts is more important than retaining the summary.
- `report` is descriptive, not causal. Small samples, missing controller feedback, follow-up runs, and controller selection bias can produce misleading routing conclusions.
- The runner validates process identity before cancellation, but lifecycle support assumes Unix process groups and `ps` behavior.
- Deadline and cancellation signals cover the agent-owned process group, not descendants that deliberately create another session/process group. They also do not roll back filesystem writes; inspect partial diffs from a terminated write worker before integration or cleanup.
- The caller owns worktree creation, integration, and deletion. Never infer that a worker in another worktree covers the current repository.

## Required review for changes

Changes to state/history paths, telemetry fields, permissions, process signaling, shell argv, result parsing, sandbox defaults, model policy, or cleanup behavior require focused regression tests and a security review.
