# Chunker

Splits one truck's pick lines into jobs a picker can carry in one trip. The picker in `../source/` then routes and assigns those jobs. This folder does not read a LoadMax solve.

Input columns: `truck_id` or `group_id`, `depot_id`, `item_id`, `location_id`, `weight`. Optional: `destination_id`, `quantity`, `volume`, `pick_time`, `departure_time`, `must_ship`, `account_tier`, `base_weight`, `pick_mode`. `pick_mode` is a free lowercase name. A blank cell has no mode. Other extra columns are kept and ignored. When `truck_id` and `group_id` are both filled, `truck_id` is the key. The key is stored in the `truck_id` column. Lines of one source order and one mode stay in one group. A table with neither id raises. A truck table is unchanged when `group_id` is absent.

`inputs/chunk_warehouse/truck_loads.csv` is a sample built from the eight-aisle warehouse. Each source order stays in one group. Trucks are filled to about 8–12 tonnes and alternate between `PACK_A` and `PACK_B`. `sku_id` and `source_order_id` are labels only. `account_tier` is blank because one truck mixes key and standard orders, and the chunker allows one tier per truck. The picker orders in `inputs/layout_warehouse` are not this file: those trips have no truck and no line weight. Rebuild the sample with `python chunker/build_truck_loads.py`.

`departure_time` is minutes since the wave start, or a datetime when `--wave-start` is set. The job cutoff is that time minus the staging buffer (30 minutes unless changed). `--wave-cutoff` is the pick deadline when the line has no departure, and that number is stored as written. A cutoff taken from a departure still subtracts the buffer. `--lead-times` replaces the buffer with the sum of `fixed_minutes + minutes_per_pallet` for one pallet, with no queue. `inputs/lead_times.example.csv` is an illustration (checker order 1, then wrapping order 2), not a site measurement. A blank `resource_count` means 1.

```text
python chunker/chunk_loads.py --loads truck_loads.csv --travel travel_times.csv --max-job-weight 2500 --out outputs/chunk_example
```

Run it from `order_picking/`. `--pickers` can supply the weight cap from the heaviest picker when `--max-job-weight` is omitted. `--destination-grouping` keeps one destination together until that destination alone exceeds the cap.

Outputs:

| File | Role |
|------|------|
| `orders.csv` | Jobs in the picker's order shape, including `truck_id` and `depot_id`. `pick_mode` is added when any line has one |
| `order_items.csv` | One row per input line. The same `pick_mode` column, when any line has one. A line with no `pick_time` receives the mode's `default_pick_time` |
| `jobs.csv` | Cutoff, priority, walking minutes, and whether the job is over the cap. `pick_mode` is added with the order column. `pallets` appears only when `--mode-profile` is set |
| `by_depot/<depot_id>/` | The same files for one dock |

`--mode-profile` reads `pick_mode,max_job_lines,max_job_weight,max_job_volume,default_pick_time,job_overhead,pallet_basis`. A blank cell keeps the global chunker value. A filled line, weight, or volume cap is the smaller of that number and the global cap. `pallet_basis` is `job` (one pallet per job, the default) or `line` (one pallet per line). The per-mode weight and volume caps sit on top of the picker limits. `walk_minutes` stays the walk. On the cover, the mode's pick time and overhead are added after the Held-Karp tour. `inputs/mode_profile.example.csv` is an illustration, not a site measurement:

| pick_mode | max_job_lines | max_job_weight | max_job_volume | default_pick_time | job_overhead | pallet_basis |
|-----------|---------------|----------------|----------------|-------------------|--------------|--------------|
| bulk | 1 | blank, so the global cap | blank | 0.5 | 2 | line |
| pickface | 15 | blank | blank | 0.5 | 5 | job |

`import_ddl.py` turns a caller-mapped export into `demand.csv`. The map is `source_column,target_column`. It does not hard-code a site or SAP name. Required targets are `depot_id`, `sku_id`, `quantity`, and `group_id` or `truck_id`. Optional targets include `batch_id`, `uom`, `destination_id`, `must_ship`, `departure_time`, `account_tier`, and `location_id`. The SKU master uses `weight` or `unit_weight_kg`, and `volume` or `unit_volume_m3` when that column is present. A bad quantity, a repeated line, and a missing SKU are written to `import_rejected.csv` and the process exits 1. The quantity message names the file, the row, and the column. When `location_id` is mapped, `truck_loads.csv` is also written so the chunker can read the same rows. An unmapped `item_id` is written as `I1`, `I2`, and so on.

```text
python chunker/import_ddl.py --source export.csv --column-map column_map.csv --sku-master sku_master.csv --out outputs/imported
```

`allocate_lines.py` places demand on caller-supplied stock before the chunker runs. It does not read `wh_inventory.csv` and it does not solve an allocation MIP. Demand is processed by `departure_time` (blank last), then `must_ship`, then input order. Whole pallets come from bulk when `pallet_qty` is set and a bulk row holds that quantity; that line is `bulk`. Whole cases come from pick-face stock, then from bulk, and the line keeps that row's `location_type`. Units below one case use an already-opened case at the same location and batch, or open the smallest pick-face row that covers them. Inside a tier the earliest expiry wins, then the location nearest the dock, then the location id. A blank `case_qty` means 1. A blank `pallet_qty` means there is no pallet tier. The master columns are `unit_weight_kg` and `unit_volume_m3`. A missing SKU is written to `allocation_rejected.csv` and the process exits 1. A short row is written to `short_stock.csv` (`sku_id,requested,allocated,short`) and the process exits 1 unless `--allow-short`. `allocation_summary.csv` records the full-pallet rate, the full-case rate, the opened-case count, and the depot-to-location walking minutes. The grouping key is written in `truck_id`.

```text
python chunker/allocate_lines.py --demand demand.csv --stock stock.csv --sku-master sku_master.csv --travel travel_times.csv --out outputs/allocated
```

`chunk_model.py` is the set-partitioning alternative. HiGHS picks one exact cover of each truck. A source order that fits a picker stays whole, except that a mixed-mode source order is split by mode because bulk and pick-face are different tasks. Orders on the same dock may share a job, including orders for different stops. `--destination-grouping` keeps each job to one stop, the same flag as the savings command. A job must fit one picker on weight and volume together, and it may visit at most 16 pick locations. With the default line cap of 15, a job that passes the line cap already has at most 15 pick locations. A line or source order that fits no picker is listed in `rejected.csv` and is not split to force a job. `no_picker_for_mode` means the load fits some picker on weight and volume and no picker who fits allows the mode. Optional `allowed_modes` is a free list separated by semicolons. A blank cell allows every mode. Two named modes are never put in one job. A blank-mode line may join either mode. The default time limit is 120 seconds per truck. A cover found before that limit is kept, and `solve.csv` records its relative MIP gap. A gap of 0 is a proved optimum. A truck with no cover yet uses the savings jobs instead of stopping the run, and that row has no gap. A must-ship line is covered once, and only by a job that was generated as priority-eligible. The default is any feasible job that contains the line. `--priority-max-lines` is the optional smaller cap.

```text
python chunker/chunk_model.py --loads inputs/chunk_warehouse/truck_loads.csv --travel inputs/layout_warehouse/travel_times.csv --pickers inputs/layout_warehouse/pickers.csv --out outputs/chunk_model
```

`jobs.csv` `walk_minutes` from this command is the Held-Karp tour. The savings command still writes the nearest-neighbor walk. The formulation is `formulation.md`. The cost of keeping a fitting order in one job is `order_whole_cost_memo.md`.

`--jack-tier-merges` is off by default. When it is on, a merge of two or more source orders may pass the pallet jack only if the job holds a line that jack cannot carry. A single source order that fits a picker stays whole. The heaviest-picker cap remains the default.

## LoadMax sequence

This folder does not read a LoadMax solve. A later link, if one is approved, would read `loadmax_sequence.csv` with columns `truck_id, container, layer, position, sku_id, quantity`. Those rows would map onto the existing `load_stops` depth so a pick trip can follow loading order. `priority_weight` and LoadMax `container_priority` score different decisions, so there is no wrapper and that file is not read.

Point the picker at one depot folder:

```text
python source/order_picking_optimizer.py --orders outputs/chunk_example/by_depot/DEPOT/orders.csv --items outputs/chunk_example/by_depot/DEPOT/order_items.csv --pickers inputs/layout_example/pickers.csv --travel travel_times.csv --depot DEPOT
```
