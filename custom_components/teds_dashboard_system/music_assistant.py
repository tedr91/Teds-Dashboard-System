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
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_MA_ADMIN_TOKEN, DOMAIN

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

# Prefix when no Music Assistant admin token is configured (or MA rejected it). MA gates
# config writes behind an admin role, so without a token we can't auto-create — the
# websocket handler maps this to a distinct code so the UI shows the guided/token path.
GUIDE_NEEDS_TOKEN = "GUIDE_NEEDS_TOKEN: "

# An async Music Assistant command runner (bound to the admin HTTP endpoint).
_Cmd = Callable[..., Awaitable[Any]]


def _config_endpoint(hass: HomeAssistant) -> tuple[str, str] | None:
    """Return ``(base_url, admin_token)`` for MA config writes, or ``None``.

    MA gates provider/player config writes behind an admin role, and the HA integration's
    own connection is a non-admin "system user". So we drive config writes over MA's
    JSON-RPC HTTP API (``POST {base}/api``) authenticated with the admin token from this
    integration's options, reusing the Music Assistant server URL the integration already
    connects to. Returns ``None`` when no token is configured (→ guided setup) or MA isn't
    set up.
    """
    tds = next(iter(hass.config_entries.async_entries(DOMAIN)), None)
    token = ((tds.options.get(CONF_MA_ADMIN_TOKEN) if tds else "") or "").strip()
    if not token:
        return None
    ma = next(
        (
            e
            for e in hass.config_entries.async_entries("music_assistant")
            if e.state is ConfigEntryState.LOADED
        ),
        None,
    )
    url = ma.data.get("url") if ma else None
    if not url:
        return None
    base = str(url).rstrip("/")
    if base.endswith("/ws"):
        base = base[:-3]
    return base, token


async def _config_command(
    session: Any, base_url: str, token: str, command: str, **args: Any
) -> Any:
    """Run a Music Assistant API command over its JSON-RPC HTTP endpoint as admin."""
    payload = {"command": command, "message_id": uuid.uuid4().hex, "args": args or {}}
    async with session.post(
        f"{base_url}/api",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    ) as resp:
        text = await resp.text()
        if resp.status == 200:
            return json.loads(text) if text.strip() else None
        if resp.status in (401, 403):
            raise HomeAssistantError(
                f"{GUIDE_NEEDS_TOKEN}Music Assistant rejected the admin token "
                f"(HTTP {resp.status}). Set a valid Music Assistant admin token in "
                "Ted's Dashboard System → Configure."
            )
        raise HomeAssistantError(
            f"Music Assistant API error (HTTP {resp.status}): {text[:200]}"
        )


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


async def _hass_plugin_enabled(cmd: _Cmd) -> bool:
    """True when Music Assistant's `hass` plugin provider is set up and enabled."""
    provs = await cmd("config/providers", provider_domain=_HASS_PLUGIN_DOMAIN)
    return any((p or {}).get("enabled", True) for p in (provs or []))


async def _ensure_hass_plugin(cmd: _Cmd) -> None:
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
    if await _hass_plugin_enabled(cmd):
        _LOGGER.info("teds MA auto-create: Home Assistant plugin provider already set up")
        return
    _LOGGER.info(
        "teds MA auto-create: Home Assistant plugin provider missing — trying to add it "
        "(works when Music Assistant runs as the HA add-on)"
    )
    try:
        await cmd(
            "config/providers/save", provider_domain=_HASS_PLUGIN_DOMAIN, values={}
        )
    except HomeAssistantError:
        raise
    except Exception as err:  # noqa: BLE001 - save rejected = needs user input (external MA)
        _LOGGER.info(
            "teds MA auto-create: couldn't auto-add the Home Assistant plugin provider "
            "(likely an external Music Assistant server needing a URL + token): %s",
            err,
        )
        raise HomeAssistantError(
            f"{GUIDE_HASS_PLUGIN}Music Assistant's Home Assistant connection isn't set up. "
            "In Music Assistant, add the Home Assistant provider under Settings → "
            "Providers, then try again."
        ) from err
    # Wait for the newly-added plugin to come online before adding the dependent player.
    deadline = asyncio.get_running_loop().time() + _REGISTER_TIMEOUT_S
    while asyncio.get_running_loop().time() < deadline:
        if await _hass_plugin_enabled(cmd):
            _LOGGER.info("teds MA auto-create: Home Assistant plugin provider is now online")
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

    endpoint = _config_endpoint(hass)
    if endpoint is None:
        raise HomeAssistantError(
            f"{GUIDE_NEEDS_TOKEN}Set a Music Assistant admin token in Ted's Dashboard "
            "System → Configure to let it set this device up as a Music Assistant player, "
            "or add the device yourself in Music Assistant → Settings → Providers → Home "
            "Assistant Players."
        )
    base_url, token = endpoint
    session = async_get_clientsession(hass, verify_ssl=False)

    async def cmd(command: str, **args: Any) -> Any:
        return await _config_command(session, base_url, token, command, **args)

    _LOGGER.info("teds MA auto-create: starting for %s", entity_id)

    # 1) The player provider depends on the Home Assistant plugin provider being set up.
    #    Auto-add it when Music Assistant runs as the HA add-on; otherwise guide the user.
    await _ensure_hass_plugin(cmd)

    # 2) Ensure the hass_players provider exists and includes this entity.
    hp_provs = await cmd(
        "config/providers", provider_domain=_HASS_PLAYERS_DOMAIN, include_values=True
    )
    existing = hp_provs[0] if hp_provs else None
    current: list[str] = []
    if existing:
        raw = _config_value(existing.get("values"), _CONF_PLAYERS, [])
        current = list(raw) if isinstance(raw, (list, tuple)) else []

    if entity_id in current:
        _LOGGER.info("teds MA auto-create: %s already in the HA MediaPlayers provider", entity_id)
    else:
        _LOGGER.info(
            "teds MA auto-create: adding %s to the HA MediaPlayers provider (%s)",
            entity_id,
            "updating existing" if existing else "creating provider",
        )
        await cmd(
            "config/providers/save",
            provider_domain=_HASS_PLAYERS_DOMAIN,
            instance_id=_HASS_PLAYERS_INSTANCE if existing else None,
            values={_CONF_PLAYERS: [*current, entity_id]},
        )

    # 3) The hass_players provider registers each player with player_id == entity_id.
    #    Saving the provider config reloads it, so wait for our player to appear.
    _LOGGER.info("teds MA auto-create: waiting for player %s to register", entity_id)
    deadline = asyncio.get_running_loop().time() + _REGISTER_TIMEOUT_S
    while asyncio.get_running_loop().time() < deadline:
        players = await cmd("config/players", provider=_HASS_PLAYERS_INSTANCE)
        if any((p or {}).get("player_id") == entity_id for p in (players or [])):
            break
        await asyncio.sleep(_POLL_INTERVAL_S)
    else:
        _LOGGER.warning(
            "teds MA auto-create: player %s did not register within %ss",
            entity_id,
            _REGISTER_TIMEOUT_S,
        )
        raise HomeAssistantError(
            "Music Assistant accepted the player but it didn't register in time. It may "
            "still appear shortly — refresh and check again."
        )

    # 4) Configure the new player: expose to HA + friendly icon (one save), then Smart
    #    Crossfade separately (it's unavailable on low-memory servers; don't let that
    #    failure undo the rest).
    _LOGGER.info("teds MA auto-create: configuring player %s (expose to HA + icon)", entity_id)
    await cmd(
        "config/players/save",
        player_id=entity_id,
        values={_CONF_EXPOSE_TO_HA: True, _CONF_ICON: _PLAYER_ICON},
    )
    try:
        await cmd(
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

    _LOGGER.info("teds MA auto-create: done for %s", entity_id)
    return {"player_id": entity_id}
