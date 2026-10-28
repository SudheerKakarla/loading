# LoadMax agent setup

Portable vibe-coding kit from `C:\Users\sudhe\AgentsFiles`, mapped onto Grok’s project paths. Do not copy that template’s `src/` or placeholder tests — this repo is `loadmax/` + `examples/`.

| Kit idea | Here | When to use |
|----------|------|-------------|
| `AGENTS.md` | repo-root `AGENTS.md` | Every session. Keep lean; facts go in `DECISIONS.md`. |
| Subagent: code checker | `.grok/agents/code-checker.md` | After Python edits. Independent compile check. |
| Subagent: web researcher | `.grok/agents/web-researcher.md` | External docs/solver questions. Compact cited answer. |
| Skill: code understanding | `.grok/skills/code-understanding/` | Onboarding, “how does X work”. Read-only. |
| Prompt: feature spec | `.grok/skills/feature-spec/` | New behavior. Ambiguity first, then smallest change. |
| Prompt: refactor | `.grok/skills/refactor/` | Structure only; no mix of features/fixes. |
| Prompt: code review | `.grok/skills/loadmax-review/` | Loading/optimizer diffs (axles, volume, objective). Bundled `/review` for generic diffs. |

Invoke skills with `/name` (or say the trigger phrase). Spawn agents with `spawn_subagent` using `subagent_type` equal to the agent `name`.

Not fetched from the kit (wrong layout for this repo): `src/main.py`, `tests/conftest.py`, `tests/test_example.py`, kit `AGENTS.md` `src/` / ruff-mypy-pytest loop. Add real tests under `tests/` when a unit is worth pinning (CSV parse, axle math, volume denominator) — not a 600 s MIP. The truck solver is scipy HiGHS (`loadmax/container_loading_highs.py`).
