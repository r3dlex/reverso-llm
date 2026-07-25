#!/usr/bin/env python3
"""Deterministically reconcile a Northstar handoff into repo governance."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

MANIFEST_PATH = Path(".ai/workflows/repo-workflow.json")
GRAPH_PATH = Path(".ai/traceability/graph.json")
HANDOFF_DIR = Path(".ai/handoff")
SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SUPPORTED_NODE_TYPES = frozenset(
    {
        "adr",
        "brd",
        "eval-result",
        "evidence",
        "handoff",
        "issue",
        "plan",
        "pr",
        "prd",
        "pull_request",
        "test",
        "trajectory-trace",
        "validation",
        "workflow",
    }
)


class ReconciliationError(ValueError):
    """Raised when inputs are unsafe or violate the governed schema."""


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ReconciliationError(f"{label} is not valid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ReconciliationError(f"{label} must be a JSON object: {path}")
    return value


def _frontmatter(text: str, label: str) -> dict[str, str]:
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise ReconciliationError(f"{label} must start with YAML frontmatter")
    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise ReconciliationError(f"{label} has unterminated YAML frontmatter") from exc

    result: dict[str, str] = {}
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition(":")
        if not separator or not key.strip() or not value.strip():
            raise ReconciliationError(f"{label} frontmatter must use scalar mappings")
        normalized_key = key.strip()
        if normalized_key in result:
            raise ReconciliationError(
                f"{label} frontmatter repeats key: {normalized_key}"
            )
        result[normalized_key] = value.strip().strip("\"'")
    return result


def _reject_symlink_components(root: Path, relative: Path, label: str) -> None:
    current = root
    for component in relative.parts:
        current /= component
        if current.is_symlink():
            raise ReconciliationError(
                f"{label} must not use symlink components: {relative.as_posix()}"
            )


def _repo_file(root: Path, raw_path: str, label: str) -> tuple[str, Path]:
    requested = Path(raw_path)
    candidate = requested if requested.is_absolute() else root / requested
    resolved = candidate.resolve()
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise ReconciliationError(f"{label} must remain inside repo root") from exc
    if not resolved.is_file():
        raise ReconciliationError(f"{label} does not exist: {relative.as_posix()}")
    return relative.as_posix(), resolved


def _extract_sliced_goals(document: str, label: str) -> str:
    lines = document.splitlines()
    try:
        start = lines.index("## Sliced goals")
    except ValueError as exc:
        raise ReconciliationError(
            f"{label} must contain a Sliced goals section"
        ) from exc

    end = len(lines)
    for index in range(start + 1, len(lines)):
        if lines[index].startswith("## "):
            end = index
            break
    section = "\n".join(lines[start:end]).strip()
    if "|" not in section:
        raise ReconciliationError(f"{label} Sliced goals section must contain a table")
    return section


def _validate_terminal_sliced_goals(sliced_goals: str, label: str) -> None:
    rows = [
        [cell.strip() for cell in line.strip().strip("|").split("|")]
        for line in sliced_goals.splitlines()
        if line.strip().startswith("|")
    ]
    if len(rows) < 3 or "status" not in [cell.casefold() for cell in rows[0]]:
        raise ReconciliationError(
            f"{label} Sliced goals table must contain a Status column and goals"
        )
    status_index = [cell.casefold() for cell in rows[0]].index("status")
    unfinished = []
    for row in rows[2:]:
        if len(row) != len(rows[0]):
            raise ReconciliationError(f"{label} Sliced goals table is malformed")
        status = row[status_index]
        if not re.fullmatch(
            r"(?:complete(?:d)?|shipped|deferred)(?:\s+.*)?",
            status,
            re.IGNORECASE,
        ):
            unfinished.append(status)
    if unfinished:
        raise ReconciliationError(
            f"unfinished {label} sliced goals prevent completion: "
            f"{', '.join(unfinished)}"
        )


def _validate_manifest(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    if manifest.get("schema_version") != "1.0":
        raise ReconciliationError("workflow manifest schema_version must be 1.0")
    branches = manifest.get("optional_branches")
    if not isinstance(branches, list) or not all(
        isinstance(branch, dict) for branch in branches
    ):
        raise ReconciliationError("workflow manifest optional_branches must be objects")
    required_branch_fields = ("id", "enabled_when", "status")
    if not all(
        all(
            isinstance(branch.get(field), str) and branch[field]
            for field in required_branch_fields
        )
        for branch in branches
    ):
        raise ReconciliationError(
            "workflow manifest branch fields must be non-empty strings"
        )
    ids = [branch["id"] for branch in branches]
    if len(ids) != len(set(ids)):
        raise ReconciliationError("workflow manifest contains conflicting branch IDs")
    return branches


def _validate_graph(
    graph: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if graph.get("schema_version") != "1.1":
        raise ReconciliationError("traceability graph schema_version must be 1.1")
    if graph.get("root_repo_id") != "reverso-root":
        raise ReconciliationError(
            "traceability graph root_repo_id must be reverso-root"
        )

    nodes = graph.get("nodes")
    edges = graph.get("edges")
    if not isinstance(nodes, list) or not all(isinstance(node, dict) for node in nodes):
        raise ReconciliationError("traceability graph nodes must be objects")
    if not isinstance(edges, list) or not all(isinstance(edge, dict) for edge in edges):
        raise ReconciliationError("traceability graph edges must be objects")

    node_ids = [node.get("id") for node in nodes]
    if not all(isinstance(node_id, str) and node_id for node_id in node_ids):
        raise ReconciliationError("traceability graph node IDs must be strings")
    if len(node_ids) != len(set(node_ids)):
        raise ReconciliationError("traceability graph contains conflicting node IDs")
    node_id_set = set(node_ids)

    required_node_fields = ("id", "type", "title", "status", "repo_id")
    for node in nodes:
        if not all(
            isinstance(node.get(field), str) and node[field]
            for field in required_node_fields
        ):
            raise ReconciliationError(
                "traceability graph node fields must be non-empty strings"
            )
        if node["type"] not in SUPPORTED_NODE_TYPES:
            raise ReconciliationError(
                f"traceability graph node type is unsupported: {node['type']}"
            )
        location_fields = [field for field in ("path", "host_url") if field in node]
        if not location_fields or not all(
            isinstance(node[field], str) and node[field] for field in location_fields
        ):
            raise ReconciliationError(
                "traceability graph nodes require a non-empty path or host_url"
            )

    edge_keys: list[tuple[str, str, str]] = []
    for edge in edges:
        required_edge_fields = (
            "source",
            "target",
            "relation",
            "created_by",
            "evidence_path",
        )
        if not all(
            isinstance(edge.get(field), str) and edge[field]
            for field in required_edge_fields
        ):
            raise ReconciliationError(
                "traceability graph edge fields must be non-empty strings"
            )
        source = edge["source"]
        target = edge["target"]
        relation = edge["relation"]
        if source not in node_id_set or target not in node_id_set:
            raise ReconciliationError(
                "traceability graph contains unresolved edge targets"
            )
        edge_keys.append((source, target, relation))
    if len(edge_keys) != len(set(edge_keys)):
        raise ReconciliationError("traceability graph contains conflicting edges")

    for node in nodes:
        backlinks = node.get("backlinks")
        if not isinstance(backlinks, list) or not all(
            isinstance(backlink, str) for backlink in backlinks
        ):
            raise ReconciliationError(
                "traceability graph backlinks must be string lists"
            )
        if any(backlink not in node_id_set for backlink in backlinks):
            raise ReconciliationError(
                "traceability graph contains unresolved backlinks"
            )
    return nodes, edges


def _replace_by_id(
    records: list[dict[str, Any]], record_id: str, replacement: dict[str, Any]
) -> None:
    for index, record in enumerate(records):
        if record.get("id") == record_id:
            records[index] = replacement
            return
    records.append(replacement)


def _replace_edge(edges: list[dict[str, Any]], replacement: dict[str, Any]) -> None:
    key = (
        replacement["source"],
        replacement["target"],
        replacement["relation"],
    )
    for index, edge in enumerate(edges):
        candidate = (edge.get("source"), edge.get("target"), edge.get("relation"))
        if candidate == key:
            edges[index] = replacement
            return
    edges.append(replacement)


def _render_handoff(
    *,
    slug: str,
    status: str,
    completed_via: str | None,
    spec_path: str,
    work_item_path: str,
    issue_id: str,
    plan_id: str,
    handoff_id: str,
    sliced_goals: str,
) -> str:
    handoff_path = f".ai/handoff/northstar-{slug}.md"
    manifest_id = f"northstar-handoff-{slug}"
    completion = (
        f"Completion: Implemented by {completed_via}.\n\n" if completed_via else ""
    )
    return (
        "---\n"
        f"title: Northstar A to B handoff for {slug}\n"
        f"status: {status}\n"
        f"slug: {slug}\n"
        "---\n\n"
        f"# Northstar A to B Handoff: {slug}\n\n"
        f"{completion}"
        "## Contract\n\n"
        f"- Spec: `{spec_path}`\n"
        f"- Work item: `{work_item_path}`\n"
        f"- Issue node: `{issue_id}`\n"
        f"- Plan node: `{plan_id}`\n"
        f"- Handoff node: `{handoff_id}`\n"
        f"- Handoff path: `{handoff_path}`\n"
        f"- Manifest record: `optional_branches[id={manifest_id}]` in "
        "`.ai/workflows/repo-workflow.json`\n"
        "- Traceability graph: `.ai/traceability/graph.json`\n\n"
        f"{sliced_goals}\n\n"
        "## Execution\n\n"
        "Autobahn consumes the ready goals in this handoff and ships each goal "
        "through its governed one-PR loop. Completed and deferred goals are not "
        "implementation inputs.\n"
    )


def _json_bytes(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode()


def _validate_output_path(
    root: Path, path: Path, label: str, *, must_exist: bool
) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ReconciliationError(f"{label} must remain inside repo root") from exc

    _reject_symlink_components(root, relative, label)

    parent = path.parent.resolve()
    try:
        parent.relative_to(root)
    except ValueError as exc:
        raise ReconciliationError(
            f"{label} parent must remain inside repo root"
        ) from exc
    if not parent.is_dir():
        raise ReconciliationError(
            f"{label} parent is not a directory: {relative.parent.as_posix()}"
        )
    if must_exist and not path.is_file():
        raise ReconciliationError(f"{label} is missing: {relative.as_posix()}")
    if not must_exist and path.exists() and not path.is_file():
        raise ReconciliationError(
            f"{label} is not a regular file: {relative.as_posix()}"
        )


def _write_temp(path: Path, content: bytes) -> Path:
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
    descriptor, raw_temp = tempfile.mkstemp(
        prefix=f".{path.name}.northstar-reconcile.",
        suffix=".tmp",
        dir=path.parent,
    )
    temp = Path(raw_temp)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    return temp


def reconcile(
    *,
    root: Path,
    spec: str,
    work_item: str,
    slug: str,
    completed_via: str | None = None,
    replace_fn: Callable[[str | Path, str | Path], None] | None = None,
) -> dict[str, str]:
    """Validate, render, and reconcile a single Northstar handoff."""

    root = root.resolve()
    if not root.is_dir():
        raise ReconciliationError(f"repo root is not a directory: {root}")
    if not SLUG_PATTERN.fullmatch(slug):
        raise ReconciliationError("slug must use lowercase kebab-case")
    if completed_via is not None:
        completed_via = completed_via.strip()
        forbidden_dashes = (chr(0x2013), chr(0x2014))
        if (
            not completed_via
            or "\n" in completed_via
            or "\r" in completed_via
            or any(dash in completed_via for dash in forbidden_dashes)
        ):
            raise ReconciliationError(
                "--completed-via must be a non-empty ASCII-dash single-line value"
            )

    canonical_spec_path = f"docs/specifications/ACTIVE/{slug}.md"
    canonical_work_item_path = f".ai/work-intake/{slug}.md"
    if spec != canonical_spec_path:
        raise ReconciliationError(f"canonical spec path must be {canonical_spec_path}")
    if work_item != canonical_work_item_path:
        raise ReconciliationError(
            f"canonical work item path must be {canonical_work_item_path}"
        )
    _reject_symlink_components(root, Path(spec), "spec")
    _reject_symlink_components(root, Path(work_item), "work item")
    spec_path, spec_file = _repo_file(root, spec, "spec")
    work_item_path, work_item_file = _repo_file(root, work_item, "work item")
    spec_text = spec_file.read_text()
    work_item_text = work_item_file.read_text()
    if _frontmatter(spec_text, "spec").get("slug") != slug:
        raise ReconciliationError("spec frontmatter slug does not match --slug")
    if _frontmatter(work_item_text, "work item").get("slug") != slug:
        raise ReconciliationError("work item frontmatter slug does not match --slug")

    issue_id = f"issue:reverso-root:{slug}"
    plan_id = f"plan:reverso-root:northstar-{slug}"
    handoff_id = f"handoff:reverso-root:northstar-{slug}"
    if f"`{issue_id}`" not in work_item_text:
        raise ReconciliationError("work item does not declare the expected issue ID")
    sliced_goals = _extract_sliced_goals(work_item_text, "work item")
    if completed_via is not None:
        _validate_terminal_sliced_goals(sliced_goals, "work item")
        if "## Sliced goals" in spec_text:
            spec_sliced_goals = _extract_sliced_goals(spec_text, "spec")
            _validate_terminal_sliced_goals(spec_sliced_goals, "spec")

    manifest_file = root / MANIFEST_PATH
    graph_file = root / GRAPH_PATH
    handoff_file = root / HANDOFF_DIR / f"northstar-{slug}.md"
    _validate_output_path(root, manifest_file, "workflow manifest", must_exist=True)
    _validate_output_path(root, graph_file, "traceability graph", must_exist=True)
    _validate_output_path(root, handoff_file, "handoff", must_exist=False)

    manifest = _load_json(manifest_file, "workflow manifest")
    branches = _validate_manifest(manifest)
    manifest_id = f"northstar-handoff-{slug}"

    graph = _load_json(graph_file, "traceability graph")
    nodes, edges = _validate_graph(graph)
    handoff_path = handoff_file.relative_to(root).as_posix()
    owned_ids = {issue_id, plan_id, handoff_id}
    terminal_records = [
        record
        for record in (*branches, *nodes)
        if (record.get("id") == manifest_id or record.get("id") in owned_ids)
        and record.get("status") == "complete"
    ]
    if completed_via is None and terminal_records:
        raise ReconciliationError("completed handoff requires --completed-via")

    is_complete = completed_via is not None
    handoff_status = "complete" if is_complete else "active"
    manifest_record = {
        "id": manifest_id,
        "enabled_when": "northstar_handoff_present",
        "status": "complete" if is_complete else "available",
    }
    if completed_via is not None:
        manifest_record["completed_via"] = completed_via
    _replace_by_id(branches, manifest_id, manifest_record)

    expected_nodes = (
        {
            "id": issue_id,
            "type": "issue",
            "title": f"northstar issue: {slug}",
            "status": "complete" if is_complete else "ready-for-agent",
            "repo_id": "reverso-root",
            "path": work_item_path,
            "backlinks": [],
        },
        {
            "id": plan_id,
            "type": "plan",
            "title": f"northstar sliced plan: {slug}",
            "status": handoff_status,
            "repo_id": "reverso-root",
            "path": handoff_path,
            "backlinks": [issue_id],
        },
        {
            "id": handoff_id,
            "type": "handoff",
            "title": f"northstar A to B handoff: {slug}",
            "status": handoff_status,
            "repo_id": "reverso-root",
            "path": handoff_path,
            "backlinks": [plan_id],
        },
    )

    expected_ids = {node["id"] for node in expected_nodes}
    expected_paths = {work_item_path, handoff_path}
    for node in nodes:
        node_id = node["id"]
        if node_id in expected_ids:
            expected_type = next(
                expected["type"]
                for expected in expected_nodes
                if expected["id"] == node_id
            )
            if node.get("type") != expected_type:
                raise ReconciliationError(f"conflicting node type for {node_id}")
            if node.get("repo_id") != "reverso-root":
                raise ReconciliationError(f"conflicting repo_id for {node_id}")
        elif node.get("path") in expected_paths:
            raise ReconciliationError(
                f"conflicting node path for {node_id}: {node.get('path')}"
            )
    for expected in expected_nodes:
        _replace_by_id(nodes, expected["id"], expected)

    expected_edges = (
        {
            "source": issue_id,
            "target": plan_id,
            "relation": "planned-by",
            "created_by": "northstar-reconciler",
            "evidence_path": work_item_path,
        },
        {
            "source": plan_id,
            "target": handoff_id,
            "relation": "summarized-by",
            "created_by": "northstar-reconciler",
            "evidence_path": handoff_path,
        },
    )
    expected_pairs = {
        (edge["source"], edge["target"]): edge["relation"] for edge in expected_edges
    }
    for edge in edges:
        pair = (edge["source"], edge["target"])
        if pair in expected_pairs and edge["relation"] != expected_pairs[pair]:
            raise ReconciliationError(
                f"conflicting edge relation for {edge['source']} -> {edge['target']}"
            )
        for expected in expected_edges:
            has_owned_endpoint = (
                edge["source"] == expected["source"]
                or edge["target"] == expected["target"]
            )
            if (
                edge["relation"] == expected["relation"]
                and has_owned_endpoint
                and pair != (expected["source"], expected["target"])
            ):
                raise ReconciliationError(
                    "conflicting owned edge for "
                    f"{expected['source']} -> {expected['target']}"
                )
    for expected in expected_edges:
        _replace_edge(edges, expected)

    _validate_manifest(manifest)
    _validate_graph(graph)
    handoff = _render_handoff(
        slug=slug,
        status=handoff_status,
        completed_via=completed_via,
        spec_path=spec_path,
        work_item_path=work_item_path,
        issue_id=issue_id,
        plan_id=plan_id,
        handoff_id=handoff_id,
        sliced_goals=sliced_goals,
    )
    handoff_frontmatter = _frontmatter(handoff, "rendered handoff")
    if handoff_frontmatter != {
        "title": f"Northstar A to B handoff for {slug}",
        "status": handoff_status,
        "slug": slug,
    }:
        raise ReconciliationError("rendered handoff frontmatter is invalid")

    manifest_content = _json_bytes(manifest)
    graph_content = _json_bytes(graph)
    json.loads(manifest_content)
    json.loads(graph_content)

    temp_files: list[Path] = []
    try:
        temp_manifest = _write_temp(manifest_file, manifest_content)
        temp_files.append(temp_manifest)
        temp_graph = _write_temp(graph_file, graph_content)
        temp_files.append(temp_graph)
        temp_handoff = _write_temp(handoff_file, handoff.encode())
        temp_files.append(temp_handoff)
        replace = replace_fn or os.replace
        replace(temp_manifest, manifest_file)
        replace(temp_graph, graph_file)
        replace(temp_handoff, handoff_file)
    finally:
        for temp_file in temp_files:
            temp_file.unlink(missing_ok=True)

    return {
        "manifest": MANIFEST_PATH.as_posix(),
        "graph": GRAPH_PATH.as_posix(),
        "handoff": handoff_path,
        "issue_id": issue_id,
        "plan_id": plan_id,
        "handoff_id": handoff_id,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Reconcile a governed Northstar handoff"
    )
    parser.add_argument("--root", required=True)
    parser.add_argument("--spec", required=True)
    parser.add_argument("--work-item", required=True)
    parser.add_argument("--slug", required=True)
    parser.add_argument("--completed-via")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = reconcile(
            root=Path(args.root),
            spec=args.spec,
            work_item=args.work_item,
            slug=args.slug,
            completed_via=args.completed_via,
        )
    except ReconciliationError as exc:
        print(f"northstar-reconcile: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
