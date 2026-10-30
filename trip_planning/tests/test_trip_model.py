"""Cover, penalty, tie-break, fleet cap, and the solo guarantee."""

import random

from config import tie_break_weights
from helpers import cfg, customer, customers, order, orders, rate, rates, trucks
from plan_trips import plan
from reporting import summary_map
from trip_model import _penalty


def _run(order_rows, customer_rows, rate_rows, settings=None, truck_rows=None):
    return plan(
        orders(*order_rows),
        customers(*customer_rows),
        rates(*rate_rows),
        truck_rows if truck_rows is not None else trucks(),
        settings or cfg(time_limit_s=30),
    )


def test_past_due_is_unplanned():
    # doc_date 10 Aug is after RDD 8 Aug, so the only legal ship date is already missed.
    result = _run(
        [order("P1", "A", "2025-08-10", "2025-08-08", 5, 100, 1)],
        [customer("A")],
        [rate("A", trip_4w=100)],
    )
    assert result.ok
    assert result.trips.empty
    assert list(result.exclusions["reason"]) == ["past_due"]
    assert list(result.exclusions["category"]) == ["business"]
    assert "P1" not in set(result.trip_orders["order_id"]) if not result.trip_orders.empty else True


def test_each_data_quality_reason_and_reconciliation():
    # M0 is both unknown and zero cases: no_master_customer wins.
    # G1 is the only plannable order: 4 cases, 100 kg, 1 m3.
    result = _run(
        [
            order("G1", "C1", "2025-08-01", "2025-08-05", 4, 100, 1),
            order("M1", "ZZ", "2025-08-01", "2025-08-05", 2, 20, 0.2),
            order("M0", "ZZ", "2025-08-01", "2025-08-05", 0, 5, 0.1),
            order("Z1", "C1", "2025-08-01", "2025-08-05", 0, 7, 0.1),
            order("N1", "C2", "2025-08-01", "2025-08-05", 3, 30, 0.3),
            order("T1", "C3", "2025-08-01", "2025-08-05", 6, 60, 0.4),
        ],
        [customer("C1"), customer("C2"), customer("C3")],
        [rate("C1", trip_4w=100), rate("C3", trip_18w=500)],
        truck_rows=trucks(
            {"truck_type": "4W", "capacity_kg": 1500, "capacity_m3": 4},
            {"truck_type": "18W", "capacity_kg": None, "capacity_m3": None},
        ),
    )
    reasons = dict(zip(result.exclusions["order_id"], result.exclusions["reason"]))
    assert reasons["M1"] == "no_master_customer"
    assert reasons["M0"] == "no_master_customer"
    assert reasons["Z1"] == "zero_quantity"
    assert reasons["N1"] == "no_rate"
    assert reasons["T1"] == "truck_type_not_offered"
    assert list(result.trip_orders["order_id"]) == ["G1"]
    assert result.ok
    values = summary_map(result)
    # Input 4+2+0+0+3+6 = 15 cases. Planned 4. Excluded 11.
    assert values["input_cases"] == "15"
    assert values["planned_cases"] == "4"
    assert values["excluded_cases"] == "11"
    assert abs(float(values["input_kg"]) - (100 + 20 + 5 + 7 + 30 + 60)) < 1e-6


def test_penalty_is_larger_than_any_saving_and_feasible_shipments_ship():
    result = _run(
        [
            order("A1", "A", "2025-08-01", "2025-08-05", 1, 100, 1),
            order("B1", "B", "2025-08-01", "2025-08-05", 1, 100, 1),
        ],
        [customer("A"), customer("B")],
        [rate("A", trip_4w=100), rate("B", trip_4w=80)],
    )
    assert result.ok
    assert result.penalty_total == 0
    assert set(result.trip_orders["order_id"]) == {"A1", "B1"}
    # P = sum of solos + 1. A saving cannot exceed the sum of solos, so P is strictly larger.
    from candidates import generate_candidates
    from shipments import build_shipments

    rate_frame = rates(rate("A", trip_4w=100), rate("B", trip_4w=80))
    built = build_shipments(
        orders(
            order("A1", "A", "2025-08-01", "2025-08-05", 1, 100, 1),
            order("B1", "B", "2025-08-01", "2025-08-05", 1, 100, 1),
        ),
        customers(customer("A"), customer("B")),
        rate_frame,
        trucks(),
    )
    found = generate_candidates(built.shipments, rate_frame, trucks(), cfg())
    solos = [item.cost for item in found.candidates if item.is_solo]
    assert _penalty(found.candidates) == sum(solos) + 1
    assert _penalty(found.candidates) > sum(solos)


def test_tie_break_prefers_fewer_early_days_and_loses_to_one_baht():
    # Three FTL shipments, 600 kg each, only a 4W (1,500 kg). A pair fits; the triple is 1,800 kg and does not.
    # A and B share 10 Aug. C is 11 Aug. Pair cost is max of the two trip rates.
    rows = [
        order("A1", "A", "2025-08-01", "2025-08-10", 1, 600, 1),
        order("B1", "B", "2025-08-01", "2025-08-10", 1, 600, 1),
        order("C1", "C", "2025-08-01", "2025-08-11", 1, 600, 1),
    ]
    people = [customer("A"), customer("B"), customer("C")]
    only_4w = trucks({"truck_type": "4W", "capacity_kg": 1500, "capacity_m3": 4})

    # Equal rates of 100. AB+C costs 200 with 0 early days. BC+A and AC+B cost 200 with 1 early day.
    equal = _run(rows, people, [rate("A", trip_4w=100), rate("B", trip_4w=100), rate("C", trip_4w=100)], truck_rows=only_4w)
    assert equal.ok
    assert equal.total_cost == 200
    together = _partner_trip(equal)
    assert together == {"A", "B"}

    # A=100, B=101, C=200.
    # AB+C = max(100, 101) + 200 = 301, early 0.
    # BC+A = max(101, 200) + 100 = 300, early 1.
    # One baht beats the tie-break, so B and C share the truck.
    uneven = _run(rows, people, [rate("A", trip_4w=100), rate("B", trip_4w=101), rate("C", trip_4w=200)], truck_rows=only_4w)
    assert uneven.ok
    assert uneven.total_cost == 300
    assert _partner_trip(uneven) == {"B", "C"}
    # n = 3, max_early_days = 1, bound = 3 * 2 = 6, weight = 0.5 / 7.
    # Even the whole bound is under 1 THB: 6 * weight = 6 * 0.5 / 7 < 1.
    early, upgrade = tie_break_weights(3, 1)
    assert early == upgrade
    assert 6 * early < 1


def test_fleet_limit_leaves_the_dearer_shipment():
    # Each load is 800 kg, so the two do not fit one 4W (1,500). No 6W is offered.
    # Solos cost 100 and 80. max_trucks=1 keeps the 80 and marks the other fleet_limit.
    result = _run(
        [
            order("A1", "A", "2025-08-01", "2025-08-05", 1, 800, 1),
            order("B1", "B", "2025-08-01", "2025-08-05", 1, 800, 1),
        ],
        [customer("A"), customer("B")],
        [rate("A", trip_4w=100), rate("B", trip_4w=80)],
        settings=cfg(max_trucks=1, time_limit_s=30),
        truck_rows=trucks({"truck_type": "4W", "capacity_kg": 1500, "capacity_m3": 4}),
    )
    assert result.ok
    assert result.total_cost == 80
    assert list(result.trip_orders["order_id"]) == ["B1"]
    assert list(result.exclusions["reason"]) == ["fleet_limit"]
    assert int(result.trips.iloc[0]["n_trucks"]) == 1


def test_random_instance_never_costs_more_than_the_solos():
    rng = random.Random(16)
    order_rows = []
    customer_rows = []
    rate_rows = []
    for index in range(6):
        ship_to = f"S{index}"
        service = "FTL" if index % 2 == 0 else "LTL"
        order_rows.append(
            order(
                f"O{index}",
                ship_to,
                "2025-08-01",
                "2025-08-03",
                rng.randint(5, 30),
                rng.randint(200, 1200),
                1,
            )
        )
        customer_rows.append(customer(ship_to, service=service))
        rate_rows.append(
            rate(
                ship_to,
                trip_4w=400 + 10 * index,
                trip_6w=900 + 10 * index,
                ltl_mt=5 if service == "LTL" else "",
            )
        )
    result = _run(order_rows, customer_rows, rate_rows, settings=cfg(max_shipments_per_trip=3, time_limit_s=30))
    assert result.ok
    assert result.total_cost <= result.baseline_cost + 1e-6
    assert set(result.trip_orders["order_id"]) == {f"O{index}" for index in range(6)}
    values = summary_map(result)
    assert values["solver_status"] == "optimal"
    assert float(values["solver_gap_max"]) == 0


def _partner_trip(result):
    counts = result.trip_shipments.groupby("trip_id")["ship_to"].apply(list)
    shared = [ship_tos for ship_tos in counts if len(ship_tos) == 2]
    assert len(shared) == 1
    return set(shared[0])
