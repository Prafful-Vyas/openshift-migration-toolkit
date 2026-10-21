"""Migration wave planning from the dependency graph.

A wave contains apps whose dependencies have all moved in earlier waves, so each
app's dependencies are already on OpenShift when it cuts over. Within a wave the
least critical apps go first: problems with the runbook or platform show up on a
tier 3 app before a tier 1 app is touched.
"""

from __future__ import annotations

from .inventory import App


class CycleError(ValueError):
    pass


def plan_waves(apps: list[App], max_per_wave: int | None = None) -> list[list[App]]:
    by_name = {a.name: a for a in apps}
    remaining = {a.name: set(a.depends_on) for a in apps}
    done: set[str] = set()
    waves: list[list[App]] = []
    while remaining:
        ready = [n for n, deps in remaining.items() if deps <= done]
        if not ready:
            raise CycleError(f"dependency cycle: {' -> '.join(_find_cycle(remaining))}")
        ready.sort(key=lambda n: (-by_name[n].tier, n))
        chunks = [ready] if not max_per_wave else [ready[i:i + max_per_wave] for i in range(0, len(ready), max_per_wave)]
        for chunk in chunks:
            waves.append([by_name[n] for n in chunk])
        done |= set(ready)
        for n in ready:
            del remaining[n]
    return waves


def _find_cycle(graph: dict[str, set[str]]) -> list[str]:
    start = next(iter(graph))
    path, seen = [start], {start}
    node = start
    while True:
        node = next(d for d in sorted(graph[node]) if d in graph)
        if node in seen:
            return path[path.index(node):] + [node]
        path.append(node)
        seen.add(node)
