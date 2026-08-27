"""Per-device calendar selection shared by TDS voice intents."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any


def tds_device_id(identifiers: Iterable[Sequence[str]]) -> str | None:
    """Map Home Assistant device identifiers to TDS's settings key.

    Unpacks defensively: HA does not guarantee 2-tuples, and real instances carry
    longer ones (HomeKit Bridge child devices use a 3-element
    `("homekit", <id>, "homekit.bridge")`). Anything that isn't a `("browser_mod", id)`
    pair is skipped rather than raising, so callers can safely sweep the whole registry.
    """
    for identifier in identifiers or ():
        if isinstance(identifier, (str, bytes)) or not isinstance(identifier, Sequence):
            continue
        if len(identifier) < 2 or identifier[0] != "browser_mod":
            continue
        if identifier[1]:
            return f"bm:{identifier[1]}"
    return None


def selected_calendars(
    global_value: Any,
    device_settings: Mapping[str, Mapping[str, Any]],
    device_id: str | None,
) -> list[str]:
    """Return this device's calendar subset, limited to the global allow-list."""
    global_calendars = [
        entity_id
        for entity_id in global_value if isinstance(entity_id, str)
    ] if isinstance(global_value, list) else []
    settings = device_settings.get(device_id, {}) if device_id else {}
    chosen = settings.get("calendars_list", global_calendars)
    if not isinstance(chosen, list):
        chosen = global_calendars
    return [
        entity_id
        for entity_id in chosen
        if isinstance(entity_id, str) and entity_id in global_calendars
    ]


def expanded_calendars(
    selected: Iterable[str],
    calendar_options: Any,
) -> list[str]:
    """Expand selected virtual calendars to the entities their card displays."""
    options = calendar_options if isinstance(calendar_options, Mapping) else {}
    expanded: list[str] = []
    for entity_id in selected:
        if entity_id not in expanded:
            expanded.append(entity_id)
        item = options.get(entity_id)
        if not isinstance(item, Mapping) or item.get("virtual") is not True:
            continue
        members = item.get("virtual_members")
        if not isinstance(members, list):
            continue
        for member in members:
            if isinstance(member, str) and member not in expanded:
                expanded.append(member)
    return expanded