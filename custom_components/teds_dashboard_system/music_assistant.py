"""Bridge to the Music Assistant server for auto-creating a device's MA player.

Ted's cards resolve a device's Music Assistant player by matching its speaker to an
existing ``music_assistant``-platform ``media_player`` entity. When none exists, this
module can create one on the user's behalf by driving the Music Assistant *server* API
through the connection the Home Assistant ``music_assistant`` integration already holds
(``entry.runtime_data.mass`` — a ``MusicAssistantClient``). No separate MA URL/token is
needed; we reuse the authenticated client.

The flow mirrors what the user would do by hand in the MA UI:
  1. Ensure the **Home Assistant** plugin provider (``hass``) is set up (the player
     provider depends on it).
  2. Ensure the **Home Assistant MediaPlayers** provider (``hass_players``) exists and
     includes this device's ``media_player`` entity in its ``players`` selection.
  3. Configure that player (its MA ``player_id`` equals the HA ``entity_id``): expose it
     to HA, set a friendly icon, and enable Smart Crossfade.

Key strings/values are taken verbatim from the Music Assistant server source
(``music_assistant/constants.py`` and the ``hass_players`` provider).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

_LOGGER = logging.getLogger(__name__)

# Music Assistant provider domains (from each provider's manifest.json).
_HASS_PLUGIN_DOMAIN = "hass"
_HASS_PLAYERS_DOMAIN = "hass_players"
# hass_players is single-instance, so its instance_id equals its domain.
_HASS_PLAYERS_INSTANCE = _HASS_PLAYERS_DOMAIN
# Config key selecting which HA media_player entity_ids to import (multi-value list).
_CONF_PLAYERS = "players"

# Player config keys/values (from music_assistant/constants.py).
_CONF_EXPOSE_TO_HA = "expose_player_to_ha"
_CONF_ICON = "icon"
_CONF_SMART_FADES = "smart_fades_mode"
_SMART_CROSSFADE = "smart_crossfade"
_PLAYER_ICON = "mdi-television-speaker"

# How long to wait for the player to (re)register after saving the provider config.
_REGISTER_TIMEOUT_S = 15
_POLL_INTERVAL_S = 0.5

# Prefix on the user-facing error when the `hass` plugin provider can't be auto-added
# (an external Music Assistant server needs a URL + token). The websocket handler turns
# this into a distinct error code so the UI can show a *guided* step, not a hard failure.
GUIDE_HASS_PLUGIN = "GUIDE_HASS_PLUGIN: "


def _get_mass_client(hass: HomeAssistant) -> Any | None:
    """Return the live MusicAssistantClient from the HA music_assistant integration.

    Reuses the existing authenticated server connection rather than opening our own.
    Returns ``None`` when the integration isn't set up / loaded.
    """
    for entry in hass.config_entries.async_entries("music_assistant"):
        if entry.state is not ConfigEntryState.LOADED:
            continue
        data = getattr(entry, "runtime_data", None)
        mass = getattr(data, "mass", None)
        if mass is not None:
            return mass
    return None


async def _command(mass: Any, command: str, **kwargs: Any) -> Any:
    """Send a raw Music Assistant API command via the shared client."""
    send = getattr(mass, "send_command", None)
    if send is None:  # pragma: no cover - defensive; client API changed
        raise HomeAssistantError("Music Assistant client does not support send_command.")
    return await send(command, **kwargs)


def _config_value(values: Any, key: str, default: Any = None) -> Any:
    """Extract a config-entry value robustly across serialized shapes.

    Provider/player config ``values`` may serialize an entry as a raw value or as a
    ``{"value": ..., "default_value": ...}`` mapping; handle both.
    """
    if not isinstance(values, dict):
        return default
    raw = values.get(key)
    if isinstance(raw, dict):
        val = raw.get("value")
        if val is None:
            val = raw.get("default_value")
        return default if val is None else val
    return default if raw is None else raw


async def _hass_plugin_enabled(mass: Any) -> bool:
    """True when Music Assistant's `hass` plugin provider is set up and enabled."""
    provs = await _command(mass, "config/providers", provider_domain=_HASS_PLUGIN_DOMAIN)
    return any((p or {}).get("enabled", True) for p in (provs or []))


async def _ensure_hass_plugin(mass: Any) -> None:
    """Ensure the `hass` plugin provider exists (the player provider depends on it).

    When Music Assistant runs as the Home Assistant **add-on**, that provider needs no
    user input (URL = the supervisor API, token auto-retrieved), so an empty
    ``config/providers/save`` instantiates it. On an **external** MA server the provider
    requires a user-supplied URL + long-lived token, so the save is rejected during
    validation — we surface a *guided* error instead (prefixed ``GUIDE_HASS_PLUGIN``) so
    the caller can point the user at Music Assistant's own provider setup.

    Trying the zero-input save (rather than pre-detecting add-on vs external) keeps this
    robust to Music Assistant API differences: it only ever succeeds when no input is
    needed, and validation cleanly rejects it (saving nothing) otherwise.
    """
    if await _hass_plugin_enabled(mass):
        return
    try:
        await _command(
            mass, "config/providers/save", provider_domain=_HASS_PLUGIN_DOMAIN, values={}
        )
    except HomeAssistantError:
        raise
    except Exception as err:  # noqa: BLE001 - save rejected = needs user input (external MA)
        raise HomeAssistantError(
            f"{GUIDE_HASS_PLUGIN}Music Assistant's Home Assistant connection isn't set up. "
            "In Music Assistant, add the Home Assistant provider under Settings → "
            "Providers, then try again."
        ) from err
    # Wait for the newly-added plugin to come online before adding the dependent player.
    deadline = asyncio.get_running_loop().time() + _REGISTER_TIMEOUT_S
    while asyncio.get_running_loop().time() < deadline:
        if await _hass_plugin_enabled(mass):
            return
        await asyncio.sleep(_POLL_INTERVAL_S)
    raise HomeAssistantError(
        "Set up Music Assistant's Home Assistant connection, but it didn't come online in "
        "time. It may still finish shortly — try again in a moment."
    )


async def async_create_music_player(hass: HomeAssistant, entity_id: str) -> dict[str, Any]:
    """Create/configure a Music Assistant player for a device's media_player entity.

    Returns ``{"player_id": entity_id}`` on success. Raises ``HomeAssistantError`` with a
    user-facing message when a prerequisite is missing or the server rejects a step.
    """
    if not entity_id or not entity_id.startswith("media_player."):
        raise HomeAssistantError("A media_player entity is required to create an MA player.")

    mass = _get_mass_client(hass)
    if mass is None:
        raise HomeAssistantError(
            "The Music Assistant integration isn't set up or connected in Home Assistant."
        )

    # 1) The player provider depends on the Home Assistant plugin provider being set up.
    #    Auto-add it when Music Assistant runs as the HA add-on; otherwise guide the user.
    await _ensure_hass_plugin(mass)

    # 2) Ensure the hass_players provider exists and includes this entity.
    hp_provs = await _command(
        mass, "config/providers", provider_domain=_HASS_PLAYERS_DOMAIN, include_values=True
    )
    existing = hp_provs[0] if hp_provs else None
    current: list[str] = []
    if existing:
        raw = _config_value(existing.get("values"), _CONF_PLAYERS, [])
        current = list(raw) if isinstance(raw, (list, tuple)) else []

    if entity_id not in current:
        await _command(
            mass,
            "config/providers/save",
            provider_domain=_HASS_PLAYERS_DOMAIN,
            instance_id=_HASS_PLAYERS_INSTANCE if existing else None,
            values={_CONF_PLAYERS: [*current, entity_id]},
        )

    # 3) The hass_players provider registers each player with player_id == entity_id.
    #    Saving the provider config reloads it, so wait for our player to appear.
    deadline = asyncio.get_running_loop().time() + _REGISTER_TIMEOUT_S
    while asyncio.get_running_loop().time() < deadline:
        players = await _command(mass, "config/players", provider=_HASS_PLAYERS_INSTANCE)
        if any((p or {}).get("player_id") == entity_id for p in (players or [])):
            break
        await asyncio.sleep(_POLL_INTERVAL_S)
    else:
        raise HomeAssistantError(
            "Music Assistant accepted the player but it didn't register in time. It may "
            "still appear shortly — refresh and check again."
        )

    # 4) Configure the new player: expose to HA + friendly icon (one save), then Smart
    #    Crossfade separately (it's unavailable on low-memory servers; don't let that
    #    failure undo the rest).
    await _command(
        mass,
        "config/players/save",
        player_id=entity_id,
        values={_CONF_EXPOSE_TO_HA: True, _CONF_ICON: _PLAYER_ICON},
    )
    try:
        await _command(
            mass,
            "config/players/save",
            player_id=entity_id,
            values={_CONF_SMART_FADES: _SMART_CROSSFADE},
        )
    except HomeAssistantError:
        raise
    except Exception as err:  # noqa: BLE001 - Smart Crossfade is best-effort
        _LOGGER.warning(
            "Created MA player %s but could not enable Smart Crossfade: %s", entity_id, err
        )

    return {"player_id": entity_id}
