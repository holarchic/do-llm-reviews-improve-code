import copy
import unittest

import deps


def assert_valid(test, graph, order):
    position = {package: i for i, package in enumerate(order)}
    for package in order:
        for dep in graph[package]:
            test.assertLess(position[dep], position[package], f"{dep} must precede {package}")


class OrderTest(unittest.TestCase):
    def test_chain(self):  # clauses 2, 3
        graph = {"app": ["lib"], "lib": ["core"], "core": []}
        self.assertEqual(deps.resolve(graph, ["app"]), ["core", "lib", "app"])

    def test_diamond_once(self):  # clause 2
        graph = {"a": ["b", "c"], "b": ["d"], "c": ["d"], "d": []}
        order = deps.resolve(graph, ["a"])
        self.assertEqual(order, ["d", "b", "c", "a"])

    def test_only_reachable_packages(self):  # clause 2
        graph = {"a": ["b"], "b": [], "unused": [], "z": ["a"]}
        self.assertEqual(deps.resolve(graph, ["a"]), ["b", "a"])

    def test_alphabetically_smallest_order(self):  # clause 4
        graph = {"m": [], "b": ["x"], "x": [], "a": ["z"], "z": []}
        self.assertEqual(deps.resolve(graph, ["m", "b", "a"]), ["m", "x", "b", "z", "a"])

    def test_smallest_order_is_not_dfs_order(self):  # clause 4
        graph = {"top": ["c", "b"], "c": ["a"], "b": [], "a": []}
        self.assertEqual(deps.resolve(graph, ["top"]), ["a", "b", "c", "top"])

    def test_large_graph_valid_and_deterministic(self):  # clauses 3, 4
        graph = {f"p{i:02d}": [f"p{j:02d}" for j in range(i) if (i * 7 + j) % 5 == 0] for i in range(40)}
        order = deps.resolve(graph, list(graph))
        assert_valid(self, graph, order)
        self.assertEqual(sorted(order), sorted(graph))
        self.assertEqual(order, deps.resolve(graph, reversed(list(graph))))
        self.assertEqual(order[:3], ["p00", "p01", "p02"])

    def test_duplicates_count_once(self):  # clause 7
        graph = {"a": ["b", "b"], "b": []}
        self.assertEqual(deps.resolve(graph, ["a", "a", "b"]), ["b", "a"])

    def test_no_targets(self):  # clause 7
        self.assertEqual(deps.resolve({"a": []}, []), [])

    def test_accepts_iterables(self):  # interface
        graph = {"a": ("b",), "b": ()}
        self.assertEqual(deps.resolve(graph, iter(["a"])), ["b", "a"])

    def test_graph_not_modified(self):  # clause 8
        graph = {"a": ["b", "c"], "b": ["c"], "c": []}
        before = copy.deepcopy(graph)
        deps.resolve(graph, ["a"])
        self.assertEqual(graph, before)


class MissingTest(unittest.TestCase):
    def test_missing_target(self):  # clause 5
        with self.assertRaises(deps.MissingDependencyError) as caught:
            deps.resolve({"a": []}, ["nope"])
        self.assertEqual((caught.exception.name, caught.exception.required_by), ("nope", None))
        self.assertIsInstance(caught.exception, ValueError)

    def test_missing_dependency(self):  # clause 5
        with self.assertRaises(deps.MissingDependencyError) as caught:
            deps.resolve({"a": ["b"], "b": ["ghost"]}, ["a"])
        self.assertEqual((caught.exception.name, caught.exception.required_by), ("ghost", "b"))

    def test_missing_reported_before_cycle(self):  # clause 5
        graph = {"a": ["b"], "b": ["a", "ghost"]}
        with self.assertRaises(deps.MissingDependencyError):
            deps.resolve(graph, ["a"])

    def test_missing_outside_closure_ignored(self):  # clauses 2, 5
        graph = {"a": [], "other": ["ghost"]}
        self.assertEqual(deps.resolve(graph, ["a"]), ["a"])


class CycleTest(unittest.TestCase):
    def test_self_dependency(self):  # clause 6
        with self.assertRaises(deps.CycleError) as caught:
            deps.resolve({"a": ["a"]}, ["a"])
        self.assertEqual(caught.exception.members, ["a"])
        self.assertIsInstance(caught.exception, ValueError)

    def test_members_exclude_dependents(self):  # clause 6
        graph = {"app": ["x"], "x": ["y"], "y": ["z"], "z": ["x"], "lib": []}
        with self.assertRaises(deps.CycleError) as caught:
            deps.resolve(graph, ["app", "lib"])
        self.assertEqual(caught.exception.members, ["x", "y", "z"])

    def test_two_cycles(self):  # clause 6
        graph = {"top": ["a", "c"], "a": ["b"], "b": ["a"], "c": ["d"], "d": ["c"]}
        with self.assertRaises(deps.CycleError) as caught:
            deps.resolve(graph, ["top"])
        self.assertEqual(caught.exception.members, ["a", "b", "c", "d"])

    def test_cycle_outside_closure_ignored(self):  # clauses 2, 6
        graph = {"a": [], "loop": ["loop"]}
        self.assertEqual(deps.resolve(graph, ["a"]), ["a"])
