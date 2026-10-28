---
name: loadmax-review
description: >
  Review a LoadMax loading/optimizer diff against the slot model, axle split,
  volume formula, and live objective. Use when the user asks to review HiGHS,
  summary, axle, or CSV-reader changes, or runs /loadmax-review.
  For generic diffs, the bundled /review skill is enough.
when-to-use: "review loading, review optimizer, review axles, review HiGHS, /loadmax-review"
---

# Skill: LoadMax review

Review the **diff first**, not the whole repo. Inspect surrounding code only to establish correctness.

## Scope

1. Identify changed behavior from the diff (uncommitted, a named branch, or a described change).
2. Focus findings on changed code.
3. Re-read CSVs if a finding depends on a pallet count, weight, or cap. Do not trust a chat recap.

## What to check (this order)

### 1. Domain correctness

- Slot model still one pallet per `(container, layer, position)`? No accidental continuous x–y packing or pallet rotation.
- Truck axles still A+C front and B+D rear? Empty/`0` `max_front_wt` / `max_rear_wt` still skip those constraints.
- Volume % still Pyomo denominator: `H × (max_plt_n − 2) + 2 × H_door`.
- Live 4-term objective preserved unless the change is explicitly about the objective. Commented older fill in `container_loading_highs.py` stays unless the user asked to delete it.
- `palletProfitDict` still dump-only; first-truck bias still `container_priority`.
- Live path still does not import `future_addons/` (SQL, COOIS, `UtilityClass`, `loadBreak`) unless that was the request.

### 2. Regression risk

- Compact packing, door positions, `first_plt_n`, two layers, A-B 30-pad map.
- Summary overwrite under `optimization_results/` — no accidental new default filename that clobbers a comparison the user is watching.

### 3. Tests and checks

- Cheap units (parse, axle math, volume) covered if the diff touched them.
- A full A-B solve is not required to "pass review" unless mix/axles/volume changed.

### 4. Clarity

- Names match surrounding code. No dead code or stale comments that contradict `DECISIONS.md`.
- Skip style-only nits.

### 5. Safety

- No secrets (SQL password, `.env`) in code or logs.
- Paths and subprocess args handled safely.

## Output

For each substantive issue:

- **File + line**
- **Severity**: `critical` / `warning` / `suggestion`
- **Issue**
- **Why it matters** (concrete failure: wrong rear axle, inflated volume %, silent drop of priority SKUs)
- **Fix**

End with a short overall summary of what was checked. If nothing substantive, say so.

## What NOT to do

- Do not rewrite working code for preference.
- Do not audit unrelated files.
- Do not claim "looks good" without walking the domain list above when those files changed.
