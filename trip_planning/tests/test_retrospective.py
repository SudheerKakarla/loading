"""Synthetic checks for permissions, calendars, caps, and publication."""

import json
from pathlib import Path

import pandas as pd
import pytest

from baseline_cost import TruckOffer, fleet_frontier
from candidates import generate_candidates
from config import TripConfig
from helpers import cfg, customer, customers, order, orders, rate, rates, trucks
from plan_trips import main, plan
from reporting import summary_map
from scenarios import load_manifest, run_scenarios
from shipments import build_shipments

SAMPLE = Path(__file__).resolve().parents[1] / "inputs" / "sample"
REFERENCE = Path(__file__).resolve().parents[1] / "scenarios" / "reference.json"


def _small(settings=None, **plan_kwargs):
    # One 4W at 100. Released 1 Aug, due 5 Aug, 100 kg. Solo cost is 100.
    return plan(
        orders(order("A1", "A", "2025-08-01", "2025-08-05", 1, 100, 1)),
        customers(customer("A", province="Bangkok")),
        rates(rate("A", trip_4w=100)),
        trucks({"truck_type": "4W", "capacity_kg": 1500, "capacity_m3": 4}),
        settings or cfg(max_early_days=0, time_limit_s=30),
        **plan_kwargs,
    )


def test_integer_config_rejects_bool_and_float():
    with pytest.raises(ValueError):
        TripConfig(max_trucks=True)
    with pytest.raises(ValueError):
        TripConfig(transit_days=1.5)


def test_numeric_permission_is_rejected():
    people = customers(customer("A"))
    people["allow_truck_pricing"] = 1
    with pytest.raises(ValueError):
        plan(
            orders(order("A1", "A", "2025-08-01", "2025-08-05", 1, 100, 1)),
            people,
            rates(rate("A", trip_4w=100)),
            trucks(),
            cfg(time_limit_s=30),
        )


def test_case_permission_prices_the_case_branch():
    # Truck would be 1,000. Case is 2 * 10 = 20. Truck permission is off, so the plan pays 20.
    people = customers(customer("A", service="LTL"))
    people["allow_truck_pricing"] = False
    result = plan(
        orders(order("A1", "A", "2025-08-01", "2025-08-05", 2, 100, 1)),
        people,
        rates(rate("A", service="LTL", trip_4w=1000, ltl_mt=10)),
        trucks(),
        cfg(max_early_days=0, time_limit_s=30),
    )
    assert result.ok, result.errors
    assert result.total_cost == 20
    assert result.trips.iloc[0]["price_basis"] == "case"


def test_sharing_denial_blocks_the_same_ship_to_on_another_day():
    # Each solo is 100. Together the truck would be max(100, 100) = 100.
    # Sharing is denied, including this ship-to with its other RDD, so the plan pays 200.
    people = customers(customer("A"))
    people["allow_sharing"] = False
    result = plan(
        orders(
            order("A1", "A", "2025-08-01", "2025-08-05", 1, 100, 1),
            order("A2", "A", "2025-08-01", "2025-08-06", 1, 100, 1),
        ),
        people,
        rates(rate("A", trip_4w=100)),
        trucks({"truck_type": "4W", "capacity_kg": 1500, "capacity_m3": 4}),
        cfg(time_limit_s=30),
    )
    assert result.ok, result.errors
    assert result.total_cost == 200
    assert not result.trips["shipment_ids"].str.contains(";").any()


def test_approved_rdd_merges_groups_and_leaves_the_input_unchanged():
    raw = orders(
        order("A1", "A", "2025-07-01", "2025-08-01", 1, 100, 1),
        order("A2", "A", "2025-07-01", "2025-08-05", 1, 100, 1),
    )
    snapshot = raw.copy()
    overrides = pd.DataFrame(
        [{"order_id": "A2", "approved_rdd": "2025-08-01", "reason": "customer asked"}]
    )
    result = plan(
        raw,
        customers(customer("A")),
        rates(rate("A", trip_4w=100)),
        trucks({"truck_type": "4W", "capacity_kg": 1500, "capacity_m3": 4}),
        cfg(time_limit_s=30),
        delivery_date_overrides=overrides,
    )
    assert result.ok, result.errors
    assert result.n_shipments == 1
    assert result.total_cost == 100
    audit = result.trip_orders.set_index("order_id")
    assert audit.loc["A2", "original_rdd"] == "2025-08-05"
    assert audit.loc["A2", "effective_rdd"] == "2025-08-01"
    assert audit.loc["A2", "override_reason"] == "customer asked"
    pd.testing.assert_frame_equal(raw, snapshot)


def test_dispatch_closure_suppresses_savings():
    # The only legal departure is 5 Aug. Closing it leaves the shipment dateable but unplanned.
    calendar = pd.DataFrame(
        [{"calendar_type": "dispatch", "date": "2025-08-05", "is_open": "false", "ship_to": ""}]
    )
    result = _small(planning_calendar=calendar)
    assert result.ok, result.errors
    assert list(result.exclusions["reason"]) == ["calendar_unavailable"]
    assert result.baseline_cost == 100
    assert summary_map(result)["saving_thb"] == ""


def test_dated_open_reopens_a_weekly_closure():
    # 3 Aug 2025 is a Sunday. The weekly rule closes it. The dated rule opens it again.
    weekly = pd.DataFrame(
        [
            {
                "calendar_type": "dispatch",
                "ship_to": "",
                "weekday": "Sunday",
                "is_open": "false",
                "start_date": "2025-08-01",
                "end_date": "2025-08-31",
            }
        ]
    )
    dated = pd.DataFrame(
        [{"calendar_type": "dispatch", "date": "2025-08-03", "is_open": "true", "ship_to": ""}]
    )
    closed = plan(
        orders(order("A1", "A", "2025-08-01", "2025-08-03", 1, 100, 1)),
        customers(customer("A")),
        rates(rate("A", trip_4w=100)),
        trucks({"truck_type": "4W", "capacity_kg": 1500, "capacity_m3": 4}),
        cfg(max_early_days=0, time_limit_s=30),
        weekly_calendar=weekly,
    )
    opened = plan(
        orders(order("A1", "A", "2025-08-01", "2025-08-03", 1, 100, 1)),
        customers(customer("A")),
        rates(rate("A", trip_4w=100)),
        trucks({"truck_type": "4W", "capacity_kg": 1500, "capacity_m3": 4}),
        cfg(max_early_days=0, time_limit_s=30),
        planning_calendar=dated,
        weekly_calendar=weekly,
    )
    assert closed.ok and list(closed.exclusions["reason"]) == ["calendar_unavailable"]
    assert opened.ok and opened.total_cost == 100


def test_receiving_closure_is_not_a_dispatch_closure():
    # Transit is 1 day, so the only ship date is 5 Aug and the arrival is 6 Aug.
    # Dispatch is open. Receiving is closed on arrival, so the shipment stays unplanned.
    calendar = pd.DataFrame(
        [{"calendar_type": "receiving", "date": "2025-08-06", "is_open": "false", "ship_to": "A"}]
    )
    result = plan(
        orders(order("A1", "A", "2025-08-01", "2025-08-06", 1, 100, 1)),
        customers(customer("A", province="Bangkok")),
        rates(rate("A", trip_4w=100)),
        trucks({"truck_type": "4W", "capacity_kg": 1500, "capacity_m3": 4}),
        cfg(max_early_days=0, transit_days=1, time_limit_s=30),
        planning_calendar=calendar,
    )
    assert result.ok, result.errors
    assert list(result.exclusions["reason"]) == ["calendar_unavailable"]


def test_wide_rdd_spread_can_share_when_deadlines_meet():
    # A is due 10 Aug with no transit. B is due 12 Aug with 3 days transit.
    # Deadlines are 10 Aug and 9 Aug, so 9 Aug is inside both one-day windows.
    # The raw RDD gap is 2 days. The old raw-RDD filter would have dropped the pair.
    frame = orders(
        order("A1", "A", "2025-07-01", "2025-08-10", 1, 100, 1),
        order("B1", "B", "2025-07-01", "2025-08-12", 1, 100, 1),
    )
    people = customers(customer("A", province="South"), customer("B", province="North"))
    cards = rates(rate("A", trip_4w=100), rate("B", trip_4w=100))
    fleet = trucks({"truck_type": "4W", "capacity_kg": 1500, "capacity_m3": 4})
    settings = cfg(compat_mode="any", transit_days_by_province={"South": 0, "North": 3}, time_limit_s=30)
    built = build_shipments(frame, people, cards, fleet, settings)
    found = generate_candidates(built.shipments, cards, fleet, settings)
    shared = [item for item in found.candidates if len(item.member_ids) == 2]
    assert shared
    assert shared[0].ship_date.isoformat() == "2025-08-09"


def test_equal_rdds_do_not_share_when_transit_splits_the_windows():
    # Both are due 5 Aug. B needs 3 days, so its latest departure is 2 Aug.
    # A's earliest departure is 4 Aug. The windows do not meet.
    frame = orders(
        order("A1", "A", "2025-07-01", "2025-08-05", 1, 100, 1),
        order("B1", "B", "2025-07-01", "2025-08-05", 1, 100, 1),
    )
    people = customers(customer("A", province="South"), customer("B", province="North"))
    cards = rates(rate("A", trip_4w=100), rate("B", trip_4w=100))
    fleet = trucks({"truck_type": "4W", "capacity_kg": 1500, "capacity_m3": 4})
    settings = cfg(compat_mode="any", transit_days_by_province={"South": 0, "North": 3}, time_limit_s=30)
    built = build_shipments(frame, people, cards, fleet, settings)
    found = generate_candidates(built.shipments, cards, fleet, settings)
    assert not any(len(item.member_ids) == 2 for item in found.candidates)


def test_cap_uses_every_open_date_and_matched_solo_ignores_a_leftover():
    rows = [
        order("A1", "A", "2025-08-01", "2025-08-05", 1, 800, 1),
        order("B1", "B", "2025-08-01", "2025-08-05", 1, 800, 1),
    ]
    people = [customer("A"), customer("B")]
    cards = [rate("A", trip_4w=100), rate("B", trip_4w=80)]
    fleet = trucks({"truck_type": "4W", "capacity_kg": 1500, "capacity_m3": 4})
    # 4 Aug and 5 Aug are both open. One truck per day carries both loads: 100 + 80 = 180.
    spread = plan(
        orders(*rows),
        customers(*people),
        rates(*cards),
        fleet,
        cfg(max_trucks=1, time_limit_s=30),
    )
    assert spread.ok, spread.errors
    assert spread.total_cost == 180
    assert set(spread.trip_orders["order_id"]) == {"A1", "B1"}
    assert set(spread.trips["ship_date"].astype(str).str.slice(0, 10)) == {"2025-08-04", "2025-08-05"}
    # One legal day and one truck keeps the 80. The 100 stays in the full baseline, not the matched solo.
    held = plan(
        orders(*rows),
        customers(*people),
        rates(*cards),
        fleet,
        cfg(max_trucks=1, max_early_days=0, time_limit_s=30),
    )
    assert held.ok
    assert held.baseline_cost == 180
    assert held.total_cost == 80
    assert summary_map(held)["saving_thb"] == ""
    from scenarios import _matched_solo

    assert _matched_solo(held) == 80


def test_frontier_keeps_a_dearer_mix_that_uses_fewer_trucks():
    # 2,000 kg needs two 4W at 100 each (200) or one 6W at 250.
    # A budget of 2 keeps both. A budget of 1 keeps only the single 6W.
    offers = (
        TruckOffer("4W", 1500, 4, 100),
        TruckOffer("6W", 5000, 25, 250),
    )
    wide = fleet_frontier(2000, 1, offers, 2)
    tight = fleet_frontier(2000, 1, offers, 1)
    assert {(mix.n_trucks, mix.cost) for mix in wide} == {(2, 200), (1, 250)}
    assert [(mix.n_trucks, mix.cost) for mix in tight] == [(1, 250)]


def test_plan_refuses_a_nonempty_directory(tmp_path):
    out = tmp_path / "plan"
    out.mkdir()
    assert main(["--inputs", str(SAMPLE), "--out", str(out), "--time-limit", "30"]) == 0
    assert main(["--inputs", str(SAMPLE), "--out", str(out), "--time-limit", "30"]) == 1
    assert (out / "trips.csv").is_file()
    assert (out / "missing_rate_customers.csv").is_file()
    assert (out / "run_metadata.json").is_file()


def test_reference_manifest_and_unsafe_names(tmp_path):
    manifest = load_manifest(REFERENCE)
    assert manifest["baseline"] == "transit0"
    assert [item["name"] for item in manifest["scenarios"]] == [
        "transit0",
        "transit1",
        "transit2",
        "cap8",
        "cap4",
    ]
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"baseline": "a", "baseline": "b", "scenarios": []}\n', encoding="utf-8")
    with pytest.raises(ValueError):
        load_manifest(duplicate)
    device = tmp_path / "device.json"
    device.write_text(json.dumps({"baseline": "CON", "scenarios": [{"name": "CON"}]}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_manifest(device)


def test_two_scenarios_publish_together(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "baseline": "base",
                "scenarios": [
                    {"name": "base", "config": {"time_limit_s": 30}},
                    {"name": "also", "config": {"time_limit_s": 30, "transit_days": 0}},
                ],
            }
        ),
        encoding="utf-8",
    )
    out = tmp_path / "scenarios"
    comparison = run_scenarios(SAMPLE, manifest, out)
    assert set(comparison["scenario"]) == {"base", "also"}
    assert comparison["assumed_unapproved"].all()
    assert comparison["scope_comparable"].all()
    assert float(comparison.loc[comparison["scenario"] == "also", "cost_delta_vs_baseline"].iloc[0]) == 0
    assert (out / "scenario_comparison.csv").is_file()
    assert (out / "base" / "run_metadata.json").is_file()
    assert (out / "also" / "trips.csv").is_file()
    with pytest.raises(FileExistsError):
        run_scenarios(SAMPLE, manifest, out)
