"""Recorder exclusions for large data-carrier sensor attributes.

Several sensors (teds_settings, teds_vision_events, teds_assist_responses) expose
large payloads as state attributes for live reading; the cards consume that data
over WebSocket, not from recorded history. Recording these blobs exceeds the
recorder's 16 KB attribute limit (logged as a warning) and bloats the database,
so exclude them from recording. Live state attributes are unaffected.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant, callback


@callback
def exclude_attributes(hass: HomeAssistant) -> set[str]:
    """Attribute names never worth recording for this integration's entities."""
    return {
        # teds_settings
        "defaults",
        "global",
        "devices",
        "registry",
        # teds_vision_events
        "events",
        # teds_assist_responses
        "responses",
        "history",
    }
