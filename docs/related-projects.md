# Related projects

This comparison was checked against public GitHub repositories on 2026-07-24. It describes project scope, not quality.

| Project | Overlap | Main difference from agent-cli-workers |
|---|---|---|
| [grok-orchestra](https://github.com/Sora-bluesky/grok-orchestra) | Grok + Codex, prompt packets, single-writer controls, verification gates | PowerShell/Windows-oriented harness with Grok as interactive operator and one-shot Codex delegation; no Grok native-session adapter or bounded capsule collection |
| [Agent Deck](https://github.com/asheshgoplani/agent-deck) | Multiple agent CLIs, persistent sessions, native forks/resume, worktrees | TUI/tmux mission control for a fleet of interactive sessions |
| [Claude Squad](https://github.com/smtg-ai/claude-squad) | Background terminal agents and isolated Git worktrees | TUI/tmux workspace manager; does not define a compact provider-output handoff protocol |
| [Agent Orchestrator](https://github.com/AgentWrapper/agent-orchestrator) | Agent adapters, isolated workspaces, session state, follow-up | Desktop app plus daemon and automated PR/CI/review/merge feedback loops |
| [Kodo](https://github.com/ikamensh/kodo) | Multi-CLI workers, resumable runs, role separation, verification | Autonomous planner/team loop intended for long unattended coding runs |
| [Ouroboros](https://github.com/neeboo/ouroboros) | Resumable Codex execution, worktree sessions, verifier and repair loops | SQLite-backed task graph, dashboard, planner/verifier/integrator control plane |

GitHub code search found no public repository using the exact six-field “completion capsule” term and no Grok `sessionId`/`--resume` implementation in `grok-orchestra` at the time of checking.

The intended niche here is therefore not “another fleet manager.” It is a small skill-driven primitive that lets an existing Codex session delegate one task without importing a large transcript or adopting a new control plane.
