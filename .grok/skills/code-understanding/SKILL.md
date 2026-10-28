---
name: code-understanding
description: >
  Answer questions about this codebase without modifying it. Use for onboarding,
  architecture tracing, locating behavior, and explaining execution or data flow.
  Use when the user asks how loading, axles, volume %, the objective, CSV input,
  or a symbol works, or runs /code-understanding.
when-to-use: "how does this work, where is X, trace, onboarding, explain the model, /code-understanding"
---

# Skill: Code Understanding

Read-only. Trace concrete entry points before summarizing.

## LoadMax entry points

| Question about | Start here |
|----------------|------------|
| Truck solve CLI | `examples/run_highs.py` |
| HiGHS MIP / objective / stacking | `loadmax/container_loading_highs.py` |
| CSV → `inputParams` | `loadmax/data_reader.py` |
| Utilization / volume % / axle columns | `loadmax/results_summary.py` |
| Geometry axles (not the MIP constraint) | `loadmax/axle_geometry.py` |
| Optional Gurobi | `loadmax/optimizer.py` (skip unless asked) |
| Original Pyomo / Excel / SQL | `loadmax/future_addons/` (do not import on the live path) |
| Floor maps 20/21/22 | `docs/truck_pallet_layouts.html` |
| Inputs / MIP / constraints | `docs/loadmax_model.html` |
| Confirmed numbers | `DECISIONS.md` then re-read the CSV |

A-B dump: `data/im_input_A_B.csv`. Do not trust a chat recap for pallet counts or weights.

## Workflow

1. Restate the concrete question and pick the smallest entry point from the table.
2. Inspect repo structure and that file before opening many others.
3. Search for symbols, CLI args, tests, and call sites related to the question.
4. Trace the smallest relevant path.
5. Use tests as evidence for intended behavior when they exist.
6. Distinguish observed code from inference or undocumented intent.
7. Return:
   - entry point(s);
   - execution/data flow;
   - important modules/symbols;
   - relevant tests (or that none exist);
   - uncertainties or follow-up areas.

## Constraints

- Do not edit files unless the user separately asks for changes.
- Do not claim a path was inspected if it was not.
- Prefer precise file/symbol references over broad summaries.
- Do not dump large files; retrieve focused sections.
- Do not call `future_addons/` unless the question is about those scripts.
