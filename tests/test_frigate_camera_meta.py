"""Unit tests for ``frigate_camera_meta`` (pure unique_id parsing).

Home Assistant isn't installed in CI, so we stub the few ``homeassistant`` modules
``frigate.py`` imports at module load, then drive it with a fake entity registry.
"""

import importlib.util
import pathlib
import sys
import types


def _install_ha_stubs() -> None:
    if "homeassistant" in sys.modules:
        return
    ha = types.ModuleType("homeassistant")
    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = object
    core.callback = lambda fn: fn
    helpers = types.ModuleType("homeassistant.helpers")
    for name in ("area_registry", "device_registry", "entity_registry"):
        sub = types.ModuleType(f"homeassistant.helpers.{name}")
        setattr(helpers, name, sub)
        sys.modules[f"homeassistant.helpers.{name}"] = sub
    ha.core = core
    ha.helpers = helpers
    sys.modules["homeassistant"] = ha
    sys.modules["homeassistant.core"] = core
    sys.modules["homeassistant.helpers"] = helpers


def _load_frigate():
    _install_ha_stubs()
    mod_path = (
        pathlib.Path(__file__).resolve().parents[1]
        / "custom_components"
        / "teds_dashboard_system"
        / "frigate.py"
    )
    spec = importlib.util.spec_from_file_location("frigate", mod_path)
    frigate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(frigate)
    return frigate


class _Entity:
    def __init__(self, entity_id, unique_id, platform="frigate", domain="camera", disabled=False):
        self.entity_id = entity_id
        self.unique_id = unique_id
        self.platform = platform
        self.domain = domain
        self.disabled = disabled


class _Registry:
    def __init__(self, entities):
        self.entities = {e.entity_id: e for e in entities}


class _Hass:
    def __init__(self, frigate_data=None):
        # Mirrors hass.data["frigate"][entry_id]["config"]["mqtt"]["client_id"].
        self.data = {"frigate": frigate_data} if frigate_data is not None else {}


def _meta_for(entities, frigate_data=None):
    frigate = _load_frigate()
    frigate.er.async_get = lambda _hass: _Registry(entities)
    return frigate.frigate_camera_meta(_Hass(frigate_data))


_DATA = {"an-entry": {"config": {"mqtt": {"client_id": "frigate"}}}}


def test_parses_entry_and_camera_name():
    meta = _meta_for(
        [_Entity("camera.front_yard", "an-entry:camera:front_yard")], _DATA
    )
    assert meta == {
        "camera.front_yard": {"instance_id": "frigate", "camera_name": "front_yard"}
    }


def test_camera_name_with_underscores_kept_intact():
    meta = _meta_for(
        [_Entity("camera.front_door_package", "an-entry:camera:front_door_package")], _DATA
    )
    assert meta["camera.front_door_package"]["camera_name"] == "front_door_package"


def test_instance_id_is_mqtt_client_id_not_entry_id():
    # The proxy expects Frigate's MQTT client_id, never the HA config-entry id.
    data = {"e1": {"config": {"mqtt": {"client_id": "myfrigate"}}}}
    meta = _meta_for([_Entity("camera.cam", "e1:camera:cam")], data)
    assert meta["camera.cam"]["instance_id"] == "myfrigate"


def test_instance_id_empty_when_client_id_unavailable():
    # No hass.data for the entry -> empty (card falls back to the no-instance path).
    meta = _meta_for([_Entity("camera.cam", "e1:camera:cam")], {})
    assert meta["camera.cam"]["instance_id"] == ""


def test_skips_non_frigate_and_non_camera_and_disabled():
    entities = [
        _Entity("camera.unifi", "xyz", platform="unifiprotect"),
        _Entity("switch.front_yard_detect", "an-entry:switch:front_yard:detect", domain="switch"),
        _Entity("camera.off", "an-entry:camera:off", disabled=True),
        _Entity("camera.keep", "an-entry:camera:keep"),
    ]
    assert list(_meta_for(entities, _DATA)) == ["camera.keep"]


def test_skips_malformed_unique_id():
    entities = [
        _Entity("camera.bad", "no-colon-camera-marker"),
        _Entity("camera.empty_instance", ":camera:foo"),
        _Entity("camera.good", "an-entry:camera:good"),
    ]
    assert list(_meta_for(entities, _DATA)) == ["camera.good"]
