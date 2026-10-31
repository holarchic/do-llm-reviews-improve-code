"""Which tasks a written manifest freezes.

A task whose files a manifest hashes belongs to that experiment's record: its
`expected.json` is part of the freeze, so `validate` never rewrites it. A
correction to such a task is a new task beside it (`<id>-v2`).

Fails closed: only a manifest (or manifests directory) that does not exist is
absent. Any other error listing, reading or parsing one raises ManifestMismatch,
so no filesystem fault makes a frozen task look unfrozen. `glob()` and
`is_file()` are avoided because they swallow such errors.
"""

import json
from pathlib import Path

from . import manifest


def frozen_tasks() -> dict[str, str]:
    """Task id -> the file name of the first manifest that freezes it."""
    found: dict[str, str] = {}
    for path in _manifest_paths():
        for name in _files_table(path):
            parts = Path(name).parts
            if parts[:2] == ("eval", "tasks") and len(parts) > 3:
                found.setdefault(parts[2], path.name)
    return found


def _manifest_paths() -> list[Path]:
    try:
        listed = sorted(p for p in manifest.MANIFESTS.iterdir() if p.suffix == ".json")
    except FileNotFoundError:
        listed = []
    except OSError as exc:
        raise manifest.ManifestMismatch(f"cannot list {manifest.MANIFESTS.name}/: {exc!r}") from None
    return [manifest.MANIFEST, *listed]


def _files_table(path: Path) -> dict[str, str]:
    """The manifest's path -> hash table; empty when the manifest does not exist."""
    try:
        names = json.loads(path.read_text(encoding="utf-8"))["files"]
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, KeyError, TypeError) as exc:
        raise manifest.ManifestMismatch(f"{path.name} has no readable files table: {exc!r}") from None
    if not isinstance(names, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in names.items()):
        raise manifest.ManifestMismatch(f"{path.name}: files is not a table of path -> hash")
    return names
