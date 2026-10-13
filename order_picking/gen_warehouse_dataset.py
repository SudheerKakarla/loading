"""
Realistic warehouse dataset generator for the Order Picking Planning
Optimizer -- INPUT FILES ONLY.

This script intentionally does NOT compute a travel-time matrix. Per
instruction, ``build_travel_matrix.py`` is run separately by the user
against the ``aisles.csv`` / ``cross_aisles.csv`` / ``locations.csv`` files
produced here. This script only builds, in ``warehouse_dataset/``:

    aisles.csv, cross_aisles.csv, locations.csv   -> feed build_travel_matrix.py
    skus.csv, inventory.csv                       -> informational master data
    orders.csv, order_items.csv, pickers.csv       -> feed order_picking_optimizer.py

Every column name below was taken directly from reading the actual
functions that consume it, not assumed:

  build_travel_matrix.load_layout() requires:
    aisles:       aisle_id, x, y_min, y_max, (optional) one_way in {"", up, down}
    cross_aisles: cross_id, y, x_min, x_max, (optional) one_way in {"", right, left}
    locations:    location_id, aisle_id, y
    (depot is a CLI/arg triple depot_id/depot_x/depot_y, never a CSV row)

  order_picking_optimizer._eligible() / solve_order_assignment() require:
    orders:  order_id, total_weight, (optional) total_volume,
             (optional) priority_weight, cutoff_time, account_tier,
             wait_time, must_ship, required_equipment
    pickers: picker_id, equipment_type, max_weight, (optional) max_volume,
             shift_duration

  order_picking_optimizer.route_orders() requires:
    order_items: order_id, location_id, (optional) pick_time
    (item_id is accepted but never read; kept only for traceability)

Three mistakes found by actually running the output through the real code
are fixed here (not just by inspection):

1. SKU/slot data-loss bug: an earlier version wrote inventory.csv from a
   slot->sku dict, which silently drops every SKU that collides onto an
   already-used slot. This version writes inventory.csv directly from the
   sku->slot mapping (one row per SKU, always lossless); a location_id may
   legitimately appear on more than one row, which models shared small-parts
   bins for low-velocity SKUs, a normal practice, not a defect.

2. Column-name mismatch: an earlier version used total_weight_kg /
   total_volume_m3 / equipment on orders.csv and pickers.csv. The actual
   code reads total_weight / total_volume / equipment_type. This version
   uses the exact attribute names the code reads.

3. Unrealistic per-unit volume: an earlier version drew unit_volume_m3 from
   0.01-0.50 m3 for regular SKUs, which, over 3-12 lines, produced order
   volumes (median ~7.3 m3) that exceeded even the forklift's 4.0 m3 cart
   capacity for 81% of orders -- found only by running solve_order_assignment
   and seeing almost every order rejected as "no_eligible_picker" regardless
   of weight. Regular SKU unit volume is now 1-30 litres (0.001-0.03 m3),
   consistent with small-parts piece-pick goods; bulk SKU unit volume is
   0.05-0.15 m3 (a bulkier case/carton), so a handful of heavy orders can
   still occasionally be volume-constrained too, which is realistic, but it
   is no longer the dominant outcome for the dataset as a whole.

Additionally, a deliberate "heavy" fraction of orders is built from a
small bulk-item pool so that per-order weight actually exceeds a pallet
jack's capacity, which is what makes forklift-only eligibility (C4) a real,
exercised branch rather than untested dead code.
"""

import csv
import math
import random
from pathlib import Path

SEED = 20261010
rng = random.Random(SEED)

OUT = Path("warehouse_dataset")
OUT.mkdir(parents=True, exist_ok=True)

# ── Layout constants (confirmed-good physical dimensions from the prior
#    review; unchanged here) ────────────────────────────────────────────
N_AISLES = 8
AISLE_SPACING = 8.0
AISLE_X_START = 8.0
N_BAYS = 30
BAY_PITCH = 2.0
BAY_Y_START = 2.0
Y_MIN, Y_MAX = 0.0, 62.0
X_MIN, X_MAX = 0.0, 72.0
PACK_A = ("PACK_A", 0.0, 0.0)
PACK_B = ("PACK_B", 72.0, 0.0)

N_SKU_A, N_SKU_B, N_SKU_C, N_SKU_BULK = 100, 150, 230, 20
N_SKU = N_SKU_A + N_SKU_B + N_SKU_C + N_SKU_BULK  # 500

N_ORDERS = 300
N_PICKERS_JACK = 8
N_PICKERS_FORK = 4
SHIFT_MIN = 480.0
CAP_JACK_KG = 2500.0
CAP_FORK_KG = 5000.0
CAP_JACK_VOL = 2.0
CAP_FORK_VOL = 4.0
PICK_TIME_PER_LINE = 0.5
HEAVY_SHARE = 0.10
MUST_SHIP_SHARE = 0.05
KEY_TIER_SHARE = 0.20


def write_csv(path, rows, fieldnames):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
    print(f"  wrote {path.name}  ({len(rows)} rows)")


# ══════════════════════════════════════════════════════════════════════
# 1. AISLES  -- column names: aisle_id, x, y_min, y_max, one_way
# ══════════════════════════════════════════════════════════════════════
aisle_ids = [f"A{i+1}" for i in range(N_AISLES)]
aisle_x = {aid: AISLE_X_START + i * AISLE_SPACING for i, aid in enumerate(aisle_ids)}
aisles = []
for aid in aisle_ids:
    one_way = "up" if aid == "A3" else ""  # one real one-way aisle for coverage
    aisles.append(
        {"aisle_id": aid, "x": aisle_x[aid], "y_min": Y_MIN, "y_max": Y_MAX, "one_way": one_way}
    )
write_csv(OUT / "aisles.csv", aisles, ["aisle_id", "x", "y_min", "y_max", "one_way"])

# ══════════════════════════════════════════════════════════════════════
# 2. CROSS-AISLES -- column names: cross_id, y, x_min, x_max, one_way
# ══════════════════════════════════════════════════════════════════════
cross_aisles = [
    {"cross_id": "CA_FRONT", "y": Y_MIN, "x_min": X_MIN, "x_max": X_MAX, "one_way": ""},
    {"cross_id": "CA_BACK", "y": Y_MAX, "x_min": X_MIN, "x_max": X_MAX, "one_way": ""},
]
write_csv(OUT / "cross_aisles.csv", cross_aisles, ["cross_id", "y", "x_min", "x_max", "one_way"])

# ══════════════════════════════════════════════════════════════════════
# 3. LOCATIONS -- column names: location_id, aisle_id, y  (+ informational abc_class)
#    Left/right racks at a bay share one centreline point (stated layout
#    assumption); each still gets its own location_id.
# ══════════════════════════════════════════════════════════════════════
locations = []
loc_by_class = {"A": [], "B": [], "C": []}
for aid in aisle_ids:
    for bay in range(1, N_BAYS + 1):
        y = BAY_Y_START + (bay - 1) * BAY_PITCH
        abc = "A" if bay <= 10 else ("B" if bay <= 20 else "C")
        for side in ("L", "R"):
            loc_id = f"L-{aid}-{bay:02d}-{side}"
            locations.append({"location_id": loc_id, "aisle_id": aid, "y": y, "abc_class": abc})
            loc_by_class[abc].append(loc_id)

# 8 dedicated bulk-item floor positions, one per aisle, near the back cross-aisle.
bulk_locations = []
for aid in aisle_ids:
    loc_id = f"L-{aid}-BULK"
    bulk_locations.append(loc_id)
    locations.append({"location_id": loc_id, "aisle_id": aid, "y": 61.0, "abc_class": "BULK"})

write_csv(OUT / "locations.csv", locations, ["location_id", "aisle_id", "y", "abc_class"])

# ══════════════════════════════════════════════════════════════════════
# 4. DEPOTS -- informational reference only. build_travel_matrix.py takes
#    depot_id/depot_x/depot_y as arguments, never a CSV row, so this file
#    is not read by any script; it only tells you what to pass twice.
# ══════════════════════════════════════════════════════════════════════
depots = [
    {"depot_id": PACK_A[0], "x": PACK_A[1], "y": PACK_A[2]},
    {"depot_id": PACK_B[0], "x": PACK_B[1], "y": PACK_B[2]},
]
write_csv(OUT / "depots_reference.csv", depots, ["depot_id", "x", "y"])

# ══════════════════════════════════════════════════════════════════════
# 5. SKU MASTER (informational) -- 80/20/5 ABC demand split + a small
#    bulk-item class used only to make a few orders genuinely too heavy
#    for a pallet jack.
#    Unit volumes: 1-30 litres for regular piece-pick goods, 50-150 litres
#    for bulk cases (fix #3 above).
# ══════════════════════════════════════════════════════════════════════
skus = []
sku_ids = {"A": [], "B": [], "C": [], "BULK": []}
for k in range(N_SKU_A):
    sku_ids["A"].append(f"SKU-A{k+1:04d}")
for k in range(N_SKU_B):
    sku_ids["B"].append(f"SKU-B{k+1:04d}")
for k in range(N_SKU_C):
    sku_ids["C"].append(f"SKU-C{k+1:04d}")
for k in range(N_SKU_BULK):
    sku_ids["BULK"].append(f"SKU-BULK{k+1:03d}")

sku_weight = {}
sku_volume = {}
sku_demand = {}
for cls, ids in sku_ids.items():
    for sid in ids:
        if cls == "BULK":
            w = round(rng.uniform(40.0, 120.0), 2)
            v = round(rng.uniform(0.05, 0.15), 3)
            d = 0.003  # ordered rarely; weighting handled separately for heavy orders
        else:
            w = round(rng.uniform(0.5, 25.0), 2)
            v = round(rng.uniform(0.001, 0.03), 4)
            share = {"A": 0.80, "B": 0.15, "C": 0.05}[cls]
            d = share / len(ids)
        sku_weight[sid] = w
        sku_volume[sid] = v
        sku_demand[sid] = d
        skus.append(
            {
                "sku_id": sid,
                "abc_class": cls,
                "unit_weight_kg": w,
                "unit_volume_m3": v,
                "demand_rate": round(d, 6),
            }
        )
write_csv(
    OUT / "skus.csv", skus, ["sku_id", "abc_class", "unit_weight_kg", "unit_volume_m3", "demand_rate"]
)

# ══════════════════════════════════════════════════════════════════════
# 6. SLOT <-> SKU ASSIGNMENT  (dedicated storage; round-robin within class)
# ══════════════════════════════════════════════════════════════════════
rng.shuffle(loc_by_class["A"])
rng.shuffle(loc_by_class["B"])
rng.shuffle(loc_by_class["C"])
bulk_pool = list(bulk_locations)
rng.shuffle(bulk_pool)

sku_to_loc = {}
for cls, ids in sku_ids.items():
    pool = bulk_pool if cls == "BULK" else loc_by_class[cls]
    for i, sid in enumerate(ids):
        sku_to_loc[sid] = pool[i % len(pool)]

# FIX #1: build inventory.csv from the sku->location mapping (one row per
# SKU, always 500 rows). This is lossless by construction; a location_id
# may repeat across rows when a bin is shared, which is intended for the
# C and BULK classes here (230 SKUs onto 160 slots, 20 onto 8 slots).
inventory = [{"sku_id": sid, "location_id": loc, "qty_on_hand": rng.randint(50, 500)}
             for sid, loc in sku_to_loc.items()]
write_csv(OUT / "inventory.csv", inventory, ["sku_id", "location_id", "qty_on_hand"])

# ══════════════════════════════════════════════════════════════════════
# 7. PICKERS -- column names: picker_id, equipment_type, max_weight,
#               max_volume, shift_duration  (exact match to _eligible())
# ══════════════════════════════════════════════════════════════════════
pickers = []
for i in range(N_PICKERS_JACK):
    pickers.append(
        {
            "picker_id": f"PJ{i+1:02d}",
            "equipment_type": "pallet_jack",
            "max_weight": CAP_JACK_KG,
            "max_volume": CAP_JACK_VOL,
            "shift_duration": SHIFT_MIN,
        }
    )
for i in range(N_PICKERS_FORK):
    pickers.append(
        {
            "picker_id": f"FK{i+1:02d}",
            "equipment_type": "forklift",
            "max_weight": CAP_FORK_KG,
            "max_volume": CAP_FORK_VOL,
            "shift_duration": SHIFT_MIN,
        }
    )
write_csv(
    OUT / "pickers.csv", pickers, ["picker_id", "equipment_type", "max_weight", "max_volume", "shift_duration"]
)

# ══════════════════════════════════════════════════════════════════════
# 8. ORDERS + ORDER ITEMS
#    column names: order_id, total_weight, total_volume, priority_weight,
#                  cutoff_time, account_tier, wait_time, must_ship,
#                  required_equipment   (exact match to solve_order_assignment)
#    order_items: order_id, item_id, location_id, pick_time
# ══════════════════════════════════════════════════════════════════════
regular_sku_ids = sku_ids["A"] + sku_ids["B"] + sku_ids["C"]
regular_weights = [sku_demand[s] for s in regular_sku_ids]
bulk_sku_ids = sku_ids["BULK"]

orders_rows = []
items_rows = []

n_heavy_target = round(N_ORDERS * HEAVY_SHARE)
heavy_flags = [True] * n_heavy_target + [False] * (N_ORDERS - n_heavy_target)
rng.shuffle(heavy_flags)

for idx in range(N_ORDERS):
    o = idx + 1
    oid = f"O{o:04d}"
    is_heavy = heavy_flags[idx]
    n_lines = rng.randint(3, 12)
    n_bulk_lines = min(rng.randint(1, 2), n_lines) if is_heavy else 0
    n_regular_lines = n_lines - n_bulk_lines

    chosen = []
    pool = list(range(len(regular_sku_ids)))
    weights = list(regular_weights)
    while len(chosen) < n_regular_lines and pool:
        pick = rng.choices(pool, weights=[weights[i] for i in pool])[0]
        chosen.append(regular_sku_ids[pick])
        pool.remove(pick)

    total_weight = 0.0
    total_volume = 0.0
    line_no = 0
    for sid in chosen:
        line_no += 1
        qty = rng.randint(1, 6)
        w = round(sku_weight[sid] * qty, 2)
        v = round(sku_volume[sid] * qty, 4)
        total_weight += w
        total_volume += v
        items_rows.append(
            {
                "order_id": oid,
                "item_id": f"{oid}-{line_no}",
                "location_id": sku_to_loc[sid],
                "pick_time": PICK_TIME_PER_LINE,
            }
        )

    if is_heavy:
        # Size the bulk line(s) deterministically so this order ends up
        # over a pallet jack's capacity by a clear margin -- otherwise
        # "heavy" orders would only probably trigger C4, not reliably.
        bulk_choices = rng.sample(bulk_sku_ids, k=n_bulk_lines)
        target_total = CAP_JACK_KG + rng.uniform(100.0, 600.0)
        remaining_needed = max(0.0, target_total - total_weight)
        for i, sid in enumerate(bulk_choices):
            line_no += 1
            share = remaining_needed / len(bulk_choices) if remaining_needed > 0 else 0.0
            qty = max(1, min(25, math.ceil(share / sku_weight[sid]))) if share > 0 else rng.randint(2, 5)
            w = round(sku_weight[sid] * qty, 2)
            v = round(sku_volume[sid] * qty, 4)
            total_weight += w
            total_volume += v
            items_rows.append(
                {
                    "order_id": oid,
                    "item_id": f"{oid}-{line_no}",
                    "location_id": sku_to_loc[sid],
                    "pick_time": PICK_TIME_PER_LINE,
                }
            )

    # Priority inputs: leave priority_weight blank so compute_priority_weight
    # (already wired into solve_order_assignment) derives it from these.
    must_ship = rng.random() < MUST_SHIP_SHARE
    account_tier = ""
    cutoff_time = ""
    wait_time = ""
    if not must_ship:
        account_tier = "key" if rng.random() < KEY_TIER_SHARE else ""
        if rng.random() < 0.70:
            cutoff_time = round(rng.uniform(-2.0, 20.0), 2)  # hours remaining; negative = overdue
        else:
            wait_time = round(rng.uniform(0.0, 20.0), 2)  # hours queued, used when no cutoff

    # A small, separate equipment-mandated subset (hazardous/fragile
    # handling), independent of the weight-driven forklift requirement
    # above, so both eligibility branches in _eligible() are exercised.
    required_equipment = "forklift" if (not is_heavy and rng.random() < 0.02) else ""

    orders_rows.append(
        {
            "order_id": oid,
            "total_weight": round(total_weight, 2),
            "total_volume": round(total_volume, 3),
            "priority_weight": "",
            "cutoff_time": cutoff_time,
            "account_tier": account_tier,
            "wait_time": wait_time,
            "must_ship": must_ship,
            "required_equipment": required_equipment,
        }
    )

write_csv(
    OUT / "orders.csv",
    orders_rows,
    [
        "order_id",
        "total_weight",
        "total_volume",
        "priority_weight",
        "cutoff_time",
        "account_tier",
        "wait_time",
        "must_ship",
        "required_equipment",
    ],
)
write_csv(OUT / "order_items.csv", items_rows, ["order_id", "item_id", "location_id", "pick_time"])

# ══════════════════════════════════════════════════════════════════════
# 9. SANITY CHECKS (printed, not written)
# ══════════════════════════════════════════════════════════════════════
print("\n── Sanity checks ──────────────────────────────────────────────")

# (a) inventory <-> order_items consistency: inventory.csv must be lossless,
#     one row per sku_id (fix #1).
inv_sku_ids = {row["sku_id"] for row in inventory}
print(f"  inventory.csv rows: {len(inventory)}  unique sku_id: {len(inv_sku_ids)}  (expect both {N_SKU})")
assert len(inventory) == N_SKU and len(inv_sku_ids) == N_SKU, "inventory.csv is lossy"

# (b) every location_id referenced by order_items actually exists in locations.csv
all_loc_ids = {row["location_id"] for row in locations}
bad_refs = [r["location_id"] for r in items_rows if r["location_id"] not in all_loc_ids]
print(f"  order_items location_id references not found in locations.csv: {len(bad_refs)}")
assert not bad_refs

# (c) forklift-required-by-weight coverage
weights_all = [r["total_weight"] for r in orders_rows]
over_weight_cap = sum(1 for w in weights_all if w > CAP_JACK_KG)
print(f"  orders with total_weight > {CAP_JACK_KG} kg (forklift-only by weight): "
      f"{over_weight_cap} / {N_ORDERS} ({100*over_weight_cap/N_ORDERS:.1f}%)")
print(f"  weight range: {min(weights_all):.1f} - {max(weights_all):.1f} kg")
assert over_weight_cap > 0, "no order exercises the forklift-by-weight branch"
assert max(weights_all) < CAP_FORK_KG, "a heavy order exceeds even the forklift weight cap"

# (c2) FIX #3 check: volume must not make nearly every order infeasible.
volumes_all = [r["total_volume"] for r in orders_rows]
over_every_cap = sum(1 for v in volumes_all if v > CAP_FORK_VOL)
print(f"  orders with total_volume > every picker's cap ({CAP_FORK_VOL} m3): "
      f"{over_every_cap} / {N_ORDERS} ({100*over_every_cap/N_ORDERS:.1f}%)")
print(f"  volume range: {min(volumes_all):.3f} - {max(volumes_all):.3f} m3 "
      f"(median {sorted(volumes_all)[len(volumes_all)//2]:.3f})")
assert over_every_cap / N_ORDERS < 0.15, "too many orders are volume-infeasible for every picker"

# (d) line count bounds
n_lines_all = [sum(1 for r in items_rows if r["order_id"] == o["order_id"]) for o in orders_rows]
print(f"  lines per order: {min(n_lines_all)} - {max(n_lines_all)} (mean {sum(n_lines_all)/len(n_lines_all):.2f})")
assert min(n_lines_all) >= 3 and max(n_lines_all) <= 12

# (e) priority input mix
n_must = sum(1 for r in orders_rows if r["must_ship"])
n_key = sum(1 for r in orders_rows if r["account_tier"] == "key")
n_cutoff = sum(1 for r in orders_rows if r["cutoff_time"] != "")
n_wait = sum(1 for r in orders_rows if r["wait_time"] != "")
print(f"  must_ship: {n_must} ({100*n_must/N_ORDERS:.1f}%)  key tier: {n_key}  "
      f"has cutoff_time: {n_cutoff}  has wait_time: {n_wait}")

# (f) explicit required_equipment subset (separate from weight-driven forklift need)
n_req_fork = sum(1 for r in orders_rows if r["required_equipment"] == "forklift")
print(f"  orders with explicit required_equipment=forklift (independent of weight): {n_req_fork}")

print("\n  All checks passed.")
