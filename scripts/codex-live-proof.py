#!/usr/bin/env -S uv run python
"""Manual opt-in Codex live proof runner.

By default this exits before touching real auth or network. Use only on a trusted
local machine and never paste the resulting report if it contains unexpected
fields.
"""

from __future__ import annotations

import argparse
import json
import sys

from reverso.protocols.adapters.codex_live_proof import (
    CodexLiveProofSkipped,
    require_live_opt_in,
    run_official_cli_live_proof,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a gated Codex live proof")
    parser.add_argument("--lane", choices=("official", "direct"), default="official")
    parser.add_argument("--json", action="store_true", help="emit JSON report")
    args = parser.parse_args()

    try:
        if args.lane == "official":
            report = run_official_cli_live_proof()
        else:
            require_live_opt_in("direct")
            report_dict = {
                "lane": "direct",
                "status": "skipped",
                "reason": "direct live proof requires a ProviderAuth with bearer_token(); CodexOAuthAuth is validate-only",
            }
            if args.json:
                print(json.dumps(report_dict, indent=2, sort_keys=True))
            else:
                print(json.dumps(report_dict, indent=2, sort_keys=True))
            return 0
    except CodexLiveProofSkipped as exc:
        report_dict = {"lane": args.lane, "status": "skipped", "reason": str(exc)}
        if args.json:
            print(json.dumps(report_dict, indent=2, sort_keys=True))
        else:
            print(f"skipped: {exc}")
        return 0

    report_dict = report.to_public_dict()
    if args.json:
        print(json.dumps(report_dict, indent=2, sort_keys=True))
    else:
        print(json.dumps(report_dict, indent=2, sort_keys=True))
    return 0 if report.status in {"passed", "skipped"} else 1


if __name__ == "__main__":
    sys.exit(main())
