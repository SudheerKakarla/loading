"""Synthetic checks for conditional truck caps and class consolidation.

One-truck cases give Class R a fill premium (skuPriorityIncentive) so the
solver loads that pallet and the cap is the binding rule. With a plain fill
objective and spare fillers, the solver would leave Class R off to keep the
larger nominal cap. That is the existing "pallets may stay unloaded" rule.
"""

from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from loadmax.container_loading_highs import ContainerLoadingOptimizerHiGHS, LoadObj
from loadmax.data_reader import (
    _normalize_im_input,
    capacity_infeasible_message,
    read_im_input_csv,
    resolve_capacity_classes,
)
from loadmax.results_summary import build_container_results_summary


ROOT = Path(__file__).resolve().parents[1]


def make_instance(
    *,
    n_trucks: int = 2,
    n_pos: int = 3,
    n_layers: int = 2,
    fill_demand: int = 4,
    r_demand: int = 0,
    t_demand: int = 0,
    r_partial: bool = False,
    priority_incentive: float = 1.0,
    priority_class_r: bool = False,
    priority_class_t: bool = False,
    partial_fraction: float = 1.0,
    pallet_weight: float = 10.0,
    max_wt: float = 100000.0,
    truck_priority: float = 10.0,
    priorities: dict | None = None,
    cap_r: int | None = None,
    cap_t: int | None = None,
    truck_caps: dict | None = None,
    truck_weights: dict | None = None,
    compact: bool = False,
    first_plt_n: int = 0,
    consolidation_weight: float | None = None,
    mix_weight: float | None = None,
    mode: str | None = None,
    include_partial: bool = False,
):
    trucks = list(range(1, n_trucks + 1))
    positions = list(range(1, n_pos + 1))
    layers = list(range(1, n_layers + 1))
    full = ["FILL"]
    prtl: list[str] = []
    if r_demand and not r_partial:
        full.append("CLASS_R")
    if r_demand and r_partial:
        prtl.append("CLASS_R")
    if t_demand:
        full.append("CLASS_T")
    if include_partial and "PART" not in prtl:
        prtl.append("PART")
    pallets = full + prtl
    materials = {
        "FILL": "MAT_F",
        "CLASS_R": "MAT_R",
        "CLASS_T": "MAT_T",
        "PART": "MAT_P",
    }
    demand_full = {"FILL": fill_demand}
    demand_prtl = {}
    if "CLASS_R" in full:
        demand_full["CLASS_R"] = r_demand
    if "CLASS_T" in full:
        demand_full["CLASS_T"] = t_demand
    if "CLASS_R" in prtl:
        demand_prtl["CLASS_R"] = r_demand
    if "PART" in prtl:
        demand_prtl["PART"] = 0
    sku = {}
    if priority_class_r:
        sku["MAT_R"] = 1
    if priority_class_t:
        sku["MAT_T"] = 1
    params = {
        "marketID": "TEST",
        "containerList": trucks,
        "layerList": layers,
        "positionList": positions,
        "sectionList": [],
        "door_posn": [],
        "frontPositionList": [],
        "rearPositionList": [],
        "leftPositionList": [],
        "rightPositionList": [],
        "positionGroupBalanceCoefficientDict": {},
        "positionSequenceDict": {p: p for p in positions},
        "fullPalletList": full,
        "partialPalletList": prtl,
        "materialList": sorted({materials[p] for p in pallets}),
        "palletMaterialDict": {p: materials[p] for p in pallets},
        "palletWeightDict": {p: pallet_weight for p in pallets},
        "palletHeightDict": {p: 10.0 for p in pallets},
        "palletNumCasesDict": {p: 10 for p in pallets},
        "palletBatchDict": {p: "B1" for p in pallets},
        "fullPalletDemandByMaterialBatch": demand_full,
        "partialPalletDemandByMaterialBatch": demand_prtl,
        "partialFractionValueForPartialPallet": {p: partial_fraction for p in prtl},
        "max_prior_full": {p: 0 for p in full},
        "max_prior_prtl": {p: 0 for p in prtl},
        "container_priority": (
            dict(priorities) if priorities is not None else {c: truck_priority for c in trucks}
        ),
        "priorityIncentive": priority_incentive,
        "skuPriorityIncentive": sku,
        "max_wt": max_wt,
        "max_front_wt": None,
        "max_rear_wt": None,
        "max_plt_n": n_pos,
        "max_lyr_n": n_layers,
        "containerHeight": 200,
        "containerHeightAtDoor": 200,
        "load_allow_ht": 0,
        "unique_sku_n": 100,
        "sku_penalty": 0,
        "front_to_back_tolerance": 0,
        "side_to_side_tolerance": 0,
        "first_plt_n": first_plt_n,
        "enforce_compact_packing": "Yes" if compact else "No",
    }
    if cap_r is not None:
        params["reducedCapMaxPallets"] = cap_r
    if cap_t is not None:
        params["tempControlledMaxPallets"] = cap_t
    if r_demand:
        params["palletReducedCapClassDict"] = {"CLASS_R": "Yes"}
    if t_demand:
        params["palletRefrigeratedDict"] = {"CLASS_T": "Yes"}
    if truck_caps is not None:
        params["containerMaxPallets"] = dict(truck_caps)
    if truck_weights is not None:
        params["containerMaxWt"] = dict(truck_weights)
    if consolidation_weight is not None:
        params["reducedCapConsolidationWeight"] = consolidation_weight
    if mix_weight is not None:
        params["reducedCapTempMixWeight"] = mix_weight
    if mode is not None:
        params["reducedCapConsolidationMode"] = mode
    return params


def solve(params, time_limit: float = 15.0):
    opt = ContainerLoadingOptimizerHiGHS(
        inputParams=params,
        run_ocean=False,
        time_limit=time_limit,
        gap=0.0,
        sku_penalty_factor=0.0,
        vdr_stacking_rules=False,
    )
    opt.build_model()
    result = opt.solve()
    solution = opt.get_solution()
    instructions = opt.generate_loading_instructions() if result.get("has_solution") else []
    return opt, result, solution, instructions


def tally(instructions):
    rows = {}
    for item in instructions:
        row = rows.setdefault(item.container, {"n": 0, "r": 0, "t": 0})
        row["n"] += 1
        if item.palletID == "CLASS_R":
            row["r"] += 1
        if item.palletID == "CLASS_T":
            row["t"] += 1
    return rows


def _base_lists():
    return {
        "containerList": [1],
        "layerList": [1],
        "positionList": [1, 2],
        "fullPalletList": ["FILL"],
        "partialPalletList": [],
        "palletMaterialDict": {"FILL": 10},
        "fullPalletDemandByMaterialBatch": {"FILL": 1},
        "partialPalletDemandByMaterialBatch": {},
    }


class CapacityClassTests(unittest.TestCase):
    def test_t1_no_class_data_keeps_the_model(self):
        base = make_instance(fill_demand=3, n_trucks=2)
        flagged = make_instance(fill_demand=3, n_trucks=2)
        flagged["palletRefrigeratedDict"] = {"FILL": "Yes"}
        opt_a, result_a, _, instr_a = solve(base)
        opt_b, result_b, _, instr_b = solve(flagged)
        self.assertEqual(len(opt_a._reg), len(opt_b._reg))
        self.assertEqual(len(opt_a._cb._rows), len(opt_b._cb._rows))
        self.assertEqual(result_a["status"], 0, result_a["message"])
        self.assertEqual(result_b["status"], 0, result_b["message"])
        self.assertAlmostEqual(result_a["objective_value"], result_b["objective_value"], places=4)
        self.assertEqual(len(instr_a), len(instr_b))
        rows = build_container_results_summary(base, instr_a)
        self.assertTrue(rows)
        self.assertNotIn("Pallet Count", rows[0])

    def test_t2_cap_enforced_for_one_class_r_pallet(self):
        params = make_instance(
            n_trucks=1,
            fill_demand=10,
            r_demand=1,
            r_partial=True,
            partial_fraction=0.5,
            cap_r=3,
            priority_incentive=20,
            priority_class_r=True,
        )
        _, result, solution, instructions = solve(params)
        self.assertEqual(result["status"], 0, result["message"])
        rows = tally(instructions)
        self.assertEqual(rows[1]["r"], 1)
        self.assertEqual(rows[1]["n"], 3)
        self.assertEqual(solution["pallet_cap"][1], 3)
        self.assertEqual(solution["rc_present"][1], 1)
        summary = build_container_results_summary(params, instructions)
        self.assertEqual(summary[0]["Pallet Cap"], 3)
        self.assertEqual(summary[0]["Class R Pallets"], 1)

    def test_t3_cap_is_binary_not_proportional(self):
        caps = []
        for demand in (1, 30):
            params = make_instance(
                n_trucks=1,
                fill_demand=10,
                r_demand=demand,
                cap_r=4,
                priority_incentive=20,
                priority_class_r=True,
            )
            _, result, solution, instructions = solve(params)
            self.assertEqual(result["status"], 0, result["message"])
            rows = tally(instructions)
            self.assertEqual(rows[1]["n"], 4)
            self.assertGreaterEqual(rows[1]["r"], 1)
            self.assertEqual(solution["pallet_cap"][1], 4)
            caps.append(solution["pallet_cap"][1])
        self.assertEqual(caps, [4, 4])

    def test_t4_class_r_is_grouped_on_one_truck(self):
        params = make_instance(
            n_trucks=4,
            fill_demand=2,
            r_demand=3,
            cap_r=4,
        )
        _, result, solution, instructions = solve(params)
        self.assertEqual(result["status"], 0, result["message"])
        rows = tally(instructions)
        rc = [truck for truck, row in rows.items() if row["r"]]
        self.assertEqual(len(rc), 1)
        self.assertEqual(sum(row["r"] for row in rows.values()), 3)
        self.assertEqual(sum(solution["rc_present"].values()), 1)

    def test_t5_overflow_uses_the_minimum_number_of_trucks(self):
        params = make_instance(
            n_trucks=4,
            fill_demand=0,
            r_demand=10,
            cap_r=4,
        )
        _, result, _, instructions = solve(params)
        self.assertEqual(result["status"], 0, result["message"])
        rows = tally(instructions)
        rc_rows = [row for row in rows.values() if row["r"]]
        self.assertEqual(len(rc_rows), 3)
        self.assertTrue(all(row["n"] <= 4 for row in rc_rows))
        self.assertEqual(sum(row["r"] for row in rows.values()), 10)

    def test_t6_fillers_top_up_the_class_r_truck(self):
        params = make_instance(
            n_trucks=2,
            fill_demand=8,
            r_demand=2,
            cap_r=4,
        )
        _, result, _, instructions = solve(params)
        self.assertEqual(result["status"], 0, result["message"])
        rows = tally(instructions)
        rc_rows = [row for row in rows.values() if row["r"]]
        self.assertEqual(len(rc_rows), 1)
        self.assertEqual(rc_rows[0]["r"], 2)
        self.assertEqual(rc_rows[0]["n"], 4)
        self.assertEqual(sum(row["n"] for row in rows.values()), 10)

    def test_t7_class_r_and_class_t_are_separated(self):
        params = make_instance(
            n_trucks=3,
            n_pos=2,
            fill_demand=0,
            r_demand=2,
            t_demand=2,
            cap_r=4,
        )
        _, result, solution, instructions = solve(params)
        self.assertEqual(result["status"], 0, result["message"])
        self.assertEqual(sum(solution["rc_tc_mix"].values()), 0)
        rows = tally(instructions)
        self.assertEqual(sum(1 for row in rows.values() if row["r"]), 1)
        self.assertEqual(sum(1 for row in rows.values() if row["t"]), 1)
        self.assertEqual(sum(row["r"] + row["t"] for row in rows.values()), 4)

    def test_t8_one_truck_mixes_and_takes_the_lower_cap(self):
        params = make_instance(
            n_trucks=1,
            fill_demand=10,
            r_demand=1,
            t_demand=1,
            cap_r=4,
            cap_t=3,
            priority_incentive=20,
            priority_class_r=True,
            priority_class_t=True,
        )
        _, result, solution, instructions = solve(params)
        self.assertEqual(result["status"], 0, result["message"])
        rows = tally(instructions)
        self.assertEqual(rows[1]["n"], 3)
        self.assertEqual(rows[1]["r"], 1)
        self.assertEqual(rows[1]["t"], 1)
        self.assertEqual(solution["rc_tc_mix"][1], 1)
        self.assertEqual(solution["pallet_cap"][1], 3)
        summary = build_container_results_summary(params, instructions)
        self.assertEqual(summary[0]["Class Mix"], "Yes")

    def test_t9_each_truck_keeps_its_own_nominal_cap(self):
        params = make_instance(
            n_trucks=2,
            fill_demand=20,
            truck_caps={1: 2, 2: 3},
        )
        _, result, solution, instructions = solve(params)
        self.assertEqual(result["status"], 0, result["message"])
        rows = tally(instructions)
        self.assertEqual(rows[1]["n"], 2)
        self.assertEqual(rows[2]["n"], 3)
        self.assertEqual(solution["pallet_cap"][1], 2)
        self.assertEqual(solution["pallet_cap"][2], 3)

    def test_t10_compact_packing_with_an_odd_cap_does_not_crash(self):
        params = make_instance(
            n_trucks=1,
            n_pos=4,
            n_layers=1,
            fill_demand=6,
            r_demand=1,
            cap_r=3,
            compact=True,
            first_plt_n=2,
            priority_incentive=20,
            priority_class_r=True,
        )
        _, result, solution, instructions = solve(params)
        self.assertIn(result["status"], {0, 2})
        if result["status"] == 2:
            self.assertIn("INFEASIBLE", result["message"])
            self.assertIn("not relaxed", result["message"])
            return
        rows = tally(instructions)
        self.assertLessEqual(rows[1]["n"], 3)
        self.assertEqual(solution["pallet_cap"][1], 3)
        self.assertGreaterEqual(rows[1]["r"], 1)

    def test_hard_mode_limits_class_r_trucks(self):
        params = make_instance(
            n_trucks=4,
            fill_demand=0,
            r_demand=10,
            cap_r=4,
            consolidation_weight=0,
            mode="hard",
        )
        _, result, solution, instructions = solve(params)
        self.assertEqual(result["status"], 0, result["message"])
        self.assertEqual(sum(solution["rc_present"].values()), 3)
        self.assertEqual(sum(1 for item in instructions if item.palletID == "CLASS_R"), 10)
        message = capacity_infeasible_message(resolve_capacity_classes(params))
        self.assertIn("No fallback", message)

    def test_per_truck_weight_moves_pallets_off_the_light_truck(self):
        plain = make_instance(
            n_trucks=2,
            n_pos=2,
            n_layers=1,
            fill_demand=2,
            pallet_weight=100,
            max_wt=1000,
        )
        capped = make_instance(
            n_trucks=2,
            n_pos=2,
            n_layers=1,
            fill_demand=2,
            pallet_weight=100,
            max_wt=1000,
            truck_weights={1: 50, 2: 1000},
        )
        opt_plain, _, _, _ = solve(plain)
        opt_cap, result, _, instructions = solve(capped)
        self.assertEqual(len(opt_plain._reg), len(opt_cap._reg))
        self.assertEqual(len(opt_plain._cb._rows), len(opt_cap._cb._rows))
        self.assertEqual(result["status"], 0, result["message"])
        self.assertTrue(instructions)
        self.assertTrue(all(item.container == 2 for item in instructions))

    def test_penalty_uses_the_smallest_truck_priority(self):
        params = make_instance(
            n_trucks=2,
            n_pos=2,
            n_layers=1,
            fill_demand=0,
            r_demand=2,
            cap_r=1,
            priorities={1: 800, 2: 300},
        )
        plan = resolve_capacity_classes(params)
        self.assertEqual(plan.lambda_r, 150)
        self.assertEqual(plan.mu_mix, 75)
        _, result, _, instructions = solve(params)
        self.assertEqual(result["status"], 0, result["message"])
        rows = tally(instructions)
        self.assertEqual(sum(row["r"] for row in rows.values()), 2)
        self.assertEqual(len(rows), 2)

    def test_partial_fraction_keeps_the_penalty_below_the_partial(self):
        params = make_instance(
            n_trucks=2,
            n_pos=2,
            n_layers=1,
            fill_demand=0,
            r_demand=1,
            r_partial=True,
            partial_fraction=0.3,
            cap_r=1,
            priorities={1: 800, 2: 300},
        )
        plan = resolve_capacity_classes(params)
        self.assertAlmostEqual(plan.lambda_r, 45.0)
        self.assertAlmostEqual(plan.mu_mix, 22.5)

    def test_a_non_class_partial_does_not_shrink_the_penalty(self):
        params = make_instance(
            n_trucks=2,
            n_pos=2,
            n_layers=1,
            fill_demand=0,
            r_demand=1,
            cap_r=1,
            priorities={1: 800, 2: 300},
            include_partial=True,
            partial_fraction=0.01,
        )
        plan = resolve_capacity_classes(params)
        self.assertEqual(plan.lambda_r, 150)
        self.assertEqual(plan.mu_mix, 75)

    def test_unset_container_zero_does_not_shrink_the_penalty(self):
        params = make_instance(
            n_trucks=1,
            n_pos=2,
            n_layers=1,
            fill_demand=0,
            r_demand=1,
            cap_r=1,
            priorities={1: 800},
        )
        params["containerList"] = [0, 1]
        params["container_priority"] = {1: 800}
        _normalize_im_input(params)
        plan = resolve_capacity_classes(params)
        self.assertEqual(params["container_priority"][0], 0.5)
        self.assertEqual(plan.lambda_r, 400.0)
        self.assertAlmostEqual(plan.lambda_for(0), 0.25)
        self.assertEqual(plan.lambda_for(1), 400.0)
        self.assertAlmostEqual(plan.mu_for(0), 0.125)
        self.assertEqual(plan.mu_for(1), 200.0)

        direct = make_instance(
            n_trucks=1,
            n_pos=1,
            n_layers=1,
            fill_demand=0,
            r_demand=2,
            cap_r=1,
            priorities={1: 800},
        )
        direct["containerList"] = [0, 1]
        direct["container_priority"] = {1: 800}
        direct_plan = resolve_capacity_classes(direct)
        self.assertEqual(direct["container_priority"][0], 0.5)
        self.assertEqual(direct_plan.lambda_r, 400.0)
        self.assertAlmostEqual(direct_plan.lambda_for(0), 0.25)
        _, result, _, instructions = solve(direct)
        self.assertEqual(result["status"], 0, result["message"])
        loaded = {item.container for item in instructions if item.palletID == "CLASS_R"}
        self.assertEqual(loaded, {0, 1})

        explicit = make_instance(
            n_trucks=1,
            n_pos=2,
            n_layers=1,
            fill_demand=0,
            r_demand=1,
            cap_r=1,
            priorities={1: 800},
        )
        explicit["containerList"] = [0, 1]
        explicit["container_priority"] = {0: 0.5, 1: 800}
        _normalize_im_input(explicit)
        explicit_plan = resolve_capacity_classes(explicit)
        self.assertAlmostEqual(explicit_plan.lambda_r, 0.25)

    def test_hard_mode_counts_each_trucks_own_cap(self):
        params = make_instance(
            n_trucks=4,
            n_pos=4,
            n_layers=2,
            fill_demand=0,
            r_demand=10,
            cap_r=6,
            truck_caps={1: 3, 2: 3, 3: 3, 4: 8},
            mode="hard",
            consolidation_weight=0,
        )
        plan = resolve_capacity_classes(params)
        self.assertEqual(plan.truck_limit, 3)
        _, result, solution, instructions = solve(params)
        self.assertEqual(result["status"], 0, result["message"])
        self.assertEqual(sum(1 for item in instructions if item.palletID == "CLASS_R"), 10)
        self.assertEqual(sum(solution["rc_present"].values()), 3)

    def test_weight_utilization_uses_the_truck_cap(self):
        def row_for(container, weight):
            item = LoadObj()
            item.container = container
            item.position = 1
            item.weight = weight
            item.palletID = "FILL"
            return item

        shared = make_instance(n_trucks=2, n_pos=1, n_layers=1, max_wt=1000)
        shared_rows = build_container_results_summary(shared, [row_for(1, 100)])
        self.assertEqual(shared_rows[0]["Utilization (Weight)"], 10.0)

        capped = make_instance(
            n_trucks=2,
            n_pos=1,
            n_layers=1,
            max_wt=0,
            truck_weights={1: 100, 2: 500},
        )
        rows = build_container_results_summary(
            capped, [row_for(1, 100), row_for(2, 100)]
        )
        by_truck = {row["Container"]: row["Utilization (Weight)"] for row in rows}
        self.assertEqual(by_truck[1], 100.0)
        self.assertEqual(by_truck[2], 20.0)

    def test_t12_bad_input_is_rejected_on_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)

            def dump(name, extra):
                path = folder / name
                payload = _base_lists()
                payload.update(extra)
                with path.open("w", newline="", encoding="utf-8") as handle:
                    writer = csv.writer(handle)
                    for key, value in payload.items():
                        writer.writerow([key, repr(value)])
                return path

            with self.assertRaises(ValueError) as too_big:
                read_im_input_csv(dump("big.csv", {"reducedCapMaxPallets": 5}))
            self.assertIn("reducedCapMaxPallets", str(too_big.exception))

            with self.assertRaises(ValueError) as unknown_truck:
                read_im_input_csv(dump("truck.csv", {"containerMaxPallets": {99: 1}}))
            self.assertIn("unknown container", str(unknown_truck.exception))

            with self.assertRaises(ValueError) as unknown_pallet:
                read_im_input_csv(
                    dump("pallet.csv", {"palletReducedCapClassDict": {"NOPE": "Yes"}})
                )
            self.assertIn("unknown pallet", str(unknown_pallet.exception))

            with self.assertRaises(ValueError) as fraction:
                read_im_input_csv(dump("fraction.csv", {"reducedCapMaxPallets": 1.5}))
            self.assertIn("integer", str(fraction.exception))

            with self.assertRaises(ValueError) as zero:
                read_im_input_csv(dump("zero.csv", {"reducedCapMaxPallets": 0}))
            self.assertIn("positive integer", str(zero.exception))

            with self.assertRaises(ValueError) as negative:
                read_im_input_csv(dump("negative.csv", {"tempControlledMaxPallets": -5}))
            self.assertIn("positive integer", str(negative.exception))

            blank = make_instance()
            blank["reducedCapMaxPallets"] = ""
            self.assertIsNone(resolve_capacity_classes(blank).cap_r)

            with self.assertRaises(ValueError) as material:
                read_im_input_csv(dump("material.csv", {"reducedCapMaterialList": [999]}))
            self.assertIn("matches no pallet", str(material.exception))

            with self.assertRaises(ValueError) as mode:
                read_im_input_csv(
                    dump("mode.csv", {"reducedCapConsolidationMode": "maybe"})
                )
            self.assertIn("soft", str(mode.exception))

            params = read_im_input_csv(
                dump(
                    "ok.csv",
                    {
                        "reducedCapMaxPallets": 2,
                        "palletReducedCapClassDict": {"FILL": "yes"},
                        "reducedCapMaterialList": ["10"],
                        "containerMaxPallets": {1.0: 2},
                    },
                )
            )
        self.assertEqual(params["palletReducedCapClassDict"], {"FILL": "Yes"})
        self.assertEqual(params["containerMaxPallets"], {1: 2})
        plan = resolve_capacity_classes(params)
        self.assertEqual(plan.cap_r, 2)
        self.assertIn("FILL", plan.class_r_ids)

    def test_existing_ab_file_stays_inactive(self):
        path = ROOT / "data" / "im_input_A_B.csv"
        if not path.is_file():
            self.skipTest("A-B input is not in the workspace")
        params = read_im_input_csv(path)
        plan = resolve_capacity_classes(params)
        self.assertFalse(plan.report)
        self.assertFalse(plan.class_r_active)
        self.assertFalse(plan.use_per_truck_wt)
        self.assertEqual(plan.lambda_r, 0.0)
        self.assertEqual(plan.mu_mix, 0.0)


if __name__ == "__main__":
    unittest.main()
