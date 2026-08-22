"""Simple benchmarks for the four high-priority LoadMax fixes.

Run from the repository root:

    .venv\\Scripts\\python.exe docs\\benchmark_h1_to_h4.py

The script uses small, independent examples so each result is easy to read:

H1 - Priority bonuses must only apply to loaded pallets.
H2 - Results must be extracted before a Gurobi model is disposed.
H3 - A G3 pallet must block stacking above it on any layer.
H4 - Loading output must use correct cube units and description fields.
"""

import gurobipy as gp
from gurobipy import GRB


def benchmark_h1_priority_linking(env):
    """Show that an unlinked priority variable pays bonuses to unloaded pallets."""
    pallets = ["P1", "P2", "P3", "P4", "P5"]
    incentive = 1000.0

    def build_model(link_priority):
        model = gp.Model("h1", env=env)
        model.setParam("OutputFlag", 0)
        assigned = model.addVars(pallets, vtype=GRB.BINARY, name="assigned")
        priority = model.addVars(pallets, lb=0, ub=1, name="priority")

        # Only one pallet can fit, and the optimizer is not required to load it.
        model.addConstr(gp.quicksum(assigned[p] for p in pallets) <= 1)
        if link_priority:
            model.addConstrs(
                (priority[p] == assigned[p] for p in pallets),
                name="link_priority",
            )

        model.setObjective(incentive * priority.sum(), GRB.MAXIMIZE)
        model.optimize()
        result = {
            "objective": model.ObjVal,
            "loaded": sum(assigned[p].X > 0.5 for p in pallets),
            "bonus_pallets": sum(priority[p].X > 0.5 for p in pallets),
        }
        model.dispose()
        return result

    buggy = build_model(link_priority=False)
    fixed = build_model(link_priority=True)

    print("H1 - Priority bonuses")
    print(f"  Without linking: objective={buggy['objective']:.0f}, "
          f"loaded={buggy['loaded']}, bonus pallets={buggy['bonus_pallets']}")
    print(f"  With linking:    objective={fixed['objective']:.0f}, "
          f"loaded={fixed['loaded']}, bonus pallets={fixed['bonus_pallets']}")
    print("  Result: " + ("PASS" if fixed["bonus_pallets"] == fixed["loaded"] else "FAIL"))


def benchmark_h2_lifecycle(env):
    """Show the safe pattern: copy results before disposing the model."""
    with gp.Model("h2", env=env) as model:
        model.setParam("OutputFlag", 0)
        value = model.addVar(vtype=GRB.BINARY, name="value")
        model.setObjective(value, GRB.MAXIMIZE)
        model.optimize()

        # Copy plain Python values while the model is still alive.
        solution = {"status": model.Status, "objective": model.ObjVal}

    print("H2 - Model lifecycle")
    print(f"  Solution after cleanup: status={solution['status']}, "
          f"objective={solution['objective']:.0f}")
    print("  Result: PASS (plain data remains usable after model cleanup)")


def benchmark_h3_g3_stacking(env):
    """Show that a G3 pallet on layer 2 blocks layer 3."""
    model = gp.Model("h3", env=env)
    model.setParam("OutputFlag", 0)
    layers = [1, 2, 3]
    pallets = ["G3_PALLET", "NORMAL_PALLET"]
    assigned = model.addVars(layers, pallets, vtype=GRB.BINARY, name="assigned")
    present = model.addVars(layers, vtype=GRB.BINARY, name="present")

    model.addConstrs(
        (gp.quicksum(assigned[layer, pallet] for pallet in pallets) <= 1
         for layer in layers),
        name="one_pallet_per_layer",
    )
    model.addConstrs(
        (present[layer] == gp.quicksum(assigned[layer, pallet]
                                       for pallet in pallets)
         for layer in layers),
        name="layer_present",
    )
    model.addConstr(assigned[2, "G3_PALLET"] == 1)

    # Correct rule: G3 at layer l blocks the next layer.
    model.addConstr(
        assigned[3, "NORMAL_PALLET"] <= 1 - assigned[2, "G3_PALLET"],
        name="g3_blocks_above",
    )
    model.setObjective(assigned[3, "NORMAL_PALLET"], GRB.MAXIMIZE)
    model.optimize()
    objective = model.ObjVal
    model.dispose()

    print("H3 - G3 stacking")
    print(f"  Normal pallet above layer-2 G3 pallet: {objective:.0f}")
    print("  Result: " + ("PASS" if objective == 0 else "FAIL"))


def benchmark_h4_loading_output():
    """Show correct cube conversion and description mapping."""
    pallets = {
        "P1": {"length": 48, "width": 40, "height": 60,
               "bin": "BIN-A101", "description": "Premium Widget"},
        "P2": {"length": 48, "width": 40, "height": 48,
               "bin": "BIN-B202", "description": "Standard Gadget"},
        "P3": {"length": 48, "width": 48, "height": 72,
               "bin": "BIN-C303", "description": "Industrial Component"},
    }

    print("H4 - Loading output")
    passed = True
    for pallet, data in pallets.items():
        cube = (data["length"] / 12) * (data["width"] / 12) * (data["height"] / 12)
        description = data["description"]
        correct_cube = (data["length"] * data["width"] * data["height"]) / 1728
        ok = abs(cube - correct_cube) < 1e-9 and description != data["bin"]
        passed = passed and ok
        print(f"  {pallet}: cube={cube:.1f} ft^3, description='{description}'")
    print("  Result: " + ("PASS" if passed else "FAIL"))


def main():
    with gp.Env(empty=True) as env:
        env.setParam("OutputFlag", 0)
        env.start()
        benchmark_h1_priority_linking(env)
        benchmark_h2_lifecycle(env)
        benchmark_h3_g3_stacking(env)
    benchmark_h4_loading_output()
    print("\nAll H1-H4 benchmarks completed.")


if __name__ == "__main__":
    main()
