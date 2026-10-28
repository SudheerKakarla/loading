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

`inputs/layout_example/` is the six-aisle warehouse with six orders. Pallet jacks are rated 2500 kg and volume 2.0. The forklift is rated 5000 kg and volume 4.0. `inputs/layout_wave/` is that same building with 12 pickers and 300 orders. `inputs/layout_small/` is the two-aisle picture. The math write-up is [docs/order_picking_model.html](../docs/order_picking_model.html). Closed tries, so the same measurement is not run again, are [TRIED.md](TRIED.md).

Run the commands below from `order_picking/`.

## Solve the six orders

The default balance weight is 0. The orders can pile onto one pallet jack and leave the other idle. O4 is 3200 kg and names a forklift, so only P3 can take it. This writes `outputs/layout_example/assignments.csv`.

```text
python source/order_picking_optimizer.py --orders inputs/layout_example/orders.csv --items inputs/layout_example/order_items.csv --pickers inputs/layout_example/pickers.csv --travel inputs/layout_example/travel_times.csv --depot DEPOT --assignments-out outputs/layout_example/assignments.csv
```

`--share-shift` sets the balance weight to 0.001. One ordinary order is worth 1000 minutes of unevenness, so an order that still fits is not dropped to make the shift look even. On this sample all six orders stay picked and the score stays 100006. P1 takes O3 (98.5 min), P2 takes O2 and O5 (194 min), and P3 takes O1, O4, and O6 (212.5 min). The forklift also takes light orders when `required_equipment` is blank.

Optional `allowed_modes` on a picker is a free list of lowercase names separated by semicolons. A blank cell allows every mode. Optional `pick_mode` on a job is one of those names. A blank job can go to any picker. A named mode can go only to a picker whose list is blank or includes that mode. `required_equipment` is still the machine name.

Optional `home_depot` names the dock that picker works. A blank cell, and a file with no such column, leaves the picker eligible at every dock. Optional `non_pick_minutes` is subtracted once from `shift_duration`. A blank cell leaves the full shift. The assignment and the dock screen both use that remainder. `python source/task_sheet.py` writes one row per item line from an assignment file. Stops follow `order_items.csv`. The sheet is for a person to read. It does not solve, and it is not an import file for another system.

`--lateness-weight` defaults to 0. A positive value charges minutes past `cutoff_time` (minutes from the shift start) after each picker is sequenced by earliest deadline. A must-ship job's lateness tier is 100. An ordinary job's tier is 1. The release weight is unchanged. `--prefer-jack` defaults to 0 and, when set, penalises a jack-eligible job given to a forklift, in release-weight units. `--require-must-ship` forces those jobs to be released and can make the model infeasible. `--report-dir` writes `utilisation.csv`, `dropped.csv`, and `late_jobs.csv`. Each dock solve uses every picker in the file.

The default objective is still that one weighted sum. The solve stops at an absolute tolerance of a tenth of the smallest positive job weight, raised above solver noise when that still sits below half a job. A caller relative gap is ignored. HiGHS receives that tolerance as `mip_rel_gap = absolute / objective_upper`, because SciPy has no absolute-gap option. `--staged-assignment` releases must-ship jobs by count, then other jobs by weight, then minimises lateness. `--withhold-if-late-at-start` leaves out a job that is already too long for every eligible picker and reports `cannot_meet_cutoff`. `python source/run_docks.py` solves each depot folder and then raises if one picker's summed busy minutes exceed the shift left after `non_pick_minutes`. A named `home_depot` keeps that picker off the other docks. A pass is that minute total. Clock overlap and the walk between docks stay outside the screen. `--staging-capacity` or `--staging-file` (`depot_id,capacity_pallets`) writes `staging.csv` in each dock folder and prints one line per dock. `--fail-on-staging-overflow` raises after those files are written. The solve is unchanged. Without a `pallets` column each finished job counts as one pallet. `python source/assignment_reports.py <utilisation.csv> <utilisation.csv>` checks utilisation files that were already written. `python source/downstream_check.py --schedule schedule.csv --lead-times lead_times.csv --out outputs/downstream.csv` queues each step after the pick finish. Departure is the deadline plus the staging buffer. The report names the first job that misses departure at each step. That queue is a report. The savings chunker raises when a volume cap is set and a line has no volume. A cutoff of 30 on a chunker job is 30 minutes.

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

The crew is `inputs/crew.csv`: equipment type, how many, kilograms, volume, and shift minutes. The shipped rows use a 480 minute shift, eight pallet jacks at 2500 kg, and four forklifts at 5000 kg. Change `shift_duration` on those rows to change the shift. Optional crew columns `allowed_modes`, `home_depot`, and `non_pick_minutes` are copied onto each picker when the header is present. `allowed_modes` is a free name, or several names separated by semicolons. The order mix is `inputs/wave_settings.csv`: wave size, seed, pick time, lines per order, the share of heavy orders, and the priority mix. `--orders`, `--seed`, `--crew`, and `--settings` override those files. The wave files have no `pick_mode` column. `inputs/mode_profile.example.csv` is an illustration for the chunker, not a site measurement. The first command copies the six-aisle geometry and writes the wave from those two files. Fifty orders are over the lightest machine and name a heavier one. With the shipped crew that machine is the 2500 kg jack, so those orders name a forklift. The six-order sample keeps its own shift in `inputs/layout_example/pickers.csv`. A solve finishes 59 of 300 with the default balance weight (score 1100090) and 60 of 300 with `--share-shift` (score about 1100092 before the small balance penalty). Every picker is already near a full 480 minutes, so sharing changes little. The shift clock on this building is what runs out. The saved wave file is the shared plan.

## Chunk a truck into picker jobs

`chunker/chunk_loads.py` reads a truck-load table and the same travel-time matrix the picker uses. It writes `orders.csv` and `order_items.csv`. `by_depot/<depot_id>/` is one folder per dock, because the picker still takes a single `--depot`. A blank `required_equipment` is any picker. `priority_weight` is written on each job, so the current picker uses that weight. The cutoff is minutes since the wave start. `--lateness-weight` reads that cutoff as minutes from the shift start. This command does not read a LoadMax solve. The equations are `chunker/formulation.md`. The cost of keeping a fitting order whole is `chunker/order_whole_cost_memo.md`.

Optional `pick_mode` on a load line is a free lowercase name. A blank cell has no mode. Two different named modes never share a job. A blank line may join either mode. A mixed-mode source order is split by mode, because those lines are different tasks. When any line has a mode, `orders.csv`, `order_items.csv`, and `jobs.csv` include `pick_mode`. The cover reads optional `allowed_modes` on the picker file. `no_picker_for_mode` is used when the line fits a picker on weight and volume and no such picker allows the mode. `--mode-profile`, `--wave-cutoff`, and `--lead-times` are optional on both chunkers. The column lists stay as they are when those flags are omitted. Lines of one source order stay in one group. The chunker README has the profile table, the group key, the import map, and the stock rule.

```text
python chunker/build_truck_loads.py
python chunker/chunk_loads.py --loads inputs/chunk_warehouse/truck_loads.csv --travel inputs/layout_warehouse/travel_times.csv --max-job-weight 2500 --max-job-volume 2 --out outputs/chunk_warehouse
```

`chunker/chunk_model.py` solves the same split as a HiGHS set-partitioning cover. A source order that fits a picker stays whole. Orders on the same dock may share a job, including orders for different stops. `--destination-grouping` keeps each job to one stop. Pass the picker file. The time limit is 120 seconds per truck. Rows that fit no single picker are written to `rejected.csv`.

```text
python chunker/chunk_model.py --loads inputs/chunk_warehouse/truck_loads.csv --travel inputs/layout_warehouse/travel_times.csv --pickers inputs/layout_warehouse/pickers.csv --out outputs/chunk_model
```

## Calibration and a site folder

`python source/calibrate_from_task_log.py --log task_log.csv --column-map column_map.csv --out outputs/calibration` reads a caller-mapped log. It writes `mode_profile.csv` and `calibration_report.md`. A mode with fewer than `--min-rows` accepted rows (default 5) keeps its pick time and overhead blank. The report is not a default. No task log is in the repository, so the illustrative profile is unchanged.

`inputs/site_template/` is the file shape: layout, `location_type`, crew, staging, lead times, a mode profile, and two pick lines. It is not the Thai site. `inputs/site_th/` is reserved for the extract. `python source/validate_site.py --site inputs/site_template --out outputs/site_template/travel_times.csv` builds the travel matrix and checks every pick location. `--speed` defaults to 1 distance-unit per minute. `location_type` must be `bulk` or `pickface` when the column is present. The travel builder ignores it.

## Tests

From the repo root:

```text
python -m pytest order_picking
```
