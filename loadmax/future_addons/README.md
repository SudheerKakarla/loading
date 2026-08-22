# Future add-ons

Scripts from the original LoadMax app that are **not** used by the current HiGHS path (`examples/run_highs.py`). Keep them here as reference until they are wired in.

These files still import old app helpers (`UtilityClass`, `UtilityClass_Loadbreak`, SQL, Excel templates). They will not run as a drop-in from this folder.

`preprocess_trucks.py` reads SKU master data from SQL. Set `LOADMAX_SQL_PWD` (see `.env.example`). Do not put a password in source.

| File | Role | When to use |
|------|------|-------------|
| `optimizer_pyomo.py` | Original Pyomo + Gurobi truck/ocean model (the screenshot results). Source of the 4-term objective, compact packing, axle A+C / B+D, volume %. | Reference when matching old UI results. |
| `preprocess_trucks.py` | Excel demand + SKU master → full/partial pallet rows, height, weight (40×48). | When you have raw truck Excel instead of `im_input_*.csv`. |
| `read_inputs.py` | Template (Config / Limits / Options) + preprocessed CSV → `inputParams` dump. | After preprocess, to build the CSV HiGHS already reads. |
| `loadBreak.py` | COOIS warehouse tool: split some full pallets into two half-pallets (layers / height / weight) to hit a full:partial ratio. | Optional, only when you need more partials for stacking. Not part of every truck solve. |

Current solver stack stays in `loadmax/`:

- `container_loading_highs.py` — HiGHS MIP
- `optimizer.py` — Gurobi VDR (optional)
- `data_reader.py` — `im_input_*.csv`
- `results_summary.py` — utilization + front/rear cargo
- `models.py` — pallet / load objects
