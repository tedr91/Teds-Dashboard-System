"""Tests for per-device calendar selection used by TDS voice."""

from __future__ import annotations

import importlib.util
import pathlib


def _load_module():
    path = (
        pathlib.Path(__file__).resolve().parents[1]
        / "custom_components"
        / "teds_dashboard_system"
        / "calendar_scope.py"
    )
    spec = importlib.util.spec_from_file_location("calendar_scope", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


scope = _load_module()


def test_maps_browser_mod_device_identifier() -> None:
    assert scope.tds_device_id({("browser_mod", "office_panel")}) == "bm:office_panel"
    assert scope.tds_device_id({("mobile_app", "phone")}) is None


def test_oversized_identifier_tuples_do_not_raise() -> None:
    """HA does not guarantee 2-tuples; HomeKit Bridge children carry 3 elements.

    Callers that sweep the whole device registry hit these, so unpacking has to be
    defensive or `list_stale_devices` fails outright on any instance running HomeKit.
    """
    homekit = ("homekit", "01KVNGT3MHVM5ER3G1ZXNDSW1N", "homekit.bridge")

    assert scope.tds_device_id({homekit}) is None
    # Still found when a real browser_mod identifier sits alongside a 3-element one.
    assert scope.tds_device_id([homekit, ("browser_mod", "kitchen")]) == "bm:kitchen"
    # And a longer browser_mod identifier is still matched on its first two elements.
    assert scope.tds_device_id([("browser_mod", "kitchen", "extra")]) == "bm:kitchen"


def test_malformed_identifiers_are_skipped() -> None:
    assert scope.tds_device_id([("browser_mod",)]) is None
    assert scope.tds_device_id([("browser_mod", "")]) is None
    assert scope.tds_device_id(["browser_mod", ("browser_mod", "kitchen")]) == "bm:kitchen"
    assert scope.tds_device_id([]) is None
    assert scope.tds_device_id(None) is None


def test_device_inherits_global_calendars_without_override() -> None:
    assert scope.selected_calendars(
        ["calendar.family", "calendar.work"],
        {},
        "bm:office_panel",
    ) == ["calendar.family", "calendar.work"]


def test_device_uses_its_curated_subset() -> None:
    assert scope.selected_calendars(
        ["calendar.family", "calendar.work"],
        {"bm:office_panel": {"calendars_list": ["calendar.work"]}},
        "bm:office_panel",
    ) == ["calendar.work"]


def test_empty_override_and_unknown_calendars_remain_empty() -> None:
    assert scope.selected_calendars(
        ["calendar.family"],
        {"bm:office_panel": {"calendars_list": []}},
        "bm:office_panel",
    ) == []
    assert scope.selected_calendars(
        ["calendar.family"],
        {"bm:office_panel": {"calendars_list": ["calendar.private"]}},
        "bm:office_panel",
    ) == []


def test_expands_virtual_calendar_members_once() -> None:
    assert scope.expanded_calendars(
        ["calendar.family", "calendar.work"],
        {
            "calendar.family": {
                "virtual": True,
                "virtual_members": ["calendar.ted", "calendar.work"],
            }
        },
    ) == ["calendar.family", "calendar.ted", "calendar.work"]