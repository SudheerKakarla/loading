"""Cost rules. Each expected number is the arithmetic in the comment above it."""

from types import SimpleNamespace

import pandas as pd

from baseline_cost import RateCard, TruckType, build_baseline, parse_rates
from config import TripConfig
from helpers import customer, customers, order, orders, rate, rates, trucks
from pricing import solo_quote, trip_cost
from shipments import build_shipments

TRUCKS = [
    TruckType("4W", 1500, 4),
    TruckType("6W", 5000, 25),
    TruckType("10W", 15000, 35),
    TruckType("18W", None, None),
]


def _card(ship_to, trip, handling=None, ltl_mt=None, ltl_gt=None):
    return RateCard(
        ship_to,
        dict(trip),
        dict(handling or {}),
        ltl_mt,
        ltl_gt,
    )


def _stop(ship_to, service, cases=1):
    return SimpleNamespace(ship_to=ship_to, service=service, cases=cases)


def test_single_ship_to_is_trip_plus_handling():
    # 1,000 trip + 100 handling = 1,100.
    card = _card("A", {"4W": 1000}, {"4W": 100})
    quote = solo_quote("FTL", 1, 100, 1, card, TRUCKS)
    assert quote is not None
    assert quote.cost == 1100
    assert quote.price_basis == "truck"
    priced = trip_cost([_stop("A", "FTL")], "4W", {"A": card}, TripConfig())
    assert priced == (1100, "truck")


def test_blank_trip_rate_is_not_offered():
    # 4W is 0, so it is blank. 800 kg fits a 4W, but the only offer is 6W at 2,000.
    frame = rates(rate("A", trip_4w=0, trip_6w=2000))
    card = parse_rates(frame)["A"]
    assert "4W" not in card.trip
    quote = solo_quote("FTL", 1, 800, 1, card, TRUCKS)
    assert quote is not None
    assert quote.cost == 2000
    assert quote.fleet is not None
    assert quote.fleet.counts == (("6W", 1),)


def test_truck_choice_prefers_one_6w_over_two_4w():
    # 2,500 kg does not fit one 4W (1,500). Two 4W cost 2 * 1,000 = 2,000.
    # One 6W costs 1,500, which is cheaper, so the fleet is one 6W.
    card = _card("A", {"4W": 1000, "6W": 1500})
    quote = solo_quote("FTL", 1, 2500, 1, card, TRUCKS)
    assert quote is not None
    assert quote.cost == 1500
    assert quote.fleet is not None
    assert quote.fleet.counts == (("6W", 1),)


def test_volume_binds_before_weight():
    # 100 kg fits one 4W, but 10 m3 needs ceil(10 / 4) = 3 of them: 3 * 1,000 = 3,000.
    # One 6W holds 25 m3 and costs 1,500, so volume chooses the 6W.
    card = _card("A", {"4W": 1000, "6W": 1500})
    quote = solo_quote("FTL", 1, 100, 10, card, TRUCKS)
    assert quote is not None
    assert quote.cost == 1500
    assert quote.fleet is not None
    assert quote.fleet.counts == (("6W", 1),)


def test_multi_drop_is_max_trip_plus_handling():
    # per drop: max(1,000, 800) + 100 + 50 = 1,150.
    # per truck: max(1,000, 800) + max(100, 50) = 1,100.
    cards = {
        "A": _card("A", {"4W": 1000}, {"4W": 100}),
        "B": _card("B", {"4W": 800}, {"4W": 50}),
    }
    members = [_stop("A", "FTL"), _stop("B", "FTL")]
    assert trip_cost(members, "4W", cards, TripConfig(handling_basis="per_drop")) == (1150, "truck")
    assert trip_cost(members, "4W", cards, TripConfig(handling_basis="per_truck")) == (1100, "truck")


def test_ltl_cheaper_of_two():
    # 100 cases * 10 = 1,000, which is below the 1,500 truck, so the basis is case.
    cheap = _card("A", {"4W": 1500}, ltl_mt=10)
    quote = solo_quote("LTL", 100, 100, 1, cheap, TRUCKS)
    assert quote is not None
    assert (quote.cost, quote.price_basis) == (1000, "case")
    # 100 cases * 20 = 2,000, which is above 1,500, so the basis is truck.
    dear = _card("A", {"4W": 1500}, ltl_mt=20)
    quote = solo_quote("LTL", 100, 100, 1, dear, TRUCKS)
    assert quote is not None
    assert (quote.cost, quote.price_basis) == (1500, "truck")


def test_ftl_rider_adds_nothing_for_the_ltl_cases():
    # Truck price is max(1,000, 400) = 1,000. The LTL case bill would be 100 * 50 = 5,000
    # and is not added, because an FTL shipment is on the truck.
    cards = {
        "A": _card("A", {"4W": 1000}),
        "B": _card("B", {"4W": 400}, ltl_mt=50),
    }
    members = [_stop("A", "FTL", cases=1), _stop("B", "LTL", cases=100)]
    assert trip_cost(members, "4W", cards, TripConfig()) == (1000, "truck")


def test_baseline_columns_and_three_totals():
    # FTL fleet = 1,000. LTL case = 10 * 1 = 10, LTL fleet = 500.
    # All trips = 1,000 + 500 = 1,500.
    # LTL-per-case mix = 1,000 + 10 = 1,010.
    # Cheaper of two = 1,000 + 10 = 1,010, of which FTL is 1,000 and LTL is 10.
    frame = pd.DataFrame(
        [
            {
                "shipment_id": "s1",
                "ship_to": "A",
                "rdd": "2025-08-01",
                "cases": 1,
                "weight_kg": 100,
                "volume_m3": 1,
                "service": "FTL",
            },
            {
                "shipment_id": "s2",
                "ship_to": "B",
                "rdd": "2025-08-01",
                "cases": 1,
                "weight_kg": 100,
                "volume_m3": 1,
                "service": "LTL",
            },
        ]
    )
    cards = {
        "A": _card("A", {"4W": 1000}),
        "B": _card("B", {"4W": 500}, ltl_mt=10),
    }
    detail, summary = build_baseline(frame, cards, TRUCKS)
    assert list(detail["case_cost"])[1] == 10
    assert list(detail["fleet_cost"]) == [1000, 500]
    assert list(detail["baseline_cost"]) == [1000, 10]
    assert list(detail["basis"]) == ["truck", "case"]
    assert summary["all_trips_cost"] == 1500
    assert summary["ltl_case_mix_cost"] == 1010
    assert summary["cheaper_cost"] == 1010
    assert summary["cheaper_ftl_cost"] == 1000
    assert summary["cheaper_ltl_cost"] == 10
    assert summary["ltl_case_below_truck"] == 1
    assert summary["ltl_case_above_truck"] == 0


def test_zero_case_rate_does_not_hide_a_missing_truck_on_an_ftl_row():
    # The helper above is the pricing oracle. This one checks the shipment filter
    # still plans an LTL row that has a case rate and no truck.
    built = build_shipments(
        orders(order("L1", "L", "2025-08-01", "2025-08-05", 4, 100, 1)),
        customers(customer("L", service="LTL")),
        rates(rate("L", ltl_gt=3)),
        trucks(),
    )
    assert built.exclusions.empty
    assert list(built.shipments["service"]) == ["LTL"]
