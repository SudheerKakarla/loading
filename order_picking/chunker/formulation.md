# Truck Load Chunker — Mathematical Formulation

The model is solved by HiGHS (`scipy.optimize.milp`) in `chunk_model.py`, in this folder. The Clarke-Wright chunker in `chunk_loads.py` stays the standalone heuristic. It also supplies candidates when `--destination-grouping` is on and a stop is too large to enumerate. The cost of keeping a fitting order whole is `order_whole_cost_memo.md`.

## 1. Sets and Indices

- $T$: trucks. One model is solved for each pair $(t, depot_t)$. A truck with two docks gets two models, because each tour starts and ends at its own dock.
- $L_t$: pick lines of that truck at that dock.
- $P$: pickers, each with equipment type and caps $(W_p, V_p)$.
- A bundle (one line, one source order, or one candidate) fits picker $p$ only when both limits hold on that same picker:

$$
w \le W_p \quad \text{and} \quad (v \text{ is absent, or } V_p \text{ is absent, or } v \le V_p)
$$

Comparing $w$ with $\max_p W_p$ and $v$ with $\max_p V_p$ separately is not used. A bundle under the heaviest weight and under the largest volume can still fit nobody.

- $L_t^{bad}$: lines that fit no picker. Removed before the model and reported with `exceeds_max_weight`, `exceeds_max_volume`, `exceeds_max_weight_and_volume`, or `no_single_picker`.
- $O_t^{bad}$: source orders whose remaining lines fit no single picker, even though each line does. Removed whole and reported with the same reasons. Those orders are not split into feasible pieces.
- $L_t^{ok}$: lines that remain. Only these enter the model.
- $K_t$: candidate jobs. A source order that fits one picker, the line cap, and 16 pick locations is kept whole. By default, orders at one dock may share a job even when their stops differ. The pool is every union of those orders that still fits the line cap, the location cap, and one picker. An extension that already fails is not expanded. That pool does not switch to Clarke-Wright columns.
  - `--destination-grouping` keeps each candidate inside one stop. The pool is then every union of those whole orders when the stop has at most `max_enumerate_lines` orders (default 10), and otherwise each order alone plus the Clarke-Wright merges of those orders. Pairs and triples are not listed.
- $K_t^{priority}(l)$: candidates that contain must-ship line $l$ and were allowed at generation. With the default, that is every generated candidate containing $l$. An optional `priority_max_lines` drops larger must-ship jobs before they enter $K_t$. There is no earliness ranking.

A candidate that contains a must-ship line and fails the priority rule is not generated. Clarke-Wright jobs that contain a must-ship line stay in the pool and are marked priority-eligible, unless `priority_max_lines` excludes them. The "at least as good as Clarke-Wright" guarantee needs those jobs present.

## 2. Parameters

- $w_l, v_l, loc_l, stop_l$: line weight, volume, pick location, and destination. $v_l$ may be absent only when `--ignore-volume` is set or no picker has a volume cap. Otherwise the run stops and names that flag.
- $order_l$: source order. A fitting order is not split. Orders at one dock may share a job across stops. `--destination-grouping` limits a job to one stop. A file with no `source_order_id` column treats every line as its own order.
- $ms_l \in \{0,1\}$: must-ship flag.
- $a_{lk} \in \{0,1\}$: line $l$ is in candidate $k$.
- $U_k$: distinct pick locations in candidate $k$. A shared bin is visited once.
- $\tau(i,j)$: directed travel time. The same matrix the picker uses.
- $\mathrm{HK}(depot, U_k)$: shortest closed directed tour from the dock through $U_k$. Held-Karp allows 16 pick locations plus the dock. A candidate with more pick locations is not generated.
- $\theta$: overhead per selected job, default 5 minutes.
- $\pi_l$: pick time of line $l$, default 0.5 minutes. Under an exact cover the sum of $\pi_l$ is the same for every feasible solution, so pick time does not change which cover is chosen. It is still in $c_k$ so the objective matches a picker duration.
- $c_k = \mathrm{HK}(depot, U_k) + \theta + \sum_l \pi_l a_{lk}$.
- $m_t$: optional cap on the number of selected jobs.

Caps are not constraints inside the MIP. A candidate that does not fit a picker is never created. A singleton of every atom is created. An atom is a whole source order when that order fits, and one line otherwise. A job only a forklift can carry (5000 kg and 4.0 m³ on the shipped crew) sets `required_equipment` to `forklift`. A job a pallet jack can carry leaves `required_equipment` blank, because the live picker treats a blank as any machine and then applies weight and volume.

## 3. Decision Variables

$$
z_k \in \{0,1\} \qquad \forall k \in K_t
$$

## 4. Objective

$$
\min \sum_{k \in K_t} c_k z_k
$$

## 5. Constraints

**C1 — Exact cover, every line.** No exception for must-ship lines:

$$
\sum_{k \in K_t} a_{lk} z_k = 1 \qquad \forall l \in L_t^{ok}
$$

**C2 — Priority pool, in addition to C1.** For a must-ship line, the selected job is in the priority pool:

$$
\sum_{k \in K_t^{priority}(l)} a_{lk} z_k = 1 \qquad \forall l \in L_t^{ok} \text{ with } ms_l = 1
$$

C2 does not replace C1. If a non-priority candidate also contains the line, C1 counts it and C2 does not, so that candidate's $z_k$ is forced to 0. Generation already refuses to build that candidate. Both layers stay: generation keeps the pool clean, and the two equalities still reject a poisoned pool. When every candidate that contains the line is priority-eligible, the two rows are the same and the solver stores one of them.

**C3 — Stops may share a job, by default.** Not an inequality. `--destination-grouping` keeps a candidate inside one stop. An order that fits a picker is not split across candidates in either mode.

**C4 — Optional job-count cap.** $\sum_k z_k \le m_t$ when $m_t$ is set.

## 6. Complete Model

For each truck and dock:

$$
\begin{aligned}
\min \quad & \sum_{k \in K_t} c_k z_k \\
\text{s.t.} \quad
& \sum_{k \in K_t} a_{lk} z_k = 1 && \forall l \in L_t^{ok} \\
& \sum_{k \in K_t^{priority}(l)} a_{lk} z_k = 1 && \forall l \in L_t^{ok},\ ms_l = 1 \\
& \sum_{k \in K_t} z_k \le m_t && \text{(optional)} \\
& z_k \in \{0,1\} && \forall k \in K_t
\end{aligned}
$$

## 7. Notes

**Priority-eligible, default.** Any generated job that contains the must-ship line. No line-count cap and no earliest-dispatch ranking unless `priority_max_lines` is set. Must-ship lines therefore stay with the other lines of their source order when that job fits a picker. The job's `priority_weight` is still the unscaled must-ship weight (100000). That weight is not in this objective.

**Pre-filtering.** $L_t^{bad}$ and $O_t^{bad}$ are reported by id and reason. They do not make HiGHS return infeasible. A line inside an order that is $O_t^{bad}$ is not also listed as a bad line.

**Pool quality.** By default every feasible union of whole orders at the dock is present, so the cover is optimal for this objective inside the whole-order rule, still subject to the line cap and the location cap. That can still walk more than the line-level savings chunker, which may split an order. With `--destination-grouping`, a stop small enough to enumerate has every feasible union of its whole orders, and a larger stop is optimal only inside the generated pool. Clarke-Wright candidates are capped by one picker, the one with the highest weight, using that picker's own volume. On `inputs/chunk_warehouse/truck_loads.csv` (2,247 lines, 2026-10-13) the default cover is 171 jobs and 2,975.885 minutes. One stop per job is 195 jobs and 3,204.623 minutes. Line-level savings on those lines is 168 jobs and 2,371.931 minutes.

**Time limit.** The default is 120 seconds per truck and dock. If HiGHS returns a cover, that cover is kept even when the limit stopped the proof, and `solve.csv` records the relative gap. A gap of 0 means the cover is proved optimal. If it returns no cover, that truck uses the savings chunker's jobs instead of aborting the run, and that row has no gap. The limit applies to HiGHS after the candidate tours are priced. Pricing the tours is outside that clock.

**Tour versus pick time.** Held-Karp sees distinct locations. Pick time is charged per line.

**Ties.** Two covers with the same cost are both optimal. HiGHS may return either.

**Scale.** The default pool keeps a union only while the line cap, the location cap, and one picker still fit. It does not list $2^{n}$. `--destination-grouping` enumerates a stop of at most 10 orders ($2^{10}$ unions) and does not list pairs and triples above that. Held-Karp allows 16 pick locations plus the dock. A line has one pick location, so a job that passes a line cap of 15 has at most 15 distinct pick locations. At that line cap the 16-location check removes no further candidate. The unit tests use a handful of lines. A full truck from `inputs/chunk_warehouse/truck_loads.csv` is a separate run, not part of those tests.
