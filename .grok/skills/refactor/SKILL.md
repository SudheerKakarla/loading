---
name: refactor
description: >
  Restructure LoadMax code without changing externally observable behavior.
  Use when the user says refactor, extract, rename, split a module, or /refactor.
when-to-use: "refactor, extract function, rename, split module, /refactor"
---

# Skill: Refactor

## How to invoke

"Refactor {file, class, or function}" or `/refactor`.

## Before touching anything

1. Identify exactly what is being refactored.
2. State in one sentence the structural change and why.
3. Assess whether existing tests (or a cheap characterization check) cover current observable behavior: solve status, assigned pallets, axle columns, volume %.
4. If protection is insufficient, add characterization tests first (`tests/` for parse/axle/volume units). Get approval for non-trivial work.

## Constraints

- Do not intentionally change externally observable behavior.
- Do not bundle feature work or unrelated bug fixes.
- Small logical changes, not a wholesale rewrite.
- Preserve public names (`read_im_input_csv`, `ContainerLoadingOptimizerHiGHS`, summary writers) unless the user approved an API migration.
- Do not create commits unless asked.
- Do not move live code into `future_addons/` or the reverse unless asked.
- Do not introduce a `src/` layout.

## Common patterns

### Extract function

Descriptive name; replace the old call site without duplication. Prefer testing observable behavior.

### Rename

Update source, tests, docs, and references. Search the repo for stale names. Skip generated/vendor/`__pycache__` noise.

### Move to a separate module

Create the module under `loadmax/`, move code, update imports (`loadmax/__init__.py` if the name is exported). Check import cycles. Run compile-check after the move.

### Break up a large function

Extract cohesive responsibilities. Preserve behavior. Avoid trivial helpers.

## After refactoring

1. `python -m py_compile` on touched files. Spawn `code-checker` when available.
2. Do not run a 600 s A-B solve unless behavior of mix/axles/volume could have changed.
3. Report structural change, behavior intentionally preserved, and exact check results.
