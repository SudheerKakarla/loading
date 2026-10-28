# Order Picking Planning Optimizer — v0.3

**2026-10-22.** This note is what to copy. The equations the code implements are in `order_picking_planning_optimizer_spec.md`. Draft v0.2 (`planning_optimizer_spec_draft_v0.2.md`) is the earlier design and is not the live spec.

v0.2 is easy to misread. Its Phase 1 default is Held–Karp, it carries a `deadline_penalty_weight`, and an older copy of it set `mip_gap` to 0.01 and listed lexicographic release as out of scope. None of those four is what the solver does.

## What the solver does

The default objective is one weighted sum. `--staged-assignment` is off unless the caller sets it. When it is on, the stages are must-ship count, then other jobs by weight, then lateness, then imbalance only when `balance_weight` > 0, then the jack count only when `prefer_jack` > 0. Each stage locks the objective value it reached, at tolerance `1e-6`. The public score is the release-weight sum.

A caller `mip_gap` is stored as `gap_requested` and does not loosen the stop. The stop is a tenth of the smallest positive weight, raised above about `1e-6` times an objective upper bound when that still sits below half a job. HiGHS receives `mip_rel_gap = absolute / max(objective_upper, 1)`. Gurobi receives `MIPGap = 0` and `MIPGapAbs` equal to the absolute tolerance.

Phase 1 default is nearest neighbor plus directed 2-opt. `--phase1 highs` asks for an exact directed tour. Lateness, when it is on, is minutes past `cutoff_time` under earliest-deadline order. There is no `deadline_penalty_weight` and no hard-deadline status.

`run_docks.py` sums busy minutes against `shift_duration`. A pass is that total. Clock overlap and the walk between docks stay outside the screen.

## Where staged release came from

Staged release was added because the weighted sum failed two of our own cases. T8 drops one of two equal jobs when lateness costs more than the weight. A relative gap also dropped five PACK_B jobs (`TRK02-J02`, `TRK14-J09`, `TRK14-J15`, `TRK16-J01`, `TRK16-J03`) while jack pickers sat idle. The tenth-of-a-job stop and the opt-in stages are the fix for those cases.

Arvato’s warehouse slides make the same point, that a single weighted objective needs careful tuning. If those slides are cited, cite them as corroboration. They are not the origin of `--staged-assignment`. Their allocation and module-distance batching were not adopted. The list, with the condition that would reopen each item, is `../webinar notes/not_adopted.md`.

## Still open

The first open chunker item is the heaviest-picker cap. A job can be sized to the heaviest machine, so pallet jacks can sit idle. Switching that cap to the lightest picker stays closed, because weight and volume are judged together. After that cap: loading order, dock staging space, and aisle congestion. The table is `../TRIED.md`.
