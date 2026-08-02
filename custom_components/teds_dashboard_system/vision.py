"""Camera Vision Analysis engine for Ted's Dashboard System.

Watches the binary_sensors a camera exposes (motion / person / animal / car),
captures a few stills across the event window, and asks Home Assistant's native
``ai_task`` building block (OpenAI, Ollama, …) to classify the event into a
severity plus a short and long summary. Results are stored on the manager and
served to the Vision timeline card. No third-party vision integration required.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util

from .const import DOMAIN, EVENT_NAVIGATE, EVENT_SETTINGS, MEDIA_FOLDER_NAME, VISION_SEVERITIES

_LOGGER = logging.getLogger(__name__)

# Detection event types we can classify a camera's binary_sensors into. Matched by
# device_class + entity-id/name keywords (Frigate/Reolink/UniFi Protect conventions).
_DETECTOR_KEYWORDS: dict[str, tuple[str, ...]] = {
    "person": ("person", "human", "people"),
    "animal": ("animal", "pet", "dog", "cat"),
    "car": ("vehicle", "car", "truck"),
    "package": ("package", "parcel", "delivery"),
}
_MOTION_CLASSES = {"motion", "moving", "occupancy", "presence"}
# vision severity -> notification severity used for the on-trigger Teds notification.
_SEVERITY_TO_NOTIF = {
    "critical": "danger",
    "suspicious": "warning",
    "harmless": "info",
    "unknown": "info",
}

_ANALYSIS_INSTRUCTIONS = (
    "You are a home security camera analyst. The attached images are sequential "
    "frames from a {event_type} event on the '{camera_name}' camera{area_phrase}. "
    "Analyze what is happening and classify it.\n"
    "- severity: 'critical' for an active threat, break-in, or emergency; "
    "'suspicious' for unexpected or concerning activity worth a human review; "
    "'harmless' for routine or expected activity (residents, pets, deliveries, "
    "passing cars); 'unknown' only if the frames are too unclear to judge.\n"
    "- false_alarm: true ONLY if the detector fired but nothing of genuine interest is "
    "actually present (e.g. shifting shadows, rain, insects on the lens, lighting or "
    "exposure changes, a moving tree/flag); otherwise false.\n"
    "- short_summary: one concise sentence describing what the clip shows.\n"
    "- long_summary: a detailed paragraph covering who or what is present, what "
    "they are doing, and any notable details."
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
    result: dict[str, list[str]] = {}
    for ent in er.async_entries_for_device(ent_reg, cam_entry.device_id, include_disabled_entities=False):
        if ent.domain != "binary_sensor":
            continue
        etype = _classify_detector(hass, ent)
        if etype:
            result.setdefault(etype, []).append(ent.entity_id)
    return result


def _classify_detector(hass: HomeAssistant, ent: er.RegistryEntry) -> str | None:
    """Map a binary_sensor to a detection event type via keywords / device_class."""
    haystack = f"{ent.entity_id} {(ent.original_name or ent.name or '')}".lower()
    for etype, words in _DETECTOR_KEYWORDS.items():
        if any(w in haystack for w in words):
            return etype
    dev_class = ent.device_class or ent.original_device_class
    state = hass.states.get(ent.entity_id)
    if state is not None and not dev_class:
        dev_class = state.attributes.get("device_class")
    if dev_class in _MOTION_CLASSES or "motion" in haystack or "movement" in haystack:
        return "motion"
    return None


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
        self._watch: dict[str, tuple[str, str]] = {}  # sensor eid -> (camera_id, event_type)
        self._cooldowns: dict[str, float] = {}        # camera_id -> monotonic last trigger

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
        for cam_id, cfg in cams.items():
            if not (cfg or {}).get("enabled"):
                continue
            triggers = cfg.get("triggers") or []
            if not triggers:
                continue
            detectors = discover_camera_detectors(self.hass, cam_id)
            for t_idx, trig in enumerate(triggers):
                for eid in detectors.get((trig or {}).get("type"), []):
                    self._watch.setdefault(eid, []).append((cam_id, t_idx))
        if self._watch:
            self._unsub_state = async_track_state_change_event(
                self.hass, list(self._watch), self._on_state
            )

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
        quick_entity = s.get("vision_ai_task_entity") or preferred_image_ai_task_entity(self.hass)

        # Pass 1 — one fast frame when two-pass, else the full configured capture.
        attachments, files = await self._capture(camera_id, event_id, quick=two_pass)
        files.update(await self._finalize_media(event_id, files, "snapshot" if two_pass else mode))
        analysis = await self._analyze(
            camera_id, cam_name, area_name, event_type, attachments, entity_id=quick_entity
        )
        severity = (analysis or {}).get("severity") or "unknown"
        if severity not in VISION_SEVERITIES:
            severity = "unknown"
        false_alarm = _as_bool((analysis or {}).get("false_alarm"))
        event = {
            "id": event_id,
            "camera_id": camera_id,
            "camera_name": cam_name,
            "area": area_id,
            "area_name": area_name,
            "event_type": event_type,
            "created": started.isoformat(),
            "ts_start": started.isoformat(),
            "ts_end": dt_util.utcnow().isoformat(),
            "severity": severity,
            "false_alarm": false_alarm,
            "short_summary": (analysis or {}).get("short_summary")
            or ("Analysis unavailable — check the AI Task setup." if analysis is None else ""),
            "long_summary": (analysis or {}).get("long_summary") or "",
            "thumbnail_url": files.get("thumbnail_url"),
            "clip_url": files.get("clip_url"),
            "reviewed": False,
            "trigger_entity": trigger_entity,
            "_files": files,
        }

        fa_mode = s.get("vision_false_alarm_mode") or "log_only"
        if false_alarm and fa_mode == "drop":
            await self._cleanup_files([event])  # discard: don't store, don't fire
            return None
        dropped = await self.manager.add_vision_event(event)
        if dropped:
            await self._cleanup_files(dropped)
        # Fire actions unless a false alarm is being filtered (log_only skips actions; off runs).
        if trigger is not None and not (false_alarm and fa_mode == "log_only"):
            await self._run_actions(event, trigger, area_id)

        # Pass 2 — full-window capture + detailed model refine, patched in place.
        if two_pass:
            f_attach, f_files = await self._capture(camera_id, event_id)
            f_files.update(await self._finalize_media(event_id, f_files, mode))
            detailed_entity = self._detailed_entity(s, quick_entity)
            if _entity_supports_video(self.hass, detailed_entity) and f_files.get("video_media_id"):
                f_attach = [*f_attach, {
                    "media_content_id": f_files["video_media_id"],
                    "media_content_type": "video/mp4",
                }]
            refined = await self._analyze(
                camera_id, cam_name, area_name, event_type, f_attach, entity_id=detailed_entity
            )
            files = {**files, **f_files}
            patch: dict = {
                "_files": files,
                "thumbnail_url": f_files.get("thumbnail_url") or event["thumbnail_url"],
                "clip_url": f_files.get("clip_url") or event["clip_url"],
            }
            if refined:
                patch["long_summary"] = refined.get("long_summary") or event["long_summary"]
                patch["short_summary"] = refined.get("short_summary") or event["short_summary"]
                rsev = refined.get("severity")
                if rsev in VISION_SEVERITIES:
                    patch["severity"] = rsev
                if "false_alarm" in refined:
                    patch["false_alarm"] = _as_bool(refined["false_alarm"])
            await self.manager.update_vision_event(event_id, **patch)
            event.update(patch)
            # The detailed pass can flip the verdict — re-apply "drop" on the final result.
            if event["false_alarm"] and fa_mode == "drop":
                await self.manager.remove_vision_event(event_id)
                await self._cleanup_files([event])
                return None
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
                    "description": "True if the detection is very likely a false alarm.",
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
    async def _run_actions(self, event: dict, trigger: dict, area_id: str | None) -> None:
        """Run a trigger's actions, gated by its severity filter (empty = all severities)."""
        severities = trigger.get("severities") or []
        if severities and event["severity"] not in severities:
            return
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


def _cleanup_paths(paths: list[str], dirs: list[str]) -> None:
    for p in paths:
        _remove_quiet(p)
    for d in dirs:
        try:
            os.rmdir(d)
        except OSError:
            pass
