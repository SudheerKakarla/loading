# AGENTS.md

How to work in this repo. Confirmed facts live in `DECISIONS.md`.
Do not copy chat recaps into this file.

## Layout

| Path | Role |
|------|------|
| `loadmax/` | Live library: HiGHS MIP, optional Gurobi, CSV reader, summary |
| `loadmax/future_addons/` | Original app scripts (Pyomo, Excel preprocess, load-break). Not on the HiGHS path |
| `data/` | Dumped `im_input_*.csv` plus source photos |
| `examples/run_highs.py` | Truck solve CLI |
| `docs/` | Usage / migration notes; `truck_pallet_layouts.html` for floor maps |
| `optimization_results/` | Loading instructions + utilization CSVs (overwrite on re-run) |
| `attachments/` | Extra screenshots (PalletLayout / Config) |

No `src/` layout, no pip package unless asked.

## How to run

```bash
pip install -r requirements.txt
python examples/run_highs.py
python examples/run_highs.py data/im_input_A_B.csv 0.01
```

HiGHS via scipy (`scipy.optimize.milp`). Gurobi (`loadmax/optimizer.py`) is optional; `__init__.py` imports it only if `gurobipy` is installed.

Defaults for A-B work: truck mode, compact packing from the CSV (`yes`), time limit 600 s, MIP gap 0.01. Axle limits are used when `max_front_wt` / `max_rear_wt` are positive numbers.

## Constraints

- Slot model: one pallet per `(container, layer, position)`. Not continuous x–y packing. Pallet L×W is 40″×48″ unless a Config layout says otherwise; the solver does not rotate pallets.
- Floor map comes from PalletLayout / Config (groups A/B/C/D → front/rear/left/right, door, `first_plt_n`). A-B is the 30-pad 40′ map, 2 layers.
- Truck axles: front = Σ (A+C)×weight on front positions; rear = Σ (B+D)×weight on rear. A-B caps 19920 / 22080 lb, total 42000 lb.
- Volume % in the summary matches original Pyomo:  
  `sum(stack heights) / (containerHeight × (max_plt_n − 2) + 2 × containerHeightAtDoor)`.
- Live truck objective (Pyomo 4-term): priority SKU (material in `skuPriorityIncentive`) gets `container_priority × priorityIncentive`; others get `container_priority`; partials × `prtl_frac`; plus `y_prior × incentive`; minus SKU penalty. Older fill (no 2× for priority SKUs) is commented in `container_loading_highs.py`, not deleted.
- `palletProfitDict` / `pallet_sku_priority_incentive` ranks are dump extras. Original Pyomo does not use them. First-truck bias is `container_priority`.
- Do not call `future_addons/` (SQL, COOIS, `UtilityClass`) unless the user asks. `loadBreak.py` is optional warehouse split, not part of every solve.

## Session habit

One job per chat. After a finding the user confirms, add a short dated block to `DECISIONS.md`.
Re-read CSVs for numbers; do not trust a previous chat recap.
