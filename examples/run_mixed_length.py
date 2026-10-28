"""
run_mixed_length.py
===================
Additional HiGHS model: lot size along the truck comes from palletLengthDict.
Usable trailer length matches the old pad row (n_along * typical length, or
an explicit usableLength / containerLength field). The solver chooses how
many lots of each length fit on each side. 1.0 m / 1.2 m are not hardcoded.

  python examples/run_mixed_length.py
  python examples/run_mixed_length.py data/im_input_A_B.csv 0.01
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd

from loadmax.container_loading_mixed_length import (
    ContainerLoadingOptimizerMixedLength,
)
from loadmax.data_reader import bind_sku_priority_to_pallets, read_im_input_csv
from loadmax.result_paths import (
    metadata_payload,
    resolve_run_dir,
    results_root as results_root_of,
    write_source_metadata,
)
from loadmax.results_summary import (
    build_container_results_summary,
    print_container_results_summary,
    write_container_results_summary,
)


def _apply_ab_priority_materials(params):
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Mixed-length truck loading (lot size from palletLengthDict).",
    )
    parser.add_argument(
        "csv_path",
        nargs="?",
        default=str(ROOT / "data" / "im_input_A_B.csv"),
    )
    parser.add_argument("gap", nargs="?", type=float, default=0.01)
    parser.add_argument("--time-limit", type=float, default=600.0)
    parser.add_argument("--vdr", action="store_true")
    parser.add_argument("--ocean", action="store_true")
    parser.add_argument("--build-only", action="store_true", help="build MIP, do not solve")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    csv_path = Path(args.csv_path)
    if not csv_path.is_file():
        csv_path = ROOT / args.csv_path
    results_root = results_root_of(ROOT)
    out_dir = resolve_run_dir(results_root, "mixed_length", csv_path)
    out_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 80)
    print("MIXED-LENGTH TRUCK LOADING")
    print("=" * 80)
    print(f"  Input   : {csv_path}")
    print(f"  Results : {out_dir}")
    print(f"  Time    : {args.time_limit}s   gap={args.gap}")
    print("=" * 80)

    params = read_im_input_csv(csv_path)
    if str(params.get("marketID", "")).upper() in {"A-B", "AB"}:
        params = _apply_ab_priority_materials(params)

    optimizer = ContainerLoadingOptimizerMixedLength(
        inputParams=params,
        run_ocean=args.ocean,
        time_limit=args.time_limit,
        gap=args.gap,
        sku_penalty_factor=float(params.get("sku_penalty_factor") or 0.0),
        vdr_stacking_rules=args.vdr,
    )
    optimizer.build_model()
    write_source_metadata(
        out_dir,
        metadata_payload(
            solver="mixed_length",
            csv_path=csv_path,
            extra={
                "usable_length": optimizer._L,
                "kmax": optimizer._kmax,
                "distinct_lengths": sorted(set(optimizer._lengths.values())),
                "compile_only": bool(args.build_only),
            },
        ),
    )
    if args.build_only:
        print("Build-only succeeded.")
        return

    solve_result = optimizer.solve()
    instructions = optimizer.generate_loading_instructions()
    lots = optimizer.lot_rows()
    print("Result:")
    print(f"  Status     : {solve_result.get('message')}")
    print(f"  Objective  : {solve_result.get('objective_value')}")
    print(f"  Instructions: {len(instructions)}")
    n_full = sum(1 for i in instructions if not i.is_partial)
    n_prtl = sum(1 for i in instructions if i.is_partial)
    print(f"  Full / partial: {n_full} / {n_prtl}")

    lot_by = {(r["Container"], r["Side"], r["Lot"]): r for r in lots}
    rows = []
    for instr in instructions:
        lot = lot_by.get((instr.container, instr.location, instr.sequence), {})
        rows.append({
            "Container": instr.container,
            "Side": instr.location,
            "Lot": instr.sequence,
            "Layer": instr.layer,
            "PalletID": getattr(instr, "palletID", ""),
            "Material": instr.material,
            "Batch": instr.batch,
            "Cases": instr.cases,
            "Weight": instr.weight,
            "Height": instr.height,
            "LotLength": lot.get("LotLength", ""),
            "X_center": lot.get("X_center", ""),
            "Is_front": lot.get("Is_front", ""),
            "Is_Partial": instr.is_partial,
            "Stacking_Group": instr.stacking_group,
        })
    instr_file = out_dir / "loading_instructions.csv"
    pd.DataFrame(rows).to_csv(instr_file, index=False)
    print(f"Wrote {instr_file}")
    if lots:
        pd.DataFrame(lots).to_csv(out_dir / "lots.csv", index=False)

    # Summary uses original max_plt_n volume denom; axles from lot x-split
    # are already in the MIP. Map lots to synthetic pads for the summary table.
    summary_params = dict(params)
    left_pos, right_pos, front_pos, rear_pos = [], [], [], []
    bal = {}
    for r in lots:
        pos = int(r["Lot"]) * 2 - (1 if r["Side"] == "left" else 0)
        if r["Side"] == "left":
            left_pos.append(pos)
            bal[(pos, "A" if r["Is_front"] else "B")] = 1.0
            bal[(pos, "C")] = 0.0
            bal[(pos, "D")] = 0.0
        else:
            right_pos.append(pos)
            bal[(pos, "C" if r["Is_front"] else "D")] = 1.0
            bal[(pos, "A")] = 0.0
            bal[(pos, "B")] = 0.0
        (front_pos if r["Is_front"] else rear_pos).append(pos)
    summary_params["leftPositionList"] = left_pos
    summary_params["rightPositionList"] = right_pos
    summary_params["frontPositionList"] = front_pos
    summary_params["rearPositionList"] = rear_pos
    summary_params["positionList"] = sorted(set(left_pos + right_pos))
    summary_params["positionGroupBalanceCoefficientDict"] = bal

    for instr in instructions:
        k = instr.sequence
        instr.position = k * 2 - (1 if instr.location == "left" else 0)

    summary_rows = build_container_results_summary(summary_params, instructions)
    print_container_results_summary(summary_rows)
    try:
        write_container_results_summary(summary_rows, out_dir / "results_summary.csv")
        print(f"Wrote {out_dir / 'results_summary.csv'}")
    except PermissionError:
        write_container_results_summary(summary_rows, out_dir / "results_summary_run.csv")

    write_source_metadata(
        out_dir,
        metadata_payload(
            solver="mixed_length",
            csv_path=csv_path,
            extra={
                "status": solve_result.get("message"),
                "objective": solve_result.get("objective_value"),
                "n_instructions": len(instructions),
                "usable_length": solve_result.get("usable_length"),
                "kmax": solve_result.get("kmax"),
            },
        ),
    )


if __name__ == "__main__":
    main()
