import copy
import unittest

import json_patch
from json_patch import (
    FailedTestError,
    InvalidOperationError,
    InvalidPointerError,
    JsonPatchError,
    PathNotFoundError,
)


def apply(document, patch):
    return json_patch.apply_patch(document, patch)


class ParsePointerTest(unittest.TestCase):  # rules 1, 2
    def test_empty_is_whole_document(self):
        self.assertEqual(json_patch.parse_pointer(""), [])

    def test_split_keeps_empty_tokens(self):
        self.assertEqual(json_patch.parse_pointer("/"), [""])
        self.assertEqual(json_patch.parse_pointer("/a//b/"), ["a", "", "b", ""])
        self.assertEqual(json_patch.parse_pointer("/a/0/c"), ["a", "0", "c"])

    def test_tilde_zero_one_is_tilde_one(self):
        self.assertEqual(json_patch.parse_pointer("/~01"), ["~1"])

    def test_tilde_one_zero_is_slash_zero(self):
        self.assertEqual(json_patch.parse_pointer("/~10"), ["/0"])

    def test_mixed_escapes(self):
        self.assertEqual(json_patch.parse_pointer("/a~1b/m~0n"), ["a/b", "m~n"])
        self.assertEqual(json_patch.parse_pointer("/~0~1"), ["~/"])
        self.assertEqual(json_patch.parse_pointer("/~00"), ["~0"])

    def test_missing_leading_slash(self):
        for bad in ("a/b", "#/a", "a"):
            with self.subTest(pointer=bad), self.assertRaises(InvalidPointerError) as caught:
                json_patch.parse_pointer(bad)
            self.assertEqual(caught.exception.pointer, bad)

    def test_unknown_escape_invalid(self):
        with self.assertRaises(InvalidPointerError) as caught:
            json_patch.parse_pointer("/a~2")
        self.assertEqual(caught.exception.pointer, "/a~2")

    def test_dangling_tilde_invalid(self):
        for bad in ("/a~", "/~~0", "/~/b"):
            with self.subTest(pointer=bad), self.assertRaises(InvalidPointerError):
                json_patch.parse_pointer(bad)


class ResolveTest(unittest.TestCase):  # rules 3-9
    DOC = {"a": {"b": [10, 20, {"c": "deep"}]}, "": 5, "a/b": 1, "m~n": 2, "-": "dash", "01": "lead"}

    def test_nested(self):
        self.assertEqual(json_patch.resolve(self.DOC, ""), self.DOC)
        self.assertEqual(json_patch.resolve(self.DOC, "/a/b/2/c"), "deep")
        self.assertEqual(json_patch.resolve(self.DOC, "/a/b/0"), 10)

    def test_escaped_keys(self):
        self.assertEqual(json_patch.resolve(self.DOC, "/a~1b"), 1)
        self.assertEqual(json_patch.resolve(self.DOC, "/m~0n"), 2)

    def test_special_tokens_are_plain_object_keys(self):
        self.assertEqual(json_patch.resolve(self.DOC, "/-"), "dash")
        self.assertEqual(json_patch.resolve(self.DOC, "/01"), "lead")
        self.assertEqual(json_patch.resolve(self.DOC, "/"), 5)

    def test_missing_key(self):
        with self.assertRaises(PathNotFoundError) as caught:
            json_patch.resolve(self.DOC, "/a/zzz")
        self.assertEqual(caught.exception.pointer, "/a/zzz")

    def test_leading_zero_index_invalid(self):
        with self.assertRaises(InvalidPointerError) as caught:
            json_patch.resolve(self.DOC, "/a/b/01")
        self.assertEqual(caught.exception.pointer, "/a/b/01")

    def test_malformed_indices_invalid(self):
        for token in ("-1", "+1", "1.0", " 1", "1 ", "1e0", ""):
            with self.subTest(token=token), self.assertRaises(InvalidPointerError):
                json_patch.resolve([1, 2, 3], "/" + token)

    def test_non_ascii_digits_invalid(self):
        for token in ("١", "²"):
            with self.subTest(token=token), self.assertRaises(InvalidPointerError):
                json_patch.resolve([1, 2, 3], "/" + token)

    def test_index_equal_to_length_not_found(self):
        with self.assertRaises(PathNotFoundError) as caught:
            json_patch.resolve({"a": [1, 2]}, "/a/2")
        self.assertEqual(caught.exception.pointer, "/a/2")

    def test_dash_on_list_not_found(self):
        with self.assertRaises(PathNotFoundError):
            json_patch.resolve([1, 2], "/-")

    def test_string_not_indexable(self):
        with self.assertRaises(PathNotFoundError):
            json_patch.resolve({"s": "abc"}, "/s/0")

    def test_scalars_not_traversable(self):
        for doc in ({"a": 1}, {"a": None}, {"a": True}, {"a": 1.5}):
            with self.subTest(doc=doc), self.assertRaises(PathNotFoundError):
                json_patch.resolve(doc, "/a/b")

    def test_first_failing_token_decides(self):
        with self.assertRaises(PathNotFoundError):
            json_patch.resolve({"a": [1]}, "/b/01")
        with self.assertRaises(InvalidPointerError):
            json_patch.resolve({"a": [1]}, "/a/01/b")
        with self.assertRaises(PathNotFoundError):
            json_patch.resolve({"a": [1]}, "/a/5/01")

    def test_direct_errors_have_no_index(self):
        with self.assertRaises(PathNotFoundError) as caught:
            json_patch.resolve({}, "/x")
        self.assertIsNone(caught.exception.index)
        with self.assertRaises(InvalidPointerError) as caught:
            json_patch.resolve({"x": 1}, "x")
        self.assertIsNone(caught.exception.index)


class StructureTest(unittest.TestCase):  # rules 10-14
    def assert_invalid(self, patch, index):
        with self.assertRaises(InvalidOperationError) as caught:
            apply({"a": 1}, patch)
        self.assertEqual(caught.exception.index, index)

    def test_patch_not_a_list(self):
        self.assert_invalid({"op": "remove", "path": "/a"}, None)
        self.assert_invalid("add", None)

    def test_operation_not_a_dict(self):
        self.assert_invalid([{"op": "test", "path": "/a", "value": 1}, ["remove", "/a"]], 1)

    def test_unknown_op_names(self):
        for name in ("Add", "merge", "", " add"):
            with self.subTest(name=name):
                self.assert_invalid([{"op": name, "path": "/a", "value": 1}], 0)

    def test_non_string_op(self):
        for name in (["add"], {"add": 1}, 1, None):
            with self.subTest(name=name):
                self.assert_invalid([{"op": name, "path": "/a", "value": 1}], 0)

    def test_missing_op_or_path(self):
        self.assert_invalid([{"path": "/a", "value": 1}], 0)
        self.assert_invalid([{"op": "remove"}], 0)

    def test_non_string_path(self):
        for path in (["a"], 0, None):
            with self.subTest(path=path):
                self.assert_invalid([{"op": "replace", "path": path, "value": 1}], 0)

    def test_missing_value(self):
        for name in ("add", "replace", "test"):
            with self.subTest(name=name):
                self.assert_invalid([{"op": name, "path": "/a"}], 0)

    def test_missing_or_non_string_from(self):
        for name in ("move", "copy"):
            with self.subTest(name=name):
                self.assert_invalid([{"op": name, "path": "/b"}], 0)
                self.assert_invalid([{"op": name, "from": 5, "path": "/b"}], 0)

    def test_null_value_is_present_for_add(self):
        self.assertEqual(apply({}, [{"op": "add", "path": "/a", "value": None}]), {"a": None})

    def test_null_value_is_present_for_replace_and_test(self):
        patch = [{"op": "replace", "path": "/a", "value": None}, {"op": "test", "path": "/a", "value": None}]
        self.assertEqual(apply({"a": 1}, patch), {"a": None})

    def test_unrequired_from_ignored_on_add(self):
        patch = [{"op": "add", "path": "/b", "value": 2, "from": "not a pointer"}]
        self.assertEqual(apply({"a": 1}, patch), {"a": 1, "b": 2})

    def test_unrequired_members_ignored(self):
        patch = [{"op": "remove", "path": "/a", "value": 99, "from": 12},
                 {"op": "move", "from": "/b", "path": "/c", "value": 99}]
        self.assertEqual(apply({"a": 1, "b": 2}, patch), {"c": 2})

    def test_structure_checked_before_pointer_syntax(self):
        with self.assertRaises(InvalidOperationError):
            apply({"a": 1}, [{"op": "add", "path": "no-slash"}])

    def test_path_syntax_checked_before_from_syntax(self):
        with self.assertRaises(InvalidPointerError) as caught:
            apply({"a": 1}, [{"op": "move", "from": "bad-from", "path": "bad-path"}])
        self.assertEqual(caught.exception.pointer, "bad-path")

    def test_earlier_operation_error_wins(self):
        with self.assertRaises(PathNotFoundError) as caught:
            apply({"a": 1}, [{"op": "remove", "path": "/zzz"}, {"op": "bogus"}])
        self.assertEqual(caught.exception.index, 0)

    def test_later_malformed_operation_reports_its_index(self):
        doc = {"a": 1}
        with self.assertRaises(InvalidOperationError) as caught:
            apply(doc, [{"op": "add", "path": "/b", "value": 2}, {"op": "remove", "path": "/a"}, {"op": "copy", "path": "/c"}])
        self.assertEqual(caught.exception.index, 2)
        self.assertEqual(doc, {"a": 1})


class AddTest(unittest.TestCase):  # rules 15, 16
    def test_add_or_overwrite_member(self):
        self.assertEqual(apply({"a": 1}, [{"op": "add", "path": "/b", "value": [2]}]), {"a": 1, "b": [2]})
        self.assertEqual(apply({"a": 1}, [{"op": "add", "path": "/a", "value": 2}]), {"a": 2})

    def test_insert_into_array_shifts(self):
        self.assertEqual(apply([1, 2, 3], [{"op": "add", "path": "/1", "value": 9}]), [1, 9, 2, 3])
        self.assertEqual(apply({"x": ["b"]}, [{"op": "add", "path": "/x/0", "value": "a"}]), {"x": ["a", "b"]})

    def test_index_equal_to_length_appends(self):
        self.assertEqual(apply([1, 2], [{"op": "add", "path": "/2", "value": 3}]), [1, 2, 3])
        self.assertEqual(apply({"e": []}, [{"op": "add", "path": "/e/0", "value": 1}]), {"e": [1]})

    def test_dash_appends(self):
        self.assertEqual(apply({"x": [[1]]}, [{"op": "add", "path": "/x/0/-", "value": 2}]), {"x": [[1, 2]]})

    def test_index_beyond_length(self):
        with self.assertRaises(PathNotFoundError) as caught:
            apply([1, 2], [{"op": "add", "path": "/3", "value": 3}])
        self.assertEqual(caught.exception.pointer, "/3")

    def test_malformed_index_invalid(self):
        for path in ("/01", "/-1"):
            with self.subTest(path=path), self.assertRaises(InvalidPointerError):
                apply([1, 2], [{"op": "add", "path": path, "value": 3}])

    def test_dash_is_a_key_on_objects(self):
        self.assertEqual(apply({}, [{"op": "add", "path": "/-", "value": 1}]), {"-": 1})

    def test_add_root_replaces_document(self):
        self.assertEqual(apply({"a": 1}, [{"op": "add", "path": "", "value": [1, 2]}]), [1, 2])
        self.assertEqual(apply([1], [{"op": "add", "path": "", "value": None}]), None)

    def test_missing_parent(self):
        with self.assertRaises(PathNotFoundError) as caught:
            apply({"a": {}}, [{"op": "add", "path": "/a/b/c", "value": 1}])
        self.assertEqual(caught.exception.pointer, "/a/b/c")

    def test_scalar_parent(self):
        for doc in ({"a": 1}, {"a": "text"}, {"a": None}):
            with self.subTest(doc=doc), self.assertRaises(PathNotFoundError):
                apply(doc, [{"op": "add", "path": "/a/b", "value": 1}])


class RemoveTest(unittest.TestCase):  # rule 17
    def test_remove_member(self):
        self.assertEqual(apply({"a": 1, "b": {"c": 2}}, [{"op": "remove", "path": "/b/c"}]), {"a": 1, "b": {}})

    def test_remove_array_element_shifts(self):
        self.assertEqual(apply([1, 2, 3], [{"op": "remove", "path": "/0"}, {"op": "remove", "path": "/1"}]), [2])

    def test_remove_missing_member(self):
        with self.assertRaises(PathNotFoundError):
            apply({"a": 1}, [{"op": "remove", "path": "/b"}])

    def test_remove_past_end_or_dash(self):
        for path in ("/2", "/-"):
            with self.subTest(path=path), self.assertRaises(PathNotFoundError):
                apply([1, 2], [{"op": "remove", "path": path}])

    def test_remove_root(self):
        with self.assertRaises(InvalidOperationError) as caught:
            apply({"a": 1}, [{"op": "remove", "path": ""}])
        self.assertEqual(caught.exception.index, 0)


class ReplaceTest(unittest.TestCase):  # rule 18
    def test_replace_member(self):
        self.assertEqual(apply({"a": 1, "b": 2}, [{"op": "replace", "path": "/a", "value": {"x": 1}}]),
                         {"a": {"x": 1}, "b": 2})

    def test_replace_array_element_keeps_length(self):
        self.assertEqual(apply([1, 2, 3], [{"op": "replace", "path": "/1", "value": 9}]), [1, 9, 3])

    def test_replace_missing_member(self):
        with self.assertRaises(PathNotFoundError):
            apply({"a": 1}, [{"op": "replace", "path": "/b", "value": 2}])

    def test_replace_past_end_or_dash(self):
        for path in ("/2", "/-"):
            with self.subTest(path=path), self.assertRaises(PathNotFoundError):
                apply([1, 2], [{"op": "replace", "path": path, "value": 3}])

    def test_replace_root(self):
        self.assertEqual(apply({"a": 1}, [{"op": "replace", "path": "", "value": "x"}]), "x")


class MoveTest(unittest.TestCase):  # rules 19, 20
    def test_move_member(self):
        doc = {"a": {"x": 1}, "b": {}}
        self.assertEqual(apply(doc, [{"op": "move", "from": "/a/x", "path": "/b/y"}]), {"a": {}, "b": {"y": 1}})
        self.assertEqual(apply({"a": [1, 2], "b": 3}, [{"op": "move", "from": "/b", "path": "/a/-"}]), {"a": [1, 2, 3]})

    def test_move_forward_in_array(self):
        self.assertEqual(apply(["a", "b", "c", "d"], [{"op": "move", "from": "/0", "path": "/2"}]), ["b", "c", "a", "d"])

    def test_move_backward_in_array(self):
        self.assertEqual(apply(["a", "b", "c", "d"], [{"op": "move", "from": "/3", "path": "/0"}]), ["d", "a", "b", "c"])

    def test_move_to_index_valid_only_before_removal(self):
        with self.assertRaises(PathNotFoundError):
            apply(["a", "b", "c"], [{"op": "move", "from": "/0", "path": "/3"}])

    def test_move_into_own_child(self):
        with self.assertRaises(InvalidOperationError) as caught:
            apply({"a": {"b": {}}}, [{"op": "move", "from": "/a", "path": "/a/b/c"}])
        self.assertEqual(caught.exception.index, 0)

    def test_move_root_into_child(self):
        with self.assertRaises(InvalidOperationError):
            apply({"a": 1}, [{"op": "move", "from": "", "path": "/b"}])

    def test_move_to_string_prefixed_sibling_allowed(self):
        self.assertEqual(apply({"a": 1}, [{"op": "move", "from": "/a", "path": "/ab"}]), {"ab": 1})

    def test_move_onto_ancestor_allowed(self):
        self.assertEqual(apply({"a": {"b": {"c": 1}}}, [{"op": "move", "from": "/a/b", "path": "/a"}]), {"a": {"c": 1}})

    def test_descendant_check_before_existence(self):
        with self.assertRaises(InvalidOperationError):
            apply({}, [{"op": "move", "from": "/x", "path": "/x/y"}])

    def test_move_same_location(self):
        self.assertEqual(apply({"a": [1, 2]}, [{"op": "move", "from": "/a/1", "path": "/a/1"}]), {"a": [1, 2]})
        with self.assertRaises(PathNotFoundError):
            apply({"a": 1}, [{"op": "move", "from": "/b", "path": "/b"}])

    def test_move_missing_from(self):
        with self.assertRaises(PathNotFoundError) as caught:
            apply({"a": 1}, [{"op": "move", "from": "/b", "path": "/c"}])
        self.assertEqual(caught.exception.pointer, "/b")

    def test_move_bad_target_leaves_document_unchanged(self):
        doc = {"a": 1, "b": {}}
        with self.assertRaises(PathNotFoundError) as caught:
            apply(doc, [{"op": "move", "from": "/a", "path": "/zzz/q"}])
        self.assertEqual(caught.exception.pointer, "/zzz/q")
        self.assertEqual(doc, {"a": 1, "b": {}})


class CopyTest(unittest.TestCase):  # rule 21
    def test_copy_member(self):
        self.assertEqual(apply({"a": [1]}, [{"op": "copy", "from": "/a", "path": "/b"}]), {"a": [1], "b": [1]})

    def test_copy_is_deep(self):
        patch = [{"op": "copy", "from": "/a", "path": "/b"}, {"op": "add", "path": "/b/x/-", "value": 2},
                 {"op": "replace", "path": "/a/y", "value": 9}]
        self.assertEqual(apply({"a": {"x": [1], "y": 0}}, patch),
                         {"a": {"x": [1], "y": 9}, "b": {"x": [1, 2], "y": 0}})

    def test_copy_into_own_child(self):
        self.assertEqual(apply({"a": {"x": 1}}, [{"op": "copy", "from": "/a", "path": "/a/b"}]),
                         {"a": {"x": 1, "b": {"x": 1}}})

    def test_copy_array_element_to_dash(self):
        self.assertEqual(apply([1, 2], [{"op": "copy", "from": "/0", "path": "/-"}]), [1, 2, 1])

    def test_copy_missing_from(self):
        with self.assertRaises(PathNotFoundError) as caught:
            apply({"a": 1}, [{"op": "copy", "from": "/b", "path": "/c"}])
        self.assertEqual(caught.exception.pointer, "/b")


class TestOperationTest(unittest.TestCase):  # rules 22, 23
    def assert_passes(self, doc, path, value):
        self.assertEqual(apply(doc, [{"op": "test", "path": path, "value": value}]), doc)

    def assert_fails(self, doc, path, value):
        with self.assertRaises(FailedTestError) as caught:
            apply(doc, [{"op": "test", "path": path, "value": value}])
        self.assertEqual(caught.exception.pointer, path)
        self.assertEqual(caught.exception.index, 0)

    def test_equal_passes(self):
        self.assert_passes({"a": {"b": [1, "x", None]}}, "/a", {"b": [1, "x", None]})
        self.assert_passes([1, {"a": 2}], "", [1, {"a": 2}])

    def test_unequal_fails(self):
        self.assert_fails({"a": [1, 2]}, "/a/1", 3)
        self.assert_fails({"a": "1"}, "/a", 1)

    def test_int_equals_float(self):
        self.assert_passes({"a": 1}, "/a", 1.0)
        self.assert_passes({"a": [2.0, {"b": 0}]}, "/a", [2, {"b": 0.0}])

    def test_bool_not_number(self):
        self.assert_fails({"a": True}, "/a", 1)
        self.assert_fails({"a": 1}, "/a", True)
        self.assert_fails({"a": False}, "/a", 0)
        self.assert_fails({"a": 0.0}, "/a", False)

    def test_bool_nested(self):
        self.assert_fails({"a": [1, True]}, "/a", [1, 1])
        self.assert_fails({"a": {"k": 1}}, "/a", {"k": True})

    def test_null_only_null(self):
        self.assert_fails({"a": None}, "/a", False)
        self.assert_fails({"a": None}, "/a", 0)
        self.assert_passes({"a": None}, "/a", None)

    def test_objects_unordered_arrays_ordered(self):
        self.assert_passes({"a": {"x": 1, "y": 2}}, "/a", {"y": 2, "x": 1})
        self.assert_fails({"a": [1, 2]}, "/a", [2, 1])
        self.assert_fails({"a": {"x": 1}}, "/a", {"x": 1, "y": None})

    def test_missing_path_is_not_found(self):
        with self.assertRaises(PathNotFoundError):
            apply({"a": 1}, [{"op": "test", "path": "/b", "value": None}])

    def test_failed_test_stops_later_operations(self):
        doc = {"a": 1}
        with self.assertRaises(FailedTestError):
            apply(doc, [{"op": "add", "path": "/b", "value": 2}, {"op": "test", "path": "/a", "value": 2},
                        {"op": "remove", "path": "/a"}])
        self.assertEqual(doc, {"a": 1})


class AtomicityTest(unittest.TestCase):  # rules 24, 25
    def test_late_failure_leaves_nested_document_unchanged(self):
        doc = {"a": {"b": [1, 2]}, "c": [{"d": 1}]}
        before = copy.deepcopy(doc)
        patch = [
            {"op": "add", "path": "/a/b/-", "value": 3},
            {"op": "remove", "path": "/c/0/d"},
            {"op": "replace", "path": "/a/b/0", "value": 0},
            {"op": "remove", "path": "/nope"},
        ]
        with self.assertRaises(PathNotFoundError) as caught:
            apply(doc, patch)
        self.assertEqual(caught.exception.index, 3)
        self.assertEqual(doc, before)

    def test_failure_index_counts_from_zero(self):
        patch = [{"op": "test", "path": "/a", "value": 1}, {"op": "add", "path": "/x/y", "value": 1}]
        with self.assertRaises(PathNotFoundError) as caught:
            apply({"a": 1}, patch)
        self.assertEqual(caught.exception.index, 1)
        patch = [{"op": "add", "path": "/a", "value": 1}, {"op": "add", "path": "/l/01", "value": 1}]
        with self.assertRaises(InvalidPointerError) as caught:
            apply({"l": []}, patch)
        self.assertEqual(caught.exception.index, 1)

    def test_success_does_not_modify_or_share_input(self):
        doc = {"a": {"b": [1]}, "c": 1}
        before = copy.deepcopy(doc)
        result = apply(doc, [{"op": "add", "path": "/a/b/-", "value": 2}, {"op": "remove", "path": "/c"}])
        self.assertEqual(result, {"a": {"b": [1, 2]}})
        self.assertEqual(doc, before)
        result["a"]["b"].append(99)
        self.assertEqual(doc, before)

    def test_untouched_branches_not_shared(self):
        doc = {"a": {"b": [1]}, "c": [2]}
        result = apply(doc, [{"op": "replace", "path": "/c", "value": 3}])
        result["a"]["b"].append(99)
        self.assertEqual(doc, {"a": {"b": [1]}, "c": [2]})

    def test_empty_patch_returns_distinct_copy(self):
        doc = {"a": [1, {"b": 2}]}
        result = apply(doc, [])
        self.assertEqual(result, doc)
        self.assertIsNot(result, doc)
        result["a"][1]["b"] = 3
        self.assertEqual(doc, {"a": [1, {"b": 2}]})

    def test_patch_values_not_modified(self):
        patch = [{"op": "add", "path": "/a", "value": {"x": [1]}}, {"op": "add", "path": "/a/x/-", "value": 2},
                 {"op": "add", "path": "/a/y", "value": 3}]
        before = copy.deepcopy(patch)
        self.assertEqual(apply({}, patch), {"a": {"x": [1, 2], "y": 3}})
        self.assertEqual(patch, before)

    def test_result_shares_nothing_with_patch(self):
        value = {"x": [1]}
        result = apply({}, [{"op": "add", "path": "/a", "value": value}])
        result["a"]["x"].append(2)
        self.assertEqual(value, {"x": [1]})
        root = [{"k": 1}]
        result = apply({}, [{"op": "replace", "path": "", "value": root}])
        result[0]["k"] = 2
        self.assertEqual(root, [{"k": 1}])

    def test_error_hierarchy(self):
        for cls in (InvalidPointerError, PathNotFoundError, FailedTestError, InvalidOperationError):
            with self.subTest(cls=cls.__name__):
                self.assertTrue(issubclass(cls, JsonPatchError))
                self.assertTrue(issubclass(cls, ValueError))


if __name__ == "__main__":
    unittest.main()
