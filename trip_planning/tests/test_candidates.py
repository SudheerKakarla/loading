"""Candidate filters: dominance, dates, and who may share a truck."""

from datetime import date

from candidates import feasible_ship_date, generate_candidates
from helpers import cfg, customer, customers, order, orders, overrides, rate, rates, trucks
from shipments import Shipment, build_shipments


def generate(order_rows, customer_rows, rate_rows, settings=None, extra=None, truck_rows=None):
    rate_frame = rates(*rate_rows)
    built = build_shipments(
        orders(*order_rows),
        customers(*customer_rows),
        rate_frame,
        truck_rows if truck_rows is not None else trucks(),
        settings or cfg(),
    )
    return generate_candidates(built.shipments, rate_frame, trucks() if truck_rows is None else truck_rows, settings or cfg(), extra)


def test_dominance_drops_a_combo_that_does_not_beat_solos():
    # Case solos are 10 * 1 = 10 each, sum 20. The shared 4W is max(1,000, 1,000) = 1,000.
    # 1,000 is not strictly below 20, so no consolidated candidate is generated.
    found = generate(
        [order("A1", "A", "2025-08-01", "2025-08-05", 10, 100, 1), order("B1", "B", "2025-08-01", "2025-08-05", 10, 100, 1)],
        [customer("A", service="LTL"), customer("B", service="LTL")],
        [rate("A", trip_4w=1000, ltl_mt=1), rate("B", trip_4w=1000, ltl_mt=1)],
    )
    assert [item.candidate_id for item in found.candidates if not item.is_solo] == []
    # Case bill 10 * 100 = 1,000 equals the truck, so the solo stays on the truck (1,000 + 1,000 = 2,000).
    # Shared truck max(100, 100) = 100, and 100 < 2,000, so the pair is kept.
    found = generate(
        [order("A1", "A", "2025-08-01", "2025-08-05", 10, 100, 1), order("B1", "B", "2025-08-01", "2025-08-05", 10, 100, 1)],
        [customer("A", service="FTL"), customer("B", service="FTL")],
        [rate("A", trip_4w=100), rate("B", trip_4w=100)],
    )
    shared = [item for item in found.candidates if not item.is_solo]
    assert len(shared) == 1
    assert shared[0].cost == 100


def test_rdd_spread_and_release_reject_a_ship_date():
    # 2025-08-01 to 2025-08-03 is 2 days, and max_early_days is 1, so the pair is rejected.
    found = generate(
        [order("A1", "A", "2025-07-01", "2025-08-01", 1, 100, 1), order("B1", "B", "2025-07-01", "2025-08-03", 1, 100, 1)],
        [customer("A"), customer("B")],
        [rate("A", trip_4w=100), rate("B", trip_4w=80)],
    )
    assert [item for item in found.candidates if not item.is_solo] == []
    # One day apart is allowed. max(100, 80) = 100 < 100 + 80 = 180.
    found = generate(
        [order("A1", "A", "2025-07-01", "2025-08-01", 1, 100, 1), order("B1", "B", "2025-07-01", "2025-08-02", 1, 100, 1)],
        [customer("A"), customer("B")],
        [rate("A", trip_4w=100), rate("B", trip_4w=80)],
    )
    shared = [item for item in found.candidates if not item.is_solo]
    assert len(shared) == 1
    assert shared[0].ship_date == date(2025, 8, 1)
    assert shared[0].early_days == 1

    late = Shipment("s", "A", date(2025, 8, 5), 1, 1, 1, date(2025, 8, 6), "FTL", "Bangkok", "FOOD", ("o",))
    # Release 6 Aug is after the only legal ship date, 5 Aug, so the shipment is not dated.
    assert feasible_ship_date((late,), cfg()) is None


def test_compatibility_province_and_overrides():
    # Different provinces do not share a truck under same_province.
    found = generate(
        [order("A1", "A", "2025-08-01", "2025-08-05", 1, 100, 1), order("B1", "B", "2025-08-01", "2025-08-05", 1, 100, 1)],
        [customer("A", province="Bangkok"), customer("B", province="Chiang Mai")],
        [rate("A", trip_4w=100), rate("B", trip_4w=80)],
    )
    assert [item for item in found.candidates if not item.is_solo] == []

    # An allow override is symmetric and beats the province rule. max(100, 80) = 100 < 180.
    found = generate(
        [order("A1", "A", "2025-08-01", "2025-08-05", 1, 100, 1), order("B1", "B", "2025-08-01", "2025-08-05", 1, 100, 1)],
        [customer("A", province="Bangkok"), customer("B", province="Chiang Mai")],
        [rate("A", trip_4w=100), rate("B", trip_4w=80)],
        extra=overrides({"ship_to_a": "B", "ship_to_b": "A", "rule": "allow"}),
    )
    assert len([item for item in found.candidates if not item.is_solo]) == 1

    # A deny override blocks two ship-tos that would otherwise share a province.
    found = generate(
        [order("A1", "A", "2025-08-01", "2025-08-05", 1, 100, 1), order("B1", "B", "2025-08-01", "2025-08-05", 1, 100, 1)],
        [customer("A", province="Bangkok"), customer("B", province="Bangkok")],
        [rate("A", trip_4w=100), rate("B", trip_4w=80)],
        extra=overrides({"ship_to_a": "A", "ship_to_b": "B", "rule": "deny"}),
    )
    assert [item for item in found.candidates if not item.is_solo] == []

    # solo_only never consolidates, including with a neighbour in the same province.
    found = generate(
        [order("A1", "A", "2025-08-01", "2025-08-05", 1, 100, 1), order("B1", "B", "2025-08-01", "2025-08-05", 1, 100, 1)],
        [customer("A"), customer("B")],
        [rate("A", trip_4w=100), rate("B", trip_4w=80)],
        settings=cfg(solo_only=("A",)),
    )
    assert [item for item in found.candidates if not item.is_solo] == []
