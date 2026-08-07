"""Camera Vision Analysis engine for Ted's Dashboard System.

Watches the binary_sensors a camera exposes (motion / person / animal / car),
captures a few stills across the event window, and asks Home Assistant's native
``ai_task`` building block (OpenAI, Ollama, …) to classify the event into a
severity plus a short and long summary. Results are stored on the manager and
served to the Vision timeline card. No third-party vision integration required.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import os
import time
import uuid

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util

from .const import DOMAIN, EVENT_NAVIGATE, EVENT_SETTINGS, MEDIA_FOLDER_NAME, VISION_SEVERITIES
from .frigate import (
    async_frigate_alert_pre_capture,
    async_frigate_event_meta,
    frigate_camera_entity,
    is_frigate_camera,
)
from .vision_classify import (
    DETECTOR_KEYWORDS as _DETECTOR_KEYWORDS,
    classify_detector_type,
)

_LOGGER = logging.getLogger(__name__)

# Severity assigned to a Frigate-native (no-AI) event, by detected object type.
_FRIGATE_NATIVE_SEVERITY = {
    "person": "suspicious",
    "package": "suspicious",
    "car": "harmless",
    "animal": "harmless",
    "motion": "harmless",
}
# vision severity -> notification severity used for the on-trigger Teds notification.
_SEVERITY_TO_NOTIF = {
    "critical": "danger",
    "suspicious": "warning",
    "harmless": "info",
    "unknown": "info",
}

_ANALYSIS_INSTRUCTIONS = (
    "You are a home security camera analyst reviewing a SHORT VIDEO CLIP, given as "
    "sequential frames in time order, from the '{camera_name}' camera{area_phrase}. A "
    "motion or occupancy sensor reported a possible '{event_type}', but that hint is "
    "UNVERIFIED and often wrong (shadows, sunlight, rain, a static object, or even the "
    "camera's own name can trigger it). Judge ONLY by what the frames actually show.\n"
    "{object_context}"
    "Report what CHANGES across the frames: what enters or leaves the scene, the "
    "direction it travels, what it does, and the state at the end. If the frames show "
    "the same scene at the start and the end with nothing moving, say so explicitly "
    "rather than narrating an arrival or departure that is not visible. A good "
    "description states a sequence with a beginning and an end; a poor one just names a "
    "static object with no verb of motion.\n"
    "- Do NOT report a person, vehicle, animal, or package unless it is actually visible "
    "and moving/acting in the frames — the sensor hint or camera name is not evidence.\n"
    "- Do NOT invent a story or assume a motive; report only what is actually visible in the frames.\n"
    "- Do NOT report a static object that never moves, even if it is a person, vehicle, animal, or package.\n"
    "- false_alarm: this flag means 'NOTHING HAPPENED', not 'the sensor hint was "
    "wrong'. Set it TRUE only when the clip shows no real activity at all: an empty "
    "scene, only shadows / light changes / weather, or a static object that never "
    "moves. Set it FALSE whenever any person, vehicle, animal, or package actually "
    "moves, arrives, departs, or acts — even if that differs from the reported "
    "'{event_type}'. A clip where a car arrives is NOT a false alarm just because "
    "the hint said 'person'. Your own short_summary must agree with this flag: if "
    "the summary names an actor performing an action, false_alarm MUST be FALSE.\n"
    "- severity: 'critical' for an active threat, break-in, or emergency; 'suspicious' "
    "for unexpected or concerning activity worth a human review; 'harmless' for routine "
    "or expected activity (residents, pets, deliveries, passing cars); 'unknown' only if "
    "the frames are too unclear to judge.\n"
    "- short_summary: one concise sentence naming the main action that occurs.\n"
    "- long_summary: a play-by-play of the sequence across the clip, in order — arrival, "
    "movement and path, actions taken, and how it ends."
)

# ai_task provider platforms (entity-registry platform) in preference order for smart
# defaults, and those known to accept VIDEO attachments (image support has no such flag).
_AI_PROVIDER_PRIORITY = ("ollama", "openai_conversation", "google_generative_ai")
# Integration domains whose ai_task entities accept a VIDEO attachment. This is the entity
# registry's `platform` value, i.e. the integration's manifest domain — Gemini's is
# "google_generative_ai_conversation". Every other provider (ollama, openai_conversation)
# raises HomeAssistantError on a non-image attachment, so they must be sent stills.
VIDEO_CAPABLE_PLATFORMS = frozenset({
    "google_generative_ai_conversation",
    "google_generative_ai",  # tolerated alias
})

# Pass 1 samples the LIVE camera at 1 fps for this long, starting the moment the review
# becomes an alert — it must never wait for Frigate's clip, which only exists at review end.
QUICK_WINDOW_SECONDS = 10

_LABELLED_SNAPSHOT_NOTE = (
    "The FIRST image is a labelled reference frame from Frigate showing the tracked "
    "object outlined by a bounding box with its label. Use it to identify the subject, "
    "then describe that subject across the frames.\n"
)



def discover_camera_detectors(hass: HomeAssistant, camera_id: str) -> dict[str, list[str]]:
    """Return the detection binary_sensors on a camera's device, grouped by event
    type: {"motion": [...], "person": [...], "animal": [...], "car": [...], ...}.

    Only types with at least one matched sensor are returned, so the settings UI can
    present exactly the options a given camera supports.
    """
    ent_reg = er.async_get(hass)
    cam_entry = ent_reg.async_get(camera_id)
    if cam_entry is None or cam_entry.device_id is None:
        return {}
    dev_reg = dr.async_get(hass)
    if dev_reg.async_get(cam_entry.device_id) is None:
        return {}
    cam_object_id = camera_id.split(".", 1)[-1]
    result: dict[str, list[str]] = {}
    for ent in er.async_entries_for_device(ent_reg, cam_entry.device_id, include_disabled_entities=False):
        if ent.domain != "binary_sensor":
            continue
        etype = _classify_detector(hass, ent, cam_object_id)
        if etype:
            result.setdefault(etype, []).append(ent.entity_id)
    return result


def _classify_detector(hass: HomeAssistant, ent: er.RegistryEntry, cam_object_id: str) -> str | None:
    """Map a camera binary_sensor to a detection type, ignoring the camera's own name."""
    dev_class = ent.device_class or ent.original_device_class
    if not dev_class:
        state = hass.states.get(ent.entity_id)
        if state is not None:
            dev_class = state.attributes.get("device_class")
    object_id = ent.entity_id.split(".", 1)[-1]
    return classify_detector_type(object_id, cam_object_id, dev_class)


def frigate_native_camera(hass: HomeAssistant, settings: dict, camera_id: str) -> bool:
    """Whether a camera is driven by Frigate's native review alerts (not its binary_sensors):
    a Frigate camera + the native-detection setting on + MQTT loaded. Single source of truth
    shared by the Vision engine and the settings UI so the two can never drift."""
    return (
        bool(settings.get("frigate_native_detection", True))
        and "mqtt" in hass.config.components
        and is_frigate_camera(hass, camera_id)
    )


def _default_trigger_actions() -> list[dict]:
    """The default action set for a new trigger — mirrors the settings UI's ``_addTrigger``."""
    return [
        {"type": "live_feed", "enabled": True, "areas": []},
        {"type": "toast", "enabled": True, "areas": []},
        {"type": "push", "enabled": False, "services": []},
        {"type": "custom", "enabled": False, "items": []},
    ]


def _frigate_label_type(label: str) -> str:
    """Map a Frigate object label (person/car/dog/...) to a Vision detector type."""
    low = label.lower()
    for etype, words in _DETECTOR_KEYWORDS.items():
        if low in words or any(w in low for w in words):
            return etype
    return "motion"


def ai_task_entities(hass: HomeAssistant) -> list[dict]:
    """List ai_task entities with whether each supports image attachments."""
    support_bit = _attachments_feature_bit()
    out: list[dict] = []
    for state in hass.states.async_all("ai_task"):
        feats = state.attributes.get("supported_features") or 0
        out.append(
            {
                "entity_id": state.entity_id,
                "name": state.attributes.get("friendly_name") or state.name,
                "supports_attachments": bool(support_bit and (int(feats) & support_bit)),
            }
        )
    return out


def preferred_ai_task_entity(hass: HomeAssistant) -> str | None:
    """The user's preferred ai_task 'generate data' entity, if one is set."""
    try:
        from homeassistant.components.ai_task.const import DATA_PREFERENCES  # noqa: PLC0415

        prefs = hass.data.get(DATA_PREFERENCES)
        return getattr(prefs, "gen_data_entity_id", None) if prefs else None
    except Exception:  # noqa: BLE001 - best-effort; ai_task may be absent
        return None


def _attachments_feature_bit() -> int:
    try:
        from homeassistant.components.ai_task import AITaskEntityFeature  # noqa: PLC0415

        return int(AITaskEntityFeature.SUPPORT_ATTACHMENTS)
    except Exception:  # noqa: BLE001
        return 0


def _ai_task_platform(hass: HomeAssistant, entity_id: str) -> str | None:
    """The integration platform that provides an ai_task entity (e.g. 'ollama')."""
    entry = er.async_get(hass).async_get(entity_id)
    return entry.platform if entry else None


def _image_ai_task_entities(hass: HomeAssistant) -> list[str]:
    """ai_task entity ids that support (image) attachments."""
    bit = _attachments_feature_bit()
    out: list[str] = []
    for state in hass.states.async_all("ai_task"):
        feats = int(state.attributes.get("supported_features") or 0)
        if bit and (feats & bit):
            out.append(state.entity_id)
    return out


def _pick_by_provider(hass: HomeAssistant, entity_ids: list[str]) -> str | None:
    """Pick by provider priority Ollama > OpenAI > Gemini, else the first found."""
    for platform in _AI_PROVIDER_PRIORITY:
        for eid in entity_ids:
            if _ai_task_platform(hass, eid) == platform:
                return eid
    return entity_ids[0] if entity_ids else None


def preferred_image_ai_task_entity(hass: HomeAssistant) -> str | None:
    """Smart default for the quick pass: an attachment-capable entity by provider priority."""
    return _pick_by_provider(hass, _image_ai_task_entities(hass)) or preferred_ai_task_entity(hass)


def preferred_video_ai_task_entity(hass: HomeAssistant) -> str | None:
    """Smart default for the detailed pass: same priority over VIDEO-capable providers."""
    vids = [e for e in _image_ai_task_entities(hass)
            if _ai_task_platform(hass, e) in VIDEO_CAPABLE_PLATFORMS]
    return _pick_by_provider(hass, vids)


def _entity_supports_video(hass: HomeAssistant, entity_id: str | None) -> bool:
    return bool(entity_id) and _ai_task_platform(hass, entity_id) in VIDEO_CAPABLE_PLATFORMS


def _as_bool(value: object) -> bool:
    """Coerce an ai_task structured value to bool — providers may return the string 'false',
    and bool('false') is True, which would flag every event as a false alarm."""
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "y")
    return bool(value)



class VisionEngine:
    """Owns detector subscriptions, capture, AI analysis, and event storage."""

    def __init__(self, hass: HomeAssistant, manager) -> None:
        self.hass = hass
        self.manager = manager
        self.cache_dir: str | None = None  # served /teds_dashboard_system/vision_cache
        self._unsub_state = None
        self._unsub_settings = None
        self._unsub_events = None  # Frigate MQTT events subscription (native cameras)
        self._watch: dict[str, tuple[str, str]] = {}  # sensor eid -> (camera_id, event_type)
        self._cooldowns: dict[str, float] = {}        # camera_id -> monotonic last trigger
        self._native_cams: set[str] = set()           # Frigate cameras driven by reviews, not sensors
        # frigate review id -> {id, discard, event_id, stop, superseded, passes}
        self._frigate_pending: dict[str, dict] = {}
        self._frigate_skip: set[str] = set()          # review ids we chose to skip (cooldown)
        self._synth_logged: set[str] = set()          # cams we've logged a synthesized catch-all for

    @callback
    def async_setup(self, entry: ConfigEntry) -> None:
        self._rebuild_listeners()
        self._unsub_settings = self.hass.bus.async_listen(EVENT_SETTINGS, self._on_settings)
        entry.async_on_unload(self._shutdown)

    @callback
    def _shutdown(self) -> None:
        if self._unsub_state:
            self._unsub_state()
            self._unsub_state = None
        if self._unsub_settings:
            self._unsub_settings()
            self._unsub_settings = None
        if self._unsub_events:
            self._unsub_events()
            self._unsub_events = None
        self._frigate_pending.clear()
        self._frigate_skip.clear()

    def _settings(self) -> dict:
        return self.manager.effective_settings()

    @callback
    def _on_settings(self, _event: Event) -> None:
        self._rebuild_listeners()

    @callback
    def _rebuild_listeners(self) -> None:
        if self._unsub_state:
            self._unsub_state()
            self._unsub_state = None
        self._watch = {}
        s = self._settings()
        if not s.get("vision_enabled"):
            return
        cams = s.get("vision_cameras") or {}
        # A Frigate camera with native detection + MQTT is driven by Frigate's own tracked
        # events (real clip/thumbnail, no local capture) rather than its binary_sensors.
        native_cams: set[str] = set()
        for cam_id, cfg in cams.items():
            if not (cfg or {}).get("enabled"):
                continue
            triggers = cfg.get("triggers") or []
            if not triggers:
                continue
            native = frigate_native_camera(self.hass, s, cam_id)
            _LOGGER.debug(
                "Ted's Vision: %s -> %s",
                cam_id, "Frigate alert-driven" if native else "binary_sensor",
            )
            if native:
                native_cams.add(cam_id)
                continue
            detectors = discover_camera_detectors(self.hass, cam_id)
            for t_idx, trig in enumerate(triggers):
                for eid in detectors.get((trig or {}).get("type"), []):
                    self._watch.setdefault(eid, []).append((cam_id, t_idx))
        self._native_cams = native_cams
        if self._watch:
            self._unsub_state = async_track_state_change_event(
                self.hass, list(self._watch), self._on_state
            )
        self.hass.async_create_task(self._sync_frigate_reviews(bool(native_cams)))

    @callback
    def _on_state(self, event: Event) -> None:
        """Detector went active (off -> on) — kick off analysis after a per-trigger cooldown."""
        new = event.data.get("new_state")
        old = event.data.get("old_state")
        if new is None or new.state != "on":
            return
        if old is not None and old.state == "on":
            return
        pairs = self._watch.get(event.data.get("entity_id"))
        if not pairs:
            return
        cams = self._settings().get("vision_cameras") or {}
        now = time.monotonic()
        for cam_id, t_idx in pairs:
            triggers = (cams.get(cam_id) or {}).get("triggers") or []
            if t_idx >= len(triggers):
                continue
            trig = triggers[t_idx] or {}
            cooldown = float(trig.get("cooldown_seconds", 60) or 0)
            key = f"{cam_id}#{t_idx}"
            if cooldown and (now - self._cooldowns.get(key, 0.0)) < cooldown:
                continue
            self._cooldowns[key] = now
            self.hass.async_create_task(
                self._handle_event(
                    cam_id,
                    (trig.get("type") or "motion"),
                    trigger_entity=event.data.get("entity_id"),
                    trigger=trig,
                )
            )

    # ── Frigate alert-driven path (tight integration) ───────
    def handles_camera(self, camera_id: str, objects: list[str] | None = None) -> bool:
        """True when the Vision engine will drive this camera's alert, so the notification
        bridge should not also notify. Native cameras always have a catch-all (so this is
        true for any object set); if a specific object set would somehow fail to match,
        return False so the bridge picks it up rather than the alert being dropped."""
        if camera_id not in self._native_cams:
            return False
        _idx, _display, trig = self._match_review_trigger(camera_id, objects or [])
        return trig is not None

    async def _sync_frigate_reviews(self, want: bool) -> None:
        """Subscribe/unsubscribe to Frigate's MQTT review stream for native cameras."""
        if want and self._unsub_events is None and "mqtt" in self.hass.config.components:
            from homeassistant.components import mqtt  # noqa: PLC0415

            from .frigate import frigate_topic_prefix  # noqa: PLC0415

            topic = f"{frigate_topic_prefix(self.hass)}/reviews"
            try:
                self._unsub_events = await mqtt.async_subscribe(
                    self.hass, topic, self._on_frigate_review, encoding=None
                )
            except Exception:  # noqa: BLE001 - MQTT not ready; retried on the next rebuild
                self._unsub_events = None
        elif not want and self._unsub_events is not None:
            self._unsub_events()
            self._unsub_events = None

    @callback
    def _on_frigate_review(self, msg) -> None:
        """Drive Vision from Frigate reviews, acting ONLY on alerts (not detections): create
        a provisional entry + fire the trigger's actions the moment a review becomes an
        alert, then refine that entry with the finished clip + AI summary when it ends."""
        try:
            data = json.loads(msg.payload)
        except (ValueError, TypeError):
            return
        phase = data.get("type")
        if phase not in ("new", "update", "end"):
            return
        after = data.get("after") or {}
        review_id, camera = after.get("id"), after.get("camera")
        if not review_id or not camera:
            return
        cam_id = frigate_camera_entity(self.hass, str(camera))
        if not cam_id or cam_id not in self._native_cams:
            return
        review_id = str(review_id)
        payload = after.get("data") or {}
        objects = [str(o) for o in (payload.get("objects") or [])]
        zones = [str(z) for z in (payload.get("zones") or [])]
        detections = [str(d) for d in (payload.get("detections") or [])]
        # Frigate event IDs are `<start_epoch>.<microseconds>-<random>`, so the smallest
        # by plain string order is the earliest-STARTED detection — the object that
        # actually opened this review (the detections list itself is not time-ordered).
        event_id = min(detections) if detections else review_id
        is_alert = str(after.get("severity") or "").lower() == "alert"
        t_idx, etype, trig = self._match_review_trigger(cam_id, objects)
        if trig is None:
            return
        if phase in ("new", "update"):
            if not is_alert:
                return  # detection-level review — TDS only acts on alerts
            if review_id in self._frigate_pending or review_id in self._frigate_skip:
                return  # already handled this review
            key = f"{cam_id}#{t_idx}"
            now = time.monotonic()
            cooldown = int(trig.get("cooldown_seconds") or 0)
            if cooldown and (now - self._cooldowns.get(key, 0)) < cooldown:
                self._frigate_skip.add(review_id)
                return
            self._cooldowns[key] = now
            self.hass.async_create_task(
                self._frigate_review_new(
                    cam_id, etype, review_id, event_id, trig,
                    objects=objects, zones=zones,
                )
            )
        else:
            self.hass.async_create_task(
                self._frigate_review_end(
                    cam_id, etype, review_id, event_id, is_alert,
                    objects=objects, zones=zones,
                )
            )

    def _match_review_trigger(
        self, cam_id: str, objects: list[str]
    ) -> tuple[int | None, str | None, dict | None]:
        """Match a review's objects to a configured trigger: an exact object-type trigger,
        else a 'motion' trigger as a catch-all. Returns (index, display_type, trigger)."""
        triggers = ((self._settings().get("vision_cameras") or {}).get(cam_id) or {}).get("triggers") or []
        mapped = [_frigate_label_type(o) for o in objects]
        priority = ("person", "package", "car", "animal", "motion")
        display = next((t for t in priority if t in mapped), "motion")
        for i, trig in enumerate(triggers):
            if (trig or {}).get("type") in mapped:
                return i, (trig or {}).get("type"), trig
        for i, trig in enumerate(triggers):
            if (trig or {}).get("type") == "motion":
                return i, display, (trig or {})
        # Frigate-native cameras always have a catch-all: Frigate already decided this review
        # is alert-worthy, so a missing 'motion' trigger must not silently drop it. Synthesize
        # the default catch-all with a stable index past the real triggers (keeps the
        # `f"{cam_id}#{t_idx}"` cooldown key deterministic per camera).
        if cam_id in self._native_cams:
            if cam_id not in self._synth_logged:
                self._synth_logged.add(cam_id)
                _LOGGER.info(
                    "Ted's Vision: %s has no 'Any Frigate alert' trigger; using the built-in "
                    "catch-all so Frigate alerts aren't dropped", cam_id,
                )
            return len(triggers), display, {
                "type": "motion",
                "cooldown_seconds": 60,
                "discard_severities": [],
                "actions": _default_trigger_actions(),
            }
        return None, None, None

    @staticmethod
    def _object_context(objects: list[str], zones: list[str]) -> str:
        """Corroborated ground truth from Frigate's object detector (empty when there's
        nothing to report) — distinct from the UNVERIFIED motion-sensor hint. Names the
        tracked object(s) and any zones so the model describes THAT object, not whatever is
        most visually salient."""
        if not objects:
            return ""
        objs = ", ".join(f"`{o}`" for o in objects)
        zone_part = f", which entered the zone(s): {', '.join(zones)}" if zones else ""
        return (
            f"Frigate's object detector tracked {objs}{zone_part} in this clip. Describe "
            "THAT object as the subject. If other objects are visible, mention them only "
            "as context — the tracked object above is the subject. Because a real object "
            "was independently tracked, this clip is NOT a false alarm: set false_alarm "
            "to FALSE unless the tracked object is completely absent from every frame.\n"
        )

    async def _frigate_review_new(
        self, camera_id: str, event_type: str, review_id: str, event_id: str, trigger: dict,
        objects: list[str] | None = None, zones: list[str] | None = None,
    ) -> None:
        """Create the provisional Vision event from the alert's thumbnail, fire the trigger's
        actions immediately — the timely moment, before the clip is finalized — and kick off
        PASS 1 against the LIVE camera right now."""
        tds_id = uuid.uuid4().hex
        s = self._settings()
        rec = {
            "id": tds_id,
            "discard": (trigger or {}).get("discard_severities") or [],
            # DEFECT E: pin the event that RAISED this alert. `detections` grows during a
            # review, so min(detections) at review end can resolve to a different (earlier-
            # started) object and hand pass 2 the wrong clip + wrong labelled snapshot.
            "event_id": event_id,
            "stop": asyncio.Event(),   # set at review end: cut pass 1's sampling short
            "superseded": False,       # set once pass 2 has published; pass 1 then stays quiet
            "passes": [],              # shared debug capture, appended by BOTH passes
        }
        self._frigate_pending[review_id] = rec
        started = dt_util.utcnow()
        area_id, area_name, cam_name = self._camera_context(camera_id)
        event = {
            "id": tds_id,
            "camera_id": camera_id,
            "camera_name": cam_name,
            "area": area_id,
            "area_name": area_name,
            "event_type": event_type,
            "created": started.isoformat(),
            "ts_start": started.isoformat(),
            "ts_end": started.isoformat(),
            "severity": _FRIGATE_NATIVE_SEVERITY.get(event_type, "unknown"),
            "false_alarm": False,
            "short_summary": f"{event_type.replace('_', ' ').title()} detected by Frigate.",
            "long_summary": "",
            "thumbnail_url": f"/api/frigate/notifications/{event_id}/thumbnail.jpg",
            "clip_url": None,  # the clip isn't finalized until the review ends
            "reviewed": False,
            "trigger_entity": None,
            "frigate_event_id": event_id,
            "frigate_review_id": review_id,
            "status": "in_progress",
            "_files": {},
        }
        dropped = await self.manager.add_vision_event(event)
        if dropped:
            await self._cleanup_files(dropped)
        # PASS 1 starts NOW, concurrently with the actions — this is the whole point of it.
        if s.get("vision_two_pass", True):
            self.hass.async_create_task(
                self._frigate_quick_pass(
                    camera_id, cam_name, area_name, event_type, event_id, rec, s,
                    self._object_context(objects or [], zones or []),
                )
            )
        await self._run_actions(event, trigger, area_id)

    async def _frigate_quick_pass(
        self, camera_id: str, cam_name: str, area_name: str | None, event_type: str,
        event_id: str, rec: dict, settings: dict, object_context: str,
    ) -> None:
        """PASS 1 — runs the moment the review becomes an alert, against the LIVE camera.
        Frigate's clip does not exist yet, so this can never use it. Samples ~1 fps for
        QUICK_WINDOW_SECONDS (cut short when the review ends), analyses, then publishes only
        if pass 2 hasn't already landed."""
        media = self._media_source_dir()
        if not media:
            return
        tmp_rel = f"{MEDIA_FOLDER_NAME}/frigate/{event_id}-quick"
        tmp_dir = os.path.join(media[1], tmp_rel)
        entity = settings.get("vision_ai_task_entity") or preferred_image_ai_task_entity(self.hass)
        capture = rec["passes"] if settings.get("vision_debug_passes") else None
        try:
            attach = await self._sample_live_frames(
                camera_id, media, tmp_rel, tmp_dir, rec["stop"],
            )
            snap = await self._frigate_snapshot_attachment(event_id, media, tmp_rel, tmp_dir)
            if snap is not None:
                attach = [snap, *attach]
                object_context = (object_context or "") + _LABELLED_SNAPSHOT_NOTE
            if not attach:
                return
            result = await self._run_pass(
                "quick", camera_id, cam_name, area_name, event_type, attach, entity,
                object_context=object_context, capture=capture,
            )
            # Pass 2 may have finished while we were analysing — it always wins.
            if not result or rec.get("superseded"):
                return
            await self.manager.update_vision_event(
                rec["id"], status="analyzing", **self._analysis_patch(result),
            )
            self._mark_published(capture, "quick")
        except Exception:  # noqa: BLE001 - pass 1 is best-effort; pass 2 still runs
            _LOGGER.exception("Ted's Vision: quick pass failed for %s", camera_id)
        finally:
            await self.hass.async_add_executor_job(_rmtree_quiet, tmp_dir)

    async def _sample_live_frames(
        self, camera_id: str, media: tuple, tmp_rel: str, tmp_dir: str,
        stop: asyncio.Event,
    ) -> list[dict]:
        """Grab one live camera still per second for up to QUICK_WINDOW_SECONDS, stopping
        early when `stop` fires (the review ended). Keeps whatever it already collected, so a
        3-second event still yields 3-4 frames."""
        from homeassistant.components.camera import async_get_image  # noqa: PLC0415

        await self.hass.async_add_executor_job(lambda: os.makedirs(tmp_dir, exist_ok=True))
        out: list[dict] = []
        for i in range(QUICK_WINDOW_SECONDS):
            try:
                image = await async_get_image(self.hass, camera_id, timeout=5)
            except Exception:  # noqa: BLE001 - a dropped frame must not kill the pass
                image = None
            if image is not None and image.content:
                name = f"qw_{i:02d}.jpg"
                await self.hass.async_add_executor_job(
                    _write_bytes, os.path.join(tmp_dir, name), image.content,
                )
                out.append({
                    "media_content_id": f"media-source://media_source/{media[0]}/{tmp_rel}/{name}",
                    "media_content_type": "image/jpeg",
                })
            if stop.is_set():
                break
            try:
                await asyncio.wait_for(stop.wait(), timeout=1.0)
                break            # stop fired during the wait — the review just ended
            except TimeoutError:
                pass             # normal 1-second tick
        return out

    async def _frigate_snapshot_attachment(
        self, event_id: str, media: tuple, tmp_rel: str, tmp_dir: str,
    ) -> dict | None:
        """Frigate's labelled snapshot (bounding box + label burned in). Best-effort — it may
        not exist yet this early in a review, in which case pass 1 runs without it."""
        blob = await self._download_frigate(event_id, "snapshot.jpg")
        if blob is None:
            return None
        await self.hass.async_add_executor_job(lambda: os.makedirs(tmp_dir, exist_ok=True))
        await self.hass.async_add_executor_job(
            _write_bytes, os.path.join(tmp_dir, "snapshot.jpg"), blob,
        )
        return {
            "media_content_id": f"media-source://media_source/{media[0]}/{tmp_rel}/snapshot.jpg",
            "media_content_type": "image/jpeg",
        }


    async def _frigate_review_end(
        self, camera_id: str, event_type: str, review_id: str, event_id: str, is_alert: bool,
        objects: list[str] | None = None, zones: list[str] | None = None,
    ) -> None:
        """Refine the provisional entry with the finished clip + AI summary. If we never
        acted on the alert's onset, create a finished entry now (no late actions)."""
        pending = self._frigate_pending.pop(review_id, None)
        tds_id = pending["id"] if pending else None
        discard_list = pending["discard"] if pending else []
        # DEFECT E: reuse the event pinned when this review became an alert. Recomputing
        # min(detections) here can resolve to a DIFFERENT object — one that started earlier
        # and got absorbed into the review later — which would download that object's clip
        # and its labelled snapshot, so the model would describe the wrong thing entirely.
        # Fall back to the caller's value only when we never saw the review start.
        if pending and pending.get("event_id"):
            event_id = pending["event_id"]
        # Pass 1 (if still sampling) stops here — the event is over, there's nothing left to
        # watch live. It keeps the frames it has and finishes its analysis.
        if pending:
            pending["stop"].set()
        skipped = review_id in self._frigate_skip
        self._frigate_skip.discard(review_id)
        if tds_id is None and (skipped or not is_alert):
            return  # cooldown-suppressed, or the review never became an alert
        s = self._settings()
        # Pass 1 already appended its record to the shared list on the pending record, so
        # pass 2 must append to that SAME list or one of the two would be lost.
        passes: list | None = None
        if s.get("vision_debug_passes"):
            passes = pending["passes"] if pending else []
        area_id, area_name, cam_name = self._camera_context(camera_id)
        clip_url = f"/api/frigate/notifications/{event_id}/clip.mp4"

        if tds_id is not None:
            await self.manager.update_vision_event(tds_id, status="analyzing", clip_url=clip_url)

        object_context = self._object_context(objects or [], zones or [])
        label = "detailed" if s.get("vision_two_pass", True) else "single"
        analysis = await self._analyze_frigate(
            camera_id, cam_name, area_name, event_type, event_id, True, s,
            object_context=object_context, capture=passes, label=label,
        )
        # Pass 2 has landed: pass 1 must not overwrite it if it is somehow still in flight.
        if pending:
            pending["superseded"] = True
        self._mark_published(passes, label)
        # Defensive backstop: the model intermittently flags false_alarm on clips its own
        # summary describes as real activity. When Frigate independently tracked an object,
        # trust the detector over the model and clear the flag.
        if analysis and analysis.get("false_alarm") and objects:
            _LOGGER.info(
                "Ted's Vision: overriding false_alarm=True for %s — Frigate tracked %s",
                camera_id, ", ".join(objects),
            )
            analysis["false_alarm"] = False
        false_alarm = _as_bool((analysis or {}).get("false_alarm"))
        fa_mode = s.get("vision_false_alarm_mode") or "log_only"
        severity = (analysis or {}).get("severity")
        if severity not in VISION_SEVERITIES:
            severity = _FRIGATE_NATIVE_SEVERITY.get(event_type, "unknown")
        discarded = self._is_discarded(discard_list, severity, false_alarm)

        if tds_id is None:
            # Joined mid-review that ended as an alert: create the finished entry (no actions).
            if discarded and fa_mode == "drop":
                return
            now = dt_util.utcnow()
            event = {
                "id": uuid.uuid4().hex,
                "camera_id": camera_id,
                "camera_name": cam_name,
                "area": area_id,
                "area_name": area_name,
                "event_type": event_type,
                "created": now.isoformat(),
                "ts_start": now.isoformat(),
                "ts_end": now.isoformat(),
                "severity": severity,
                "false_alarm": false_alarm,
                "short_summary": (analysis or {}).get("short_summary")
                or f"{event_type.replace('_', ' ').title()} detected by Frigate.",
                "long_summary": (analysis or {}).get("long_summary") or "",
                "thumbnail_url": f"/api/frigate/notifications/{event_id}/thumbnail.jpg",
                "clip_url": clip_url,
                "reviewed": False,
                "trigger_entity": None,
                "frigate_event_id": event_id,
                "frigate_review_id": review_id,
                "status": "complete",
                "discarded": discarded,
                "analysis_passes": passes or None,
                "_files": {},
            }
            dropped = await self.manager.add_vision_event(event)
            if dropped:
                await self._cleanup_files(dropped)
            return

        # Refine the provisional entry in place (the row visibly upgrades on the timeline).
        if discarded and fa_mode == "drop":
            await self.manager.remove_vision_event(tds_id)
            return
        patch: dict = {
            "clip_url": clip_url,
            # Re-assert the thumbnail from the SAME pinned event as the clip. The provisional
            # row's thumbnail was written at review start and never refreshed, which is how
            # the two drifted onto different Frigate events unnoticed — see defect E.
            "thumbnail_url": f"/api/frigate/notifications/{event_id}/thumbnail.jpg",
            "ts_end": dt_util.utcnow().isoformat(),
            "status": "complete",
            "severity": severity,
            "false_alarm": false_alarm,
        }
        if analysis:
            patch["long_summary"] = analysis.get("long_summary") or ""
            if analysis.get("short_summary"):
                patch["short_summary"] = analysis["short_summary"]
        if passes:
            patch["analysis_passes"] = passes
        if discarded:
            patch["discarded"] = True  # kept but flagged (log-only discard)
        await self.manager.update_vision_event(tds_id, **patch)
        # Fold the finished clip + summary into the toast's stored notification (if the
        # trigger created one) in place — no second toast, no repeated chime.
        await self.manager.update_vision_notifications(
            tds_id, message=patch.get("short_summary"), clip_url=clip_url,
        )

    async def _analyze_frigate(
        self, camera_id: str, cam_name: str, area_name: str | None,
        event_type: str, event_id: str, has_clip: bool, settings: dict,
        object_context: str = "", capture: list | None = None,
        label: str = "detailed",
    ) -> dict | None:
        """PASS 2 — the detailed analysis of Frigate's FINISHED clip. Pass 1 already ran live
        at review start, so there is no race here and no quick attachment set. Downloads the
        media to a temp location, analyses, then deletes it — nothing is retained by TDS."""
        quick_entity = settings.get("vision_ai_task_entity") or preferred_image_ai_task_entity(self.hass)
        detailed_entity = self._detailed_entity(settings, quick_entity)
        media = self._media_source_dir()
        full_attach: list[dict] = []
        tmp_dir: str | None = None
        if media:
            tmp_rel = f"{MEDIA_FOLDER_NAME}/frigate/{event_id}"
            tmp_dir = os.path.join(media[1], tmp_rel)
            asset = "clip.mp4" if has_clip else "snapshot.jpg"
            blob = await self._download_frigate(event_id, asset)
            if blob is not None:
                await self.hass.async_add_executor_job(lambda: os.makedirs(tmp_dir, exist_ok=True))
                local = os.path.join(tmp_dir, asset)
                await self.hass.async_add_executor_job(_write_bytes, local, blob)
                if not has_clip:
                    # Frigate's snapshot.jpg has the label + bounding box burned in.
                    full_attach = [{
                        "media_content_id": f"media-source://media_source/{media[0]}/{tmp_rel}/{asset}",
                        "media_content_type": "image/jpeg",
                    }]
                    object_context = (object_context or "") + _LABELLED_SNAPSHOT_NOTE
                else:
                    if _entity_supports_video(self.hass, detailed_entity):
                        full_attach = [{
                            "media_content_id": f"media-source://media_source/{media[0]}/{tmp_rel}/clip.mp4",
                            "media_content_type": "video/mp4",
                        }]
                    else:
                        # Stills spanning the WHOLE clip — pass 2's job is "what happened",
                        # and pass 1 already covered the opening seconds live.
                        count = max(1, int(settings.get("vision_frame_count") or 3))
                        full_attach = [
                            {
                                "media_content_id": f"media-source://media_source/{media[0]}/{tmp_rel}/{os.path.basename(fp)}",
                                "media_content_type": "image/jpeg",
                            }
                            for fp in await self._extract_frames(
                                local, tmp_dir, count, event_id=event_id,
                            )
                        ]
                    # The extracted frames are RAW (no boxes); Frigate's snapshot.jpg has the
                    # tracked object outlined + labelled. Prepend it as a reference frame.
                    snap = await self._frigate_snapshot_attachment(
                        event_id, media, tmp_rel, tmp_dir,
                    )
                    if snap is not None:
                        full_attach = [snap, *full_attach]
                        object_context = (object_context or "") + _LABELLED_SNAPSHOT_NOTE
        if not full_attach:
            # Couldn't fetch Frigate media — analyze a live snapshot so we still get a summary.
            full_attach = [{
                "media_content_id": f"media-source://camera/{camera_id}",
                "media_content_type": "image/jpeg",
            }]
        try:
            return await self._run_pass(
                label, camera_id, cam_name, area_name, event_type, full_attach,
                detailed_entity, object_context=object_context, capture=capture,
            )
        finally:
            if tmp_dir:
                await self.hass.async_add_executor_job(_rmtree_quiet, tmp_dir)


    async def _download_frigate(self, event_id: str, asset: str) -> bytes | None:
        """Fetch a Frigate notification-API asset (clip.mp4 / snapshot.jpg) over HTTP."""
        from homeassistant.helpers.aiohttp_client import async_get_clientsession  # noqa: PLC0415
        from homeassistant.helpers.network import NoURLAvailableError, get_url  # noqa: PLC0415

        try:
            base = get_url(self.hass, allow_internal=True, prefer_external=False)
        except NoURLAvailableError:
            return None
        url = f"{base}/api/frigate/notifications/{event_id}/{asset}"
        try:
            async with async_get_clientsession(self.hass).get(url) as resp:
                if resp.status != 200:
                    return None
                return await resp.read()
        except Exception:  # noqa: BLE001 - media may not be ready yet; fall back gracefully
            return None

    async def _frigate_path_offsets(
        self, event_id: str, count: int, clip_duration: float | None,
    ) -> list[float]:
        """Offsets (seconds into the downloaded clip) where Frigate actually tracked the
        object, for pass-2 still extraction.

        Frigate's ``data.path_data`` is ``[[[x, y], epoch_ts], ...]``. We spread the
        selection evenly **through that list by index** — NOT evenly in time. Frigate
        emits path points more densely while the object is moving, so index-spreading
        self-weights toward real motion; time-bucketing measured strictly worse (fewer
        distinct positions AND more duplicate frames).

        Returns [] whenever anything is missing or inconsistent, so the caller falls
        back to plain even sampling. Never raises.
        """
        if count < 1 or not clip_duration or clip_duration <= 0.5:
            return []
        try:
            meta = await async_frigate_event_meta(self.hass, event_id)
            if not meta:
                return []
            start = meta.get("start_time")
            end = meta.get("end_time")
            path = ((meta.get("data") or {}).get("path_data") or [])
            if not start or not end or end <= start or len(path) < 2:
                return []
            event_dur = float(end) - float(start)
            # Frames only exist where the clip does.
            if clip_duration < event_dur * 0.5:
                return []  # clip truncated / still being written - don't guess

            # --- align the event timeline to the clip timeline -------------------
            # The clip starts BEFORE the event by Frigate's alert pre_capture. Trust
            # the probed clip length over the configured value: if the clip is shorter
            # than expected (still recording, retention trim) scale the lead down so we
            # never seek past the object.
            pre = await async_frigate_alert_pre_capture(self.hass)
            extra = max(0.0, clip_duration - event_dur)
            lead = min(pre, extra) if extra > 0 else 0.0

            rel = sorted(
                float(p[1]) - float(start)
                for p in path
                if isinstance(p, list) and len(p) > 1 and isinstance(p[1], (int, float))
            )
            if not rel:
                return []
            last = len(rel) - 1
            picks = [rel[round(k * last / max(1, count - 1))] for k in range(count)] \
                if count > 1 else [rel[last // 2]]

            # Clamp into the clip and drop duplicates (events with fewer path points
            # than `count` would otherwise yield the same frame several times).
            # 0.10 s is a "same decoded frame" test, not a spacing rule: Frigate emits
            # path points ~0.2 s apart, so a larger epsilon would throw away genuinely
            # distinct adjacent frames. Do not raise it.
            hi = clip_duration - 0.15
            seen: list[float] = []
            for off in picks:
                t = min(max(off + lead, 0.05), hi)
                if all(abs(t - s) > 0.10 for s in seen):
                    seen.append(t)
            if not seen:
                return []
            # Top up any shortfall by subdividing the MOTION SPAN - never the whole clip.
            # Events with fewer path points than `count` are short, so the clip-wide
            # fallback would spend the remaining budget on empty pre/post-capture
            # padding, which is the exact waste this change exists to remove.
            lo_m, hi_m = rel[0] + lead, rel[-1] + lead
            for i in range(count):
                if len(seen) >= count:
                    break
                t = min(max(lo_m + (hi_m - lo_m) * (i + 0.5) / count, 0.05), hi)
                if all(abs(t - s) > 0.10 for s in seen):
                    seen.append(t)
            # Returning FEWER than `count` is a valid, desirable outcome: the object was
            # only on camera briefly. Fewer real frames beat padding with empty ones.
            return sorted(seen)[:count]
        except Exception:  # noqa: BLE001 - any surprise -> even sampling
            _LOGGER.debug("Frigate path anchoring failed for %s", event_id, exc_info=True)
            return []

    async def _extract_frames(
        self, clip_path: str, out_dir: str, count: int, event_id: str | None = None,
    ) -> list[str]:
        """ffmpeg-extract `count` stills from the clip.

        When `event_id` is given and Frigate can tell us where it tracked the object,
        the stills are taken at those moments; otherwise they're spread evenly across
        the whole clip. Best-effort — any failure degrades to even sampling, then to
        no frames at all."""
        pattern = os.path.join(out_dir, "ff_%02d.jpg")
        try:
            from homeassistant.components import ffmpeg  # noqa: PLC0415

            binary = ffmpeg.get_ffmpeg_manager(self.hass).binary
            duration = await self._probe_duration(binary, clip_path)
            offsets = (
                await self._frigate_path_offsets(event_id, max(1, count), duration)
                if event_id else []
            )
            if offsets:
                # One fast+accurate seek per offset. `-ss` before `-i` seeks by keyframe
                # then decodes forward, so it stays accurate without decoding the whole
                # clip. Frames are numbered in time order to keep _list() sorted.
                for idx, off in enumerate(offsets, start=1):
                    proc = await asyncio.create_subprocess_exec(
                        binary, "-y", "-ss", f"{off:.2f}", "-i", clip_path,
                        "-frames:v", "1", os.path.join(out_dir, f"ff_{idx:02d}.jpg"),
                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                    )
                    await proc.communicate()
                _LOGGER.debug(
                    "Vision: anchored %d frame(s) on Frigate path for %s at %s",
                    len(offsets), event_id, [round(o, 2) for o in offsets],
                )
            else:
                # Evenly sample across the clip (fps = count/duration); fall back to 1 fps
                # from the start when the duration can't be determined.
                vf = f"fps={max(1, count)}/{duration:.2f}" if duration and duration > 0.5 else "fps=1"
                proc = await asyncio.create_subprocess_exec(
                    binary, "-y", "-i", clip_path, "-vf", vf, "-frames:v", str(count), pattern,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                )
                await proc.communicate()
        except Exception:  # noqa: BLE001 - no ffmpeg / bad clip -> no frames
            return []

        def _list() -> list[str]:
            return sorted(
                os.path.join(out_dir, f) for f in os.listdir(out_dir) if f.startswith("ff_")
            )

        return await self.hass.async_add_executor_job(_list)

    async def _probe_duration(self, binary: str, clip_path: str) -> float | None:
        """Read a clip's duration (seconds) from ffmpeg's stderr banner, or None."""
        try:
            proc = await asyncio.create_subprocess_exec(
                binary, "-i", clip_path,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            )
            _out, err = await proc.communicate()
            m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", (err or b"").decode(errors="ignore"))
            if m:
                return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
        except Exception:  # noqa: BLE001 - probe is best-effort
            pass
        return None

    async def analyze(self, camera_id: str, event_type: str = "manual") -> dict | None:
        """Manually analyze a camera now (used by the analyze_camera service/test)."""
        return await self._handle_event(camera_id, event_type, trigger_entity=None)

    async def _handle_event(
        self, camera_id: str, event_type: str, trigger_entity: str | None,
        trigger: dict | None = None,
    ) -> dict | None:
        event_id = uuid.uuid4().hex
        started = dt_util.utcnow()
        area_id, area_name, cam_name = self._camera_context(camera_id)
        s = self._settings()
        two_pass = bool(s.get("vision_two_pass", True))
        mode = s.get("vision_capture_mode") or "video"
        discard_list = (trigger or {}).get("discard_severities") or []
        fa_mode = s.get("vision_false_alarm_mode") or "log_only"

        # Stage 1 — show the event and fire its actions IMMEDIATELY, from a fast placeholder
        # snapshot, before any clip capture or AI runs (act while it's happening).
        ph_files = await self._placeholder(camera_id, event_id)
        event = {
            "id": event_id,
            "camera_id": camera_id,
            "camera_name": cam_name,
            "area": area_id,
            "area_name": area_name,
            "event_type": event_type,
            "created": started.isoformat(),
            "ts_start": started.isoformat(),
            "ts_end": started.isoformat(),
            "severity": "unknown",
            "false_alarm": False,
            "short_summary": f"{event_type.replace('_', ' ').title()} detected.",
            "long_summary": "",
            "thumbnail_url": ph_files.get("thumbnail_url"),
            "clip_url": None,
            "reviewed": False,
            "trigger_entity": trigger_entity,
            "status": "in_progress",
            "_files": ph_files,
        }
        dropped = await self.manager.add_vision_event(event)
        if dropped:
            await self._cleanup_files(dropped)
        if trigger is not None:
            await self._run_actions(event, trigger, area_id)

        # Stage 2 — capture the real clip ONCE (shared by both passes); move to "analyzing"
        # with the captured thumbnail + clip.
        attachments, files = await self._capture(camera_id, event_id)
        files = {**ph_files, **files}
        files.update(await self._finalize_media(event_id, files, mode))
        event["_files"] = files
        event["thumbnail_url"] = files.get("thumbnail_url") or event["thumbnail_url"]
        event["clip_url"] = files.get("clip_url")
        await self.manager.update_vision_event(
            event_id,
            status="analyzing",
            thumbnail_url=event["thumbnail_url"],
            clip_url=event["clip_url"],
            _files=files,
        )

        # Stage 3 — analyze. Two-pass runs a quick early-window pass and the detailed
        # full-clip pass concurrently (the quick one only publishes if it beats the detailed
        # one); single-pass runs one full-clip analysis. Then move to "complete".
        quick_entity = s.get("vision_ai_task_entity") or preferred_image_ai_task_entity(self.hass)
        detailed_entity = self._detailed_entity(s, quick_entity)
        full_attach = list(attachments)
        if _entity_supports_video(self.hass, detailed_entity) and files.get("video_media_id"):
            full_attach = [*attachments, {
                "media_content_id": files["video_media_id"],
                "media_content_type": "video/mp4",
            }]
        span = max(0.0, float(s.get("vision_clip_seconds") or 10))
        quick_attach = self._first_window_attachments(attachments, span)

        async def _push_quick(patch: dict) -> None:
            await self.manager.update_vision_event(event_id, status="analyzing", **patch)

        passes: list | None = [] if s.get("vision_debug_passes") else None
        final = await self._staged_analyze(
            camera_id, cam_name, area_name, event_type,
            quick_attach, quick_entity, full_attach, detailed_entity, two_pass, _push_quick,
            capture=passes,
        )
        self._apply_analysis(event, final)
        if passes:
            event["analysis_passes"] = passes
        await self.manager.update_vision_event(
            event_id,
            status="complete",
            severity=event["severity"],
            false_alarm=event["false_alarm"],
            short_summary=event["short_summary"],
            long_summary=event["long_summary"],
            **({"analysis_passes": passes} if passes else {}),
        )

        # Discard on the FINAL result: a false alarm, or a severity the user chose to
        # discard. "drop" removes it; otherwise it stays logged (flagged discarded).
        if self._is_discarded(discard_list, event["severity"], event["false_alarm"]):
            if fa_mode == "drop":
                await self.manager.remove_vision_event(event_id)
                await self._cleanup_files([event])
                return None
            await self.manager.update_vision_event(event_id, discarded=True)
            event["discarded"] = True
        return event

    def _detailed_entity(self, settings: dict, quick_entity: str | None) -> str | None:
        """The detailed-pass ai_task entity: the explicit setting, else the quick entity if
        it is video-capable, else a video-capable default, else the quick entity."""
        chosen = settings.get("vision_ai_task_entity_detailed")
        if chosen:
            return chosen
        if _entity_supports_video(self.hass, quick_entity):
            return quick_entity
        return preferred_video_ai_task_entity(self.hass) or quick_entity

    async def _placeholder(self, camera_id: str, event_id: str) -> dict:
        """Grab one immediate snapshot so the event appears instantly, before capture/AI."""
        _attach, files = await self._capture(camera_id, event_id, quick=True)
        files.update(await self._finalize_media(event_id, files, "snapshot"))
        return files

    @staticmethod
    def _first_window_attachments(attachments: list[dict], span: float | None) -> list[dict]:
        """The subset of stills covering roughly the first 10 seconds of the window (for the
        quick pass). When the window is <=10s or its duration is unknown, use them all."""
        if len(attachments) <= 1 or not span or span <= 10:
            return attachments
        keep = max(1, round(len(attachments) * (10.0 / span)))
        return attachments[:keep]

    @staticmethod
    def _analysis_patch(result: dict | None) -> dict:
        """Build an event-update patch from a (preliminary) analysis result."""
        patch: dict = {}
        if not result:
            return patch
        sev = result.get("severity")
        if sev in VISION_SEVERITIES:
            patch["severity"] = sev
        if "false_alarm" in result:
            patch["false_alarm"] = _as_bool(result["false_alarm"])
        if result.get("short_summary"):
            patch["short_summary"] = result["short_summary"]
        if result.get("long_summary"):
            patch["long_summary"] = result["long_summary"]
        return patch

    @staticmethod
    def _apply_analysis(event: dict, result: dict | None) -> None:
        """Fold a final analysis result into the event dict, with sensible fallbacks."""
        if result:
            sev = result.get("severity")
            event["severity"] = sev if sev in VISION_SEVERITIES else (event.get("severity") or "unknown")
            event["false_alarm"] = _as_bool(result.get("false_alarm"))
            event["short_summary"] = result.get("short_summary") or event.get("short_summary") or ""
            event["long_summary"] = result.get("long_summary") or event.get("long_summary") or ""
        else:
            event["severity"] = event.get("severity") or "unknown"
            event["short_summary"] = "Analysis unavailable — check the AI Task setup."

    async def _run_pass(
        self, label: str, camera_id: str, cam_name: str, area_name: str | None,
        event_type: str, attach: list[dict], entity: str | None,
        object_context: str = "", capture: list | None = None,
    ) -> dict | None:
        """Run ONE analysis pass. When `capture` is a list, append a debug record describing
        what this pass was actually given and what it returned."""
        started = time.monotonic()
        result = await self._analyze(
            camera_id, cam_name, area_name, event_type, attach,
            entity_id=entity, object_context=object_context,
        )
        if capture is not None:
            capture.append({
                "pass": label,
                "entity_id": entity,
                "attachments": len(attach),
                # Whether the model actually received video, or stills standing in for it.
                "input": "video" if any(
                    str(a.get("media_content_type") or "").startswith("video/") for a in attach
                ) else "stills",
                "duration_ms": int((time.monotonic() - started) * 1000),
                "published": False,
                "severity": (result or {}).get("severity"),
                "false_alarm": (result or {}).get("false_alarm"),
                "short_summary": (result or {}).get("short_summary"),
                "long_summary": (result or {}).get("long_summary"),
                "failed": result is None,
            })
        return result

    @staticmethod
    def _mark_published(capture: list | None, label: str) -> None:
        """Flag the pass whose text actually reached the UI."""
        for rec in capture or []:
            if rec.get("pass") == label and not rec.get("failed"):
                rec["published"] = True

    async def _staged_analyze(
        self, camera_id: str, cam_name: str, area_name: str | None, event_type: str,
        quick_attach: list[dict], quick_entity: str | None,
        full_attach: list[dict], detailed_entity: str | None,
        two_pass: bool, on_quick, object_context: str = "",
        capture: list | None = None,
    ) -> dict | None:
        """Run the AI. Single-pass = one detailed full analysis. Two-pass = a quick pass and
        the detailed pass concurrently; the quick result is published (via ``on_quick``) only
        if the detailed pass hasn't already finished. Returns the detailed result.

        When ``capture`` is a list, each pass appends a debug record to it (label, entity,
        attachment count, elapsed ms, whether it reached the UI, and its full result)."""

        async def _run(label: str, attach: list[dict], entity: str | None) -> dict | None:
            return await self._run_pass(
                label, camera_id, cam_name, area_name, event_type, attach, entity,
                object_context=object_context, capture=capture,
            )

        if not two_pass or (quick_attach == full_attach and quick_entity == detailed_entity):
            result = await _run("single", full_attach, detailed_entity)
            self._mark_published(capture, "single")
            return result
        done = asyncio.Event()
        final: dict = {}

        async def _quick() -> None:
            result = await _run("quick", quick_attach, quick_entity)
            if done.is_set() or not result or on_quick is None:
                return
            self._mark_published(capture, "quick")
            await on_quick(self._analysis_patch(result))

        async def _detailed() -> None:
            result = await _run("detailed", full_attach, detailed_entity)
            done.set()
            if result:
                final.update(result)

        await asyncio.gather(_quick(), _detailed())
        self._mark_published(capture, "detailed")
        return final or None

    def _camera_context(self, camera_id: str) -> tuple[str | None, str | None, str]:
        """Resolve a camera's area id, area name, and friendly name."""
        ent_reg = er.async_get(self.hass)
        entry = ent_reg.async_get(camera_id)
        area_id = None
        if entry is not None:
            area_id = entry.area_id
            if area_id is None and entry.device_id:
                device = dr.async_get(self.hass).async_get(entry.device_id)
                if device:
                    area_id = device.area_id
        state = self.hass.states.get(camera_id)
        configured = ((self._settings().get("vision_cameras") or {}).get(camera_id) or {}).get("name")
        cam_name = (
            (configured if isinstance(configured, str) and configured else None)
            or (state.attributes.get("friendly_name") if state else None)
            or (entry.name or entry.original_name if entry else None)
            or camera_id
        )
        return area_id, self.manager._area_name(area_id), cam_name

    # ── capture ─────────────────────────────────────────────
    async def _capture(
        self, camera_id: str, event_id: str, quick: bool = False,
    ) -> tuple[list[dict], dict]:
        """Grab stills across the event window (and, in video mode, record the real stream).
        `quick`=True grabs a single immediate frame (the fast first pass). Returns
        (attachments, files) where attachments are ai_task-ready image dicts and files
        records on-disk paths (+ a recorded video's media id) for media + cleanup."""
        from homeassistant.components.camera import async_get_image  # noqa: PLC0415

        s = self._settings()
        mode = "snapshot" if quick else (s.get("vision_capture_mode") or "video")
        count = 1 if mode == "snapshot" else max(1, int(s.get("vision_frame_count") or 3))
        span = 0.0 if mode in ("burst", "snapshot") else max(0.0, float(s.get("vision_clip_seconds") or 10))
        interval = (span / count) if (count > 1 and span) else 0.0

        media = self._media_source_dir()
        frame_dir = os.path.join(media[1], MEDIA_FOLDER_NAME, "vision", event_id) if media else None
        if frame_dir:
            await self.hass.async_add_executor_job(
                lambda: os.makedirs(frame_dir, exist_ok=True)
            )

        # Video mode: record the real stream concurrently with grabbing stills for the AI.
        record_task = None
        if mode == "video" and frame_dir and media:
            record_task = self.hass.async_create_task(
                self._record_stream(camera_id, os.path.join(frame_dir, "event.mp4"), span or 10)
            )

        attachments: list[dict] = []
        frame_paths: list[str] = []
        content_type = "image/jpeg"
        for i in range(count):
            try:
                image = await async_get_image(self.hass, camera_id)
            except Exception:  # noqa: BLE001 - a missed frame is non-fatal
                image = None
            if image is not None:
                content_type = image.content_type or content_type
                if frame_dir:
                    ext = ".png" if "png" in content_type else ".jpg"
                    path = os.path.join(frame_dir, f"frame_{i}{ext}")
                    await self.hass.async_add_executor_job(_write_bytes, path, image.content)
                    frame_paths.append(path)
                    rel = f"{MEDIA_FOLDER_NAME}/vision/{event_id}/frame_{i}{ext}"
                    attachments.append(
                        {
                            "media_content_id": f"media-source://media_source/{media[0]}/{rel}",
                            "media_content_type": content_type,
                        }
                    )
            if interval and i < count - 1:
                await asyncio.sleep(interval)

        # Fall back to a live camera snapshot attachment when we couldn't save frames.
        if not attachments:
            attachments = [
                {
                    "media_content_id": f"media-source://camera/{camera_id}",
                    "media_content_type": "image/jpeg",
                }
            ]

        files: dict = {"frame_dir": frame_dir, "frame_paths": frame_paths}
        if record_task is not None:
            if await record_task and frame_dir and media:
                files["video_path"] = os.path.join(frame_dir, "event.mp4")
                files["video_media_id"] = (
                    f"media-source://media_source/{media[0]}/"
                    f"{MEDIA_FOLDER_NAME}/vision/{event_id}/event.mp4"
                )
        return attachments, files

    async def _record_stream(self, camera_id: str, out_path: str, seconds: float) -> bool:
        """Record the camera's live stream to `out_path` (best-effort; needs a stream)."""
        try:
            await self.hass.services.async_call(
                "camera", "record",
                {"entity_id": camera_id, "filename": out_path, "duration": max(1, int(seconds))},
                blocking=True,
            )
        except Exception:  # noqa: BLE001 - no stream / record unsupported -> fall back to stills
            _LOGGER.debug("Ted's Vision: stream record failed for %s", camera_id)
            return False
        return await self.hass.async_add_executor_job(os.path.exists, out_path)

    async def _finalize_media(self, event_id: str, files: dict, mode: str) -> dict:
        """Produce the served thumbnail + clip: recorded video (video mode) else a
        stitched-stills slideshow (clip mode). Returns url/path additions for the event."""
        out: dict = {}
        frame_paths = files.get("frame_paths") or []
        if not self.cache_dir:
            return out
        if frame_paths:
            thumb_src = frame_paths[len(frame_paths) // 2]
            thumb_dst = os.path.join(self.cache_dir, f"{event_id}.jpg")
            try:
                await self.hass.async_add_executor_job(_copy_file, thumb_src, thumb_dst)
                out["thumbnail_path"] = thumb_dst
                out["thumbnail_url"] = f"/teds_dashboard_system/vision_cache/{event_id}.jpg"
            except OSError:
                pass
        clip_dst = os.path.join(self.cache_dir, f"{event_id}.mp4")
        video_path = files.get("video_path")
        if video_path and await self.hass.async_add_executor_job(os.path.exists, video_path):
            try:
                await self.hass.async_add_executor_job(_copy_file, video_path, clip_dst)
                out["clip_path"] = clip_dst
                out["clip_url"] = f"/teds_dashboard_system/vision_cache/{event_id}.mp4"
            except OSError:
                pass
        elif mode in ("clip", "video") and len(frame_paths) > 1 and await self._stitch_clip(frame_paths, clip_dst):
            out["clip_path"] = clip_dst
            out["clip_url"] = f"/teds_dashboard_system/vision_cache/{event_id}.mp4"
        return out

    async def _stitch_clip(self, frame_paths: list[str], out_path: str, per_frame: float = 1.5) -> bool:
        """ffmpeg-concat the captured stills into a short mp4 (best-effort)."""
        list_path = out_path + ".txt"

        def _write_list() -> None:
            lines = []
            for p in frame_paths:
                lines.append(f"file '{p}'")
                lines.append(f"duration {per_frame}")
            lines.append(f"file '{frame_paths[-1]}'")  # last frame needs a trailing entry
            with open(list_path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines))

        try:
            await self.hass.async_add_executor_job(_write_list)
            from homeassistant.components import ffmpeg  # noqa: PLC0415

            binary = ffmpeg.get_ffmpeg_manager(self.hass).binary
            cmd = [
                binary, "-y", "-f", "concat", "-safe", "0", "-i", list_path,
                "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", out_path,
            ]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _out, err = await proc.communicate()
            if proc.returncode != 0:
                _LOGGER.debug(
                    "Ted's Vision: ffmpeg clip stitch failed: %s",
                    (err or b"").decode(errors="ignore")[:300],
                )
                return False
            return await self.hass.async_add_executor_job(os.path.exists, out_path)
        except Exception:  # noqa: BLE001 - clip is optional; thumbnail still works
            return False
        finally:
            await self.hass.async_add_executor_job(_remove_quiet, list_path)

    def _media_source_dir(self) -> tuple[str, str] | None:
        """(source_dir_id, filesystem_base) of HA's first local media dir, or None."""
        media_dirs = getattr(self.hass.config, "media_dirs", None) or {}
        source_dir_id = next(iter(media_dirs), None)
        if source_dir_id is None:
            return None
        return source_dir_id, media_dirs[source_dir_id]

    # ── analysis ────────────────────────────────────────────
    async def _analyze(
        self, camera_id: str, cam_name: str, area_name: str | None,
        event_type: str, attachments: list[dict], entity_id: str | None = None,
        object_context: str = "",
    ) -> dict | None:
        """Run ai_task.generate_data with structured output. None on failure."""
        area_phrase = f" in the {area_name}" if area_name else ""
        data = {
            "task_name": "Ted's Vision Analysis",
            "instructions": _ANALYSIS_INSTRUCTIONS.format(
                event_type=event_type, camera_name=cam_name, area_phrase=area_phrase,
                object_context=object_context,
            ),
            "structure": {
                "severity": {
                    "description": "Threat level of the event.",
                    "required": True,
                    "selector": {"select": {"options": list(VISION_SEVERITIES)}},
                },
                "false_alarm": {
                    "description": "True only if the analysis concluded no genuine "
                    "activity was detected (the detection was spurious).",
                    "required": True,
                    "selector": {"boolean": {}},
                },
                "short_summary": {
                    "description": "One concise sentence describing the event.",
                    "required": True,
                    "selector": {"text": {}},
                },
                "long_summary": {
                    "description": "A detailed paragraph describing the event.",
                    "required": True,
                    "selector": {"text": {"multiline": True}},
                },
            },
            "attachments": attachments,
        }
        if entity_id:
            data["entity_id"] = entity_id
        try:
            result = await self.hass.services.async_call(
                "ai_task", "generate_data", data, blocking=True, return_response=True
            )
        except Exception:  # noqa: BLE001 - provider errors shouldn't crash the engine
            _LOGGER.exception("Ted's Vision: ai_task.generate_data failed")
            return None
        out = (result or {}).get("data")
        return out if isinstance(out, dict) else None

    # ── on-trigger actions ──────────────────────────────────
    @staticmethod
    def _is_discarded(discard_severities: list, severity: str, false_alarm: bool) -> bool:
        """A finished event is discarded when the AI flags a false alarm, or its final
        severity is one the trigger is configured to discard."""
        return bool(false_alarm) or severity in (discard_severities or [])

    async def _run_actions(self, event: dict, trigger: dict, area_id: str | None) -> None:
        """Run a trigger's actions. Actions always fire (severity isn't known yet); the
        discard control gates the stored event afterward, not the actions."""
        title = f"{event['camera_name']}: {str(event['event_type']).title()}"
        message = event["short_summary"] or "Camera event detected."
        notif_sev = _SEVERITY_TO_NOTIF.get(event["severity"], "info")
        for act in trigger.get("actions") or []:
            if not (act or {}).get("enabled", True):
                continue
            atype = (act or {}).get("type")
            try:
                if atype == "toast":
                    await self._act_toast(act, event, title, message, notif_sev)
                elif atype == "push":
                    await self._act_push(act, title, message)
                elif atype == "live_feed":
                    await self._act_live_feed(act, event["camera_id"])
                elif atype == "custom":
                    await self._act_custom(act)
            except Exception:  # noqa: BLE001 - one bad action shouldn't stop the rest
                _LOGGER.exception("Ted's Vision: action %s failed", atype)

    async def _act_toast(self, act: dict, event: dict, title: str, message: str, severity: str) -> None:
        """In-dashboard toast. Empty areas list = house-wide (everywhere). Carries the
        event reference so clicking the notification can open its clip."""
        data = {
            "vision_event_id": event["id"],
            "clip_url": event.get("clip_url"),
            "thumbnail_url": event.get("thumbnail_url"),
            "camera_name": event.get("camera_name"),
        }
        for area in (act.get("areas") or [None]):
            await self.manager.notify(
                title=title, message=message, severity=severity,
                icon="mdi:cctv", area=area, source="vision", data=data,
            )

    async def _act_live_feed(self, act: dict, camera_id: str) -> None:
        """Open a muted live view of this camera on target screens (no navigation).

        Empty areas list = everywhere.
        """
        for area in (act.get("areas") or [None]):
            self.hass.bus.async_fire(
                EVENT_NAVIGATE,
                {"open_camera": camera_id, "area": area, "device_id": None},
            )

    async def _act_push(self, act: dict, title: str, message: str) -> None:
        """Push via notify entities (notify.send_message). Empty list = all notify entities."""
        services = act.get("services") or []
        if not services:
            services = [s.entity_id for s in self.hass.states.async_all("notify")]
        if not services:
            return
        await self.hass.services.async_call(
            "notify", "send_message",
            {"entity_id": services, "message": message, "title": title},
            blocking=False,
        )

    async def _act_custom(self, act: dict) -> None:
        for item in act.get("items") or []:
            kind = (item or {}).get("kind")
            entity = item.get("entity")
            try:
                if kind == "automation" and entity:
                    await self.hass.services.async_call("automation", "trigger", {"entity_id": entity}, blocking=False)
                elif kind == "script" and entity:
                    await self.hass.services.async_call("script", "turn_on", {"entity_id": entity}, blocking=False)
                elif kind == "scene" and entity:
                    await self.hass.services.async_call("scene", "turn_on", {"entity_id": entity}, blocking=False)
                elif kind == "action" and item.get("sequence"):
                    await self._run_sequence(item["sequence"])
            except Exception:  # noqa: BLE001 - one bad custom item shouldn't stop the rest
                _LOGGER.exception("Ted's Vision: custom item %s failed", kind)

    async def _run_sequence(self, sequence) -> None:
        """Run an `action` selector's sequence (any HA actions) via the Script helper."""
        from homeassistant.core import Context  # noqa: PLC0415
        from homeassistant.helpers.script import Script  # noqa: PLC0415

        seq = sequence if isinstance(sequence, list) else [sequence]
        script = Script(self.hass, seq, "Ted's Vision action", DOMAIN)
        await script.async_run(context=Context())

    # ── file cleanup ────────────────────────────────────────
    async def cleanup_event(self, event: dict) -> None:
        await self._cleanup_files([event])

    async def cleanup_events(self, events: list[dict]) -> None:
        await self._cleanup_files(events)

    async def _cleanup_files(self, events: list[dict]) -> None:
        paths: list[str] = []
        dirs: list[str] = []
        for e in events:
            files = e.get("_files") or {}
            for key in ("thumbnail_path", "clip_path"):
                if files.get(key):
                    paths.append(files[key])
            paths.extend(files.get("frame_paths") or [])
            if files.get("frame_dir"):
                dirs.append(files["frame_dir"])
        if paths or dirs:
            await self.hass.async_add_executor_job(_cleanup_paths, paths, dirs)


def _write_bytes(path: str, data: bytes) -> None:
    with open(path, "wb") as fh:
        fh.write(data)


def _copy_file(src: str, dst: str) -> None:
    import shutil  # noqa: PLC0415

    shutil.copyfile(src, dst)


def _remove_quiet(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _rmtree_quiet(path: str) -> None:
    import shutil  # noqa: PLC0415

    shutil.rmtree(path, ignore_errors=True)


def _cleanup_paths(paths: list[str], dirs: list[str]) -> None:
    for p in paths:
        _remove_quiet(p)
    for d in dirs:
        try:
            os.rmdir(d)
        except OSError:
            pass
