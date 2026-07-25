"""Server-side dependency detection for the Ted Dashboard system.

After Home Assistant has started, we inspect the running instance to determine
which optional dependencies are present, and expose the result (via
``sensor.teds_requirements``) so dashboards can surface friendly, targeted
warnings without any fragile front-end detection.

Detection sources:
- **integration** — the domain is in ``hass.config.components``.
- **resource**    — a registered Lovelace resource URL contains one of the
                    match strings (how HACS front-end plugins are loaded).
- **entity**      — at least one entity exists in the given domain.

Each requirement resolves to ``"ok"``, ``"missing"`` or ``"unknown"`` (the last
when it can't be determined — e.g. resources aren't readable — so dashboards
never false-alarm).
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant

# id -> detection spec. ``id`` doubles as the attribute name on the sensor, so a
# dashboard gates a MessageBox with e.g. {condition: state,
# entity: sensor.teds_requirements, attribute: card_mod, state: missing}.
REQUIREMENTS: list[dict] = [
    # Integrations
    {"id": "hacs", "kind": "integration", "match": ["hacs"]},
    {"id": "browser_mod", "kind": "integration", "match": ["browser_mod"]},
    {"id": "custom_icons", "kind": "integration", "match": ["custom_icons"]},
    # Front-end plugins (Lovelace resources). card_mod passes if EITHER card-mod
    # or UIX (a superset replacement) is installed.
    {"id": "layout_card", "kind": "resource", "match": ["layout-card"]},
    {"id": "ted_cards", "kind": "resource", "match": ["ted-cards"]},
    {"id": "card_mod", "kind": "resource", "match": ["card-mod", "/uix"]},
    {"id": "daylight_calendar", "kind": "resource", "match": ["daylight-calendar"]},
    {"id": "kiosk_mode", "kind": "resource", "match": ["kiosk-mode", "kiosk_mode"]},
    # Entities
    {"id": "weather", "kind": "entity", "match": ["weather"]},
]


async def _resource_urls(hass: HomeAssistant) -> list[str] | None:
    """Registered Lovelace resource URLs (lowercased), or None if unreadable."""
    try:
        from homeassistant.components.lovelace import LOVELACE_DATA

        data = hass.data.get(LOVELACE_DATA)
        res = getattr(data, "resources", None) if data else None
        if res is None:
            return None
        try:
            if hasattr(res, "async_load"):
                await res.async_load()
        except Exception:  # noqa: BLE001 - already loaded / not needed
            pass
        return [str(item.get("url", "")).lower() for item in res.async_items()]
    except Exception:  # noqa: BLE001 - lovelace shape varies across cores
        return None


async def compute_requirements(hass: HomeAssistant) -> dict[str, str]:
    """Evaluate every requirement to ``ok`` / ``missing`` / ``unknown``."""
    components = set(hass.config.components)
    urls = await _resource_urls(hass)

    result: dict[str, str] = {}
    for req in REQUIREMENTS:
        rid, kind, match = req["id"], req["kind"], req["match"]
        if kind == "integration":
            result[rid] = "ok" if any(m in components for m in match) else "missing"
        elif kind == "resource":
            if urls is None:
                result[rid] = "unknown"
            else:
                result[rid] = (
                    "ok" if any(m in u for m in match for u in urls) else "missing"
                )
        elif kind == "entity":
            result[rid] = (
                "ok"
                if any(hass.states.async_entity_ids(dom) for dom in match)
                else "missing"
            )
        else:
            result[rid] = "unknown"
    return result
