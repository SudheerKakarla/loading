"""
run_highs.py
============
Load data/im_input_1.csv and solve with HiGHS in truck mode.

Front/rear axle limits are optional: this dump has empty max_front_wt /
max_rear_wt, so those constraints are skipped. Total weight, height,
stacking, and left/right + front/back balance still apply.
"""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd

from loadmax.data_reader import read_im_input_csv, bind_sku_priority_to_pallets
from loadmax.result_paths import (
    metadata_payload,
    resolve_run_dir,
    results_root as results_root_of,
    write_source_metadata,
)


def _apply_ab_priority_materials(params):
    """Real A-B palletMaterialDict from data/material_dict.jpg.

    Fill reward matches future_addons/optimizer_pyomo.py: pallets whose material is in
    skuPriorityIncentive get container_priority * priorityIncentive, others
    get container_priority only.
    """
    mapping = {
        "plt_4": 409512105, "plt_6": 120241400, "plt_12": 409509005,
        "plt_26": 306685200, "plt_32": 406748705, "plt_37": 409561210,
        "plt_38": 409561210, "plt_103": 401856105, "plt_162": 111927302,
        "plt_169": 409501209, "plt_172": 401809113, "plt_178": 409512005,
        "plt_185": 401801510, "plt_190": 406730108, "plt_193": 409509205,
        "plt_195": 406812300, "plt_202": 406748805, "plt_204": 409161406,
        "plt_206": 406788805, "plt_207": 681941705, "plt_208": 120202500,
        "plt_209": 120234500, "plt_210": 680640600, "plt_211": 406815005,
        "plt_212": 406814902, "plt_213": 409167909, "plt_214": 409109507,
        "plt_215": 409164708, "plt_216": 511720200, "plt_217": 406818000,
        "plt_218": 681038309, "plt_219": 681116100, "plt_221": 409561210,
        "plt_222": 409561210,
    }
    params["palletMaterialDict"] = mapping
    raw_sku = dict(params.get("skuPriorityIncentive") or {})
    params = bind_sku_priority_to_pallets(params)

    incentive = float(params.get("priorityIncentive") or 1.0)
    sku_keys = set(raw_sku)
    profit = {}
    for plt, mtl in mapping.items():
        high = (
            mtl in sku_keys
            or str(mtl) in sku_keys
            or (isinstance(mtl, (int, float)) and int(mtl) in sku_keys)
        )
        profit[plt] = incentive if high else 1.0
    params["palletProfitDict"] = profit
    return params
from loadmax.container_loading_highs import ContainerLoadingOptimizerHiGHS
from loadmax.results_summary import (
    build_container_results_summary,
    print_container_results_summary,
    write_container_results_summary,
)


def main(
    csv_path=None,
    run_ocean=False,
    time_limit=600.0,
    gap=0.01,
    vdr_stacking_rules=False,
):
    csv_path = Path(csv_path or ROOT / "data" / "im_input_1.csv")
    results_root = results_root_of(ROOT)
    out_dir = resolve_run_dir(results_root, "highs", csv_path)
    out_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 80)
    print("HiGHS TRUCK LOADING")
    print("=" * 80)
    print(f"  Input   : {csv_path}")
    print(f"  Results : {out_dir}")
    print(f"  Mode    : {'ocean' if run_ocean else 'truck'}")
    print(f"  Time    : {time_limit}s   gap={gap}")
    print("=" * 80)

    inputParams = read_im_input_csv(csv_path)
    if str(inputParams.get("marketID", "")).upper() in {"A-B", "AB"}:
        inputParams = _apply_ab_priority_materials(inputParams)

    optimizer = ContainerLoadingOptimizerHiGHS(
        inputParams=inputParams,
        run_ocean=run_ocean,
        time_limit=time_limit,
        gap=gap,
        sku_penalty_factor=float(inputParams.get("sku_penalty_factor") or 0.0),
        vdr_stacking_rules=vdr_stacking_rules,
    )
    optimizer.build_model()
    solve_result = optimizer.solve()
    instructions = optimizer.generate_loading_instructions()

    print("Result:")
    print(f"  Status               : {solve_result.get('message')}")
    print(f"  Objective            : {solve_result.get('objective_value')}")
    print(f"  Loading instructions : {len(instructions)}")
    print(f"  Axle constraints     : {optimizer.enforceAxleWeightConstraints}")
    print(f"  Weight balancing     : {optimizer.enforceWeightBalancing}")

    n_full = sum(1 for i in instructions if not i.is_partial)
    n_prtl = sum(1 for i in instructions if i.is_partial)
    print(f"  Full / partial loaded: {n_full} / {n_prtl}")

    by_container = {}
    for i in instructions:
        by_container.setdefault(i.container, 0)
        by_container[i.container] += 1
    for c, n in sorted(by_container.items(), key=lambda kv: str(kv[0])):
        print(f"    Container {c}: {n} pallets")

    rows = []
    for instr in instructions:
        rows.append({
            "Container": instr.container,
            "Sequence": instr.sequence,
            "Position": instr.position,
            "Layer": instr.layer,
            "PalletID": getattr(instr, "palletID", ""),
            "Material": instr.material,
            "Batch": instr.batch,
            "Cases": instr.cases,
            "Weight": instr.weight,
            "Height": instr.height,
            "Is_Partial": instr.is_partial,
            "Stacking_Group": instr.stacking_group,
            "Warehouse_Location": instr.location,
            "Destination": instr.shipTo,
        })
    out_file = out_dir / "loading_instructions.csv"
    pd.DataFrame(rows).to_csv(out_file, index=False)
    print(f"\nWrote {out_file}")

    summary_rows = build_container_results_summary(inputParams, instructions)
    print_container_results_summary(summary_rows)
    summary_file = out_dir / "results_summary.csv"
    try:
        write_container_results_summary(summary_rows, summary_file)
        print(f"Wrote {summary_file}")
    except PermissionError:
        alt = out_dir / "results_summary_run.csv"
        write_container_results_summary(summary_rows, alt)
        print(f"Locked {summary_file}; wrote {alt}")
    write_source_metadata(
        out_dir,
        metadata_payload(
            solver="highs",
            csv_path=csv_path,
            extra={
                "scenario": out_dir.name,
                "marketID": inputParams.get("marketID"),
                "status": solve_result.get("message"),
                "objective": solve_result.get("objective_value"),
                "n_instructions": len(instructions),
                "static_driver": str(Path(__file__).resolve()),
            },
        ),
    )
    return solve_result, instructions, summary_rows


if __name__ == "__main__":
    csv_arg = sys.argv[1] if len(sys.argv) > 1 else None
    gap_arg = float(sys.argv[2]) if len(sys.argv) > 2 else 0.01
    main(csv_path=csv_arg, gap=gap_arg, time_limit=600.0)
