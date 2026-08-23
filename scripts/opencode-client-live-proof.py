#!/usr/bin/env python3
"""Run opt-in OpenCode live proof against the running Reverso gateway.

The output is intentionally secret-free JSON. Set REVERSO_OPENCODE_CLIENT_LIVE_PROOF=1
to opt in; otherwise the lane reports skipped and exits 0.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from reverso.opencode_live_proof import (
    GATEWAY_BASE_URL,
    DEFAULT_PROOF_MODEL,
    OpencodeLiveProofInputs,
    run_opencode_live_proof,
)

OPT_IN_ENV = "REVERSO_OPENCODE_CLIENT_LIVE_PROOF"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run opt-in OpenCode Messages live proof"
    )
    parser.add_argument("--base-url", default=GATEWAY_BASE_URL)
    parser.add_argument("--model", default=DEFAULT_PROOF_MODEL)
    parser.add_argument("--json", action="store_true", help="emit secret-free JSON")
    args = parser.parse_args()

    if os.environ.get(OPT_IN_ENV) != "1":
        report = {
            "lane": "opencode-messages",
            "status": "skipped",
            "reason": f"set {OPT_IN_ENV}=1 to opt in",
        }
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0

    report = run_opencode_live_proof(
        OpencodeLiveProofInputs(base_url=args.base_url, model=args.model)
    )
    payload = report.to_public_dict()
    payload["lane"] = "opencode-messages"
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if report.status == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
