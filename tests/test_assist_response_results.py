"""Tests for structured Assist response result validation."""

from __future__ import annotations

import importlib.util
import pathlib


def _load_module():
    path = (
        pathlib.Path(__file__).resolve().parents[1]
        / "custom_components"
        / "teds_dashboard_system"
        / "assist_results.py"
    )
    spec = importlib.util.spec_from_file_location("assist_results", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


normalize_assist_results = _load_module().normalize_assist_results


def test_normalizes_supported_results() -> None:
    results = normalize_assist_results([
        {
            "kind": "weather",
            "forecastType": "daily",
            "currentTemperature": 72,
            "forecast": [{"datetime": "2026-08-24", "temperature": 77}],
        },
        {
            "kind": "entity_card",
            "card": {"type": "picture-entity", "entity": "camera.front_door"},
            "cardSize": 4,
        },
    ])

    assert results == [
        {
            "kind": "weather",
            "forecastType": "daily",
            "currentTemperature": 72,
            "forecast": [{"datetime": "2026-08-24", "temperature": 77}],
        },
        {
            "kind": "entity_card",
            "card": {"type": "picture-entity", "entity": "camera.front_door"},
            "cardSize": 4,
        },
    ]


def test_drops_malformed_and_unsupported_results() -> None:
    results = normalize_assist_results([
        None,
        {"kind": "wikipedia", "content": "ignored"},
        {"kind": "weather", "forecast": "bad"},
        {"kind": "entity_card", "card": {"entity": "light.kitchen"}},
    ])

    assert results == []


def test_bounds_and_sanitizes_nested_values() -> None:
    results = normalize_assist_results([
        {
            "kind": "entity_card",
            "card": {
                "type": "entities",
                "entities": [f"sensor.item_{index}" for index in range(150)],
                "unsupported": object(),
            },
        }
    ] * 20)

    assert len(results) == 8
    assert len(results[0]["card"]["entities"]) == 100
    assert results[0]["card"]["unsupported"] is None