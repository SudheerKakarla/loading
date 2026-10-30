"""Small frames for trip-planning tests. Numbers in the tests are the oracle."""

from __future__ import annotations

import pandas as pd

from config import TripConfig


def trucks(*rows: dict) -> pd.DataFrame:
    if not rows:
        rows = (
            {"truck_type": "4W", "capacity_kg": 1500, "capacity_m3": 4},
            {"truck_type": "6W", "capacity_kg": 5000, "capacity_m3": 25},
            {"truck_type": "10W", "capacity_kg": 15000, "capacity_m3": 35},
        )
    return pd.DataFrame(rows)


def customer(ship_to, province="Bangkok", group="FOOD", service="FTL", sla_days=5, name=""):
    return {
        "ship_to": ship_to,
        "province": province,
        "group": group,
        "service": service,
        "sla_days": sla_days,
        "name": name or ship_to,
    }


def customers(*rows) -> pd.DataFrame:
    return pd.DataFrame(rows)


def order(order_id, ship_to, doc_date, rdd, cases, kg, m3) -> dict:
    return {
        "order_id": order_id,
        "ship_to": ship_to,
        "doc_date": doc_date,
        "rdd": rdd,
        "cases": cases,
        "weight_kg": kg,
        "volume_m3": m3,
    }


def orders(*rows) -> pd.DataFrame:
    return pd.DataFrame(rows)


def rate(ship_to, **fields) -> dict:
    row = {
        "ship_to": ship_to,
        "trip_4w": "",
        "trip_6w": "",
        "trip_10w": "",
        "trip_18w": "",
        "handling_4w": "",
        "handling_6w": "",
        "handling_10w": "",
        "handling_18w": "",
        "ltl_mt": "",
        "ltl_gt": "",
        "multidrop": "",
        "service": "",
    }
    row.update(fields)
    return row


def rates(*rows) -> pd.DataFrame:
    return pd.DataFrame(rows)


def overrides(*rows) -> pd.DataFrame:
    return pd.DataFrame(rows)


def cfg(**fields) -> TripConfig:
    return TripConfig(**fields)
