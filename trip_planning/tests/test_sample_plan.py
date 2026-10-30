"""The checked-in sample is small enough to recompute by hand."""

from pathlib import Path

import pytest

from plan_trips import main, plan
from reporting import summary_map
from shipments import read_inputs

SAMPLE = Path(__file__).resolve().parents[1] / "inputs" / "sample"
AUGUST = Path(__file__).resolve().parents[1] / "outputs" / "va05_aug2025"


def test_sample_matches_the_hand_total(tmp_path):
    tables = read_inputs(SAMPLE)
    result = plan(tables.orders, tables.customers, tables.rates, tables.trucks, overrides=tables.overrides)
    assert result.ok, result.errors
    # C100 FTL 4W = 1,000 + 50 handling = 1,050. C200 LTL case = 20 * 8 = 160.
    # Together, max(1,000, 900) + 50 = 1,050, which saves the 160 case bill.
    # C300 case = (5 + 1) * 15 = 90. C500 case = 2 * 12 = 24.
    # Total = 1,050 + 90 + 24 = 1,164. Dateable solos = 1,050 + 160 + 90 + 24 = 1,324.
    assert result.total_cost == 1164
    assert result.baseline_cost == 1324
    assert result.n_shipments == 5
    values = summary_map(result)
    assert values["saving_thb"] == "160"
    assert values["input_cases"] == "47"
    assert values["input_kg"] == "1770"
    assert values["input_m3"] == "4.3"
    assert values["planned_cases"] == "38"
    assert values["excluded_cases"] == "9"
    assert float(values["planned_kg"]) + float(values["excluded_kg"]) == 1770
    assert abs(float(values["planned_m3"]) + float(values["excluded_m3"]) - 4.3) < 1e-6
    shared = result.trips[result.trips["shipment_ids"].str.contains("shp_C100")]
    assert len(shared) == 1
    assert shared.iloc[0]["truck_type"] == "4W"
    assert shared.iloc[0]["cost"] == 1050
    assert shared.iloc[0]["price_basis"] == "truck"
    assert values["solver_status"] == "optimal"
    assert float(values["solver_gap_max"]) == 0
    reasons = set(result.exclusions["reason"])
    assert reasons == {"no_rate", "zero_quantity", "no_master_customer", "past_due"}
    flags = set(zip(result.flags["flag"], result.flags["key"]))
    assert ("service_conflict", "C500") in flags
    assert ("capacity_suspicious", "6W") in flags
    assert ("truck_without_capacity", "18W") in flags
    assert ("rdd_tighter_than_sla", "O9") in flags
    # Penalty is the sum of the four dateable solos plus 1, paid once for the past-due shipment.
    assert result.penalty_total == 1325

    exit_code = main(["--inputs", str(SAMPLE), "--out", str(tmp_path), "--time-limit", "30"])
    assert exit_code == 0
    assert (tmp_path / "trips.csv").is_file()
    assert (tmp_path / "summary.csv").is_file()


@pytest.mark.skipif(
    not (AUGUST / "orders.csv").is_file(),
    reason="August VA05 extracts are not in trip_planning/outputs/va05_aug2025",
)
def test_august_extracts_when_present(tmp_path):
    exit_code = main(["--inputs", str(AUGUST), "--out", str(tmp_path), "--time-limit", "60"])
    assert exit_code == 0
