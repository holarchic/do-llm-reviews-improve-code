# Follow-up: already-installed packages

Add a keyword parameter: `resolve(graph, targets, installed: Iterable[str] = ())`.

Packages in `installed` are already present. They never appear in the result, and their own dependencies are not needed on their account — `resolve` does not look past them, so an installed package need not be a key of `graph`, and a cycle reachable only through an installed package is not an error. A package still needed by something that is not installed is included as usual. Keep everything in TASK.md working, and update your tests.
