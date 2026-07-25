from __future__ import annotations

import importlib.util
import json
import os
import stat
from pathlib import Path
from types import ModuleType

import pytest

SLUG = "codex-encrypted-content-include-compatibility"
SCRIPT = Path(__file__).parents[2] / "scripts" / "reconcile_northstar_handoff.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("northstar_reconcile", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


reconciler = _load_script()


def _json(path: Path) -> dict:
    return json.loads(path.read_text())


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n")


def _fixture(root: Path) -> None:
    (root / ".ai/workflows").mkdir(parents=True)
    (root / ".ai/traceability").mkdir(parents=True)
    (root / ".ai/handoff").mkdir(parents=True)
    (root / ".ai/work-intake").mkdir(parents=True)
    (root / "docs/specifications/ACTIVE").mkdir(parents=True)

    _write_json(
        root / ".ai/workflows/repo-workflow.json",
        {
            "schema_version": "1.0",
            "optional_branches": [
                {
                    "id": "existing-branch",
                    "enabled_when": "always",
                    "status": "available",
                }
            ],
        },
    )
    _write_json(
        root / ".ai/traceability/graph.json",
        {
            "schema_version": "1.1",
            "root_repo_id": "reverso-root",
            "topology_type": "standalone",
            "generated_at": "2026-07-24T00:00:00Z",
            "nodes": [
                {
                    "id": "workflow:reverso-root:repo-workflow",
                    "type": "workflow",
                    "title": "Repo workflow manifest",
                    "status": "active",
                    "repo_id": "reverso-root",
                    "path": ".ai/workflows/repo-workflow.json",
                    "backlinks": [],
                }
            ],
            "edges": [],
        },
    )
    (root / f"docs/specifications/ACTIVE/{SLUG}.md").write_text(
        "---\n"
        "title: Compatibility spec\n"
        "status: active\n"
        f"slug: {SLUG}\n"
        "---\n\n"
        "# Compatibility spec\n"
    )
    (root / f".ai/work-intake/{SLUG}.md").write_text(
        "---\n"
        "title: Compatibility work item\n"
        "status: ready-for-agent\n"
        f"slug: {SLUG}\n"
        "---\n\n"
        "# Work item\n\n"
        f"- **Traceability node:** `issue:reverso-root:{SLUG}`\n\n"
        "## Sliced goals\n\n"
        "| Slice | Title | Status |\n"
        "|---|---|---|\n"
        "| S0 | Reconcile handoff | complete |\n"
        "| S1 | Implement compatibility | ready-for-agent |\n"
        "| S2 | Integration proof | blocked |\n\n"
        "## Acceptance criteria\n\n"
        "1. Complete.\n"
    )


def _run(
    root: Path, replace_fn=None, completed_via: str | None = None
) -> dict[str, str]:
    return reconciler.reconcile(
        root=root,
        spec=f"docs/specifications/ACTIVE/{SLUG}.md",
        work_item=f".ai/work-intake/{SLUG}.md",
        slug=SLUG,
        completed_via=completed_via,
        replace_fn=replace_fn,
    )


def _target_bytes(root: Path) -> tuple[bytes, bytes, bytes]:
    return (
        (root / ".ai/workflows/repo-workflow.json").read_bytes(),
        (root / ".ai/traceability/graph.json").read_bytes(),
        (root / f".ai/handoff/northstar-{SLUG}.md").read_bytes(),
    )


def _complete_goals(root: Path) -> None:
    work_item = root / f".ai/work-intake/{SLUG}.md"
    work_item.write_text(
        work_item.read_text()
        .replace(
            "| S1 | Implement compatibility | ready-for-agent |",
            "| S1 | Implement compatibility | completed in PR #91 |",
        )
        .replace(
            "| S2 | Integration proof | blocked |",
            "| S2 | Integration proof | deferred |",
        )
    )


def test_reconcile_creates_governed_manifest_graph_and_handoff(tmp_path: Path) -> None:
    _fixture(tmp_path)

    result = _run(tmp_path)

    manifest = _json(tmp_path / result["manifest"])
    graph = _json(tmp_path / result["graph"])
    handoff = (tmp_path / result["handoff"]).read_text()
    branch_id = f"northstar-handoff-{SLUG}"
    assert [b["id"] for b in manifest["optional_branches"]].count(branch_id) == 1
    assert (
        next(b for b in manifest["optional_branches"] if b["id"] == branch_id)["status"]
        == "available"
    )

    expected_ids = {
        f"issue:reverso-root:{SLUG}",
        f"plan:reverso-root:northstar-{SLUG}",
        f"handoff:reverso-root:northstar-{SLUG}",
    }
    nodes = {node["id"]: node for node in graph["nodes"]}
    assert expected_ids <= nodes.keys()
    assert nodes[f"plan:reverso-root:northstar-{SLUG}"]["backlinks"] == [
        f"issue:reverso-root:{SLUG}"
    ]
    assert nodes[f"handoff:reverso-root:northstar-{SLUG}"]["backlinks"] == [
        f"plan:reverso-root:northstar-{SLUG}"
    ]
    edge_keys = {
        (edge["source"], edge["target"], edge["relation"]) for edge in graph["edges"]
    }
    assert (
        f"issue:reverso-root:{SLUG}",
        f"plan:reverso-root:northstar-{SLUG}",
        "planned-by",
    ) in edge_keys
    assert (
        f"plan:reverso-root:northstar-{SLUG}",
        f"handoff:reverso-root:northstar-{SLUG}",
        "summarized-by",
    ) in edge_keys
    assert handoff.startswith("---\ntitle:")
    assert f"slug: {SLUG}" in handoff
    assert f"`issue:reverso-root:{SLUG}`" in handoff
    assert "`.ai/traceability/graph.json`" in handoff
    assert "`.ai/workflows/repo-workflow.json`" in handoff
    assert "| S1 | Implement compatibility | ready-for-agent |" in handoff


def test_reconcile_is_byte_for_byte_idempotent(tmp_path: Path) -> None:
    _fixture(tmp_path)
    _run(tmp_path)
    first = _target_bytes(tmp_path)
    first_counts = (
        len(_json(tmp_path / ".ai/workflows/repo-workflow.json")["optional_branches"]),
        len(_json(tmp_path / ".ai/traceability/graph.json")["nodes"]),
        len(_json(tmp_path / ".ai/traceability/graph.json")["edges"]),
    )

    _run(tmp_path)

    assert _target_bytes(tmp_path) == first
    assert (
        len(_json(tmp_path / ".ai/workflows/repo-workflow.json")["optional_branches"]),
        len(_json(tmp_path / ".ai/traceability/graph.json")["nodes"]),
        len(_json(tmp_path / ".ai/traceability/graph.json")["edges"]),
    ) == first_counts


def test_completed_reconcile_marks_owned_state_complete(tmp_path: Path) -> None:
    _fixture(tmp_path)
    _complete_goals(tmp_path)
    completed_via = "PRs #90, #91, and #92"

    result = _run(tmp_path, completed_via=completed_via)

    manifest = _json(tmp_path / result["manifest"])
    graph = _json(tmp_path / result["graph"])
    handoff = (tmp_path / result["handoff"]).read_text()
    branch = next(
        branch
        for branch in manifest["optional_branches"]
        if branch["id"] == f"northstar-handoff-{SLUG}"
    )
    assert branch["status"] == "complete"
    assert branch["completed_via"] == completed_via
    nodes = {node["id"]: node for node in graph["nodes"]}
    assert nodes[result["issue_id"]]["status"] == "complete"
    assert nodes[result["plan_id"]]["status"] == "complete"
    assert nodes[result["handoff_id"]]["status"] == "complete"
    assert "\nstatus: complete\n" in handoff
    assert f"Completion: Implemented by {completed_via}.\n" in handoff


def test_completed_reconcile_is_byte_for_byte_idempotent(tmp_path: Path) -> None:
    _fixture(tmp_path)
    _complete_goals(tmp_path)
    completed_via = "PRs #90, #91, and #92"
    _run(tmp_path, completed_via=completed_via)
    first = _target_bytes(tmp_path)

    _run(tmp_path, completed_via=completed_via)

    assert _target_bytes(tmp_path) == first


def test_active_reconcile_refuses_to_reopen_completed_handoff(
    tmp_path: Path,
) -> None:
    _fixture(tmp_path)
    _complete_goals(tmp_path)
    _run(tmp_path, completed_via="PRs #90, #91, and #92")
    before = _target_bytes(tmp_path)

    with pytest.raises(
        reconciler.ReconciliationError,
        match="completed handoff requires --completed-via",
    ):
        _run(tmp_path)

    assert _target_bytes(tmp_path) == before


@pytest.mark.parametrize(
    "completed_via",
    [
        "",
        "   ",
        "PR #90\nPR #91",
        f"PR #90 {chr(0x2013)} merged",
        f"PR #90 {chr(0x2014)} merged",
    ],
)
def test_invalid_completed_via_fails_before_mutation(
    tmp_path: Path, completed_via: str
) -> None:
    _fixture(tmp_path)
    manifest_path = tmp_path / ".ai/workflows/repo-workflow.json"
    graph_path = tmp_path / ".ai/traceability/graph.json"
    before = (manifest_path.read_bytes(), graph_path.read_bytes())

    with pytest.raises(reconciler.ReconciliationError, match="completed-via"):
        _run(tmp_path, completed_via=completed_via)

    assert (manifest_path.read_bytes(), graph_path.read_bytes()) == before
    assert not (tmp_path / f".ai/handoff/northstar-{SLUG}.md").exists()


@pytest.mark.parametrize(
    "unfinished_status", ["ready-for-agent", "in-progress", "blocked"]
)
def test_completed_reconcile_rejects_unfinished_goals_before_mutation(
    tmp_path: Path, unfinished_status: str
) -> None:
    _fixture(tmp_path)
    work_item_path = tmp_path / f".ai/work-intake/{SLUG}.md"
    work_item_path.write_text(
        work_item_path.read_text()
        .replace(
            "| S1 | Implement compatibility | ready-for-agent |",
            f"| S1 | Implement compatibility | {unfinished_status} |",
        )
        .replace(
            "| S2 | Integration proof | blocked |",
            "| S2 | Integration proof | deferred |",
        )
    )
    manifest_path = tmp_path / ".ai/workflows/repo-workflow.json"
    graph_path = tmp_path / ".ai/traceability/graph.json"
    before = (manifest_path.read_bytes(), graph_path.read_bytes())

    with pytest.raises(
        reconciler.ReconciliationError,
        match="unfinished work item sliced goals",
    ):
        _run(tmp_path, completed_via="PRs #90, #91, and #92")

    assert (manifest_path.read_bytes(), graph_path.read_bytes()) == before
    assert not (tmp_path / f".ai/handoff/northstar-{SLUG}.md").exists()


@pytest.mark.parametrize(
    "unfinished_status", ["ready-for-agent", "in-progress", "blocked"]
)
def test_completed_reconcile_rejects_unfinished_spec_goals_before_mutation(
    tmp_path: Path, unfinished_status: str
) -> None:
    _fixture(tmp_path)
    _complete_goals(tmp_path)
    spec_path = tmp_path / f"docs/specifications/ACTIVE/{SLUG}.md"
    spec_path.write_text(
        spec_path.read_text() + "\n## Sliced goals\n\n"
        "| Slice | Title | Status |\n"
        "|---|---|---|\n"
        f"| S1 | Implement compatibility | {unfinished_status} |\n"
    )
    manifest_path = tmp_path / ".ai/workflows/repo-workflow.json"
    graph_path = tmp_path / ".ai/traceability/graph.json"
    before = (manifest_path.read_bytes(), graph_path.read_bytes())

    with pytest.raises(
        reconciler.ReconciliationError,
        match="unfinished spec sliced goals",
    ):
        _run(tmp_path, completed_via="PRs #90, #91, and #92")

    assert (manifest_path.read_bytes(), graph_path.read_bytes()) == before
    assert not (tmp_path / f".ai/handoff/northstar-{SLUG}.md").exists()


@pytest.mark.parametrize("fail_after", [1, 2, 3])
def test_rerun_recovers_after_each_replacement_boundary(
    tmp_path: Path, fail_after: int
) -> None:
    clean = tmp_path / "clean"
    damaged = tmp_path / "damaged"
    _fixture(clean)
    _fixture(damaged)
    _run(clean)
    clean_bytes = _target_bytes(clean)
    replacements = 0

    def flaky_replace(source: str | Path, target: str | Path) -> None:
        nonlocal replacements
        os.replace(source, target)
        replacements += 1
        if replacements == fail_after:
            raise OSError(f"injected failure after replacement {fail_after}")

    with pytest.raises(OSError, match="injected failure"):
        _run(damaged, replace_fn=flaky_replace)

    _run(damaged)

    assert _target_bytes(damaged) == clean_bytes


@pytest.mark.parametrize("fail_after", [1, 2, 3])
def test_completed_rerun_recovers_after_each_replacement_boundary(
    tmp_path: Path, fail_after: int
) -> None:
    clean = tmp_path / "clean"
    damaged = tmp_path / "damaged"
    completed_via = "PRs #90, #91, and #92"
    _fixture(clean)
    _fixture(damaged)
    _complete_goals(clean)
    _complete_goals(damaged)
    _run(clean)
    _run(damaged)
    _run(clean, completed_via=completed_via)
    clean_bytes = _target_bytes(clean)
    replacements = 0

    def flaky_replace(source: str | Path, target: str | Path) -> None:
        nonlocal replacements
        os.replace(source, target)
        replacements += 1
        if replacements == fail_after:
            raise OSError(f"injected failure after replacement {fail_after}")

    with pytest.raises(OSError, match="injected failure"):
        _run(damaged, replace_fn=flaky_replace, completed_via=completed_via)

    _run(damaged, completed_via=completed_via)

    assert _target_bytes(damaged) == clean_bytes


def test_reconcile_repairs_stale_owned_records_without_touching_unrelated(
    tmp_path: Path,
) -> None:
    _fixture(tmp_path)
    _run(tmp_path)
    manifest_path = tmp_path / ".ai/workflows/repo-workflow.json"
    graph_path = tmp_path / ".ai/traceability/graph.json"
    handoff_path = tmp_path / f".ai/handoff/northstar-{SLUG}.md"
    manifest = _json(manifest_path)
    graph = _json(graph_path)
    branch_id = f"northstar-handoff-{SLUG}"
    next(b for b in manifest["optional_branches"] if b["id"] == branch_id)["status"] = (
        "stale"
    )
    for node in graph["nodes"]:
        if node["id"].startswith(
            (
                f"issue:reverso-root:{SLUG}",
                f"plan:reverso-root:northstar-{SLUG}",
                f"handoff:reverso-root:northstar-{SLUG}",
            )
        ):
            node["status"] = "stale"
            node["path"] = "stale/path"
            node["backlinks"] = []
    for edge in graph["edges"]:
        if SLUG in edge["source"]:
            edge["evidence_path"] = "stale/evidence"
    handoff_path.write_text("---\ntitle: Stale\nstatus: stale\nslug: stale\n---\n")
    _write_json(manifest_path, manifest)
    _write_json(graph_path, graph)

    _run(tmp_path)

    repaired_manifest = _json(manifest_path)
    repaired_graph = _json(graph_path)
    assert next(
        b
        for b in repaired_manifest["optional_branches"]
        if b["id"] == "existing-branch"
    ) == {
        "id": "existing-branch",
        "enabled_when": "always",
        "status": "available",
    }
    assert (
        next(b for b in repaired_manifest["optional_branches"] if b["id"] == branch_id)[
            "status"
        ]
        == "available"
    )
    nodes = {node["id"]: node for node in repaired_graph["nodes"]}
    assert nodes[f"issue:reverso-root:{SLUG}"]["path"] == f".ai/work-intake/{SLUG}.md"
    assert nodes[f"plan:reverso-root:northstar-{SLUG}"]["backlinks"] == [
        f"issue:reverso-root:{SLUG}"
    ]
    assert f"slug: {SLUG}" in handoff_path.read_text()


@pytest.mark.parametrize(
    "mutation,expected",
    [
        ("spec_slug", "spec frontmatter slug"),
        ("work_item_issue", "expected issue ID"),
        ("graph_schema", "schema_version"),
        ("graph_json", "not valid JSON"),
        ("unresolved_edge", "unresolved edge"),
        ("duplicate_node", "conflicting node IDs"),
        ("conflicting_type", "conflicting node type"),
        ("conflicting_path", "conflicting node path"),
        ("conflicting_edge", "conflicting edge relation"),
    ],
)
def test_negative_validation_fails_before_mutation(
    tmp_path: Path, mutation: str, expected: str
) -> None:
    _fixture(tmp_path)
    manifest_path = tmp_path / ".ai/workflows/repo-workflow.json"
    graph_path = tmp_path / ".ai/traceability/graph.json"
    spec_path = tmp_path / f"docs/specifications/ACTIVE/{SLUG}.md"
    work_item_path = tmp_path / f".ai/work-intake/{SLUG}.md"
    graph = _json(graph_path)
    issue_id = f"issue:reverso-root:{SLUG}"
    plan_id = f"plan:reverso-root:northstar-{SLUG}"

    if mutation == "spec_slug":
        spec_path.write_text(
            spec_path.read_text().replace(f"slug: {SLUG}", "slug: wrong")
        )
    elif mutation == "work_item_issue":
        work_item_path.write_text(
            work_item_path.read_text().replace(issue_id, "issue:wrong")
        )
    elif mutation == "graph_schema":
        graph["schema_version"] = "0.0"
        _write_json(graph_path, graph)
    elif mutation == "graph_json":
        graph_path.write_text("{")
    elif mutation == "unresolved_edge":
        graph["edges"].append(
            {
                "source": graph["nodes"][0]["id"],
                "target": "missing",
                "relation": "uses",
                "created_by": "test",
                "evidence_path": ".ai/traceability/graph.json",
            }
        )
        _write_json(graph_path, graph)
    elif mutation == "duplicate_node":
        graph["nodes"].append(dict(graph["nodes"][0]))
        _write_json(graph_path, graph)
    elif mutation in {"conflicting_type", "conflicting_edge"}:
        graph["nodes"].extend(
            [
                {
                    "id": issue_id,
                    "type": "plan" if mutation == "conflicting_type" else "issue",
                    "title": "stale",
                    "status": "active",
                    "repo_id": "reverso-root",
                    "path": f".ai/work-intake/{SLUG}.md",
                    "backlinks": [],
                },
                {
                    "id": plan_id,
                    "type": "plan",
                    "title": "stale",
                    "status": "active",
                    "repo_id": "reverso-root",
                    "path": f".ai/handoff/northstar-{SLUG}.md",
                    "backlinks": [issue_id],
                },
            ]
        )
        if mutation == "conflicting_edge":
            graph["edges"].append(
                {
                    "source": issue_id,
                    "target": plan_id,
                    "relation": "conflicts-with",
                    "created_by": "test",
                    "evidence_path": f".ai/work-intake/{SLUG}.md",
                }
            )
        _write_json(graph_path, graph)
    elif mutation == "conflicting_path":
        graph["nodes"].append(
            {
                "id": "issue:reverso-root:other",
                "type": "issue",
                "title": "other",
                "status": "active",
                "repo_id": "reverso-root",
                "path": f".ai/work-intake/{SLUG}.md",
                "backlinks": [],
            }
        )
        _write_json(graph_path, graph)

    before = (manifest_path.read_bytes(), graph_path.read_bytes())
    with pytest.raises(reconciler.ReconciliationError, match=expected):
        _run(tmp_path)

    assert (manifest_path.read_bytes(), graph_path.read_bytes()) == before
    assert not (tmp_path / f".ai/handoff/northstar-{SLUG}.md").exists()


def test_missing_input_fails_without_mutating_governance(tmp_path: Path) -> None:
    _fixture(tmp_path)
    manifest_path = tmp_path / ".ai/workflows/repo-workflow.json"
    graph_path = tmp_path / ".ai/traceability/graph.json"
    spec_path = tmp_path / f"docs/specifications/ACTIVE/{SLUG}.md"
    spec_path.unlink()
    before = (manifest_path.read_bytes(), graph_path.read_bytes())

    with pytest.raises(reconciler.ReconciliationError, match="spec does not exist"):
        reconciler.reconcile(
            root=tmp_path,
            spec=f"docs/specifications/ACTIVE/{SLUG}.md",
            work_item=f".ai/work-intake/{SLUG}.md",
            slug=SLUG,
        )

    assert (manifest_path.read_bytes(), graph_path.read_bytes()) == before


@pytest.mark.parametrize(
    ("input_name", "alternate_path", "expected"),
    [
        ("spec", "docs/specifications/ACTIVE/alternate.md", "canonical spec path"),
        ("work_item", ".ai/work-intake/alternate.md", "canonical work item path"),
    ],
)
def test_valid_input_at_noncanonical_path_fails_before_mutation(
    tmp_path: Path, input_name: str, alternate_path: str, expected: str
) -> None:
    _fixture(tmp_path)
    manifest_path = tmp_path / ".ai/workflows/repo-workflow.json"
    graph_path = tmp_path / ".ai/traceability/graph.json"
    canonical = {
        "spec": tmp_path / f"docs/specifications/ACTIVE/{SLUG}.md",
        "work_item": tmp_path / f".ai/work-intake/{SLUG}.md",
    }
    alternate = tmp_path / alternate_path
    alternate.write_bytes(canonical[input_name].read_bytes())
    arguments = {
        "root": tmp_path,
        "spec": f"docs/specifications/ACTIVE/{SLUG}.md",
        "work_item": f".ai/work-intake/{SLUG}.md",
        "slug": SLUG,
    }
    arguments[input_name] = alternate_path
    before = (manifest_path.read_bytes(), graph_path.read_bytes())

    with pytest.raises(reconciler.ReconciliationError, match=expected):
        reconciler.reconcile(**arguments)

    assert (manifest_path.read_bytes(), graph_path.read_bytes()) == before
    assert not (tmp_path / f".ai/handoff/northstar-{SLUG}.md").exists()


@pytest.mark.parametrize(
    ("source", "target", "relation"),
    [
        (
            f"issue:reverso-root:{SLUG}",
            "plan:reverso-root:other",
            "planned-by",
        ),
        (
            "issue:reverso-root:other",
            f"plan:reverso-root:northstar-{SLUG}",
            "planned-by",
        ),
        (
            f"plan:reverso-root:northstar-{SLUG}",
            "handoff:reverso-root:other",
            "summarized-by",
        ),
        (
            "plan:reverso-root:other",
            f"handoff:reverso-root:northstar-{SLUG}",
            "summarized-by",
        ),
    ],
)
def test_conflicting_owned_semantic_edge_fails_before_mutation(
    tmp_path: Path, source: str, target: str, relation: str
) -> None:
    _fixture(tmp_path)
    _run(tmp_path)
    manifest_path = tmp_path / ".ai/workflows/repo-workflow.json"
    graph_path = tmp_path / ".ai/traceability/graph.json"
    handoff_path = tmp_path / f".ai/handoff/northstar-{SLUG}.md"
    graph = _json(graph_path)
    graph["nodes"].extend(
        [
            {
                "id": "issue:reverso-root:other",
                "type": "issue",
                "title": "other issue",
                "status": "active",
                "repo_id": "reverso-root",
                "path": ".ai/work-intake/other.md",
                "backlinks": [],
            },
            {
                "id": "plan:reverso-root:other",
                "type": "plan",
                "title": "other plan",
                "status": "active",
                "repo_id": "reverso-root",
                "path": ".ai/handoff/other-plan.md",
                "backlinks": [],
            },
            {
                "id": "handoff:reverso-root:other",
                "type": "handoff",
                "title": "other handoff",
                "status": "active",
                "repo_id": "reverso-root",
                "path": ".ai/handoff/other-handoff.md",
                "backlinks": [],
            },
        ]
    )
    graph["edges"].append(
        {
            "source": source,
            "target": target,
            "relation": relation,
            "created_by": "test",
            "evidence_path": ".ai/traceability/graph.json",
        }
    )
    _write_json(graph_path, graph)
    before = (
        manifest_path.read_bytes(),
        graph_path.read_bytes(),
        handoff_path.read_bytes(),
    )

    with pytest.raises(reconciler.ReconciliationError, match="conflicting owned edge"):
        _run(tmp_path)

    assert (
        manifest_path.read_bytes(),
        graph_path.read_bytes(),
        handoff_path.read_bytes(),
    ) == before


def test_symlinked_handoff_directory_cannot_write_outside_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    outside = tmp_path / "outside"
    _fixture(root)
    outside.mkdir()
    handoff_dir = root / ".ai/handoff"
    handoff_dir.rmdir()
    handoff_dir.symlink_to(outside, target_is_directory=True)
    manifest_path = root / ".ai/workflows/repo-workflow.json"
    graph_path = root / ".ai/traceability/graph.json"
    before = (manifest_path.read_bytes(), graph_path.read_bytes())

    with pytest.raises(reconciler.ReconciliationError, match="symlink"):
        _run(root)

    assert (manifest_path.read_bytes(), graph_path.read_bytes()) == before
    assert not (outside / f"northstar-{SLUG}.md").exists()


def test_precreated_predictable_temp_symlink_is_not_followed(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _fixture(root)
    victim = tmp_path / "victim.json"
    victim.write_text("do not overwrite\n")
    predictable_temp = (
        root / ".ai/workflows" / ".repo-workflow.json.northstar-reconcile.tmp"
    )
    predictable_temp.symlink_to(victim)

    _run(root)

    assert victim.read_text() == "do not overwrite\n"
    assert predictable_temp.is_symlink()


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("malformed_node", "node fields"),
        ("malformed_edge", "edge fields"),
    ],
)
def test_malformed_graph_record_fails_before_mutation(
    tmp_path: Path, mutation: str, expected: str
) -> None:
    _fixture(tmp_path)
    manifest_path = tmp_path / ".ai/workflows/repo-workflow.json"
    graph_path = tmp_path / ".ai/traceability/graph.json"
    graph = _json(graph_path)
    if mutation == "malformed_node":
        graph["nodes"].append({"id": "malformed:node", "backlinks": []})
    else:
        workflow_id = graph["nodes"][0]["id"]
        graph["edges"].append(
            {
                "source": workflow_id,
                "target": workflow_id,
                "relation": "references",
            }
        )
    _write_json(graph_path, graph)
    before = (manifest_path.read_bytes(), graph_path.read_bytes())

    with pytest.raises(reconciler.ReconciliationError, match=expected):
        _run(tmp_path)

    assert (manifest_path.read_bytes(), graph_path.read_bytes()) == before
    assert not (tmp_path / f".ai/handoff/northstar-{SLUG}.md").exists()


@pytest.mark.parametrize(
    ("input_name", "alternate_path"),
    [
        ("spec", ".ai/evals/results/spec.md"),
        ("work_item", ".ai/evals/results/work-item.md"),
    ],
)
def test_canonical_source_symlink_is_rejected(
    tmp_path: Path, input_name: str, alternate_path: str
) -> None:
    _fixture(tmp_path)
    manifest_path = tmp_path / ".ai/workflows/repo-workflow.json"
    graph_path = tmp_path / ".ai/traceability/graph.json"
    canonical = {
        "spec": tmp_path / f"docs/specifications/ACTIVE/{SLUG}.md",
        "work_item": tmp_path / f".ai/work-intake/{SLUG}.md",
    }[input_name]
    alternate = tmp_path / alternate_path
    alternate.parent.mkdir(parents=True, exist_ok=True)
    alternate.write_bytes(canonical.read_bytes())
    canonical.unlink()
    canonical.symlink_to(alternate)
    before = (manifest_path.read_bytes(), graph_path.read_bytes())

    with pytest.raises(reconciler.ReconciliationError, match="symlink"):
        _run(tmp_path)

    assert (manifest_path.read_bytes(), graph_path.read_bytes()) == before
    assert not (tmp_path / f".ai/handoff/northstar-{SLUG}.md").exists()


@pytest.mark.parametrize(
    "branch",
    [
        {"id": "bad"},
        {"id": "bad", "enabled_when": "", "status": "available"},
        {"id": "bad", "enabled_when": "always", "status": 1},
    ],
)
def test_malformed_manifest_branch_fails_before_mutation(
    tmp_path: Path, branch: dict
) -> None:
    _fixture(tmp_path)
    manifest_path = tmp_path / ".ai/workflows/repo-workflow.json"
    graph_path = tmp_path / ".ai/traceability/graph.json"
    manifest = _json(manifest_path)
    manifest["optional_branches"].append(branch)
    _write_json(manifest_path, manifest)
    before = (manifest_path.read_bytes(), graph_path.read_bytes())

    with pytest.raises(reconciler.ReconciliationError, match="branch fields"):
        _run(tmp_path)

    assert (manifest_path.read_bytes(), graph_path.read_bytes()) == before
    assert not (tmp_path / f".ai/handoff/northstar-{SLUG}.md").exists()


@pytest.mark.parametrize("node_type", ["eval-result", "trajectory-trace"])
def test_documented_schema_1_1_node_types_are_preserved(
    tmp_path: Path, node_type: str
) -> None:
    _fixture(tmp_path)
    graph_path = tmp_path / ".ai/traceability/graph.json"
    graph = _json(graph_path)
    node = {
        "id": f"{node_type}:reverso-root:fixture",
        "type": node_type,
        "title": f"{node_type} fixture",
        "status": "active",
        "repo_id": "reverso-root",
        "path": ".ai/evals/fixture.json",
        "backlinks": [],
    }
    graph["nodes"].append(node)
    _write_json(graph_path, graph)

    _run(tmp_path)

    assert node in _json(graph_path)["nodes"]


def test_reconcile_preserves_governance_file_permissions(tmp_path: Path) -> None:
    _fixture(tmp_path)
    manifest_path = tmp_path / ".ai/workflows/repo-workflow.json"
    graph_path = tmp_path / ".ai/traceability/graph.json"
    expected_modes = {
        manifest_path: stat.S_IMODE(manifest_path.stat().st_mode),
        graph_path: stat.S_IMODE(graph_path.stat().st_mode),
    }

    result = _run(tmp_path)

    for path, expected_mode in expected_modes.items():
        assert stat.S_IMODE(path.stat().st_mode) == expected_mode
    handoff_path = tmp_path / result["handoff"]
    assert stat.S_IMODE(handoff_path.stat().st_mode) == 0o644
