# Not adopted from the Arvato / Gurobi webinar

The slides are in this folder. Allocation decides which stock unit supplies a line. Batching groups orders by distance between modules. Both sit upstream of this picker. The chunker and Phase 1 cost and sequence the closed walk. The published Arvato gains (full-pallet rate, broken-case picks, units per operator-hour, travel distance) are their results. They are not a check on this model.

Staged release in this project came from our own cases: T8, and five PACK_B jobs dropped under a relative gap. The slides make the same point, that a single weighted objective needs careful tuning. Cite that as corroboration. It is not the origin of `--staged-assignment`.

| Left out | Why | Reconsider when |
|---|---|---|
| Allocation (full pallet, then full case, then each) | A line already has one storage location. | One SKU occupies more than one pallet or case. |
| Module-distance batching, and the pairwise linearization that goes with it | The chunker costs the closed walk. Phase 1 sequences that walk. Module distance would drop the visit order. | An upstream stage is added that batches customer orders before the chunker. Do not replace the walk cost with it. |
| Grouping slots into modules | Travel is slot to slot on the aisle graph. | The warehouse data is module-level and slots are no longer the stops. |
| Tighter Big-M constants beyond the shift | M is the longest shift, adjusted when a deadline is negative. The 87-job assignment proved in about 1.5 s. | A solve is slow or unstable because of M. |
| A joint model of both docks | The choice between one roaming crew and a crew per dock is open. `run_docks.py` sums busy minutes. That sum caught PJ04 at 506.5 and PJ08 at 504.7. | That crew policy is chosen. Overlap and the 1.1-minute walk stay outside the minute sum until then. |
| Draft v0.2 `deadline_penalty_weight` and hard-deadline mode | Lateness is minutes past cutoff under earliest-deadline order. | The business asks for one penalty coefficient, or for a cutoff that makes the model infeasible. |

The correction of draft v0.2 is `../source/planning_optimizer_spec_v0.3.md`.
