"""OpenRouter's own facts: its API base, pilot 2's prices and its spend record.

Key handling and each CLI's routing for every provider, OpenRouter included, live in `providers.py`.
"""

import json
from dataclasses import dataclass
from pathlib import Path

API_BASE_URL = "https://openrouter.ai/api/v1"


@dataclass(frozen=True)
class Price:
    """Dollars per million tokens, as listed on 2026-10-21. Descriptive only: routing picks the provider."""
    input: float
    cache_read: float
    output: float


PRICES = {
    "qwen/qwen3-coder-30b-a3b-instruct": Price(input=0.07, cache_read=0.07, output=0.28),
    "z-ai/glm-4.7-flash": Price(input=0.0605, cache_read=0.01, output=0.4),
}


def spent(ledger: Path, phase: str) -> float | None:
    """Pilot 2's recorded spend for a phase (its last reading minus its baseline), for its report."""
    try:
        entry = json.loads(ledger.read_text(encoding="utf-8"))[phase]
        return entry["readings"][-1] - entry["baseline"]
    except (FileNotFoundError, KeyError, IndexError, json.JSONDecodeError):
        return None
