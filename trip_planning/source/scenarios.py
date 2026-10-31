"""Named what-if runs of the existing planner.

Each scenario starts from TripConfig defaults and fresh copies of the inputs.
Nothing in a manifest is an approval. The set is published together or not at all.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

import pandas as pd

from config import TRUCK_TYPES, TripConfig, config_field_names, norm_id
from plan_trips import plan
from provenance import file_sha256, new_run_id, table_sha256, utc_now
from publication import publish_new_directory, remove_tree, staging_dir
from reporting import write_plan
from shipments import RECOGNIZED_INPUTS, read_inputs

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_DEVICES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
_SCENARIO_KEYS = {"name", "config", "truck_overrides", "assumptions"}
_ROOT_KEYS = {"baseline", "scenarios"}


def run_scenarios(inputs: Path, manifest_path: Path, out_dir: Path) -> pd.DataFrame:
    """Solve every manifest entry and publish the comparison into a new directory."""
    destination = Path(out_dir)
    if destination.exists():
        raise FileExistsError(f"Refusing to overwrite {destination}.")
    before = _recognized_hashes(inputs)
    manifest_hash = file_sha256(manifest_path)
    manifest = load_manifest(manifest_path)
    tables = read_inputs(inputs)
    prepared = []
    for entry in manifest["scenarios"]:
        prepared.append(_prepare(entry, tables))
    staging = staging_dir(destination)
    remove_tree(staging)
    staging.mkdir(parents=True)
    try:
        results = []
        for entry, cfg, trucks in prepared:
            result = plan(
                tables.orders.copy(deep=True),
                tables.customers.copy(deep=True),
                tables.rates.copy(deep=True),
                trucks,
                cfg,
                None if tables.overrides is None else tables.overrides.copy(deep=True),
                planning_calendar=None if tables.planning_calendar is None else tables.planning_calendar.copy(deep=True),
                delivery_date_overrides=(
                    None
                    if tables.delivery_date_overrides is None
                    else tables.delivery_date_overrides.copy(deep=True)
                ),
                weekly_calendar=None if tables.weekly_calendar is None else tables.weekly_calendar.copy(deep=True),
            )
            if not result.ok:
                raise ValueError(f"Scenario {entry['name']} failed its checks: " + "; ".join(result.errors))
            result.metadata["scenario"] = entry["name"]
            result.metadata["assumptions"] = list(entry["assumptions"])
            result.metadata["approval"] = "assumed_unapproved"
            scenario_dir = staging / entry["name"]
            scenario_dir.mkdir()
            write_plan(result, scenario_dir)
            results.append((entry, result))
        after = _recognized_hashes(inputs)
        if before != after or file_sha256(manifest_path) != manifest_hash:
            raise ValueError("Inputs or the manifest changed while the scenarios were running.")
        comparison = _comparison(results, manifest["baseline"])
        comparison.to_csv(staging / "scenario_comparison.csv", index=False)
        metadata = {
            "run_id": new_run_id(),
            "created_utc": utc_now(),
            "approval": "assumed_unapproved",
            "baseline": manifest["baseline"],
            "manifest_sha256": manifest_hash,
            "input_files": before,
            "scenarios": [
                {
                    "name": entry["name"],
                    "assumptions": list(entry["assumptions"]),
                    "config": entry["config"],
                    "truck_overrides": entry["truck_overrides"],
                    "solver_status": _metric(result, "solver_status"),
                    "solver_gap_max": _metric(result, "solver_gap_max"),
                    "total_cost": result.total_cost,
                }
                for entry, result in results
            ],
            "comparison_sha256": table_sha256(comparison),
        }
        (staging / "scenario_metadata.json").write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        publish_new_directory(staging, destination)
    except Exception:
        remove_tree(staging)
        raise
    return comparison


def load_manifest(path: Path) -> dict:
    text = Path(path).read_text(encoding="utf-8-sig")
    try:
        data = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Scenario manifest is not valid JSON: {exc}.") from exc
    _reject_nonfinite(data, "manifest")
    if not isinstance(data, dict):
        raise ValueError("Scenario manifest must be a JSON object.")
    unknown = set(data) - _ROOT_KEYS
    if unknown or "baseline" not in data or "scenarios" not in data:
        raise ValueError("Scenario manifest requires baseline and scenarios, and no other keys.")
    scenarios = data["scenarios"]
    if not isinstance(scenarios, list) or not scenarios:
        raise ValueError("Scenario manifest needs a nonempty scenarios array.")
    names = []
    cleaned = []
    for entry in scenarios:
        cleaned.append(_clean_entry(entry))
        names.append(cleaned[-1]["name"])
    folded = [name.casefold() for name in names]
    if len(folded) != len(set(folded)):
        raise ValueError("Scenario names must be unique, ignoring case.")
    baseline = data["baseline"]
    if not isinstance(baseline, str) or baseline not in names:
        raise ValueError("baseline must be the exact name of one scenario.")
    return {"baseline": baseline, "scenarios": cleaned}


def _clean_entry(entry: object) -> dict:
    if not isinstance(entry, dict):
        raise ValueError("Each scenario must be an object.")
    unknown = set(entry) - _SCENARIO_KEYS
    if unknown or "name" not in entry:
        raise ValueError("Each scenario requires name and may include config, truck_overrides, and assumptions.")
    name = entry["name"]
    if not isinstance(name, str) or not _NAME.fullmatch(name) or name.upper() in _DEVICES:
        raise ValueError(f"Unsafe scenario name {name!r}.")
    config = entry.get("config", {})
    overrides = entry.get("truck_overrides", [])
    assumptions = entry.get("assumptions", [])
    if not isinstance(config, dict) or not isinstance(overrides, list) or not isinstance(assumptions, list):
        raise ValueError(f"Scenario {name} has a config, truck_overrides, or assumptions of the wrong type.")
    if any(not isinstance(item, str) for item in assumptions):
        raise ValueError(f"Scenario {name} assumptions must be strings.")
    unknown_config = set(config) - config_field_names()
    if unknown_config:
        raise ValueError(f"Scenario {name} has unknown config keys: " + ", ".join(sorted(unknown_config)))
    TripConfig(**config)
    return {
        "name": name,
        "config": config,
        "truck_overrides": [_clean_override(item, name) for item in overrides],
        "assumptions": assumptions,
    }


def _clean_override(item: object, scenario: str) -> dict:
    if not isinstance(item, dict):
        raise ValueError(f"Scenario {scenario} has a truck override that is not an object.")
    unknown = set(item) - {"truck_type", "capacity_kg", "capacity_m3"}
    if unknown or "truck_type" not in item:
        raise ValueError(f"Scenario {scenario} truck override has unexpected keys.")
    if "capacity_kg" not in item and "capacity_m3" not in item:
        raise ValueError(f"Scenario {scenario} truck override must set a capacity.")
    truck_type = str(item["truck_type"]).strip().upper()
    if truck_type not in TRUCK_TYPES:
        raise ValueError(f"Scenario {scenario} truck override uses unsupported type {truck_type}.")
    cleaned = {"truck_type": truck_type}
    for field_name in ("capacity_kg", "capacity_m3"):
        if field_name not in item:
            continue
        value = item[field_name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or float(value) <= 0:
            raise ValueError(f"Scenario {scenario} {field_name} must be a finite number > 0.")
        cleaned[field_name] = float(value)
    return cleaned


def _prepare(entry: dict, tables) -> tuple[dict, TripConfig, pd.DataFrame]:
    trucks = tables.trucks.copy(deep=True)
    known = {norm_id(value).upper() for value in trucks.get("truck_type", [])}
    for override in entry["truck_overrides"]:
        if override["truck_type"] not in known:
            raise ValueError(
                f"Scenario {entry['name']} overrides {override['truck_type']}, which is not in trucks.csv."
            )
        mask = trucks["truck_type"].map(lambda value: norm_id(value).upper()) == override["truck_type"]
        for field_name in ("capacity_kg", "capacity_m3"):
            if field_name in override:
                trucks.loc[mask, field_name] = override[field_name]
    return entry, TripConfig(**entry["config"]), trucks


def _comparison(results: list[tuple[dict, object]], baseline_name: str) -> pd.DataFrame:
    by_name = {entry["name"]: result for entry, result in results}
    baseline = by_name[baseline_name]
    rows = []
    for entry, result in results:
        scope_ok, explanation = _same_scope(result, baseline)
        delta = ""
        saving = ""
        percent = ""
        if scope_ok:
            delta = result.total_cost - baseline.total_cost
            saving = baseline.total_cost - result.total_cost
            percent = "" if abs(baseline.total_cost) <= 1e-9 else 100.0 * saving / baseline.total_cost
        values = _summary(result)
        base_values = _summary(baseline)
        rows.append(
            {
                "scenario": entry["name"],
                "assumed_unapproved": True,
                "total_cost": result.total_cost,
                "baseline_cost": result.baseline_cost,
                "saving_thb": values.get("saving_thb", ""),
                "saving_percent": values.get("saving_percent", ""),
                "matched_solo_baseline_cost": _matched_solo(result),
                "matched_solo_saving_thb": _matched_solo(result) - float(result.total_cost),
                "scope_comparable": scope_ok,
                "scope_explanation": explanation,
                "cost_delta_vs_baseline": delta,
                "saving_vs_baseline_thb": saving,
                "saving_vs_baseline_percent": percent,
                "input_cases": values.get("input_cases", ""),
                "planned_cases": values.get("planned_cases", ""),
                "excluded_cases": values.get("excluded_cases", ""),
                "input_kg": values.get("input_kg", ""),
                "planned_kg": values.get("planned_kg", ""),
                "excluded_kg": values.get("excluded_kg", ""),
                "input_m3": values.get("input_m3", ""),
                "planned_m3": values.get("planned_m3", ""),
                "excluded_m3": values.get("excluded_m3", ""),
                "trips": values.get("trips", ""),
                "trips_multi_shipment": values.get("trips_multi_shipment", ""),
                "trucks": values.get("trucks", ""),
                "solver_status": values.get("solver_status", ""),
                "solver_gap_max": values.get("solver_gap_max", ""),
                "changed_ship_dates": _changed_dates(result, baseline),
                "truck_mix_changed": _truck_mix(values) != _truck_mix(base_values),
            }
        )
    return pd.DataFrame(rows)


def _same_scope(result, baseline) -> tuple[bool, str]:
    left = _signature(result)
    right = _signature(baseline)
    if left.keys() != right.keys():
        return False, "Planned order ids differ from the named baseline."
    for order_id, payload in left.items():
        if payload != right[order_id]:
            return False, "Effective delivery dates or quantities differ from the named baseline."
    return True, "Same served orders, effective dates, and quantities."


def _signature(result) -> dict[str, tuple]:
    signature = {}
    if result.order_scope.empty:
        return signature
    for row in result.order_scope.to_dict("records"):
        signature[str(row["order_id"])] = (
            str(row["effective_rdd"])[:10],
            round(float(row["cases"]), 6),
            round(float(row["kg"]), 6),
            round(float(row["m3"]), 6),
        )
    return signature


def _changed_dates(result, baseline) -> int:
    left = _dates(result)
    right = _dates(baseline)
    return sum(1 for order_id in left.keys() & right.keys() if left[order_id] != right[order_id])


def _dates(result) -> dict[str, str]:
    if result.order_scope.empty:
        return {}
    return {str(row["order_id"]): str(row["ship_date"]) for row in result.order_scope.to_dict("records")}


def _matched_solo(result) -> float:
    """Solo price of the shipments this scenario actually put on a trip.

    Unplanned dateable shipments stay in ``baseline_cost``. They are not added
    here, so a partial plan does not look as if the leftover demand was served.
    """
    if result.trip_shipments.empty or result.baseline_shipments.empty:
        return 0.0
    served = set(result.trip_shipments["shipment_id"].astype(str))
    detail = result.baseline_shipments
    chosen = detail[detail["shipment_id"].astype(str).isin(served)]
    if chosen.empty:
        return 0.0
    costs = pd.to_numeric(chosen["baseline_cost"], errors="coerce").dropna()
    return float(costs.sum())


def _truck_mix(values: dict[str, str]) -> tuple:
    return tuple(values.get(f"trucks_{truck_type}", "") for truck_type in TRUCK_TYPES)


def _summary(result) -> dict[str, str]:
    return dict(zip(result.summary["metric"].astype(str), result.summary["value"].astype(str)))


def _metric(result, name: str) -> str:
    return _summary(result).get(name, "")


def _recognized_hashes(folder: Path) -> dict[str, str]:
    found = {}
    for name in RECOGNIZED_INPUTS:
        path = Path(folder) / name
        if path.is_file():
            found[name] = file_sha256(path)
    return found


def _reject_duplicate_keys(pairs: list[tuple]) -> dict:
    seen = set()
    payload = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"Duplicate JSON key {key}.")
        seen.add(key)
        payload[key] = value
    return payload


def _reject_nonfinite(value: object, where: str) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"Nonfinite number in {where}.")
    if isinstance(value, dict):
        for key, item in value.items():
            _reject_nonfinite(item, f"{where}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_nonfinite(item, f"{where}[{index}]")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run named unapproved trip-planning scenarios.")
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--scenarios", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    try:
        args = parser.parse_args(argv)
        comparison = run_scenarios(args.inputs, args.scenarios, args.out)
    except (ValueError, FileNotFoundError, FileExistsError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"Scenarios: {len(comparison)}")
    print(f"Wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
