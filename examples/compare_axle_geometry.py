"""Compare LoadMax A+C/B+D cargo vs pad-X trailer geometry.

No MIP change. Reads an existing loading-instructions CSV.
"""

from __future__ import annotations

import csv
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from loadmax.axle_geometry import (  # noqa: E402
    DEFAULT_PAD_PITCH_IN,
    TrailerGeometry,
    compare_container,
    pad_x_center,
)


def _load_instructions(path: Path) -> dict:
    by_truck = defaultdict(list)
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            container = row["Container"]
            try:
                container = int(float(container))
            except (TypeError, ValueError):
                pass
            row["Position"] = int(float(row["Position"]))
            row["Weight"] = float(row["Weight"] or 0)
            by_truck[container].append(row)
    return dict(by_truck)


def main() -> None:
    src = ROOT / "optimization_results" / "highs" / "im_input_A_B" / "loading_instructions.csv"
    if not src.exists():
        src = ROOT / "optimization_results" / "loading_instructions_highs_im_input_A_B.csv"
    if len(sys.argv) > 1:
        src = Path(sys.argv[1])
    if not src.exists():
        raise SystemExit(f"missing instructions: {src}")

    geo = TrailerGeometry.assumed_us_53()
    pitch = DEFAULT_PAD_PITCH_IN
    by_truck = _load_instructions(src)
    out_path = src.with_name("axle_geometry_vs_loadmax_im_input_A_B.csv")

    print("=" * 88)
    print("PAD X  (30-pad A-B, 40 in along trailer, X=0 at nose)")
    print("=" * 88)
    print(f"{'pad':>4} {'bay':>4} {'x_center_in':>12} {'x_center_ft':>12}")
    for p in range(1, 31, 2):
        x = pad_x_center(p, pitch)
        print(f" {p:>2}/{p+1:<2} {(p+1)//2:>4} {x:12.1f} {x/12:12.2f}")

    print()
    print("Assumed 53' geometry (inches, lb) — not in the Excel dump:")
    for k, v in geo.__dict__.items():
        print(f"  {k}: {v}")
    print(f"  pad_pitch_in: {pitch}")
    print()
    print("Compare cargo-only kingpin/rear (tare off) to LoadMax front/rear.")
    print("Drive/rear with tare are scale-style axles; they are not the 19920/22080 columns.")
    print()

    fieldnames = [
        "Container",
        "Pallets",
        "Cargo Weight",
        "Cargo CG in",
        "LoadMax Front",
        "Physics Cargo Front",
        "Front Delta",
        "Front Delta %",
        "LoadMax Rear",
        "Physics Cargo Rear",
        "Rear Delta",
        "Rear Delta %",
        "LoadMax Front Share %",
        "Physics Front Share %",
        "Drive Axle (tare on)",
        "Rear Axle (tare on)",
        "Front Limit",
        "Rear Limit",
    ]
    rows_out = []
    hdr = (
        f"{'Trk':>4} {'n':>4} {'W':>8} {'CG ft':>7} "
        f"{'LM F':>8} {'Phys F':>8} {'dF':>8} {'dF%':>7} "
        f"{'LM R':>8} {'Phys R':>8} {'dR':>8} {'dR%':>7} "
        f"{'LM F%':>6} {'Ph F%':>6}"
    )
    print(hdr)
    print("-" * len(hdr))
    for truck in sorted(by_truck, key=lambda c: (str(type(c)), c)):
        r = compare_container(by_truck[truck], pitch_in=pitch, geo=geo)
        print(
            f"{truck:4} {r['n_pallets']:4} {r['cargo_weight']:8.0f} {r['cargo_x_cg_in']/12:7.2f} "
            f"{r['loadmax_front']:8.0f} {r['physics_cargo_front']:8.0f} "
            f"{r['front_delta']:8.0f} {r['front_delta_pct']:6.1f}% "
            f"{r['loadmax_rear']:8.0f} {r['physics_cargo_rear']:8.0f} "
            f"{r['rear_delta']:8.0f} {r['rear_delta_pct']:6.1f}% "
            f"{r['loadmax_front_share_pct']:5.1f}% {r['physics_front_share_pct']:5.1f}%"
        )
        rows_out.append({
            "Container": truck,
            "Pallets": r["n_pallets"],
            "Cargo Weight": round(r["cargo_weight"]),
            "Cargo CG in": round(r["cargo_x_cg_in"], 1),
            "LoadMax Front": round(r["loadmax_front"]),
            "Physics Cargo Front": round(r["physics_cargo_front"]),
            "Front Delta": round(r["front_delta"]),
            "Front Delta %": round(r["front_delta_pct"], 1),
            "LoadMax Rear": round(r["loadmax_rear"]),
            "Physics Cargo Rear": round(r["physics_cargo_rear"]),
            "Rear Delta": round(r["rear_delta"]),
            "Rear Delta %": round(r["rear_delta_pct"], 1),
            "LoadMax Front Share %": round(r["loadmax_front_share_pct"], 1),
            "Physics Front Share %": round(r["physics_front_share_pct"], 1),
            "Drive Axle (tare on)": round(r["drive_axle_with_tare"]),
            "Rear Axle (tare on)": round(r["rear_axle_with_tare"]),
            "Front Limit": 19920,
            "Rear Limit": 22080,
        })

    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows_out)
    print()
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
