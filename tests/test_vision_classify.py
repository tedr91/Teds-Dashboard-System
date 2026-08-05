"""Unit tests for the camera detector classifier (pure, no Home Assistant required).

Loads ``vision_classify.py`` directly so the package's HA-importing ``__init__`` isn't run.
"""

import importlib.util
import pathlib

_MOD = (
    pathlib.Path(__file__).resolve().parents[1]
    / "custom_components"
    / "teds_dashboard_system"
    / "vision_classify.py"
)
_spec = importlib.util.spec_from_file_location("vision_classify", _MOD)
vision_classify = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vision_classify)
classify = vision_classify.classify_detector_type


def test_motion_sensor_on_keyword_named_camera_is_motion():
    # front_door_package_motion (device_class motion) must NOT read as 'package'.
    assert classify("front_door_package_motion", "front_door_package", "motion") == "motion"


def test_package_occupancy_on_keyword_named_camera():
    assert classify(
        "front_door_package_package_occupancy", "front_door_package", "occupancy"
    ) == "package"


def test_person_occupancy_on_keyword_named_camera():
    assert classify(
        "front_door_package_person_occupancy", "front_door_package", "occupancy"
    ) == "person"


def test_all_occupancy_falls_back_to_motion():
    assert classify(
        "front_door_package_all_occupancy", "front_door_package", "occupancy"
    ) == "motion"


def test_package_occupancy_on_front_door_camera():
    # Different device: camera is 'front_door', object is 'package'.
    assert classify("front_door_package_occupancy", "front_door", "occupancy") == "package"


def test_motion_without_device_class_from_name():
    assert classify("driveway_motion", "driveway", None) == "motion"


def test_unrelated_sensor_is_none():
    assert classify("front_door_battery", "front_door", "battery") is None
