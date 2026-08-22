#!/usr/bin/env python3
"""Verify or regenerate the OpenCode Go bare-exposure artifact."""

from __future__ import annotations

import sys
from pathlib import Path

from reverso.opencode_exposure import main

if __name__ == "__main__":
    repository_root = Path(__file__).resolve().parent.parent
    sys.exit(main(repo_root=repository_root, argv=sys.argv[1:]))
