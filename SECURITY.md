# Security

## Reporting

After the GitHub repository is published, report suspected vulnerabilities through GitHub private vulnerability reporting when available. Do not include secrets, raw provider transcripts, or local state files in a public issue.

## Operational risks

- Agent CLIs execute with the current user's credentials. Start with `read-only` and grant write or bypass modes per task.
- Raw stdout/stderr captures remain in the private worker directory until cleanup. `collect --capsule` minimizes what enters the controlling context but does not erase local captures.
- Ordinary `collect` is for terminal failure diagnosis and may return allowlisted final text plus stderr. Treat both as potentially sensitive.
- The runner validates process identity before cancellation, but lifecycle support assumes Unix process groups and `ps` behavior.
- The caller owns worktree creation, integration, and deletion. Never infer that a worker in another worktree covers the current repository.

## Required review for changes

Changes to state paths, permissions, process signaling, shell argv, result parsing, sandbox defaults, model policy, or cleanup behavior require focused regression tests and a security review.
