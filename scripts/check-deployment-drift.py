#!/usr/bin/env python3
"""Validate Reverso deployment provenance before governed actions."""

from __future__ import annotations

import sys
from pathlib import Path

from reverso.deployment_drift import main

if __name__ == "__main__":
    repository_root = Path(__file__).resolve().parent.parent
    sys.exit(main(repo_root=repository_root))
