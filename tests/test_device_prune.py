"""Tests for device prune-candidate reporting and explicit removal."""

from __future__ import annotations

import importlib.util
import pathlib
import sys
from datetime import datetime, timedelta, timezone


def _load(module_name: str):
    path = (
        pathlib.Path(__file__).resolve().parents[1]
        / "custom_components"
        / "teds_dashboard_system"
        / f"{module_name}.py"
    )
    name = f"custom_components.teds_dashboard_system.{module_name}"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(name)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    # device_prune imports nothing, so restore whatever another test module had
    # registered rather than leaving our copy behind.
    if previous is None:
        sys.modules.pop(name, None)
    else:
        sys.modules[name] = previous
    return module


device_prune = _load("device_prune")

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
TTL = 900


def _seen(seconds_ago: float) -> str:
    return (NOW - timedelta(seconds=seconds_ago)).isoformat()


def _registry() -> dict:
    return {
        # A live, currently-present panel.
        "bm:surfacepro": {"name": "Surface Pro", "area": "office", "last_seen": _seen(30)},
        # A live device that is merely asleep: stale last_seen, but still in HA.
        "bm:iphoneair": {"name": "iPhone Air", "area": "bedroom", "last_seen": _seen(86400)},
        # Modern auto-generated orphan, gone from HA.
        "bm:browser_mod_5e2bea59_8274057d": {"name": None, "last_seen": _seen(500000)},
        # Legacy dash-format auto-generated orphan.
        "bm:5e05cb7a-96bf37c7": {"name": None, "last_seen": _seen(400000)},
        # A human-named stale entry (e.g. a half-finished rename) — never auto-flagged.
        "bm:browser_mod_Surface_Pro_X2_Elite": {"name": "Surface", "last_seen": _seen(600000)},
        # A key outside the "bm:" namespace: liveness is unknowable.
        "va:kitchen": {"name": "Kitchen VA", "last_seen": None},
    }


def _rows() -> dict:
    rows = device_prune.candidate_rows(
        _registry(),
        {"bm:surfacepro": {"theme": "dark"}, "bm:5e05cb7a-96bf37c7": {"theme": "light"}},
        {"bm:surfacepro", "bm:iphoneair", "bm:browser_mod_Surface_Pro_X2_Elite"},
        NOW,
        TTL,
    )
    return {row["device_id"]: row for row in rows}


def test_auto_id_detection_covers_both_browser_mod_formats() -> None:
    assert device_prune.is_auto_browser_id("bm:browser_mod_5e2bea59_8274057d")
    assert device_prune.is_auto_browser_id("bm:5e05cb7a-96bf37c7")
    # Human-chosen IDs are never auto, even with the browser_mod_ prefix.
    assert not device_prune.is_auto_browser_id("bm:browser_mod_Surface_Pro_X2_Elite")
    assert not device_prune.is_auto_browser_id("bm:surfacepro")
    assert not device_prune.is_auto_browser_id("va:kitchen")
    assert not device_prune.is_auto_browser_id(None)


def test_sleeping_device_is_never_flagged_as_an_orphan() -> None:
    """A stale last_seen alone must never mark a live device for pruning."""
    row = _rows()["bm:iphoneair"]

    assert row["present"] is False
    assert row["last_seen_seconds_ago"] == 86400
    assert row["device_exists"] is True
    assert row["likely_orphan"] is False


def test_orphans_are_flagged_and_carry_judgement_context() -> None:
    rows = _rows()
    orphan = rows["bm:browser_mod_5e2bea59_8274057d"]

    assert orphan["likely_orphan"] is True
    assert orphan["device_exists"] is False
    assert orphan["auto_id"] is True
    assert orphan["present"] is False
    assert orphan["approx_bytes"] > 0

    legacy = rows["bm:5e05cb7a-96bf37c7"]
    assert legacy["likely_orphan"] is True
    assert legacy["override_keys"] == 1


def test_present_and_unknown_namespace_devices_are_kept_clear() -> None:
    rows = _rows()

    live = rows["bm:surfacepro"]
    assert live["present"] is True
    assert live["likely_orphan"] is False
    assert live["override_keys"] == 1

    # A renamed-but-still-real device is not auto-flagged even though it is stale.
    renamed = rows["bm:browser_mod_Surface_Pro_X2_Elite"]
    assert renamed["auto_id"] is False
    assert renamed["likely_orphan"] is False

    # Outside the "bm:" namespace liveness is unknown, so it can't read as an orphan.
    unknown = rows["va:kitchen"]
    assert unknown["device_exists"] is None
    assert unknown["last_seen_seconds_ago"] is None
    assert unknown["likely_orphan"] is False


def test_candidates_are_sorted_orphans_first_and_summarized() -> None:
    rows = device_prune.candidate_rows(
        _registry(), {}, {"bm:surfacepro", "bm:iphoneair"}, NOW, TTL
    )

    assert rows[0]["likely_orphan"] is True
    assert rows[1]["likely_orphan"] is True
    assert rows[-1]["device_id"] == "bm:surfacepro"

    summary = device_prune.summarize(rows)
    assert summary["total"] == 6
    # The renamed entry is now also missing from HA, but is not auto-generated.
    assert summary["likely_orphans"] == 2
    assert 0 < summary["likely_orphan_bytes"] < summary["total_bytes"]


def test_naive_and_unparseable_timestamps_do_not_raise() -> None:
    rows = device_prune.candidate_rows(
        {
            "bm:naive": {"last_seen": "2026-08-26T11:59:30"},
            "bm:garbage": {"last_seen": "not-a-timestamp"},
            "bm:missing": {},
        },
        {},
        set(),
        NOW,
        TTL,
    )
    by_id = {row["device_id"]: row for row in rows}

    assert by_id["bm:naive"]["last_seen_seconds_ago"] == 30
    assert by_id["bm:naive"]["present"] is True
    assert by_id["bm:garbage"]["last_seen_seconds_ago"] is None
    assert by_id["bm:missing"]["present"] is False


def test_settings_only_entry_is_still_reportable() -> None:
    """An override left behind without a registry entry is prunable, so report it."""
    rows = device_prune.candidate_rows(
        {"bm:surfacepro": {"name": "Surface Pro", "last_seen": _seen(30)}},
        {"bm:browser_mod_5e2bea59_8274057d": {"theme": "dark"}},
        {"bm:surfacepro"},
        NOW,
        TTL,
    )
    by_id = {row["device_id"]: row for row in rows}

    stray = by_id["bm:browser_mod_5e2bea59_8274057d"]
    assert stray["likely_orphan"] is True
    assert stray["override_keys"] == 1
    assert stray["last_seen"] is None
    assert stray["name"] is None
    # Reported once, not twice, when it exists in both structures.
    assert len(rows) == 2
