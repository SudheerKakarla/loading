# Closed tries

Read this before changing the chunker or the assignment objective. Each row was already measured, or explicitly set aside, on the 16-truck sample `inputs/chunk_warehouse/truck_loads.csv`. The fair cover is the 2,247 lines both chunkers keep. Repeat a row only when the input file or the business rule changes.

The live chunker result stays 171 jobs and 2,975.885 minutes, every truck gap 0, no fallback. After the Held-Karp distance table the full wave took 474.75 s. Assignment busy minutes are Phase 1 walk plus pick time. They do not include the chunker's 5 minutes per job, and they are not that 2,975.885.

## Pending

The release rule is now `--staged-assignment` (default off). The rows below are still not built. They were recorded on 2026-10-15 so they are not treated as done. On 2026-10-22 the order of attention is the heaviest-picker cap, then loading order, dock staging space, and aisle congestion.

| Pending | What is missing | Note |
|---|---|---|
| Heaviest-picker chunker cap | The job cap is the heaviest picker, so a job can be larger than a jack and the jacks can sit idle. | Open, and first among these rows. Switching the cap to the lightest picker stays closed: weight and volume are judged together. |
| Loading order | A picker's trips follow earliest deadline. They do not follow the order the truck is loaded, back of the truck first. | No load-sequence field is in the model. |
| Dock staging space | Nothing caps how many pallets can wait at a dock before loading. | `staging_buffer` is only the 30 minutes subtracted from departure to make the cutoff. It is not a space limit. |
| Picker congestion | Two pickers in the same aisle do not slow each other down. | Usually minor at 12 pickers. Left until the release bug and the loading order are done. |
| Replenishment | Nothing checks that the location still has stock when the picker arrives. | `wh_inventory.csv` is not read by the solver. |
| Mid-shift changes | A new or cancelled order does not trigger a re-plan. | This is the runtime question. Numba and process-level truck pricing stay closed until a re-plan during the shift is actually required. |
| Splitting large orders | The 604-minute whole-order cost is measured. Splitting only the large orders is not. | Waits on the rule owner. Note: `chunker/order_whole_cost_memo.md`. |
| Preferring jacks | Measured 2026-10-21. The default stays 0. | On the staged path any positive value minimises the count. The returned plan reaches 0 and then books PJ04 and PJ08 past 480 across the two docks. See the assignment row. |
| One crew for both docks | Each dock solve still sees the full picker file, so the 12 people are counted twice. The minute sum is a screen. It raised on the prefer-jack plan: PJ04 506.5 and PJ08 504.7 of 480. A total under 480 can still overlap on the clock, and the 1.1 min walk is outside the sum. | Open decision. `run_docks.py` does not assign a home dock. Combined busy time on the prefer-0 staged wave is about 37% of one crew. |

## Chunker

| Try | Result | Leave it |
|---|---|---|
| Beat 172 jobs on all 2,295 lines | 48 of those lines are six orders no picker can carry | Compare on the 2,247 lines both sides keep |
| Make one stop per job the default | 195 jobs, 3,204.623 min, about 229 min and 24 jobs more than mixing stops | `--destination-grouping` is the switch. `--mix-stops` is gone |
| Raise the 16-location cap without raising the 15-line cap | A 15-line job already has at most 15 pick locations, so the check removes nothing | Keep both caps |
| Drop candidates whose cheapest outgoing leg cannot win | Dropped no merged job on truck 1 (1,301 candidates) | The bound is not in the code |
| Price tours with a NumPy distance table | Slower than the Python dictionary | The live table is a plain Python list |
| Cut the 120 s HiGHS limit, including a 60 s cap on trucks 1 and 2 | HiGHS took about 0.06 s and still proved the same covers. The wall clock is tour pricing | Keep 120 s. It applies only after pricing |
| Compile Held-Karp with Numba | Not timed. A once-per-shift run does not need it | Add it only if the wave must be replanned during the shift |
| Price the 16 trucks in separate processes | Not built. It would make the file about as long as the slowest truck | Same condition as Numba |
| Put the 16-truck solve in pytest | A full wave is minutes | Tests stay on a handful of lines |
| Split orders, or split only the large ones, to close the 604 min | 604 is the floor of keeping orders whole against one savings split (168 jobs, 2,371.931 min). Same-rules Clarke-Wright, which also keeps orders whole, is 187 jobs and 3,164.469 min. The exact cover beats that by about 189 min | Wait for the rule owner |
| Treat savings `walk_minutes` as the 2,371.931 figure | That column is the nearest-neighbor tour. The comparison minutes are Held-Karp | Do not add the two columns together |
| Use jobs under 30% of the forklift cap as evidence of waste | Orders are a few hundred kilograms, so most jobs are light on every cover | Fill share is not a separate finding |
| Publish a `min_possible_jobs` column as an optimality gap | It is a packing bound. Matching it does not make the walk optimal | Do not add the column |
| Feed `inputs/layout_warehouse` through the chunker | Those orders are already one trip each, and the line weights were rolled up and dropped | Solve that folder with the picker directly |
| Convert chunker cutoffs from minutes to hours | The chunker clock is minutes since the wave start (departure minus 30) | Leave the stored minutes alone |
| Cluster must-ship lines, or cap their jobs, by default | The default pool is any generated job that contains the line | `--priority-max-lines` stays optional |
| Merge `priority_weight` with LoadMax `container_priority` or `skuPriorityIncentive` | The two systems score different decisions | Keep the fields apart. No LoadMax wrapper |

## Assignment

| Try | Result | Leave it |
|---|---|---|
| Add a new drop variable `z` | Release is already `y`. The public reasons `no_eligible_picker` and `shift_time_limit_or_priority_tradeoff` are what the tests compare | Finer detail is `equipment`, `capacity`, `equipment_and_capacity`, `shift_capacity`, or `priority_tradeoff` |
| Use the stored 100000 as the lateness weight | That number is the release weight of a must-ship job | Lateness tier is 100 for must-ship and 1 otherwise |
| Set `--lateness-weight` to 0 and call the late report the plan | Weight 0 ignores deadlines. On this file that report is 9 jobs / 213 min at PACK_A and 28 jobs / 1,146 min at PACK_B, with nothing dropped | The report is a check. It is not the objective |
| Lower the lateness weight under about 4.8 so ordinary jobs survive | An ordinary job on this file is worth about 5. Any single weight still drops the next job once its lateness costs more than that | Use `--staged-assignment`. The weighted path still drops a job when its lateness costs more than its weight. |
| Force every feasible `y = 1` in one solve | `--require-must-ship` already does this for must-ship jobs. An infeasible model returns an empty plan | `--staged-assignment` maximises how many must-ship jobs fit, then the weight of the other jobs, then minimises lateness. It locks the objective value, not the job set. |
| Read the five PACK_B drops at lateness weight 1 as a full shift | `TRK02-J02`, `TRK14-J09`, `TRK14-J15`, `TRK16-J01`, and `TRK16-J03` each take 13–15 min, fit a jack, and finish on time alone. Five jacks were idle. Together they are worth about 28 points, inside HiGHS's default 0.01% gap on an objective near 700,000 | Do not re-chunk, and do not buy pickers, from those five rows |
| Rerun those five after the tenth-of-a-job stop | 2026-10-21, lateness weight 1, 60 s per stage, folders `outputs/chunk_assign/gap_wave/{weighted,staged}/`. Both paths released all 84 PACK_A jobs and all 87 PACK_B jobs, including the five above. Late minutes were 0. Every stage stopped `proven` with achieved absolute gap 0. Weighted PACK_B took 1.47 s (absolute stop 1.6, upper bound 1,074,814). Staged PACK_B ordinary took 0.02 s (absolute stop 0.32, upper bound 414). The shared-crew check passed | The old gap was the cause. The weighted path can still drop a job when lateness costs more than its weight |
| `--prefer-jack` 0.1 after staged release, on the 16-truck file | 2026-10-21, lateness weight 1, 60 s, folder `outputs/chunk_assign/gap_wave/staged_prefer_jack/`. The staged jack stage minimises the count with coefficient 1, so 0.1 only turns that stage on. PACK_A stayed 84/84, score 400424.4. PACK_B stayed 87/87, score 700414. Late minutes stayed 0. Every stage stopped `proven`, jack incumbent 0 in about 0.14 s (uppers 288 and 300). Twelve forklift-only jobs stayed on forklifts, 152.9 min and 144.8 min. A prefer-0 solve of the same model left 25 and 26 jack-eligible jobs on forklifts. That split differs from the saved `gap_wave/staged` minutes, because those stages do not choose the machine. Each dock stayed inside 480. Added together, PJ04 is 506.5 and PJ08 is 504.7, so the shared-shift check raises | Leave the default at 0. The returned plan uses each dock's full crew and piles jack work onto two people |
| Give each dock a home crew, or charge the walk between docks | The docks are 72 m apart, about 1.1 min at speed 65. Each solve still sees all 12 pickers (5,760 min). Combined busy time is 2,144.5 min, about 37% of one crew | Dock-switch rules are not built |
| Re-chunk long jobs because the default plan is late | With lateness weight 1, PACK_A released all 84 jobs and was 0 min late. The default late minutes were an assignment that ignored deadlines | A re-chunk loop is not built |
| Treat PACK_B forklifts at 33.5% against PACK_A at 17% as a forklift shortage | Both docks have 12 forklift-only jobs, about 151 min on PACK_A and 142 min on PACK_B, out of 1,920 forklift minutes. Weight 0 does not care which machine takes a jack-eligible job | The 2026-10-21 prefer-jack row. The jack stage can move the eligible jobs off the forklifts. The returned plan then fails the shared shift |
| Cut the crew until utilisation matches 37% | Removing two jacks dropped one job on each dock and pushed late minutes over 1,100, while utilisation only rose to about 22% | The scenario runner is the test, and lateness counts |
| Reorder `picker_assignments` into deadline order | Tests require solver row order. The late report resequences | Leave the assignment list alone |
| Put `+big_m` on the assignment variable in the HiGHS completion row | That row does not force the finish time when the job is assigned, so both jobs can share a picker and still look optimal | The coefficient is `-big_m`, then minus each earlier duration |
| Let a caller relative gap stop Phase 2 | Draft v0.2 listed `mip_gap` 0.01 (1%). HiGHS's own 0.01% relative default was enough to drop five PACK_B jobs worth about 28 points | The stop is a tenth of the smallest positive weight, raised above about `1e-6` times the objective upper bound when that still sits below half a job. SciPy accepts `mip_rel_gap` only, so HiGHS sets that relative gap from the tolerance divided by the upper bound. Gurobi gets `MIPGap` 0 and `MIPGapAbs` equal to the tolerance. The caller value is `gap_requested`. |
| Pass `mip_abs_gap` through `scipy.optimize.milp` | SciPy 1.18 warns and does not treat it as a stopping option | Encode the absolute slack as `mip_rel_gap`. |
| Read the five PACK_B drops as the staged solve already being on | The weighted path is still the default. Staged mode is `--staged-assignment` | Toy checks: a 5% caller gap still releases one must-ship job plus 20 jobs of weight 2; T8b drops one job on the weighted path and keeps both, with 4 late minutes, on the staged path. |
| Withhold a job by comparing its duration with the raw cutoff | A picker who starts later has a shorter window before the cutoff | `--withhold-if-late-at-start` uses `A_r(T)`. Default off. Reason `cannot_meet_cutoff`. |
| Build a joint dock model because one person might exceed 480 | On the saved prefer-0 baseline nobody did. FK02 was 478.8. The prefer-jack plan is the case the minute sum raises: PJ04 506.5 and PJ08 504.7 | `run_docks.py` sums busy minutes against `shift_duration` (tolerance 0.0001) and does not re-solve. A passing total still leaves clock overlap and the 1.1 min walk outside the screen. The crew policy is still open |
| Change the savings cap to the lightest picker | Weight and volume are judged together on one person | The cap stays the heaviest picker. |
| Let a savings merge continue when `max_job_volume` is set and a line has no volume | The merge was refused and the run continued | `chunk_loads` raises and names the line. |

## Also left out

Numba, process-level truck parallelism, a line-level exact MIP, PyYAML, and a second drop variable. `order_picking/usethis/` is not in the tree. `loadmax/future_addons/` stays off this path.
