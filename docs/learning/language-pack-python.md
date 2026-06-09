# Python Language Pack (reverso)

Per `r3dlex/skills/ai-sdlc-init/modules/language-packs.md`, this repo uses the
**Python pack** with the following detection evidence and selected commands.

## Detection evidence

| Signal | File | Match |
| --- | --- | --- |
| `pyproject.toml` | `pyproject.toml` | yes (root, hatchling build) |
| pytest | `pyproject.toml` `[tool.pytest.ini_options]` | yes (`asyncio_mode = "auto"`, `testpaths = ["tests"]`) |
| Python | `requires-python = ">=3.11"` | yes |

## Selected local checks

| Check | Command | Source |
| --- | --- | --- |
| Unit tests | `uv run pytest tests/ -v` | AGENTS.md "Verification" section |
| Lint | `uv run ruff check src/` | operating principles |
| Typecheck | `uv run mypy src/ --ignore-missing-imports` | verification section |

## CI checks (already present in `.github/workflows/`)

| Job | File | Trigger |
| --- | --- | --- |
| Pre-commit (new) | `ci-prek.yml` | push to main / PR |

## Intentionally skipped

- `dotnet ef migrations script`: not applicable (Python repo)
- `cargo test` / `cargo clippy`: not applicable (Python repo)
- `dotnet format --verify-no-changes`: not applicable
- New dependencies: not added; pack reuses existing `pytest` already configured
  in `pyproject.toml`. `ruff` / `mypy` are gated behind `prek` and not run
  unless the user opts in locally.

## Toolchain pin

`pyproject.toml` declares `requires-python = ">=3.11"`. CI uses
`ubuntu-latest` with the system Python; no `global.json`-equivalent is
present (Python toolchain is uv-driven, no version-pin file needed).
