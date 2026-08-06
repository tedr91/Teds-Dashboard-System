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
from .frigate import frigate_camera_entity, is_frigate_camera
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
    "Describe the ACTION — what CHANGES throughout the clip: who or what "
    "arrives, the direction and path they move, what they do, and how it ends. Do NOT "
    "just describe a static scene or a parked object; report the sequence of events over "
    "time. (Good: 'A dark SUV pulls into the driveway and continues forward into the "
    "garage.' / 'A van stops, two people get out and carry a box to the porch, then "
    "leave.' Bad: 'A parked car in the driveway.' / 'Two cars in the garage.')\n"
    "- Do NOT report a person, vehicle, animal, or package unless it is actually visible "
    "and moving/acting in the frames — the sensor hint or camera name is not evidence.\n"
    "- Do NOT invent a story or assume a motive; report only what is actually visible in the frames.\n"
    "- Do NOT report a static object that never moves, even if it is a person, vehicle, animal, or package.\n"
    "- false_alarm: default FALSE unless the frames genuinely show nothing meaningful "
    "happening (an empty scene, only shadows / light changes / weather, or a static object "
    "that never moves).\n"
    "- false_alarm: set TRUE when nothing genuinely happens across the frames (an empty "
    "scene, only shadows / light changes / weather, or a static object that never moves). "
    "This is the EXPECTED result for spurious triggers, so use it freely. Set FALSE only "
    "when real activity or movement actually occurs.\n"
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
VIDEO_CAPABLE_PLATFORMS = frozenset({"google_generative_ai"})



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
        self._frigate_pending: dict[str, str] = {}    # frigate review id -> provisional TDS event id
        self._frigate_skip: set[str] = set()          # review ids we chose to skip (cooldown)

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
        native_on = bool(s.get("frigate_native_detection", True))
        mqtt_present = "mqtt" in self.hass.config.components
        native_cams: set[str] = set()
        for cam_id, cfg in cams.items():
            if not (cfg or {}).get("enabled"):
                continue
            triggers = cfg.get("triggers") or []
            if not triggers:
                continue
            frigate = is_frigate_camera(self.hass, cam_id)
            _LOGGER.debug(
                "Ted's Vision: %s -> %s (frigate_native_detection=%s, mqtt=%s, is_frigate_camera=%s)",
                cam_id,
                "Frigate alert-driven" if (native_on and mqtt_present and frigate) else "binary_sensor",
                native_on, mqtt_present, frigate,
            )
            if native_on and mqtt_present and frigate:
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
    def handles_camera(self, camera_id: str) -> bool:
        """True when the Vision engine drives this camera's alerts (Frigate event-driven),
        so the notification bridge should not also notify for it."""
        return camera_id in self._native_cams

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
        detections = [str(d) for d in (payload.get("detections") or [])]
        event_id = detections[0] if detections else review_id
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
                self._frigate_review_new(cam_id, etype, review_id, event_id, trig)
            )
        else:
            self.hass.async_create_task(
                self._frigate_review_end(cam_id, etype, review_id, event_id, is_alert)
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
        return None, None, None

    async def _frigate_review_new(
        self, camera_id: str, event_type: str, review_id: str, event_id: str, trigger: dict,
    ) -> None:
        """Create the provisional Vision event from the alert's thumbnail and fire the
        trigger's actions immediately — the timely moment, before the clip is finalized."""
        tds_id = uuid.uuid4().hex
        self._frigate_pending[review_id] = {
            "id": tds_id,
            "discard": (trigger or {}).get("discard_severities") or [],
        }
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
        await self._run_actions(event, trigger, area_id)

    async def _frigate_review_end(
        self, camera_id: str, event_type: str, review_id: str, event_id: str, is_alert: bool,
    ) -> None:
        """Refine the provisional entry with the finished clip + AI summary. If we never
        acted on the alert's onset, create a finished entry now (no late actions)."""
        pending = self._frigate_pending.pop(review_id, None)
        tds_id = pending["id"] if pending else None
        discard_list = pending["discard"] if pending else []
        skipped = review_id in self._frigate_skip
        self._frigate_skip.discard(review_id)
        if tds_id is None and (skipped or not is_alert):
            return  # cooldown-suppressed, or the review never became an alert
        s = self._settings()
        area_id, area_name, cam_name = self._camera_context(camera_id)
        clip_url = f"/api/frigate/notifications/{event_id}/clip.mp4"

        # The clip is finalized: move the row to "analyzing", and (for two-pass) let the
        # quick pass publish a preliminary summary before the detailed pass finishes.
        on_quick = None
        if tds_id is not None:
            await self.manager.update_vision_event(tds_id, status="analyzing", clip_url=clip_url)

            async def _push_quick(patch: dict) -> None:
                await self.manager.update_vision_event(tds_id, status="analyzing", **patch)

            on_quick = _push_quick

        analysis = await self._analyze_frigate(
            camera_id, cam_name, area_name, event_type, event_id, True, s, on_quick=on_quick
        )
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
            "ts_end": dt_util.utcnow().isoformat(),
            "status": "complete",
            "severity": severity,
            "false_alarm": false_alarm,
        }
        if analysis:
            patch["long_summary"] = analysis.get("long_summary") or ""
            if analysis.get("short_summary"):
                patch["short_summary"] = analysis["short_summary"]
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
        on_quick=None,
    ) -> dict | None:
        """Run the AI analysis on Frigate's clip/snapshot. Downloads the media to a temp
        location ONCE, builds a quick (early-frames) and detailed (full-clip) attachment set
        for two-pass, runs them, then deletes the temp — nothing is retained by TDS."""
        quick_entity = settings.get("vision_ai_task_entity") or preferred_image_ai_task_entity(self.hass)
        detailed_entity = self._detailed_entity(settings, quick_entity)
        two_pass = bool(settings.get("vision_two_pass", True))
        media = self._media_source_dir()
        full_attach: list[dict] = []
        quick_attach: list[dict] = []
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
                    full_attach = quick_attach = [{
                        "media_content_id": f"media-source://media_source/{media[0]}/{tmp_rel}/{asset}",
                        "media_content_type": "image/jpeg",
                    }]
                else:
                    count = max(1, int(settings.get("vision_frame_count") or 3))
                    frame_attach = [
                        {
                            "media_content_id": f"media-source://media_source/{media[0]}/{tmp_rel}/{os.path.basename(fp)}",
                            "media_content_type": "image/jpeg",
                        }
                        for fp in await self._extract_frames(local, tmp_dir, count)
                    ]
                    if _entity_supports_video(self.hass, detailed_entity):
                        full_attach = [{
                            "media_content_id": f"media-source://media_source/{media[0]}/{tmp_rel}/clip.mp4",
                            "media_content_type": "video/mp4",
                        }]
                    else:
                        full_attach = frame_attach
                    quick_attach = self._first_window_attachments(frame_attach, None) or full_attach
        if not full_attach:
            # Couldn't fetch Frigate media — analyze a live snapshot so we still get a summary.
            full_attach = quick_attach = [{
                "media_content_id": f"media-source://camera/{camera_id}",
                "media_content_type": "image/jpeg",
            }]
        try:
            return await self._staged_analyze(
                camera_id, cam_name, area_name, event_type,
                quick_attach or full_attach, quick_entity, full_attach, detailed_entity,
                two_pass, on_quick,
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

    async def _extract_frames(self, clip_path: str, out_dir: str, count: int) -> list[str]:
        """ffmpeg-extract `count` stills spread ACROSS the whole clip (so the AI sees the
        action over time, not just the first seconds). Best-effort."""
        pattern = os.path.join(out_dir, "ff_%02d.jpg")
        try:
            from homeassistant.components import ffmpeg  # noqa: PLC0415

            binary = ffmpeg.get_ffmpeg_manager(self.hass).binary
            duration = await self._probe_duration(binary, clip_path)
            # Evenly sample across the clip (fps = count/duration); fall back to 1 fps from
            # the start when the duration can't be determined.
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

        final = await self._staged_analyze(
            camera_id, cam_name, area_name, event_type,
            quick_attach, quick_entity, full_attach, detailed_entity, two_pass, _push_quick,
        )
        self._apply_analysis(event, final)
        await self.manager.update_vision_event(
            event_id,
            status="complete",
            severity=event["severity"],
            false_alarm=event["false_alarm"],
            short_summary=event["short_summary"],
            long_summary=event["long_summary"],
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

    async def _staged_analyze(
        self, camera_id: str, cam_name: str, area_name: str | None, event_type: str,
        quick_attach: list[dict], quick_entity: str | None,
        full_attach: list[dict], detailed_entity: str | None,
        two_pass: bool, on_quick,
    ) -> dict | None:
        """Run the AI. Single-pass = one detailed full analysis. Two-pass = a quick pass and
        the detailed pass concurrently; the quick result is published (via ``on_quick``) only
        if the detailed pass hasn't already finished. Returns the detailed result."""
        if not two_pass or (quick_attach == full_attach and quick_entity == detailed_entity):
            return await self._analyze(
                camera_id, cam_name, area_name, event_type, full_attach, entity_id=detailed_entity
            )
        done = asyncio.Event()
        final: dict = {}

        async def _quick() -> None:
            result = await self._analyze(
                camera_id, cam_name, area_name, event_type, quick_attach, entity_id=quick_entity
            )
            if done.is_set() or not result or on_quick is None:
                return
            await on_quick(self._analysis_patch(result))

        async def _detailed() -> None:
            result = await self._analyze(
                camera_id, cam_name, area_name, event_type, full_attach, entity_id=detailed_entity
            )
            done.set()
            if result:
                final.update(result)

        await asyncio.gather(_quick(), _detailed())
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
    ) -> dict | None:
        """Run ai_task.generate_data with structured output. None on failure."""
        area_phrase = f" in the {area_name}" if area_name else ""
        data = {
            "task_name": "Ted's Vision Analysis",
            "instructions": _ANALYSIS_INSTRUCTIONS.format(
                event_type=event_type, camera_name=cam_name, area_phrase=area_phrase
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
