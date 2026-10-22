# Traps (private)

| Trap | TASK.md rule | Hidden tests | Why a hurried build misses it |
|---|---|---|---|
| Escape decoding order | 2 | `ParsePointerTest.test_tilde_zero_one_is_tilde_one`, `test_tilde_one_zero_is_slash_zero`, `test_mixed_escapes` | `replace("~0", "~").replace("~1", "/")` turns `~01` into `/`. |
| Invalid escapes | 2 | `ParsePointerTest.test_unknown_escape_invalid`, `test_dangling_tilde_invalid` | Plain `str.replace` decoding never rejects a stray `~`. |
| Array-index grammar | 5, 8 | `ResolveTest.test_leading_zero_index_invalid`, `test_malformed_indices_invalid`, `test_non_ascii_digits_invalid`, `test_first_failing_token_decides`, `AddTest.test_malformed_index_invalid` | `int(token)` / `token.isdigit()` accept `01`, ` 1`, `+1`, or crash with a bare `ValueError` on `²`; `"01"` silently becomes index 1. |
| `-` and special tokens on objects | 4, 16 | `ResolveTest.test_special_tokens_are_plain_object_keys`, `AddTest.test_dash_is_a_key_on_objects` | Handling `-` / indices before checking the container type. |
| Existing-value lookups reject `-` and `len` | 6 | `ResolveTest.test_index_equal_to_length_not_found`, `test_dash_on_list_not_found`, `RemoveTest.test_remove_past_end_or_dash`, `ReplaceTest.test_replace_past_end_or_dash` | Sharing add's "index may equal length / `-` appends" logic with remove and replace; Python's `list[-1]`. |
| Strings and scalars are leaves | 7 | `ResolveTest.test_string_not_indexable`, `test_scalars_not_traversable`, `AddTest.test_scalar_parent` | `"abc"[0]` works in Python; `TypeError` leaks instead of `PathNotFoundError`. |
| `pointer` is the whole pointer | 9 | pointer assertions across `ResolveTest`, `AddTest`, `MoveTest`, `CopyTest`, `TestOperationTest` | Reporting the failing token, or the `path` when `from` failed. |
| Structure errors and their index | 10, 11, 24 | `StructureTest.test_patch_not_a_list`, `test_operation_not_a_dict`, `test_unknown_op_names`, `test_non_string_op` | A string patch iterates; `op in {...}` with a list op raises `TypeError`; index set to `None` or 1-based. |
| `value: None` is present | 12 | `StructureTest.test_null_value_is_present_for_add`, `test_null_value_is_present_for_replace_and_test`, `AddTest.test_add_root_replaces_document`, `TestOperationTest.test_null_only_null` | `op.get("value") is None` used as the presence check. |
| Unrequired members ignored | 13 | `StructureTest.test_unrequired_from_ignored_on_add`, `test_unrequired_members_ignored` | Parsing/validating `from` whenever it appears. |
| Check order | 14 | `StructureTest.test_structure_checked_before_pointer_syntax`, `test_path_syntax_checked_before_from_syntax`, `test_earlier_operation_error_wins`, `MoveTest.test_descendant_check_before_existence` | Validating the whole patch up front (a later malformed op beats an earlier failure); parsing `from` first in move/copy; resolving `from` before the descendant check. |
| Add index boundary | 16 | `AddTest.test_index_equal_to_length_appends`, `test_index_beyond_length` | `<` vs `<=`: rejecting `i == len`, or letting `list.insert` silently clamp `i > len`. |
| Remove root | 17 | `RemoveTest.test_remove_root` | Empty-token path crashes with `IndexError` or returns `None`. |
| Replace never creates | 18 | `ReplaceTest.test_replace_missing_member` | Reusing add's dict branch. |
| Move: remove first, then add | 19 | `MoveTest.test_move_forward_in_array`, `test_move_backward_in_array`, `test_move_to_index_valid_only_before_removal`, `test_move_onto_ancestor_allowed` | Add-then-remove, or resolving `path` before the removal. |
| Move same location | 19 | `MoveTest.test_move_same_location` | Short-circuit `from == path` without checking existence. |
| Move descendant by tokens | 20 | `MoveTest.test_move_into_own_child`, `test_move_root_into_child`, `test_move_to_string_prefixed_sibling_allowed`, `test_descendant_check_before_existence` | `path.startswith(from)` rejects `/a` → `/ab`; checking existence of `from` first reports the wrong error. |
| Copy is deep | 21 | `CopyTest.test_copy_is_deep`, `test_copy_into_own_child` | Aliasing the copied subtree; copy into own child then builds a cycle. |
| JSON equality | 23 | `TestOperationTest.test_bool_not_number`, `test_bool_nested`, `test_null_only_null`, `test_int_equals_float`, `test_objects_unordered_arrays_ordered` | Python `==` makes `True == 1`, `[1, True] == [1, 1]`, `False == 0.0`. |
| Atomicity and index | 24 | `AtomicityTest.test_late_failure_leaves_nested_document_unchanged`, `test_failure_index_counts_from_zero`, `MoveTest.test_move_bad_target_leaves_document_unchanged` | Applying in place, or a shallow copy that shares nested containers. |
| No sharing with inputs | 25 | `AtomicityTest.test_success_does_not_modify_or_share_input`, `test_untouched_branches_not_shared`, `test_empty_patch_returns_distinct_copy`, `test_patch_values_not_modified`, `test_result_shares_nothing_with_patch` | Deep-copying the document but inserting op `value`s by reference; returning `document` itself for an empty patch. |

Other assertion groups: pointer splitting and leading `/` (1); basic resolution and escaped keys (3, 4); missing key (4); add/remove/replace/copy happy paths (15–18, 21); `test` on a missing path is not-found (22); failed test stops later ops (22, 24); error hierarchy (Interface).

## Variants

- `wrong/python-equality-isdigit` — independent hurried build: `str.replace` decoding with no escape validation, `isdigit` indices, Python `==` in `test`, op values aliased into the result, `from` parsed before `path`.
- `wrong/upfront-validation-shallow-copy` — validates every op's structure before applying any, shallow-copies the document, inserts `value`s by reference, replace creates missing members.
- `wrong/move-add-then-remove` — move adds before removing, `startswith(from + "/")` child check, naive escape decoding.
- `mutants/` — one rule each: 2 `tilde-decode-order-swapped`, 5 `leading-zero-index-accepted`, 12 `null-value-treated-as-missing`, 13 `unrequired-from-parsed`, 14 `from-parsed-before-path`, 16 `add-index-equal-length-rejected`, 18 `replace-creates-missing-member`, 19 `move-same-location-skips-existence`, 20 `move-child-check-string-prefix`, 21 `copy-not-deep`, 23 `bool-equals-number`, 24 `error-index-off-by-one`.
