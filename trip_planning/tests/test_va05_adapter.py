"""Synthetic VA05 workbook. The August file is not required."""

from datetime import date

import pandas as pd
import pytest

openpyxl = pytest.importorskip("openpyxl")

from build_inputs import BUILDER_EXCLUSION_COLUMNS, build_contract
from va05_adapter import read_workbook


def _workbook(path, *, cases=10, conflict=False, sheets=True, formula=False):
    book = openpyxl.Workbook()
    va05 = book.active
    va05.title = "VA05"
    va05.append(
        [
            "Sales Document",
            "PO Number",
            "Document Date",
            "Delivery Date",
            "Ship-to",
            "Sold-to",
            "Total CTN Convert",
            "Weight",
            "Volume",
        ]
    )
    va05.append([100, 22, date(2025, 8, 1), date(2025, 8, 5), 200, 9, cases, 100, 1])
    va05.append([100, 21, date(2025, 8, 2), date(2025, 8, 5), 200, 9, 5, 50, 0.5])
    va05.append([100, None, date(2025, 8, 1), date(2025, 8, 6), 200, 9, 2, 20, 0.2])
    va05.append([300, None, date(2025, 8, 1), date(2025, 8, 5), 300, 8, 2, 10, 0.1])
    va05.append([9, None, date(2025, 8, 1), date(2025, 8, 5), 999, 1, 1, 1, 0.1])
    if conflict:
        va05["G3"] = 5
        va05["E3"] = 201
    if formula:
        va05["G2"] = "=10"
    if not sheets:
        book.save(path)
        return
    master = book.create_sheet("Master Customer")
    master.append(["Ship-to", "Sold-to", "Province", "Group", "Service Type", "SLA", "Customer Name", "Segment"])
    master.append([200, 9, "Bangkok", "FOOD", "LTL", "D+2", "Store", "Core"])
    rates = book.create_sheet("TPT Rate Card")
    rates.append(
        ["Ship-to", "Province", "Group", "Service", "Trip 4W", "Handling 4W", "LTL MT", "Remarks", "Distance km"]
    )
    rates.append([200, "Bangkok", "FOOD", "FTL", 1000, 50, 0, None, 12])
    rates.append([300, "Chiang Mai", "FOOD", "LTL", 0, None, 8, "Deliver to store", None])
    pivot = book.create_sheet("Pivot_Summary")
    pivot.append(["Type", "KG", "M3"])
    pivot.append(["4W", 1500, 4])
    pivot.append(["6W", 5000, 25])
    pivot.append(["10W", 15000, 35])
    book.save(path)


def test_adapter_aggregates_lines_and_preserves_service_provenance(tmp_path):
    path = tmp_path / "va05.xlsx"
    _workbook(path)
    extract = read_workbook(path)
    orders = extract.orders.set_index("order_id")
    # 10 + 5 cases, and the later document date wins.
    assert orders.loc["100@2025-08-05", "cases"] == 15
    assert orders.loc["100@2025-08-05", "doc_date"] == "2025-08-02"
    assert orders.loc["100@2025-08-05", "po_numbers"] == "21;22"
    assert "100@2025-08-06" in orders.index
    customer = extract.customers.set_index("ship_to")
    assert customer.loc["200", "service"] == "FTL"
    assert customer.loc["200", "service_master"] == "LTL"
    assert customer.loc["200", "service_contract"] == "FTL"
    assert customer.loc["200", "service_conflict"] == "true"
    assert customer.loc["200", "sla_days"] == "2"
    assert customer.loc["300", "in_master"] == "false"
    rates = extract.rates.set_index("ship_to")
    assert rates.loc["200", "trip_4w"] == 1000
    assert rates.loc["200", "ltl_mt"] == ""
    assert rates.loc["300", "trip_4w"] == ""
    assert rates.loc["300", "ltl_mt"] == 8
    assert rates.loc["300", "multidrop"] == "Deliver to store"
    assert rates.loc["200", "distance_km"] == 12
    trucks = extract.trucks.set_index("truck_type")
    assert trucks.loc["4W", "capacity_kg"] == 1500
    assert trucks.loc["18W", "capacity_kg"] == ""
    assert extract.diagnostics["data_orders"] == 4
    assert extract.diagnostics["data_service_conflicts"] == 1
    assert extract.diagnostics["data_zero_trip_rates_blanked"] == 1
    assert extract.diagnostics["data_zero_case_rates_blanked"] == 1


def test_builder_writes_nine_files_and_can_replace_them(tmp_path):
    path = tmp_path / "va05.xlsx"
    _workbook(path)
    out = tmp_path / "build"
    build_contract(path, out)
    names = {
        "orders.csv",
        "customers.csv",
        "rates.csv",
        "trucks.csv",
        "shipments.csv",
        "exclusions.csv",
        "issues.csv",
        "baseline_shipments.csv",
        "baseline_summary.csv",
    }
    assert {item.name for item in out.iterdir()} == names
    excluded = pd.read_csv(out / "exclusions.csv")
    assert list(excluded.columns) == BUILDER_EXCLUSION_COLUMNS
    assert set(excluded["reason"]) == {"no_master_customer"}
    summary = pd.read_csv(out / "baseline_summary.csv")
    values = dict(zip(summary["metric"], summary["value"].astype(str)))
    assert values["data_orders"] == "4"
    assert "data_customers_not_in_master" in values
    build_contract(path, out)
    assert (out / "orders.csv").is_file()


def test_bad_lines_and_missing_sheets_fail(tmp_path):
    negative = tmp_path / "negative.xlsx"
    _workbook(negative, cases=-1)
    with pytest.raises(ValueError, match="negative"):
        read_workbook(negative)
    conflict = tmp_path / "conflict.xlsx"
    _workbook(conflict, conflict=True)
    with pytest.raises(ValueError, match="conflicting"):
        read_workbook(conflict)
    missing = tmp_path / "missing.xlsx"
    _workbook(missing, sheets=False)
    with pytest.raises(ValueError, match="missing sheet"):
        read_workbook(missing)
    formula = tmp_path / "formula.xlsx"
    _workbook(formula, formula=True)
    with pytest.raises(ValueError, match="cached"):
        read_workbook(formula)
    out = tmp_path / "not-published"
    with pytest.raises(ValueError):
        build_contract(negative, out)
    assert not out.exists()
