"""Which tasks a written manifest freezes.

A task whose files a manifest hashes belongs to that experiment's record: its
`expected.json` is part of the freeze, so `validate` never rewrites it. A
correction to such a task is a new task beside it (`<id>-v2`).
"""

import json
from pathlib import Path

from . import manifest


def frozen_tasks() -> dict[str, str]:
    """Task id -> the file name of the first manifest that freezes it.

    An unreadable manifest raises ManifestMismatch: freezing is never assumed absent.
    """
    found: dict[str, str] = {}
    for path in [manifest.MANIFEST, *sorted(manifest.MANIFESTS.glob("*.json"))]:
        if not path.is_file():
            continue
        try:
            names = json.loads(path.read_text(encoding="utf-8"))["files"]
        except (json.JSONDecodeError, UnicodeDecodeError, KeyError, TypeError) as exc:
            raise manifest.ManifestMismatch(f"{path.name} has no readable files table: {exc!r}") from None
        for name in names:
            parts = Path(name).parts
            if parts[:2] == ("eval", "tasks") and len(parts) > 3:
                found.setdefault(parts[2], path.name)
    return found
