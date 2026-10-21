# Task: dependency install order

Write a Python module `deps.py` at the repository root (standard library only). Tests import it as `import deps`.

## Interface

- `resolve(graph: Mapping[str, Iterable[str]], targets: Iterable[str]) -> list[str]`
- `class MissingDependencyError(ValueError)` with attributes `name: str` and `required_by: str | None`.
- `class CycleError(ValueError)` with attribute `members: list[str]`.

## Behaviour

1. `graph` maps each package name to the names of the packages it depends on.
2. `resolve` returns an install order for the targets and every package they depend on, directly or indirectly — and no other package. Each package appears exactly once, however many packages depend on it.
3. Every package comes after all of its dependencies.
4. Whenever more than one package could come next, the alphabetically smallest (plain string order) comes first. The result is therefore the alphabetically smallest valid order, compared position by position.
5. A target that is not a key of `graph` raises `MissingDependencyError` with `name` the target and `required_by` `None`. A dependency that is not a key of `graph` raises `MissingDependencyError` with `name` the dependency and `required_by` the package that lists it. Missing packages are reported before cycles.
6. If the packages to install contain a dependency cycle (a package depending on itself counts), `resolve` raises `CycleError`. Its `members` is the sorted list of every package to install that lies on a cycle; a package that only depends on a cycle is not a member.
7. A dependency listed twice, or a target given twice, counts once. No targets → an empty list.
8. `resolve` does not modify `graph`.

Include your own tests.
