"""Queue-level dependency DAG planner for batch and parallel factory runs."""
from __future__ import annotations

import graphlib
import re
from collections.abc import Callable

_DEP_CLAUSE_RE = re.compile(
    r"\b(?:depends\s+on|blocked\s+by|after)\s+((?:#\d+(?:\s*(?:,|and)\s*)?)+)",
    re.IGNORECASE,
)
_ISSUE_REF_RE = re.compile(r"#(\d+)")


def parse_explicit_dependencies(issues: list[dict]) -> dict[int, set[int]]:
    """Extract explicit `#N` dependencies from issue bodies/titles within the batch."""
    batch_nums = {int(item["number"]) for item in issues}
    deps: dict[int, set[int]] = {num: set() for num in batch_nums}
    for item in issues:
        num = int(item["number"])
        text = f"{item.get('title') or ''}\n{item.get('body') or ''}"
        for match in _DEP_CLAUSE_RE.finditer(text):
            for ref in _ISSUE_REF_RE.findall(match.group(1)):
                dep_num = int(ref)
                if dep_num in batch_nums and dep_num != num:
                    deps[num].add(dep_num)
    return deps


def _has_path(deps: dict[int, set[int]], start: int, target: int) -> bool:
    """Return True if `start` transitively depends on `target`."""
    seen: set[int] = set()
    stack = list(deps.get(start, ()))
    while stack:
        cur = stack.pop()
        if cur == target:
            return True
        if cur not in seen:
            seen.add(cur)
            stack.extend(deps.get(cur, ()))
    return False


def _surfaces_overlap(paths_a: list[str], paths_b: list[str]) -> bool:
    """Return True if two predicted surface lists touch the same file or nested path."""
    norm_a = [p.strip().rstrip("/") for p in paths_a if p and p.strip()]
    norm_b = [p.strip().rstrip("/") for p in paths_b if p and p.strip()]
    for pa in norm_a:
        for pb in norm_b:
            if pa == pb or pa.startswith(f"{pb}/") or pb.startswith(f"{pa}/"):
                return True
    return False


def _ensure_acyclic(deps: dict[int, set[int]]) -> dict[int, set[int]]:
    """Break any remaining cycles deterministically so TopologicalSorter succeeds."""
    clean = {k: set(v) for k, v in deps.items()}
    while True:
        try:
            graphlib.TopologicalSorter(clean).prepare()
            return clean
        except graphlib.CycleError as exc:
            cycle = exc.args[1] if len(exc.args) > 1 else []
            removed = False
            for idx in range(len(cycle) - 1):
                u, v = cycle[idx], cycle[idx + 1]
                # graphlib reports [node, dep, ..., node] or [dep, node, ...] depending on direction;
                # check both orientations and remove the first matching edge.
                if v in clean.get(u, ()):
                    clean[u].remove(v)
                    removed = True
                    break
                if u in clean.get(v, ()):
                    clean[v].remove(u)
                    removed = True
                    break
            if not removed:
                # Defensive fallback: clear one edge from the first node in the cycle.
                for node in clean:
                    if clean[node]:
                        clean[node].pop()
                        break
                else:
                    return clean


def build_dependency_dag(
    issues: list[dict],
    *,
    planner_fn: Callable[[list[dict]], dict] | None = None,
) -> tuple[dict[int, set[int]], dict[int, list[str]]]:
    """Build an acyclic `{issue: {depends_on...}}` DAG and `{issue: [paths]}` map."""
    explicit = parse_explicit_dependencies(issues)
    deps: dict[int, set[int]] = {k: set(v) for k, v in explicit.items()}
    surfaces: dict[int, list[str]] = {k: [] for k in deps}
    batch_nums = set(deps.keys())

    if len(issues) > 1 and planner_fn is not None:
        try:
            plan = planner_fn(issues) or {}
            for edge in plan.get("edges") or []:
                num = int(edge["issue"])
                if num not in batch_nums:
                    continue
                for dep in edge.get("depends_on") or []:
                    dep_num = int(dep)
                    if dep_num in batch_nums and dep_num != num:
                        deps[num].add(dep_num)

            for surf in plan.get("predicted_surfaces") or []:
                num = int(surf["issue"])
                if num in batch_nums:
                    surfaces[num] = [str(p) for p in (surf.get("paths") or []) if str(p).strip()]

            ordered_nums = sorted(batch_nums)
            for idx, a in enumerate(ordered_nums):
                if not surfaces.get(a):
                    continue
                for b in ordered_nums[idx + 1 :]:
                    if not surfaces.get(b):
                        continue
                    if _surfaces_overlap(surfaces[a], surfaces[b]):
                        if not _has_path(deps, a, b) and not _has_path(deps, b, a):
                            deps[b].add(a)

            graphlib.TopologicalSorter(deps).prepare()
            return deps, surfaces
        except graphlib.CycleError:
            # Fall back to explicit dependencies if LLM/surface edges introduced a cycle.
            deps = {k: set(v) for k, v in explicit.items()}
        except Exception:
            deps = {k: set(v) for k, v in explicit.items()}

    return _ensure_acyclic(deps), surfaces
