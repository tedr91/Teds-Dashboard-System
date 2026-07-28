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
    {"id": "music_assistant", "kind": "integration", "match": ["music_assistant"]},
    {"id": "mass_queue", "kind": "integration", "match": ["mass_queue"]},
    # Front-end plugins (Lovelace resources). card_mod passes if EITHER card-mod
    # or UIX (a superset replacement) is installed.
    {"id": "layout_card", "kind": "resource", "match": ["layout-card"]},
    {"id": "ted_cards", "kind": "resource", "match": ["ted-cards"]},
    {"id": "card_mod", "kind": "resource", "match": ["card-mod", "/uix"]},
    {"id": "daylight_calendar", "kind": "resource", "match": ["daylight-calendar"]},
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


def _extra_module_urls(hass: HomeAssistant) -> list[str]:
    """Front-end extra-module URLs (lowercased) added via ``add_extra_js_url``.

    This is how *this* integration auto-loads the bundled Ted's Cards, so it must
    be checked in addition to Lovelace resources (how a HACS install loads them).
    """
    try:
        from homeassistant.components.frontend import DATA_EXTRA_MODULE_URL

        manager = hass.data.get(DATA_EXTRA_MODULE_URL)
        urls = getattr(manager, "urls", None)
        return [str(u).lower() for u in urls] if urls else []
    except Exception:  # noqa: BLE001 - frontend internals vary across cores
        return []


async def compute_requirements(hass: HomeAssistant) -> dict[str, str]:
    """Evaluate every requirement to ``ok`` / ``setup`` / ``missing`` / ``unknown``.

    ``setup`` means an integration is downloaded (present in ``custom_components/``,
    e.g. installed via HACS) but not yet added under Settings → Devices & Services,
    so dashboards can tell the user to add it rather than to install it.
    """
    from homeassistant.loader import async_get_custom_components

    components = set(hass.config.components)
    urls = await _resource_urls(hass)
    extra_urls = _extra_module_urls(hass)
    try:
        downloaded = set(await async_get_custom_components(hass))
    except Exception:  # noqa: BLE001 - loader shape varies; treat as none downloaded
        downloaded = set()

    result: dict[str, str] = {}
    for req in REQUIREMENTS:
        rid, kind, match = req["id"], req["kind"], req["match"]
        if kind == "integration":
            if any(m in components for m in match):
                result[rid] = "ok"
            elif any(m in downloaded for m in match):
                result[rid] = "setup"
            else:
                result[rid] = "missing"
        elif kind == "resource":
            # Ted's Cards may be served by this integration (add_extra_js_url)
            # rather than a HACS Lovelace resource — accept either source.
            if rid == "ted_cards" and any(m in u for m in match for u in extra_urls):
                result[rid] = "ok"
            elif urls is None:
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
