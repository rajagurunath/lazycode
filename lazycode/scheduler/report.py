"""Morning deliverable: ``report.md`` + ``report.json`` (DESIGN.md §9, Appendix B8).

:func:`write_report` reads the event log + projection tables + ``llm_calls`` and
writes the two sibling files under ``<repo>/.lazycode/reports/<job_id>/``:

* what was done, per task group (branch, changed files, node count);
* the assumption ledger (every judgment call the batch nodes recorded, §6);
* the verification transcript tails (per Verify node);
* cost actuals from ``llm_calls`` (token totals per model — the §5.1 ANALYZE
  side; the dual-baseline comparison is B7/M2);
* follow-ups / NEEDS_HUMAN nodes.

Pure read + file write — it never mutates job state.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from lazycode.ir import EventType, NodeStatus
from lazycode.store import Store, cas, eventlog
from lazycode.workspace import extract_diff_paths

from .config import SchedulerConfig
from .payloads import extract_assumptions, extract_diff, extract_text

_APPLIED = frozenset({NodeStatus.DONE.value, NodeStatus.APPLIED.value})


def _job_row(store: Store, job_id: str) -> dict[str, Any]:
    row = store.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return dict(row) if row else {"id": job_id, "goal": "", "status": "UNKNOWN"}


def _nodes(store: Store, job_id: str) -> list[dict[str, Any]]:
    rows = store.conn.execute(
        "SELECT * FROM nodes WHERE job_id = ? ORDER BY id", (job_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def _response_text(store: Store, node_id: str) -> str | None:
    row = store.conn.execute(
        "SELECT response_ref FROM llm_calls WHERE node_id = ? AND response_ref IS NOT NULL LIMIT 1",
        (node_id,),
    ).fetchone()
    if row is None or row["response_ref"] is None:
        return None
    try:
        payload = json.loads(cas.get(store, row["response_ref"]).decode("utf-8"))
    except (KeyError, ValueError):
        return None
    return extract_text(payload)


def _collect(store: Store, job_id: str) -> dict[str, Any]:
    job = _job_row(store, job_id)
    nodes = _nodes(store, job_id)

    groups: dict[str, dict[str, Any]] = {}
    grow = store.conn.execute(
        "SELECT * FROM task_groups WHERE job_id = ?", (job_id,)
    ).fetchall()
    for g in grow:
        groups[g["id"]] = {"branch": g["branch"], "worktree": g["worktree_path"], "nodes": [], "files": []}

    assumptions: list[dict[str, str]] = []
    verifications: list[dict[str, Any]] = []
    needs_human: list[dict[str, str]] = []

    for node in nodes:
        gid = node["group_id"]
        if gid in groups:
            groups[gid]["nodes"].append(node["id"])
        # Changed files + assumptions from the node's response.
        if node["op"] in ("Generate", "Edit") and node["status"] in _APPLIED:
            text = _response_text(store, node["id"])
            if text:
                files = extract_diff_paths(extract_diff(text))
                if gid in groups:
                    groups[gid]["files"].extend(f for f in files if f not in groups[gid]["files"])
                note = extract_assumptions(text)
                if note:
                    assumptions.append({"node": node["id"], "assumption": note, "risk": "review"})
        if node["status"] == NodeStatus.NEEDS_HUMAN.value:
            needs_human.append({"node": node["id"], "op": node["op"]})

    for event in eventlog.read(store, job_id):
        if event.type == EventType.VERIFY_RESULT:
            verifications.append(
                {
                    "node": event.payload.get("node_id"),
                    "passed": event.payload.get("passed"),
                    "exit_code": event.payload.get("exit_code"),
                    "tail": event.payload.get("tail", ""),
                }
            )

    cost = _cost(store, job_id)
    return {
        "job": job,
        "groups": groups,
        "assumptions": assumptions,
        "verifications": verifications,
        "needs_human": needs_human,
        "cost": cost,
    }


def _cost(store: Store, job_id: str) -> dict[str, Any]:
    node_ids = [r["id"] for r in store.conn.execute(
        "SELECT id FROM nodes WHERE job_id = ?", (job_id,)
    ).fetchall()]
    per_model: dict[str, dict[str, int]] = {}
    if not node_ids:
        return {"per_model": per_model, "total_tokens_in": 0, "total_tokens_out": 0}
    placeholders = ",".join("?" for _ in node_ids)
    rows = store.conn.execute(
        f"SELECT provider, tokens_in, tokens_out FROM llm_calls WHERE node_id IN ({placeholders})",
        node_ids,
    ).fetchall()
    total_in = total_out = 0
    for r in rows:
        model = r["provider"] or "unknown"
        bucket = per_model.setdefault(model, {"tokens_in": 0, "tokens_out": 0, "calls": 0})
        bucket["tokens_in"] += r["tokens_in"] or 0
        bucket["tokens_out"] += r["tokens_out"] or 0
        bucket["calls"] += 1
        total_in += r["tokens_in"] or 0
        total_out += r["tokens_out"] or 0
    return {"per_model": per_model, "total_tokens_in": total_in, "total_tokens_out": total_out}


def _render_md(data: dict[str, Any], job_id: str) -> str:
    job = data["job"]
    lines = [f"# Job {job_id}: {job.get('goal', '')}", ""]
    lines.append(f"Status: **{job.get('status', 'UNKNOWN')}**")
    lines.append("")

    lines.append("## What was done")
    if not data["groups"]:
        lines.append("_No task groups._")
    for gid, g in data["groups"].items():
        lines.append(f"- **{gid}** (branch `{g['branch']}`): {len(g['nodes'])} node(s)")
        if g["files"]:
            for f in g["files"]:
                lines.append(f"    - `{f}`")
    lines.append("")

    lines.append("## Assumption ledger")
    if data["assumptions"]:
        lines.append("| node | assumption | risk |")
        lines.append("|---|---|---|")
        for a in data["assumptions"]:
            note = a["assumption"].replace("\n", " ").replace("|", "\\|")
            lines.append(f"| {a['node']} | {note} | {a['risk']} |")
    else:
        lines.append("_No assumptions recorded._")
    lines.append("")

    lines.append("## Verification")
    if data["verifications"]:
        for v in data["verifications"]:
            status = "PASS" if v["passed"] else "FAIL"
            lines.append(f"- **{v['node']}** — {status} (exit {v['exit_code']})")
            if v["tail"]:
                tail = "\n".join(v["tail"].splitlines()[-20:])
                lines.append(f"```\n{tail}\n```")
    else:
        lines.append("_No verify nodes ran._")
    lines.append("")

    lines.append("## Cost")
    cost = data["cost"]
    lines.append(
        f"Actual tokens: {cost['total_tokens_in']} in / {cost['total_tokens_out']} out "
        "(baseline comparison: N/A in M0 — see benchmark harness B7)."
    )
    for model, bucket in cost["per_model"].items():
        lines.append(f"- `{model}`: {bucket['calls']} call(s), {bucket['tokens_in']} in / {bucket['tokens_out']} out")
    lines.append("")

    lines.append("## Follow-ups / NEEDS_HUMAN")
    if data["needs_human"]:
        for nh in data["needs_human"]:
            lines.append(f"- **{nh['node']}** ({nh['op']}) needs human attention")
    else:
        lines.append("_None._")
    lines.append("")
    return "\n".join(lines)


def write_report(
    store: Store, job_id: str, config: SchedulerConfig, repo_root: Path | str
) -> Path:
    """Write ``report.md`` + ``report.json`` for ``job_id``; return the directory."""
    data = _collect(store, job_id)
    report_dir = Path(repo_root) / ".lazycode" / "reports" / job_id
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "report.md").write_text(_render_md(data, job_id), encoding="utf-8")
    (report_dir / "report.json").write_text(
        json.dumps({"job_id": job_id, **data}, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    return report_dir
