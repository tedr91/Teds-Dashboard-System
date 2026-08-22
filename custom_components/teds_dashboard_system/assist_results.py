"""Validation for structured Assist response results."""

from __future__ import annotations

import math
from typing import Any

MAX_RESULTS = 8
MAX_FORECAST_ITEMS = 24
MAX_COLLECTION_ITEMS = 100
MAX_DEPTH = 10


def _json_safe(value: Any, depth: int = 0) -> Any:
    """Return a bounded JSON-safe copy, or None for unsupported values."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if depth >= MAX_DEPTH:
        return None
    if isinstance(value, list):
        return [
            _json_safe(item, depth + 1)
            for item in value[:MAX_COLLECTION_ITEMS]
        ]
    if isinstance(value, dict):
        return {
            key: _json_safe(item, depth + 1)
            for key, item in list(value.items())[:MAX_COLLECTION_ITEMS]
            if isinstance(key, str)
        }
    return None


def normalize_assist_results(results: Any) -> list[dict[str, Any]]:
    """Keep only supported, valid Weather and Entity Card result payloads."""
    if not isinstance(results, list):
        return []
    normalized: list[dict[str, Any]] = []
    for result in results[:MAX_RESULTS]:
        if not isinstance(result, dict):
            continue
        kind = result.get("kind")
        if kind == "weather":
            forecast = result.get("forecast")
            if not isinstance(forecast, list):
                forecast = []
            item = {
                "kind": "weather",
                "forecast": [
                    _json_safe(entry)
                    for entry in forecast[:MAX_FORECAST_ITEMS]
                    if isinstance(entry, dict)
                ],
            }
            for key in (
                "forecastType",
                "currentTemperature",
                "currentHumidity",
                "conditionIcon",
            ):
                value = result.get(key)
                if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                    item[key] = _json_safe(value)
            if item["forecast"] or "currentTemperature" in item or "currentHumidity" in item:
                normalized.append(item)
        elif kind == "entity_card":
            card = result.get("card")
            if not isinstance(card, dict) or not isinstance(card.get("type"), str):
                continue
            item = {"kind": "entity_card", "card": _json_safe(card)}
            card_size = result.get("cardSize")
            if isinstance(card_size, (int, float)) and not isinstance(card_size, bool):
                item["cardSize"] = _json_safe(card_size)
            normalized.append(item)
    return normalized