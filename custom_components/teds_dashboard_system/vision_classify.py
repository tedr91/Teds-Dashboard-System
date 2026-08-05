"""Pure detector-classification logic for the Vision engine.

Kept free of Home Assistant imports so the keyword/device-class rules can be unit-tested
directly. ``vision.py`` resolves the registry/device-class and delegates here.
"""

from __future__ import annotations

# Detection event types matched by object-id keywords (Frigate/Reolink/UniFi conventions).
DETECTOR_KEYWORDS: dict[str, tuple[str, ...]] = {
    "person": ("person", "human", "people"),
    "animal": ("animal", "pet", "dog", "cat"),
    "car": ("vehicle", "car", "truck"),
    "package": ("package", "parcel", "delivery"),
}
# device_class values (or object-id tokens) that mean a generic motion/occupancy detector.
MOTION_CLASSES = {"motion", "moving", "occupancy", "presence"}
# device_class values that are UNAMBIGUOUSLY a motion sensor (never an object detector).
STRICT_MOTION_CLASSES = {"motion", "moving"}


def strip_camera_prefix(object_id: str, cam_object_id: str) -> str:
    """Drop the camera's own object_id prefix from a sensor's object_id.

    A camera named after a detector keyword (e.g. ``front_door_package``) would otherwise
    taint every one of its sensors — ``front_door_package_motion`` must not read as a
    'package'. Only the portion after ``<camera>_`` is used for keyword matching.
    """
    object_id = object_id.lower()
    cam = (cam_object_id or "").lower()
    if cam and object_id.startswith(f"{cam}_"):
        return object_id[len(cam) + 1:]
    return object_id


def classify_detector_type(
    object_id: str, cam_object_id: str, device_class: str | None
) -> str | None:
    """Classify a camera binary_sensor into a detection type (or None).

    Order matters:
      1. A real motion sensor (device_class motion/moving) is always ``motion`` — it can
         never be misread as an object detector.
      2. Object keywords (person/animal/car/package) matched on the camera-stripped
         object_id win next (so a camera name can't create false hits).
      3. Otherwise a generic occupancy/presence/motion signal falls back to ``motion``.
    """
    dc = (device_class or "").lower()
    tail = strip_camera_prefix(object_id, cam_object_id)
    if dc in STRICT_MOTION_CLASSES:
        return "motion"
    for etype, words in DETECTOR_KEYWORDS.items():
        if any(w in tail for w in words):
            return etype
    if dc in MOTION_CLASSES or "motion" in tail or "movement" in tail:
        return "motion"
    return None
