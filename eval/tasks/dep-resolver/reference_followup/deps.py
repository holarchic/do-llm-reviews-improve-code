import heapq


class MissingDependencyError(ValueError):
    def __init__(self, name, required_by=None):
        detail = f" (required by {required_by})" if required_by else ""
        super().__init__(f"unknown package {name!r}{detail}")
        self.name = name
        self.required_by = required_by


class CycleError(ValueError):
    def __init__(self, members):
        super().__init__(f"dependency cycle among {', '.join(members)}")
        self.members = members


def resolve(graph, targets, installed=()):
    needed = _closure(graph, targets, frozenset(installed))
    deps = {package: set(graph[package]) & needed for package in needed}
    waiting = {package: len(d) for package, d in deps.items()}
    dependents = {package: [] for package in needed}
    for package, d in deps.items():
        for dep in d:
            dependents[dep].append(package)
    ready = [package for package, count in waiting.items() if count == 0]
    heapq.heapify(ready)
    order = []
    while ready:
        package = heapq.heappop(ready)
        order.append(package)
        for dependent in dependents[package]:
            waiting[dependent] -= 1
            if waiting[dependent] == 0:
                heapq.heappush(ready, dependent)
    if len(order) < len(needed):
        raise CycleError(_cycle_members(deps, set(needed) - set(order)))
    return order


def _closure(graph, targets, installed):
    needed, stack = set(), []
    for target in sorted(set(targets) - installed):
        if target not in graph:
            raise MissingDependencyError(target)
        stack.append(target)
    while stack:
        package = stack.pop()
        if package in needed:
            continue
        needed.add(package)
        for dep in sorted(set(graph[package]) - installed):
            if dep not in graph:
                raise MissingDependencyError(dep, package)
            stack.append(dep)
    return needed


def _cycle_members(deps, stuck):
    """Stuck packages that can reach themselves; the rest only depend on a cycle."""
    def reaches_itself(start):
        seen, stack = set(), [d for d in deps[start] if d in stuck]
        while stack:
            package = stack.pop()
            if package == start:
                return True
            if package not in seen:
                seen.add(package)
                stack.extend(d for d in deps[package] if d in stuck)
        return False

    return sorted(package for package in stuck if reaches_itself(package))
