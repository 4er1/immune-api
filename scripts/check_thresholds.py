#!/usr/bin/env python3
"""Fails the build if the trained model's simulated detection is worse than these floors.
Reads the JSON that scripts/evaluate_model.sh produces (build/eval_summary.json)."""
import json
import sys

FLOORS = {
    "normal": {"blocked_share": (0.0, 0.05)},          # at most 5% of normal sources ever get blocked
    "dow_flood": {"blocked_share": (0.8, None), "cost_avoided_share": (0.6, None)},
    "scanner": {"blocked_share": (0.8, None)},
    "credential_stuffing": {"blocked_share": (0.8, None)},
}


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "build/eval_summary.json"
    with open(path) as fh:
        summary = json.load(fh)
    failures = []
    for kind, checks in FLOORS.items():
        if kind not in summary:
            failures.append(f"{kind}: missing from the evaluation output")
            continue
        for metric, (lo, hi) in checks.items():
            value = summary[kind][metric]
            if lo is not None and value < lo:
                failures.append(f"{kind}.{metric} = {value} is below the floor {lo}")
            if hi is not None and value > hi:
                failures.append(f"{kind}.{metric} = {value} is above the ceiling {hi}")
    if failures:
        print("Model evaluation gate FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("Model evaluation gate passed:")
    for kind in FLOORS:
        print(f"  - {kind}: {summary[kind]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
