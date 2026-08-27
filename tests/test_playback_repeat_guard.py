"""Tests for the media_player.repeat_set feature-bit guard in the playback engine."""

from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import sys
import types
from types import SimpleNamespace


def _install_stubs() -> dict:
    core = types.ModuleType("homeassistant.core")
    core.callback = lambda function: function
    helpers = types.ModuleType("homeassistant.helpers")
    event = types.ModuleType("homeassistant.helpers.event")
    event.async_call_later = lambda *args, **kwargs: (lambda: None)
    network = types.ModuleType("homeassistant.helpers.network")
    network.NoURLAvailableError = Exception
    network.get_url = lambda *args, **kwargs: "http://localhost:8123"
    helpers.event = event
    helpers.network = network
    homeassistant = types.ModuleType("homeassistant")
    homeassistant.core = core
    homeassistant.helpers = helpers
    sys.modules.update(
        {
            "homeassistant": homeassistant,
            "homeassistant.core": core,
            "homeassistant.helpers": helpers,
            "homeassistant.helpers.event": event,
            "homeassistant.helpers.network": network,
        }
    )

    package = types.ModuleType("custom_components.teds_dashboard_system")
    package.__path__ = []
    saved = {}
    for module_name in tuple(sys.modules):
        if module_name.startswith("custom_components.teds_dashboard_system"):
            saved[module_name] = sys.modules.pop(module_name)
    sys.modules["custom_components.teds_dashboard_system"] = package
    const = types.ModuleType("custom_components.teds_dashboard_system.const")
    const.DEFAULT_SOUND = "default"
    sys.modules[const.__name__] = const
    return saved


def _load_engine():
    saved = _install_stubs()
    path = (
        pathlib.Path(__file__).resolve().parents[1]
        / "custom_components"
        / "teds_dashboard_system"
        / "playback.py"
    )
    name = "custom_components.teds_dashboard_system.playback"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    # Put back whatever other test modules registered; leaving our stubs in place
    # breaks their imports and their ``mock.patch`` targets.
    for module_name in tuple(sys.modules):
        if module_name.startswith(
            ("custom_components.teds_dashboard_system", "homeassistant")
        ):
            sys.modules.pop(module_name, None)
    sys.modules.update(saved)
    return module


playback = _load_engine()


class _Services:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []

    async def async_call(self, domain, service, data, blocking=False):
        self.calls.append((domain, service, data))


class _States:
    def __init__(self, features) -> None:
        self._features = features

    def get(self, entity_id):
        if entity_id not in self._features:
            return None
        return SimpleNamespace(
            state="playing",
            attributes={"supported_features": self._features[entity_id]},
        )


def _engine(features):
    services = _Services()
    hass = SimpleNamespace(services=services, states=_States(features))
    engine = playback.PlaybackEngine(SimpleNamespace(hass=hass))
    return engine, services


def _stop(features, play):
    engine, services = _engine(features)
    asyncio.run(engine._stop_and_restore(play))
    return [(service, data) for _domain, service, data in services.calls]


def _play(entity_id="media_player.panel"):
    return {
        "mp": entity_id,
        "snapshot": {"volume": 0.4, "content_id": "http://x/song.mp3", "content_type": "music"},
    }


def test_repeat_set_is_skipped_when_the_player_lacks_the_feature() -> None:
    """BrowserMod players don't implement repeat_set; calling it logs a HA error."""
    # PLAY_MEDIA | VOLUME_SET | STOP — everything except REPEAT_SET.
    calls = _stop({"media_player.panel": 512 | 4 | 4096}, _play())
    services = [service for service, _data in calls]

    assert "repeat_set" not in services
    # The rest of the restore sequence is untouched.
    assert services == ["media_stop", "volume_set", "play_media"]


def test_repeat_set_is_called_when_the_player_supports_it() -> None:
    calls = _stop({"media_player.panel": 512 | 4 | 4096 | playback.REPEAT_SET}, _play())
    services = [service for service, _data in calls]

    assert services == ["repeat_set", "media_stop", "volume_set", "play_media"]
    assert calls[0][1] == {"entity_id": "media_player.panel", "repeat": "off"}


def test_repeat_set_is_skipped_for_an_unknown_or_missing_entity() -> None:
    calls = _stop({}, _play("media_player.gone"))
    services = [service for service, _data in calls]

    assert "repeat_set" not in services
    assert services == ["media_stop", "volume_set", "play_media"]


def test_announcement_play_dicts_without_a_snapshot_still_stop_cleanly() -> None:
    """Announcement plays carry only mp/announce, so the guard must not need a snapshot."""
    calls = _stop({"media_player.panel": 512}, {"mp": "media_player.panel", "announce": False})
    services = [service for service, _data in calls]

    assert services == ["media_stop"]


def test_feature_bits_match_home_assistant() -> None:
    # MediaPlayerEntityFeature.REPEAT_SET / MEDIA_ANNOUNCE.
    assert playback.REPEAT_SET == 262144
    assert playback.MEDIA_ANNOUNCE == 1048576


def test_supports_reads_live_state_and_tolerates_bad_values() -> None:
    engine, _services = _engine({"media_player.panel": "not-a-number"})

    assert engine._supports("media_player.panel", playback.REPEAT_SET) is False
    assert engine._supports("media_player.missing", playback.REPEAT_SET) is False
