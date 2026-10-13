# Order picking

Standalone pick-wave planner. It routes each order and assigns orders to pickers. It does not load trucks. One order is one trip: the picker returns to the depot before the next order.

| Folder | Contents |
|--------|----------|
| `inputs/` | Layout CSVs and the travel-time tables the solver reads |
| `outputs/` | Assignments and PNG exports |
| `source/` | Routing, assignment, and the travel-matrix builder |
| `chunker/` | Splits a truck's pick lines into jobs this solver can take. It does not read LoadMax |
| `visualizer/` | Drawing code, plus `warehouse.html` and `assignment.html` |
| `tests/` | Checks for the solver and the pictures |
| `scenarios/` | Assignment cases for `source/run_assignment_scenarios.py` |

`inputs/layout_example/` is the six-aisle warehouse with six orders. Pallet jacks are rated 2500 kg and volume 2.0. The forklift is rated 5000 kg and volume 4.0. `inputs/layout_wave/` is that same building with 12 pickers and 300 orders. `inputs/layout_small/` is the two-aisle picture. The math write-up is [docs/order_picking_model.html](../docs/order_picking_model.html).

Run the commands below from `order_picking/`.

## Solve the six orders

The default balance weight is 0. The orders can pile onto one pallet jack and leave the other idle. O4 is 3200 kg and names a forklift, so only P3 can take it. This writes `outputs/layout_example/assignments.csv`.

```text
python source/order_picking_optimizer.py --orders inputs/layout_example/orders.csv --items inputs/layout_example/order_items.csv --pickers inputs/layout_example/pickers.csv --travel inputs/layout_example/travel_times.csv --depot DEPOT --assignments-out outputs/layout_example/assignments.csv
```

`--share-shift` sets the balance weight to 0.001. One ordinary order is worth 1000 minutes of unevenness, so an order that still fits is not dropped to make the shift look even. On this sample all six orders stay picked and the score stays 100006. P1 takes O3 (98.5 min), P2 takes O2 and O5 (194 min), and P3 takes O1, O4, and O6 (212.5 min). The forklift also takes light orders when `required_equipment` is blank.

`--lateness-weight` defaults to 0. A positive value charges minutes past `cutoff_time` (minutes from the shift start) after each picker is sequenced by earliest deadline. A must-ship job's lateness tier is 100. An ordinary job's tier is 1. The release weight is unchanged. `--prefer-jack` defaults to 0 and, when set, penalises a jack-eligible job given to a forklift, in release-weight units. `--require-must-ship` forces those jobs to be released and can make the model infeasible. `--report-dir` writes `utilisation.csv`, `dropped.csv`, and `late_jobs.csv`. Each dock solve uses every picker in the file.

```text
python source/run_assignment_scenarios.py --orders inputs/layout_example/orders.csv --items inputs/layout_example/order_items.csv --pickers inputs/layout_example/pickers.csv --travel inputs/layout_example/travel_times.csv --depot DEPOT --scenarios scenarios/assignment.yaml --out outputs/scenarios_layout_example
```

The six-order file has blank `cutoff_time` values, so the deadline scenario does not change who is late. The chunker sample writes numeric cutoffs, and that is the file the deadline scenario is meant for.

```text
python source/order_picking_optimizer.py --orders inputs/layout_example/orders.csv --items inputs/layout_example/order_items.csv --pickers inputs/layout_example/pickers.csv --travel inputs/layout_example/travel_times.csv --depot DEPOT --share-shift --assignments-out outputs/layout_example/assignments_shared.csv
```

## Warehouse map and shift chart

`visualizer/warehouse.html` and `visualizer/assignment.html` were built from the shared file. Rebuild them from that same file. Pointing `--assignments` at `assignments.csv` draws the balance-0 plan instead, with P2 idle.

```text
python visualizer/plot_picking.py html --aisles inputs/layout_example/aisles.csv --cross-aisles inputs/layout_example/cross_aisles.csv --locations inputs/layout_example/locations.csv --orders inputs/layout_example/orders.csv --items inputs/layout_example/order_items.csv --pickers inputs/layout_example/pickers.csv --travel inputs/layout_example/travel_times.csv --assignments outputs/layout_example/assignments_shared.csv --depot-id DEPOT --depot-x 0 --depot-y -2 --out-dir visualizer --scenario-id layout_example_shared --label "Six-aisle sample, shared shift"
```

Open `visualizer/warehouse.html`. The Scenario dropdown switches the layout and the shift. On the map, scroll zooms toward the pointer, drag moves the floor, and Reset shows the whole building. Choosing an order frames that walk. The other page is `visualizer/assignment.html`, and it uses the same dropdown. A new solve is added by writing into that same folder with `--scenario-id` and `--label`. Weight and volume on the side panel are this trip against the picker's cap. Time is the whole shift.

## Full wave

```text
python source/build_wave.py --out inputs/layout_wave --orders 300
python source/order_picking_optimizer.py --orders inputs/layout_wave/orders.csv --items inputs/layout_wave/order_items.csv --pickers inputs/layout_wave/pickers.csv --travel inputs/layout_wave/travel_times.csv --depot DEPOT --share-shift --assignments-out outputs/layout_wave/assignments.csv
```

The crew is `inputs/crew.csv`: equipment type, how many, kilograms, volume, and shift minutes. The shipped rows use a 480 minute shift, eight pallet jacks at 2500 kg, and four forklifts at 5000 kg. Change `shift_duration` on those rows to change the shift. The order mix is `inputs/wave_settings.csv`: wave size, seed, pick time, lines per order, the share of heavy orders, and the priority mix. `--orders`, `--seed`, `--crew`, and `--settings` override those files. The first command copies the six-aisle geometry and writes the wave from those two files. Fifty orders are over the lightest machine and name a heavier one. With the shipped crew that machine is the 2500 kg jack, so those orders name a forklift. The six-order sample keeps its own shift in `inputs/layout_example/pickers.csv`. A solve finishes 59 of 300 with the default balance weight (score 1100090) and 60 of 300 with `--share-shift` (score about 1100092 before the small balance penalty). Every picker is already near a full 480 minutes, so sharing changes little. The shift clock on this building is what runs out. The saved wave file is the shared plan.

## Chunk a truck into picker jobs

`chunker/chunk_loads.py` reads a truck-load table and the same travel-time matrix the picker uses. It writes `orders.csv` and `order_items.csv`. `by_depot/<depot_id>/` is one folder per dock, because the picker still takes a single `--depot`. A blank `required_equipment` is any picker. `priority_weight` is written on each job, so the current picker uses that weight. The cutoff is minutes since the wave start. `--lateness-weight` reads that cutoff as minutes from the shift start. This command does not read a LoadMax solve. The equations are `chunker/formulation.md`. The cost of keeping a fitting order whole is `chunker/order_whole_cost_memo.md`.

```text
python chunker/build_truck_loads.py
python chunker/chunk_loads.py --loads inputs/chunk_warehouse/truck_loads.csv --travel inputs/layout_warehouse/travel_times.csv --max-job-weight 2500 --max-job-volume 2 --out outputs/chunk_warehouse
```

`chunker/chunk_model.py` solves the same split as a HiGHS set-partitioning cover. A source order that fits a picker stays whole. Orders on the same dock may share a job, including orders for different stops. `--destination-grouping` keeps each job to one stop. Pass the picker file. The time limit is 120 seconds per truck. Rows that fit no single picker are written to `rejected.csv`.

```text
python chunker/chunk_model.py --loads inputs/chunk_warehouse/truck_loads.csv --travel inputs/layout_warehouse/travel_times.csv --pickers inputs/layout_warehouse/pickers.csv --out outputs/chunk_model
```

## Tests

From the repo root:

```text
python -m pytest order_picking
```
