---
name: feature-spec
description: >
  Specify then implement a focused LoadMax change. Use when adding solver,
  summary, CLI, or CSV behavior, or when the user says "implement this feature",
  "feature spec", or /feature-spec.
when-to-use: "new feature, implement, add a flag, change the objective, /feature-spec"
---

# Skill: Feature Specification

## How to invoke

Fill in the template (or extract it from the user's request) and follow the agent instructions. Say `/feature-spec` or "implement this using the feature-spec skill".

## Template

### Feature name

### User-visible behavior

What the user can do and observe (CLI, CSVs in `optimization_results/`, printed summary).

### Inputs

Types, formats, CSV keys, trust boundaries.

### Outputs

Return values, files, side effects, errors.

### Edge cases to handle

Empty axle caps, compact packing yes/no, partials, missing `palletMaterialDict`, door positions.

### What success looks like

Observable acceptance criteria. What a test or a short solve must prove.

### What's out of scope

Explicitly exclude adjacent work (`future_addons/`, Gurobi, ocean mode, load-break unless asked).

### Compatibility / constraints

Must preserve slot model, A+C/B+D axles, Pyomo volume denominator, live 4-term objective unless the spec changes one of those on purpose.

---

## Agent instructions

1. Read the spec and existing relevant code. Re-read CSVs if numbers matter.
2. Ask the minimum clarifying questions. Do not implement while a requirement that could change the design is unresolved.
3. If the change has a cheap testable unit (CSV parse, axle math, volume denominator, mapping), add tests under `tests/` first:
   - happy path;
   - each specified edge case;
   - at least one invalid/failure case where applicable;
   - regression that must remain unchanged.
   Do not add a 600 s MIP as a unit test.
4. For non-trivial features, show proposed tests (or the planned check) and wait for approval.
5. Implement the smallest focused change in `loadmax/` or `examples/`. Never introduce a `src/` tree.
6. Run `python -m py_compile` on touched files. Spawn `code-checker` when available.
7. Smoke-run `python examples/run_highs.py data/im_input_A_B.csv 0.01` only if the change can alter load mix, axles, or volume %.
8. Summarize what was built, assumptions, and anything not verified. After the user confirms a finding, add a short dated block to `DECISIONS.md`.
