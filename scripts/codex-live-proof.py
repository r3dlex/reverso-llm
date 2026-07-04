#!/usr/bin/env python3
"""Run opt-in Codex live proof lanes.

The output is intentionally secret-free JSON. Live token/network proof must be
explicitly opted in with the lane-specific environment variables documented in
ADR 0016.
"""

from __future__ import annotations

import argparse
import json
import sys

from reverso.protocols.adapters.codex_live_proof import (
    CodexLiveProofSkipped,
    run_direct_live_proof_sync,
    run_official_cli_live_proof,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run opt-in Codex OAuth live proof")
    parser.add_argument("--lane", choices=("official", "direct"), required=True)
    parser.add_argument("--json", action="store_true", help="emit secret-free JSON")
    args = parser.parse_args()

    try:
        if args.lane == "official":
            report = run_official_cli_live_proof()
        else:
            report = run_direct_live_proof_sync()
    except CodexLiveProofSkipped as exc:
        report_dict = {"lane": args.lane, "status": "skipped", "reason": str(exc)}
        if args.json:
            print(json.dumps(report_dict, indent=2, sort_keys=True))
        else:
            print(json.dumps(report_dict, indent=2, sort_keys=True))
        return 0

    report_dict = report.to_public_dict()
    if args.json:
        print(json.dumps(report_dict, indent=2, sort_keys=True))
    else:
        print(json.dumps(report_dict, indent=2, sort_keys=True))
    return 0 if report.status == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
