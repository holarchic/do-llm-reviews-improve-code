# Traps (private)

| Trap | TASK.md clause | Hidden tests |
|---|---|---|
| Offsets converted to UTC, incl. crossing midnight | 1 | `test_offset_converted_to_utc`, `test_negative_offset_crosses_midnight`, `test_offsets_land_in_same_utc_window` |
| Offset required | 1 | `test_missing_offset_is_invalid` |
| `WARNING` → `WARN`, any case | 2 | `test_level_case_and_warning` |
| Epoch alignment, not wall-clock | 7 | `test_epoch_alignment` |
| Floor (not truncation) before 1970 | 7 | `test_before_epoch_floors_down` |
| Message keeps inner spacing and brackets | 4 | `test_message_keeps_brackets_and_spaces` |

Other assertion groups: basic parse (1–4); fractional seconds (1); unknown level (2); service charset (3); empty message (4); invalid lines → None (5); bad width (6); counts and sort order, out-of-order input (8); fraction stays in window (7); skipping invalid lines (8); any iterable, UTC starts (Interface, 7).

Follow-up: sort by count then name; n limits; level rules; validation (FOLLOWUP).

## Degraded reference (judge calibration)

`degraded/` passes every hidden test. Injected defects: one-letter names and a long inline regex; a broad `except Exception` swallowing everything in `parse_line` and an if/else ladder in place of a level table; hand-rolled counting dict and commented-out code.
