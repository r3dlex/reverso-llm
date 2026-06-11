"""Suite-enforced dash check for project documentation.

Per the C3 done gate, every Markdown file under ``docs/`` and ``README.md`` at
the repo root must be free of U+2014 (em dash) and U+2013 (en dash). The repo
policy keeps prose ASCII so that grep-based audits stay accurate and so that
copy/paste into tooling does not introduce invisible character drift.

The dash character literals are referenced via ``\\u2014`` / ``\\u2013``
escapes so this test file itself remains dash-clean and does not trip its own
assertion when the scan reaches ``tests/`` in adjacent jobs.
"""

from __future__ import annotations

from pathlib import Path

EM_DASH = "\u2014"
EN_DASH = "\u2013"
FORBIDDEN = (EM_DASH, EN_DASH)


def _iter_targets() -> list[Path]:
    repo_root = Path(".").resolve()
    targets: list[Path] = []
    docs_dir = repo_root / "docs"
    if docs_dir.is_dir():
        targets.extend(sorted(docs_dir.rglob("*.md")))
    readme = repo_root / "README.md"
    if readme.is_file():
        targets.append(readme)
    return targets


def test_docs_have_no_em_or_en_dashes() -> None:
    targets = _iter_targets()
    assert targets, "expected to find at least docs/ markdown or README.md"

    offenders: list[str] = []
    for path in targets:
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            for ch in FORBIDDEN:
                if ch in line:
                    label = "U+2014 (em dash)" if ch == EM_DASH else "U+2013 (en dash)"
                    offenders.append(f"{path}:{lineno}: {label} in {line!r}")

    assert not offenders, "forbidden dash characters found:\n" + "\n".join(offenders)
