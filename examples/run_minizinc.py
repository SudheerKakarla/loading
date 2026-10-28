"""
run_minizinc.py
===============
Solve truck loading with MiniZinc + HiGHS (separate from scipy HiGHS).

Default: data/im_input_A_B.csv, 600 s, gap 1%, then compare to HiGHS CSVs.

Artifacts go under optimization_results/minizinc/<csv-stem>/ (or
<stem>_<input-hash8> if that stem already stored different CSV bytes).
SciPy HiGHS writes to optimization_results/highs/<csv-stem>/.
.fzn/.ozn are written only for --compile-only.

  python examples/run_minizinc.py
  python examples/run_minizinc.py data/im_input_A_B.csv 0.01
  python examples/run_minizinc.py --compile-only
"""

from __future__ import annotations

import argparse
import csv
import os
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd

from loadmax.container_loading_minizinc import (
    MODEL_PATH,
    ContainerLoadingOptimizerMiniZinc,
    reconstruct_objective,
    slots_from_instruction_csv,
)
from loadmax.result_paths import (
    find_run_file,
    metadata_payload,
    resolve_run_dir,
    results_root as results_root_of,
    sha256_file,
    write_source_metadata,
)
from loadmax.data_reader import bind_sku_priority_to_pallets, read_im_input_csv
from loadmax.results_summary import (
    build_container_results_summary,
    print_container_results_summary,
    write_container_results_summary,
)


def _apply_ab_priority_materials(params):
    """Same A-B overlay as examples/run_highs.py (material_dict.jpg)."""
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


def _write_instructions(instructions, path: Path) -> None:
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
    pd.DataFrame(rows).to_csv(path, index=False)


def _load_instruction_counts(path: Path) -> dict:
    by = defaultdict(lambda: {"full": 0, "prtl": 0, "weight": 0.0})
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            c = row["Container"]
            try:
                c = int(float(c))
            except (TypeError, ValueError):
                pass
            partial = str(row.get("Is_Partial", "")).strip().lower() in {
                "true", "1", "yes",
            }
            if partial:
                by[c]["prtl"] += 1
            else:
                by[c]["full"] += 1
            by[c]["weight"] += float(row.get("Weight") or 0)
    return dict(by)


def _load_summary(path: Path) -> dict:
    rows = {}
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            c = row["Container"]
            try:
                c = int(float(c))
            except (TypeError, ValueError):
                pass
            rows[c] = row
    return rows


def _num(val, default=0.0) -> float:
    if val is None or val == "":
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _margin(limit, cargo) -> Any:
    if limit in ("", None) or cargo in ("", None):
        return ""
    return _num(limit) - _num(cargo)


def _fmt_gap(gap) -> str:
    if gap in (None, ""):
        return ""
    return f"{100.0 * float(gap):.3f}%"


def _first_existing(paths) -> Optional[Path]:
    for path in paths:
        if path is not None and path.is_file():
            return path
    return None


def compare_to_highs(
    input_stem: str,
    mzn_summary_rows: list,
    mzn_instructions,
    results_root: Path,
    scenario_dir: Path,
    csv_path: Path,
    input_params: dict,
    mzn_solve: dict,
) -> Path:
    highs_instr = _first_existing([
        find_run_file(
            results_root,
            "highs",
            csv_path,
            "loading_instructions.csv",
            [f"loading_instructions_highs_{input_stem}.csv"],
        ),
        results_root / "highs" / input_stem / "loading_instructions.csv",
        results_root / f"loading_instructions_highs_{input_stem}.csv",
    ])
    highs_sum = _first_existing([
        find_run_file(
            results_root,
            "highs",
            csv_path,
            "results_summary.csv",
            [
                f"results_summary_highs_{input_stem}_run.csv",
                f"results_summary_highs_{input_stem}.csv",
            ],
        ),
        results_root / "highs" / input_stem / "results_summary.csv",
        results_root / f"results_summary_highs_{input_stem}_run.csv",
        results_root / f"results_summary_highs_{input_stem}.csv",
    ])
    compare_path = scenario_dir / "comparison_vs_highs.csv"

    print("\n" + "=" * 80)
    print("MINIZINC vs HiGHS")
    print("=" * 80)
    if highs_instr is None and highs_sum is None:
        print(f"  No HiGHS baseline for {input_stem}")
        print("=" * 80)
        return compare_path

    mzn_by = defaultdict(lambda: {"full": 0, "prtl": 0})
    mzn_slots = []
    for instr in mzn_instructions:
        if instr.is_partial:
            mzn_by[instr.container]["prtl"] += 1
        else:
            mzn_by[instr.container]["full"] += 1
        mzn_slots.append(
            (instr.container, instr.layer, instr.position, instr.palletID, instr.is_partial)
        )
    highs_by = _load_instruction_counts(highs_instr) if highs_instr is not None else {}
    highs_sum_rows = _load_summary(highs_sum) if highs_sum is not None else {}
    mzn_sum = {row["Container"]: row for row in mzn_summary_rows}

    highs_obj = ""
    highs_status = ""
    if highs_instr is not None:
        highs_slots = slots_from_instruction_csv(highs_instr, input_params)
        highs_obj = reconstruct_objective(input_params, highs_slots)
        highs_status = "incumbent_csv"

    mzn_status = mzn_solve.get("status", "")
    mzn_obj = mzn_solve.get("objective_value")
    mzn_bound = mzn_solve.get("objective_bound")
    mzn_gap = mzn_solve.get("gap")
    mzn_recon = mzn_solve.get("reconstructed_objective")
    print(f"  MiniZinc status    : {mzn_status}")
    print(f"  MiniZinc objective : {mzn_obj}")
    print(f"  MiniZinc bound     : {mzn_bound}")
    print(f"  MiniZinc gap       : {_fmt_gap(mzn_gap)}")
    print(f"  MiniZinc reconstructed: {mzn_recon}")
    print(f"  HiGHS CSV status   : {highs_status or '(missing)'}")
    print(f"  HiGHS reconstructed: {highs_obj}")
    print("  HiGHS bound/gap    : (not stored in the loading CSV)")

    containers = sorted(
        set(mzn_by) | set(highs_by) | set(mzn_sum) | set(highs_sum_rows),
        key=lambda c: (str(type(c)), c),
    )
    cmp_rows = []
    tot = {
        "mzn_full": 0, "highs_full": 0, "mzn_prtl": 0, "highs_prtl": 0,
    }
    print(
        f"{'Ctr':>4} {'MzF':>4} {'HiF':>4} {'dF':>4} "
        f"{'MzP':>4} {'HiP':>4} {'dP':>4} "
        f"{'MzVol':>6} {'HiVol':>6} {'MzWt':>6} {'HiWt':>6} "
        f"{'MzFrM':>7} {'HiFrM':>7} {'MzRrM':>7} {'HiRrM':>7}"
    )
    solver_cols = {
        "MiniZinc Status": mzn_status,
        "HiGHS Status": highs_status,
        "MiniZinc Objective": mzn_obj if mzn_obj is not None else "",
        "HiGHS Objective": highs_obj,
        "MiniZinc Bound": mzn_bound if mzn_bound is not None else "",
        "HiGHS Bound": "",
        "MiniZinc Gap": mzn_gap if mzn_gap is not None else "",
        "HiGHS Gap": "",
        "MiniZinc Reconstructed Objective": mzn_recon if mzn_recon is not None else "",
    }
    for c in containers:
        mz_f = mzn_by[c]["full"]
        mz_p = mzn_by[c]["prtl"]
        hi_f = highs_by.get(c, {}).get("full", 0)
        hi_p = highs_by.get(c, {}).get("prtl", 0)
        mz_row = mzn_sum.get(c) or {}
        hi_row = highs_sum_rows.get(c) or {}
        mz_vol = _num(mz_row.get("Utilization (Volume)"))
        hi_vol = _num(hi_row.get("Utilization (Volume)"))
        mz_wt = _num(mz_row.get("Utilization (Weight)"))
        hi_wt = _num(hi_row.get("Utilization (Weight)"))
        mz_fr = _num(mz_row.get("Cargo Weight: Front"))
        hi_fr = _num(hi_row.get("Cargo Weight: Front"))
        mz_rr = _num(mz_row.get("Cargo Weight: Rear"))
        hi_rr = _num(hi_row.get("Cargo Weight: Rear"))
        front_limit = mz_row.get("Front Limit", hi_row.get("Front Limit", ""))
        rear_limit = mz_row.get("Rear Limit", hi_row.get("Rear Limit", ""))
        mz_fr_m = _margin(front_limit, mz_fr)
        hi_fr_m = _margin(front_limit, hi_fr)
        mz_rr_m = _margin(rear_limit, mz_rr)
        hi_rr_m = _margin(rear_limit, hi_rr)
        tot["mzn_full"] += mz_f
        tot["highs_full"] += hi_f
        tot["mzn_prtl"] += mz_p
        tot["highs_prtl"] += hi_p
        print(
            f"{c:>4} {mz_f:4d} {hi_f:4d} {mz_f - hi_f:4d} "
            f"{mz_p:4d} {hi_p:4d} {mz_p - hi_p:4d} "
            f"{mz_vol:6.1f} {hi_vol:6.1f} {mz_wt:6.1f} {hi_wt:6.1f} "
            f"{_num(mz_fr_m):7.0f} {_num(hi_fr_m):7.0f} "
            f"{_num(mz_rr_m):7.0f} {_num(hi_rr_m):7.0f}"
        )
        cmp_rows.append({
            "Container": c,
            **solver_cols,
            "MiniZinc Full": mz_f,
            "HiGHS Full": hi_f,
            "Full Diff": mz_f - hi_f,
            "MiniZinc Partial": mz_p or "",
            "HiGHS Partial": hi_p or "",
            "Partial Diff": mz_p - hi_p,
            "MiniZinc Volume %": mz_vol,
            "HiGHS Volume %": hi_vol,
            "MiniZinc Weight %": mz_wt,
            "HiGHS Weight %": hi_wt,
            "MiniZinc Front": mz_fr,
            "HiGHS Front": hi_fr,
            "Front Limit": front_limit,
            "MiniZinc Front Margin": mz_fr_m,
            "HiGHS Front Margin": hi_fr_m,
            "MiniZinc Rear": mz_rr,
            "HiGHS Rear": hi_rr,
            "Rear Limit": rear_limit,
            "MiniZinc Rear Margin": mz_rr_m,
            "HiGHS Rear Margin": hi_rr_m,
        })
    print("-" * 80)
    print(
        f"{'TOT':>4} {tot['mzn_full']:4d} {tot['highs_full']:4d} "
        f"{tot['mzn_full'] - tot['highs_full']:4d} "
        f"{tot['mzn_prtl']:4d} {tot['highs_prtl']:4d} "
        f"{tot['mzn_prtl'] - tot['highs_prtl']:4d}"
    )
    print(f"  HiGHS instructions: {highs_instr if highs_instr is not None else '(missing)'}")
    print(f"  HiGHS summary     : {highs_sum if highs_sum is not None else '(missing)'}")
    print("=" * 80)

    cmp_rows.append({
        "Container": "TOTAL",
        **solver_cols,
        "MiniZinc Full": tot["mzn_full"],
        "HiGHS Full": tot["highs_full"],
        "Full Diff": tot["mzn_full"] - tot["highs_full"],
        "MiniZinc Partial": tot["mzn_prtl"] or "",
        "HiGHS Partial": tot["highs_prtl"] or "",
        "Partial Diff": tot["mzn_prtl"] - tot["highs_prtl"],
    })
    pd.DataFrame(cmp_rows).to_csv(compare_path, index=False)
    print(f"Wrote {compare_path}")
    return compare_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Solve LoadMax trucks with MiniZinc + HiGHS and compare to scipy HiGHS.",
    )
    parser.add_argument(
        "csv_path",
        nargs="?",
        default=str(ROOT / "data" / "im_input_A_B.csv"),
        help="im_input CSV (default: data/im_input_A_B.csv)",
    )
    parser.add_argument(
        "gap",
        nargs="?",
        type=float,
        default=0.01,
        help="relative MIP gap (default: 0.01)",
    )
    parser.add_argument("--time-limit", type=float, default=600.0, help="seconds")
    parser.add_argument("--solver", default="highs", help="MiniZinc solver id")
    parser.add_argument("--threads", type=int, default=0)
    parser.add_argument("--vdr", action="store_true", help="enable G2/G3/G4 stacking")
    parser.add_argument("--ocean", action="store_true")
    parser.add_argument(
        "--compile-only",
        action="store_true",
        help="flatten the model, do not search",
    )
    parser.add_argument("--minizinc", default=None, help="path to minizinc.exe")
    parser.add_argument(
        "--work-dir",
        default=None,
        help="scenario directory (default: optimization_results/minizinc/<csv-stem>/)",
    )
    parser.add_argument(
        "--warm-start",
        default=None,
        help="SciPy/HiGHS loading-instructions CSV to warm-start MiniZinc/HiGHS",
    )
    parser.add_argument(
        "--no-warm-start",
        action="store_true",
        help="cold start even if a HiGHS loading CSV exists",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    csv_path = Path(args.csv_path)
    if not csv_path.is_file():
        csv_path = ROOT / args.csv_path
    results_root = results_root_of(ROOT)
    os.makedirs(results_root, exist_ok=True)
    override = Path(args.work_dir) if args.work_dir else None
    scenario_dir = resolve_run_dir(results_root, "minizinc", csv_path, override)
    work_dir = scenario_dir / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    input_hash = sha256_file(csv_path)

    print("=" * 80)
    print("MINIZINC TRUCK LOADING")
    print("=" * 80)
    print(f"  Input    : {csv_path}")
    print(f"  Results  : {scenario_dir}")
    print(f"  CSV hash : {input_hash[:8]}")
    print(f"  Mode     : {'ocean' if args.ocean else 'truck'}")
    print(f"  Time     : {args.time_limit}s   gap={args.gap}   solver={args.solver}")
    print("=" * 80)

    input_params = read_im_input_csv(csv_path)
    if str(input_params.get("marketID", "")).upper() in {"A-B", "AB"}:
        input_params = _apply_ab_priority_materials(input_params)

    if args.no_warm_start:
        warm_path = None
    elif args.warm_start:
        warm_path = Path(args.warm_start)
    else:
        warm_path = find_run_file(
            results_root,
            "highs",
            csv_path,
            "loading_instructions.csv",
            [f"loading_instructions_highs_{csv_path.stem}.csv"],
        )
    warm_slots = None
    if warm_path is not None:
        if not warm_path.is_file():
            raise SystemExit(f"warm-start file not found: {warm_path}")
        warm_slots = slots_from_instruction_csv(warm_path, input_params)
        print(f"  Warm-start from {warm_path} ({len(warm_slots)} slots)")
    else:
        print("  Warm-start: off")

    metadata = metadata_payload(
        solver="minizinc",
        csv_path=csv_path,
        extra={
            "scenario": scenario_dir.name,
            "marketID": input_params.get("marketID"),
            "n_vehicles": len(input_params.get("containerList") or []),
            "n_full": len(input_params.get("fullPalletList") or []),
            "n_prtl": len(input_params.get("partialPalletList") or []),
            "n_positions": len(input_params.get("positionList") or []),
            "n_layers": len(input_params.get("layerList") or []),
            "backend": args.solver,
            "time_limit": args.time_limit,
            "gap": args.gap,
            "compile_only": bool(args.compile_only),
            "warm_start_csv": str(warm_path.resolve()) if warm_path else None,
            "warm_start_sha256": sha256_file(warm_path) if warm_path else None,
            "static_model": str(MODEL_PATH),
            "static_driver": str(Path(__file__).resolve()),
        },
    )
    write_source_metadata(scenario_dir, metadata)

    optimizer = ContainerLoadingOptimizerMiniZinc(
        inputParams=input_params,
        run_ocean=args.ocean,
        time_limit=args.time_limit,
        gap=args.gap,
        sku_penalty_factor=float(input_params.get("sku_penalty_factor") or 0.0),
        vdr_stacking_rules=args.vdr,
        minizinc_exe=Path(args.minizinc) if args.minizinc else None,
        solver=args.solver,
        work_dir=work_dir,
        threads=args.threads,
        warm_slots=warm_slots,
    )
    dzn = optimizer.build_model()

    if args.compile_only:
        fzn = work_dir / "truck_loading.fzn"
        ozn = work_dir / "truck_loading.ozn"
        cmd = [
            str(optimizer.minizinc_exe), "--solver", args.solver, "-c",
            "-O1",
            "--fzn", str(fzn),
            "--ozn", str(ozn),
            str(MODEL_PATH), str(dzn),
        ]
        print("  Compile:", " ".join(cmd))
        proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
        print(proc.stdout)
        if proc.returncode != 0:
            print(proc.stderr)
            raise SystemExit(proc.returncode)
        ozn = fzn.with_suffix(".ozn")
        print(f"  Wrote {fzn}")
        print(f"  Wrote {ozn}" if ozn.is_file() else "  No .ozn emitted")
        print("Compile-only succeeded. Flattened files are for debug; a normal solve regenerates internally.")
        return

    solve_result = optimizer.solve()
    instructions = optimizer.generate_loading_instructions()
    print("Result:")
    print(f"  Status               : {solve_result.get('status')}")
    print(f"  Objective            : {solve_result.get('objective_value')}")
    print(f"  Bound                : {solve_result.get('objective_bound')}")
    print(f"  Gap                  : {solve_result.get('gap')}")
    print(f"  Warm-started         : {solve_result.get('warm_started')}")
    print(f"  Loading instructions : {len(instructions)}")
    print(f"  Axle constraints     : {optimizer.enforceAxleWeightConstraints}")
    print(f"  Weight balancing     : {optimizer.enforceWeightBalancing}")
    n_full = sum(1 for i in instructions if not i.is_partial)
    n_prtl = sum(1 for i in instructions if i.is_partial)
    print(f"  Full / partial loaded: {n_full} / {n_prtl}")
    by_container = {}
    for i in instructions:
        by_container[i.container] = by_container.get(i.container, 0) + 1
    for c, n in sorted(by_container.items(), key=lambda kv: str(kv[0])):
        print(f"    Container {c}: {n} pallets")

    instr_file = scenario_dir / "loading_instructions.csv"
    _write_instructions(instructions, instr_file)
    print(f"\nWrote {instr_file}")

    summary_rows = build_container_results_summary(input_params, instructions)
    print_container_results_summary(summary_rows)
    summary_file = scenario_dir / "results_summary.csv"
    try:
        write_container_results_summary(summary_rows, summary_file)
        print(f"Wrote {summary_file}")
    except PermissionError:
        alt = scenario_dir / "results_summary_run.csv"
        write_container_results_summary(summary_rows, alt)
        print(f"Locked {summary_file}; wrote {alt}")

    compare_to_highs(
        csv_path.stem,
        summary_rows,
        instructions,
        results_root,
        scenario_dir,
        csv_path,
        input_params,
        solve_result,
    )
    metadata["status"] = solve_result.get("status")
    metadata["objective"] = solve_result.get("objective_value")
    metadata["bound"] = solve_result.get("objective_bound")
    metadata["gap"] = solve_result.get("gap")
    write_source_metadata(scenario_dir, metadata)


if __name__ == "__main__":
    main()
