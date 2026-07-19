#!/usr/bin/env python3
"""Run the exact-opt-in credentialed Kimi proof and write redacted evidence."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from reverso.kimi_live_proof import (
    HttpLiveProofProbe,
    ProofFailure,
    require_live_opt_in,
    run_proof,
    write_manifest,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run credentialed Kimi live proof")
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        require_live_opt_in()
    except ProofFailure as exc:
        print(f"kimi live proof blocked: {exc.category}", file=sys.stderr)
        return 2

    manifest = run_proof(HttpLiveProofProbe())
    write_manifest(args.manifest, manifest)
    return 0 if manifest["overall"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
