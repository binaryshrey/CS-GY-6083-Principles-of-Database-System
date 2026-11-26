"""Reading EXPLAIN (ANALYZE, FORMAT JSON) output: plan issues, cost fields and a
compact text rendering to show the LLM in the rewrite prompt."""
from . import config

ISSUES = ("correlated_subplan", "large_seq_scan", "nested_loop_large", "sort_spill", "hash_spill")


def walk(node, parent_rel=None, depth=0):
    yield node, depth
    for child in node.get("Plans", []):
        yield from walk(child, child.get("Parent Relationship"), depth + 1)


def _rows_read(node):
    loops = node.get("Actual Loops", 1) or 1
    return (node.get("Actual Rows", 0) + node.get("Rows Removed by Filter", 0)) * loops


def find_issues(plan_json):
    """Return the set of issue labels (the values allowed in plan_issue.issue)."""
    root = plan_json[0]["Plan"]
    found = set()
    for node, _ in walk(root):
        ntype = node.get("Node Type")
        # A SubPlan re-executed per outer row is a correlated subquery
        if node.get("Parent Relationship") == "SubPlan" and node.get("Actual Loops", 1) > 1:
            found.add("correlated_subplan")
        if ntype == "Seq Scan" and _rows_read(node) >= config.LARGE_SCAN_ROWS:
            found.add("large_seq_scan")
        if ntype == "Nested Loop":
            kids = node.get("Plans", [])
            if len(kids) == 2 and kids[1].get("Actual Loops", 0) >= config.NESTED_LOOP_LOOPS:
                found.add("nested_loop_large")
        if ntype == "Sort" and node.get("Sort Space Type") == "Disk":
            found.add("sort_spill")
        if ntype == "Hash" and node.get("Hash Batches", 1) > 1:
            found.add("hash_spill")
    return found


def cost_fields(plan_json):
    root = plan_json[0]["Plan"]
    return {
        "startup_cost": float(root.get("Startup Cost", 0)),
        "total_cost": float(root.get("Total Cost", 0)),
        "est_rows": int(root.get("Plan Rows", 0)),
    }


_DETAIL_KEYS = ("Relation Name", "Index Name", "Join Type", "Hash Cond", "Merge Cond", "Join Filter",
                "Index Cond", "Filter", "Sort Key", "Group Key", "Sort Space Type", "Hash Batches",
                "Subplan Name")


def render(plan_json, max_lines=60):
    """Indented one-line-per-node view with actual rows, loops and the details that
    explain slowness. Much shorter than raw JSON, so it fits in the prompt."""
    lines = []
    for node, depth in walk(plan_json[0]["Plan"]):
        rel = node.get("Parent Relationship")
        head = ("  " * depth) + ("-> " if depth else "") + node.get("Node Type", "?")
        if rel in ("SubPlan", "InitPlan"):
            head += f" [{rel}]"
        parts = [f"est_rows={node.get('Plan Rows')}", f"actual_rows={node.get('Actual Rows')}",
                 f"loops={node.get('Actual Loops')}", f"cost={node.get('Total Cost')}"]
        if node.get("Rows Removed by Filter"):
            parts.append(f"removed_by_filter={node['Rows Removed by Filter']}")
        for k in _DETAIL_KEYS:
            v = node.get(k)
            if v not in (None, "", []):
                parts.append(f"{k.lower().replace(' ', '_')}={v}")
        lines.append(head + "  " + " ".join(str(p) for p in parts))
        if len(lines) >= max_lines:
            lines.append("  ... (plan truncated)")
            break
    exec_ms = plan_json[0].get("Execution Time")
    if exec_ms is not None:
        lines.append(f"Execution Time: {exec_ms:.3f} ms")
    return "\n".join(lines)


def relations(plan_json):
    """Base tables the plan reads; used by the loader to fill question_table."""
    return {n["Relation Name"] for n, _ in walk(plan_json[0]["Plan"]) if "Relation Name" in n}
