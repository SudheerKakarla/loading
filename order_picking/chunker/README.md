# Chunker

Splits one truck's pick lines into jobs a picker can carry in one trip. The picker in `../source/` then routes and assigns those jobs. This folder does not read a LoadMax solve.

Input columns: `truck_id`, `depot_id`, `item_id`, `location_id`, `weight`. Optional: `destination_id`, `quantity`, `volume`, `pick_time`, `departure_time`, `must_ship`, `account_tier`, `base_weight`. Extra columns are kept and ignored.

`inputs/chunk_warehouse/truck_loads.csv` is a sample built from the eight-aisle warehouse. Each source order stays on one truck. Trucks are filled to about 8–12 tonnes and alternate between `PACK_A` and `PACK_B`. `sku_id` and `source_order_id` are labels only. `account_tier` is blank because one truck mixes key and standard orders, and the chunker allows one tier per truck. The picker orders in `inputs/layout_warehouse` are not this file: those trips have no truck and no line weight. Rebuild the sample with `python chunker/build_truck_loads.py`.

`departure_time` is minutes since the wave start, or a datetime when `--wave-start` is set. The job cutoff is that time minus the staging buffer (30 minutes unless changed).

```text
python chunker/chunk_loads.py --loads truck_loads.csv --travel travel_times.csv --max-job-weight 2500 --out outputs/chunk_example
```

Run it from `order_picking/`. `--pickers` can supply the weight cap from the heaviest picker when `--max-job-weight` is omitted. `--destination-grouping` keeps one destination together until that destination alone exceeds the cap.

Outputs:

| File | Role |
|------|------|
| `orders.csv` | Jobs in the picker's order shape, including `truck_id` and `depot_id` |
| `order_items.csv` | One row per input line |
| `jobs.csv` | Cutoff, priority, walking minutes, and whether the job is over the cap |
| `by_depot/<depot_id>/` | The same files for one dock |

`chunk_model.py` is the set-partitioning alternative. HiGHS picks one exact cover of each truck. A source order that fits a picker stays whole. Orders on the same dock may share a job, including orders for different stops. `--destination-grouping` keeps each job to one stop, the same flag as the savings command. A job must fit one picker on weight and volume together, and it may visit at most 16 pick locations. With the default line cap of 15, a job that passes the line cap already has at most 15 pick locations. A line or source order that fits no picker is listed in `rejected.csv` and is not split to force a job. The default time limit is 120 seconds per truck. A cover found before that limit is kept, and `solve.csv` records its relative MIP gap. A gap of 0 is a proved optimum. A truck with no cover yet uses the savings jobs instead of stopping the run, and that row has no gap. A must-ship line is covered once, and only by a job that was generated as priority-eligible. The default is any feasible job that contains the line. `--priority-max-lines` is the optional smaller cap.

```text
python chunker/chunk_model.py --loads inputs/chunk_warehouse/truck_loads.csv --travel inputs/layout_warehouse/travel_times.csv --pickers inputs/layout_warehouse/pickers.csv --out outputs/chunk_model
```

`jobs.csv` `walk_minutes` from this command is the Held-Karp tour. The savings command still writes the nearest-neighbor walk. The formulation is `formulation.md`. The cost of keeping a fitting order in one job is `order_whole_cost_memo.md`.

Point the picker at one depot folder:

```text
python source/order_picking_optimizer.py --orders outputs/chunk_example/by_depot/DEPOT/orders.csv --items outputs/chunk_example/by_depot/DEPOT/order_items.csv --pickers inputs/layout_example/pickers.csv --travel travel_times.csv --depot DEPOT
```
