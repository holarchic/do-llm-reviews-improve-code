import unittest

import deps


class InstalledTest(unittest.TestCase):
    def test_installed_omitted(self):
        graph = {"app": ["lib"], "lib": ["core"], "core": []}
        self.assertEqual(deps.resolve(graph, ["app"], installed=["core"]), ["lib", "app"])

    def test_not_traversed_past_installed(self):
        graph = {"app": ["lib", "util"], "lib": ["core"], "core": [], "util": []}
        self.assertEqual(deps.resolve(graph, ["app"], installed=["lib"]), ["util", "app"])

    def test_still_needed_elsewhere(self):
        graph = {"app": ["lib", "core"], "lib": ["core"], "core": []}
        self.assertEqual(deps.resolve(graph, ["app"], installed=["lib"]), ["core", "app"])

    def test_installed_need_not_be_in_graph(self):
        graph = {"app": ["sys"]}
        self.assertEqual(deps.resolve(graph, ["app"], installed=["sys"]), ["app"])

    def test_cycle_behind_installed_is_ignored(self):
        graph = {"app": ["lib"], "lib": ["x"], "x": ["lib"]}
        self.assertEqual(deps.resolve(graph, ["app"], installed=["lib"]), ["app"])

    def test_installed_target_omitted(self):
        self.assertEqual(deps.resolve({"a": []}, ["a"], installed=["a"]), [])
