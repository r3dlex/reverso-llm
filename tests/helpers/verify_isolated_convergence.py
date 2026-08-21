#!/usr/bin/env python3
"""Test-only entrypoint for hermetic client convergence verification."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from reverso.deployment_drift import DeploymentDriftError, verify_isolated_convergence


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--rtk-bin", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = verify_isolated_convergence(home=args.home, rtk_bin=args.rtk_bin)
    except DeploymentDriftError as exc:
        print(f"isolated-convergence: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
