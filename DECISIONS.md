# Decisions

Short facts we confirmed. Re-read CSVs if an exact pallet count or weight matters.

## 2026-08-22

- Repo is git-initialized locally. SQL password for `preprocess_trucks.py` is `LOADMAX_SQL_PWD` (see `.env.example`), not source. Wait for a remote name before first push.

- Live path is HiGHS (`loadmax/container_loading_highs.py`) from dumped `data/im_input_*.csv`. Run `python examples/run_highs.py data/im_input_A_B.csv 0.01` (600 s, gap 1%).
- Original screenshot results come from `loadmax/future_addons/optimizer_pyomo.py` (Pyomo + Gurobi). Keep that file as reference; do not delete the older commented objective.
- A-B market dump: 6 trucks, 30 floor positions, 2 layers, door `[30, 29]`, `first_plt_n=12`, compact packing yes, `max_wt=42000`, front/rear 19920/22080. Demand 217 full + 2 partial types (`plt_221`/`plt_222`).
- `palletMaterialDict` for A-B is the Excel row in `data/material_dict.jpg`. `plt_37`, `plt_38`, `plt_221`, `plt_222` share SKU `409561210`. Applied in `examples/run_highs.py` (`_apply_ab_priority_materials`) because that row was missing from the first photos.
- Pallet footprint is assumed 40″×48″ (`preprocess_trucks.py`). Length/width are not MIP decisions. Three 40′ PalletLayout variants (20 same-way, 21 mixed, 22 pinwheel) are explained in `docs/truck_pallet_layouts.html`. A-B used the 30-pad map.
- Axle cargo in the UI is A+C (front) and B+D (rear), so front+rear ≈ total weight. HiGHS originally used only A / B; that overran rear. Fixed to match Pyomo.
- Volume % gap vs the screenshot (~4%) was the denominator: Pyomo uses `(H × (max_plt_n−2) + 2×H_door)` = 9×28+2×3.5 = 259, not 30×9 = 270. Summary now matches Pyomo.
- Priority into the solver: Excel YES → `skuPriorityIncentive` membership (2× fill) and `max_prior_*` (Balance Pallets). Partial fraction = leftover cases / cases per pallet. `palletProfitDict` = 10/rank on BR16 is dump-only; Pyomo does not read it. First truck is `container_priority` (A-B 800…300; `read_inputs` uses 1.0 / last 0.9).
- HiGHS A-B with real materials, compact packing, 600 s: optimal, gap 0.89%, 215 full + 1 partial (216) vs screenshot 213+2 (215). Mix differs (HiGHS took more fulls, one fewer partial). Axles under limits. 300 s had stopped at 2.12% / 212+2.
- Unused original scripts live in `loadmax/future_addons/`: `optimizer_pyomo.py`, `preprocess_trucks.py`, `read_inputs.py`, `loadBreak.py`. See that folder’s README. Do not import them on the default HiGHS path.

## Open (not decided)

- Whether to persist `palletMaterialDict` into `data/im_input_A_B.csv` instead of overlaying it in `run_highs.py`.
- Whether compact packing should stay on for every A-B compare (it matches Pyomo when Config says yes; it also changes the mix vs a hole-allowed solve).
- Whether to wire `preprocess_trucks` + `read_inputs` so raw Excel builds `im_input` without screenshots.
- Whether `loadBreak.py` (split fulls into half-pallets) should run only when utilization is short.
