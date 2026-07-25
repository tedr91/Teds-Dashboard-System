"""Server-side sound playback for Ted's Cards alerts (notifications/timers/alarms).

When an alert fires, the engine resolves the target `system_sound_player`(s) from the
firing area's present devices' effective settings (falling back to the global
system-sound player, then the device's own registered player), deduped by entity.
Devices in Do Not Disturb are skipped.

Playing over existing media uses one of two strategies per player:

- **Announce-capable players** get `media_player.play_media` with `announce: True`,
  which natively ducks/pauses the current media and auto-resumes it when the alert
  finishes — no manual restore needed. Repeating alerts re-announce every sound
  length.
- **Other players** (e.g. BrowserMod, Squeezelite) fall back to snapshotting the
  current volume + media, playing the alert, then restoring volume and resuming the
  previous media on stop. Repeating alerts re-play the sound every sound length
  (native `repeat_set` isn't honoured for a one-shot media URL on most players).

Stopping is immediate (`media_stop`), and every player is returned to its prior
state so nothing is left in a weird state.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import os
import uuid

from homeassistant.core import callback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.network import NoURLAvailableError, get_url

from .const import DEFAULT_SOUND

_LOGGER = logging.getLogger(__name__)

# MediaPlayerEntityFeature.MEDIA_ANNOUNCE bit (2**20).
MEDIA_ANNOUNCE = 1 << 20
# Fallback sound length (seconds) when the real duration can't be determined.
DEFAULT_DURATION = 5.0
# Bundled sounds are served under this path; the files live in ./sounds/.
SOUNDS_URL_PREFIX = "/teds_dashboard_system/sounds/"


class PlaybackEngine:
    def __init__(self, manager) -> None:
        self._m = manager
        self.hass = manager.hass
        # notif_id -> {"plays": [...], "loops": [...], "cancels": [...]}
        self._active: dict[str, dict] = {}
        # sound url -> length in seconds (resolved lazily, then cached).
        self._durations: dict[str, float] = {}
        # (engine\nmessage) -> exact TTS length in seconds (measured + cached).
        self._tts_durations: dict[str, float] = {}
        self._sounds_dir = os.path.join(os.path.dirname(__file__), "sounds")

    @staticmethod
    def _sound_url(value, kind) -> str:
        """Resolve a sound setting to a URL ("default" → bundled file for the kind)."""
        if not value or value == DEFAULT_SOUND:
            return f"{SOUNDS_URL_PREFIX}{kind}.mp3"
        return value

    @staticmethod
    def _notification_sound(eff, severity):
        """Per-severity notification sound, falling back to the general one, then default."""
        val = eff.get(f"notification_sound_{severity}") if severity else None
        if not val or val == DEFAULT_SOUND:
            val = eff.get("notification_sound")
        if not val or val == DEFAULT_SOUND:
            return f"{SOUNDS_URL_PREFIX}notification.mp3"
        return val

    def on_notification(self, item) -> None:
        """Single entry point: drive sound for any created notification.

        Maps the notification `source` to a playback kind (alarm/timer play their
        own alert sound; everything else plays the severity's notification sound).
        """
        source = item.get("source")
        # Announcements drive their own TTS + chime via `announce()`, not a generic sound.
        if source == "announcement":
            return
        kind = "alarm" if source == "alarm" else "timer" if source == "timer" else "notification"
        self.play(
            kind,
            item.get("area"),
            item.get("id"),
            severity=item.get("severity"),
            timeout=item.get("timeout"),
        )

    def _targets(self, area):
        """(effective_settings, player) for each distinct system-sound player in the fired area.

        House-wide (area is None) targets every present device; otherwise only the
        devices whose registered area matches. Falls back to the global system-sound
        player when no present device supplies one. Devices in DND are skipped.
        """
        m = self._m
        seen = set()
        out = []
        for did, entry in m._present_devices():
            if area and entry.get("area") != area:
                continue
            eff = m.effective_settings(did)
            if eff.get("do_not_disturb"):
                continue
            # Per-device / global system-sound player, else the device's own client player.
            mp = eff.get("system_sound_player") or entry.get("media_player")
            if not mp or mp in seen:
                continue
            seen.add(mp)
            out.append((eff, mp))
        if not out:
            eff = m.effective_settings(None)
            mp = eff.get("system_sound_player")
            if mp and not eff.get("do_not_disturb"):
                out.append((eff, mp))
        return out

    def play(self, kind, area, notif_id, severity=None, timeout=None) -> None:
        """Play an alert of `kind` ("timer"|"alarm"|"notification") for an area.

        Announce-capable players duck/pause and auto-resume their current media;
        other players snapshot their volume + media, play the alert, then restore.
        Timer/alarm alerts repeat by re-playing the sound every sound length;
        ringing stops on dismiss (`stop`) or after `timeout` seconds.
        """
        targets = self._targets(area)
        if not targets:
            return
        repeat = kind in ("timer", "alarm") and bool(
            targets[0][0].get(f"{kind}_alert_repeat", True)
        )
        plays = []
        for eff, mp in targets:
            if kind == "notification":
                sound = self._notification_sound(eff, severity)
                volume = eff.get("notification_volume", 50)
            else:
                sound = self._sound_url(eff.get(f"{kind}_alert_sound"), kind)
                volume = eff.get(f"{kind}_alert_volume", 60)
            announce, snapshot = self._inspect(mp)
            plays.append({
                "mp": mp,
                "sound": sound,
                "volume": volume,
                "announce": announce,
                "snapshot": snapshot,
            })

        self.hass.async_create_task(self._run(plays, notif_id, repeat, timeout))

    # ── announcements (spoken TTS + optional repeating chime) ────────────────
    # Default spoken preface (used when the announce_intro_phrase setting is unset).
    _INCOMING_PHRASE = "Incoming announcement"
    # Small gap inserted between clips so they don't butt right against each other.
    _STEP_GAP = 0.2
    # Pause between the spoken preface/title and the message body.
    _MSG_GAP = 0.5
    # Announcement play hardening: some cast/Alexa devices silently drop the first
    # play when idle/asleep. After issuing the clip we verify (in the background) that
    # the target actually started, and re-issue up to _PLAY_RETRIES times if not.
    _PLAY_RETRIES = 2
    _VERIFY_STEP = 0.5
    _VERIFY_WINDOW = 3.0
    # Give an idle/asleep player a brief moment to come available before playing.
    _AVAIL_WAIT = 2.0

    async def prepare_announcement(self, message, title=None, areas=None, devices=None,
                                   volume=None):
        """Resolve targets + engine and PRE-GENERATE + measure the spoken clips.

        Generating the TTS up front warms Home Assistant's server-side TTS cache and
        yields each clip's exact length, so the sequence later plays back-to-back with
        no synthesis pause and no under/over-run. Returns a prep dict, or None when
        there's no speaker to target (the caller still shows the on-screen message).

        The spoken sequence is: chime → [preface] → [title] → 0.5s pause → message →
        chime. The preface comes from the `announce_intro_phrase` setting (blank = no
        preface); the title is spoken when it's a non-empty, non-default heading.
        """
        targets = self._announce_targets(areas, devices)
        if not targets:
            return None
        eff0 = targets[0][0]
        vol = volume if volume is not None else eff0.get("announce_volume", 80)
        engine = eff0.get("announce_tts_engine") or None
        chime = self._sound_url(eff0.get("announce_sound"), "notification")
        chime_len = await self._sound_duration(chime)
        intro_phrase = eff0.get("announce_intro_phrase")
        if intro_phrase is None:
            intro_phrase = self._INCOMING_PHRASE
        intro_phrase = intro_phrase.strip()
        # The default title is "Announcement" (a placeholder heading), which we don't
        # want spoken; only speak a real, custom title.
        title = (title or "").strip()
        spoken_title = title if title and title.lower() != "announcement" else ""
        intro_media = self._tts_media_id(intro_phrase, engine) if intro_phrase else None
        title_media = self._tts_media_id(spoken_title, engine) if spoken_title else None
        message_media = self._tts_media_id(message, engine)
        intro_dur = (await self._tts_duration(intro_phrase, engine)) if intro_media is not None else 0.0
        title_dur = (await self._tts_duration(spoken_title, engine)) if title_media is not None else 0.0
        message_dur = (await self._tts_duration(message, engine)) if message_media is not None else 0.0
        plays = [{"mp": mp, "announce": self._inspect(mp)[0]} for _eff, mp in targets]
        # Stitch the whole sequence (incl. the finishing chime) into ONE clip so the
        # target device's per-play startup latency is paid once (falls back to separate
        # clips if this fails). combined_dur is the clip's measured length, used to
        # start the repeating alert chime exactly when the clip ends.
        combined_url, combined_dur = await self._build_combined(
            chime, message, engine, intro_phrase, spoken_title,
            intro_media, title_media, message_media,
        )
        return {
            "plays": plays, "chime": chime, "chime_len": chime_len,
            "intro_media": intro_media, "title_media": title_media,
            "message_media": message_media,
            "intro_dur": intro_dur, "title_dur": title_dur, "message_dur": message_dur,
            "vol": vol, "combined_url": combined_url, "combined_dur": combined_dur,
        }

    def start_prepared(self, prep, notif_id, persistent=False, timeout=None) -> None:
        """Kick off a prepared announcement sequence (no-op when there's no target).

        Persistent ("until dismissed") announcements loop the alert chime after the
        stitched clip until they're dismissed.
        """
        if not prep:
            return
        self.hass.async_create_task(self._run_prepared(prep, notif_id, bool(persistent), timeout))

    def _announce_targets(self, areas, devices):
        """(effective_settings, player) for each distinct player targeted by an announcement.

        Targets the union of present devices whose registered area is in `areas` and
        present devices whose id is in `devices`. When neither is given it's house-wide
        (every present device). Devices in Do Not Disturb are skipped.
        """
        m = self._m
        area_set = set(areas or [])
        device_set = set(devices or [])
        house_wide = not area_set and not device_set
        seen = set()
        out = []
        for did, entry in m._present_devices():
            if not (house_wide or did in device_set or entry.get("area") in area_set):
                continue
            eff = m.effective_settings(did)
            if eff.get("do_not_disturb"):
                continue
            mp = eff.get("system_sound_player") or entry.get("media_player")
            if not mp or mp in seen:
                continue
            seen.add(mp)
            out.append((eff, mp))
        if not out and house_wide:
            eff = m.effective_settings(None)
            mp = eff.get("system_sound_player")
            if mp and not eff.get("do_not_disturb"):
                out.append((eff, mp))
        return out

    async def _run_prepared(self, prep, notif_id, loop_chime, timeout) -> None:
        """Play the prepared announcement.

        Preferred path: ONE stitched clip (ding → preface → title → 0.5s pause →
        message → ding) played with a single call, so a high-latency device pays its
        per-play startup cost once and the sequence has no internal gaps. If stitching
        wasn't available, falls back to playing the clips as a spaced sequence.
        A placeholder entry is registered up front so a dismiss mid-sequence aborts it.
        """
        if notif_id:
            old = self._active.pop(notif_id, None)
            if old:
                self._cancel_timers(old)
            self._active[notif_id] = {"plays": [], "loops": [], "cancels": []}

        def _live() -> bool:
            return notif_id is None or notif_id in self._active

        plays = prep["plays"]
        chime = prep["chime"]
        chime_len = prep["chime_len"]
        intro_dur = prep["intro_dur"]
        title_dur = prep.get("title_dur", 0.0)
        message_dur = prep["message_dur"]
        vol = prep["vol"]
        combined_url = prep.get("combined_url")
        gap = self._STEP_GAP
        self._dlog(
            "start nid=%s combined=%s chime=%.2f intro=%.2f msg=%.2f targets=%d"
            % (notif_id, bool(combined_url), chime_len, intro_dur, message_dur, len(plays))
        )

        # Preferred: a single stitched clip.
        if combined_url:
            t = self.hass.loop.time()
            await self._play_media_all(plays, combined_url, vol, _live)
            self._dlog("combined play issued in %.2fs" % (self.hass.loop.time() - t))
            if _live() and loop_chime and notif_id:
                # The stitched clip ends on its baked-in finishing chime; start the
                # repeating alert chime exactly when the clip ends (measured length),
                # so it carries on from that chime like a finished timer — no gap.
                total = prep.get("combined_dur") or (
                    chime_len * 2 + intro_dur + self._MSG_GAP + message_dur
                )
                entry = self._active.get(notif_id)
                if entry is not None:
                    for p in plays:
                        entry["loops"].append(
                            self._schedule_announce_chime(notif_id, p, chime, chime_len, total, vol)
                        )
            elif notif_id:
                self._active.pop(notif_id, None)
            return

        # Fallback: spaced sequence (each clip is a separate play).
        # 1) incoming-signal chime
        await self._play_chime_all(plays, chime, vol)
        await asyncio.sleep(chime_len + gap)
        # 2) spoken preface ("Incoming announcement"), if configured
        if _live() and prep["intro_media"] is not None:
            await self._speak_all(plays, prep["intro_media"], vol)
            await asyncio.sleep(intro_dur + gap)
        # 3) the announcement title, if it's a real custom heading
        if _live() and prep.get("title_media") is not None:
            await self._speak_all(plays, prep["title_media"], vol)
            await asyncio.sleep(title_dur + self._MSG_GAP)
        # 4) the announcement text
        if _live() and prep["message_media"] is not None:
            await self._speak_all(plays, prep["message_media"], vol)
            await asyncio.sleep(message_dur + gap)
        if not _live():
            return

        # 5) alert chime — repeat until dismissed for persistent+repeat, else once.
        entry = self._active.get(notif_id) if notif_id else None
        if loop_chime and notif_id and entry is not None:
            for p in plays:
                entry["loops"].append(
                    self._schedule_announce_chime(notif_id, p, chime, chime_len, 0, vol)
                )
        else:
            await self._play_chime_all(plays, chime, vol)
            if notif_id:
                self._active.pop(notif_id, None)

    async def _play_media_all(self, plays, url, volume, live=None) -> None:
        """Play one media URL on every target (announce-ducked when supported).

        Each play is verified in the background: if the target hasn't started within a
        few seconds (an intermittent drop on some idle Alexa/cast devices) the clip is
        re-issued up to `_PLAY_RETRIES` times. `live` (optional) gates the retries so a
        dismissed announcement isn't replayed.
        """
        for p in plays:
            await self._await_available(p["mp"])
            prev = self.hass.states.get(p["mp"])
            prev_state = prev.state if prev else None
            await self._issue_media(p["mp"], url, volume, p["announce"])
            self.hass.async_create_task(
                self._verify_and_retry(p["mp"], url, volume, p["announce"], prev_state, live)
            )

    async def _await_available(self, mp) -> None:
        """Wait briefly for an idle/asleep player to come available before playing."""
        waited = 0.0
        while waited < self._AVAIL_WAIT:
            st = self.hass.states.get(mp)
            if st is not None and st.state not in ("unavailable", "unknown"):
                return
            await asyncio.sleep(self._VERIFY_STEP)
            waited += self._VERIFY_STEP

    async def _issue_media(self, mp, url, volume, announce) -> None:
        """Issue a single media play (announce-ducked or direct)."""
        if announce:
            await self._announce(mp, url, volume)
        else:
            await self._play_once(mp, url, volume)

    def _media_started(self, mp, url, prev_state) -> bool:
        """Heuristic: did `mp` actually start playing `url`?

        True when the player now reports our media id, or it transitioned INTO active
        playback from a non-playing prior state (so pre-existing music isn't mistaken
        for a successful announcement).
        """
        st = self.hass.states.get(mp)
        if st is None:
            return False
        cid = st.attributes.get("media_content_id")
        if isinstance(cid, str) and cid and (cid == url or url.rsplit("/", 1)[-1] in cid):
            return True
        return st.state in ("playing", "buffering") and prev_state not in ("playing", "buffering")

    async def _verify_and_retry(self, mp, url, volume, announce, prev_state, live=None) -> None:
        """Poll for the play to start; re-issue up to `_PLAY_RETRIES` times if it didn't."""
        for attempt in range(self._PLAY_RETRIES):
            waited = 0.0
            while waited < self._VERIFY_WINDOW:
                await asyncio.sleep(self._VERIFY_STEP)
                waited += self._VERIFY_STEP
                if self._media_started(mp, url, prev_state):
                    if attempt:
                        self._dlog("announce started on %s after retry %d" % (mp, attempt))
                    return
            if live is not None and not live():
                return  # dismissed mid-verify — don't replay
            self._dlog("announce didn't start on %s; re-issuing (retry %d)" % (mp, attempt + 1))
            await self._issue_media(mp, url, volume, announce)

    def _dlog(self, msg) -> None:
        """Announcement timing diagnostics (INFO when Debug mode is on, else DEBUG)."""
        try:
            debug = bool(self._m.effective_settings(None).get("debug_mode"))
        except Exception:  # noqa: BLE001
            debug = False
        (_LOGGER.info if debug else _LOGGER.debug)("[announce] %s", msg)

    # --- combined-clip stitching (ffmpeg) ----------------------------------
    def _combined_url(self, name) -> str:
        """Absolute (preferred) or relative URL for a stitched clip filename."""
        path = f"/teds_dashboard_system/announce_cache/{name}"
        try:
            return f"{get_url(self.hass)}{path}"
        except NoURLAvailableError:
            return path

    async def _build_combined(self, chime, message, engine, intro_phrase, spoken_title,
                              intro_media, title_media, message_media):
        """Stitch chime+[preface]+[title]+pause+message+chime into ONE cached mp3.

        Returns (url, duration_seconds), or (None, 0.0) when the cache dir, TTS, or
        ffmpeg isn't available. Cached by (engine, chime, preface, title, message) so
        repeat sends of the same announcement reuse the file with no re-encoding. The
        measured duration lets the caller start the repeating alert chime exactly when
        the clip ends (so the loop continues seamlessly from the baked-in chime).
        """
        cache_dir = self._m.announce_cache_dir
        if not cache_dir or message_media is None:
            return None, 0.0
        try:
            raw = f"{engine or ''}|{chime}|{intro_phrase}|{spoken_title}|{message}"
            key = hashlib.sha1(raw.encode("utf-8")).hexdigest()
            out_name = f"{key}.mp3"
            out_path = os.path.join(cache_dir, out_name)
            url = self._combined_url(out_name)
            if not await self.hass.async_add_executor_job(os.path.exists, out_path):
                from homeassistant.components import tts  # noqa: PLC0415

                chime_bytes = await self._audio_bytes(chime)
                _me, msg_bytes = await tts.async_get_media_source_audio(self.hass, message_media)
                if not (chime_bytes and msg_bytes):
                    return None, 0.0
                intro_bytes = title_bytes = None
                if intro_media is not None:
                    _ie, intro_bytes = await tts.async_get_media_source_audio(self.hass, intro_media)
                if title_media is not None:
                    _te, title_bytes = await tts.async_get_media_source_audio(self.hass, title_media)
                # Ordered sequence: chime, [preface], [title], 0.5s pause, message, chime.
                segments: list = [("a", chime_bytes)]
                if intro_bytes:
                    segments.append(("a", intro_bytes))
                if title_bytes:
                    segments.append(("a", title_bytes))
                segments.append(("s", self._MSG_GAP))
                segments.append(("a", msg_bytes))
                segments.append(("a", chime_bytes))
                ok = await self._concat_to(cache_dir, out_path, segments)
                if not ok:
                    return None, 0.0
            dur = await self.hass.async_add_executor_job(self._read_file_duration, out_path)
            return url, (dur or 0.0)
        except Exception as ex:  # noqa: BLE001 - stitching is best-effort
            _LOGGER.debug("Ted's Cards announce: combine failed: %s", ex)
            return None, 0.0

    async def _audio_bytes(self, url):
        """Bytes of a sound URL (bundled file from disk, else HTTP fetch)."""
        if url.startswith(SOUNDS_URL_PREFIX):
            path = os.path.join(self._sounds_dir, os.path.basename(url))
            return await self.hass.async_add_executor_job(self._read_file, path)
        return await self.hass.async_add_executor_job(self._fetch_bytes, url)

    @staticmethod
    def _read_file(path):
        try:
            with open(path, "rb") as fh:
                return fh.read()
        except OSError:
            return None

    @staticmethod
    def _read_file_duration(path) -> float | None:
        """Length in seconds of the audio file at `path` (via mutagen), or None."""
        try:
            import mutagen  # noqa: PLC0415 - optional dep, only needed for durations

            audio = mutagen.File(path)
            if audio and audio.info and audio.info.length:
                return round(float(audio.info.length), 2)
        except Exception:  # noqa: BLE001 - duration is best-effort
            return None
        return None

    def _fetch_bytes(self, url):
        try:
            import requests  # noqa: PLC0415

            fetch = url
            if url.startswith("/"):
                try:
                    fetch = f"{get_url(self.hass)}{url}"
                except NoURLAvailableError:
                    return None
            resp = requests.get(fetch, timeout=10)
            return resp.content if resp.ok else None
        except Exception:  # noqa: BLE001
            return None

    async def _concat_to(self, cache_dir, out_path, segments) -> bool:
        """ffmpeg-concat an ordered list of segments → out_path (mp3).

        Each segment is ("a", audio_bytes) for a clip or ("s", seconds) for a
        synthesized silence pause. Segments are concatenated in order.
        """
        stem = uuid.uuid4().hex
        tmp_paths: list[str] = []
        input_args: list[str] = []
        labels: list[str] = []
        writes: list[tuple[str, bytes]] = []
        for i, (kind, val) in enumerate(segments):
            if kind == "a":
                p = os.path.join(cache_dir, f"{stem}_{i}.mp3")
                tmp_paths.append(p)
                writes.append((p, val))
                input_args += ["-i", p]
            else:  # silence pause
                input_args += [
                    "-f", "lavfi", "-t", f"{val}", "-i", "anullsrc=r=44100:cl=stereo",
                ]
            labels.append(f"[{i}:a]")

        def _write():
            for p, b in writes:
                with open(p, "wb") as fh:
                    fh.write(b)

        def _cleanup():
            for p in tmp_paths:
                try:
                    os.remove(p)
                except OSError:
                    pass

        try:
            await self.hass.async_add_executor_job(_write)
            from homeassistant.components import ffmpeg  # noqa: PLC0415

            binary = ffmpeg.get_ffmpeg_manager(self.hass).binary
            chain = "".join(labels) + f"concat=n={len(segments)}:v=0:a=1[out]"
            cmd = [
                binary, "-y", *input_args,
                "-filter_complex", chain,
                "-map", "[out]", "-ac", "2", "-ar", "44100", out_path,
            ]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _out, err = await proc.communicate()
            if proc.returncode != 0:
                _LOGGER.debug(
                    "Ted's Cards announce: ffmpeg concat failed: %s",
                    (err or b"").decode(errors="ignore")[:300],
                )
                return False
            return await self.hass.async_add_executor_job(os.path.exists, out_path)
        finally:
            await self.hass.async_add_executor_job(_cleanup)

    async def _play_chime_all(self, plays, chime, volume) -> None:
        """Play the alert chime once on every target player."""
        for p in plays:
            if p["announce"]:
                await self._announce(p["mp"], chime, volume)
            else:
                await self._play_once(p["mp"], chime, volume)

    async def _speak_all(self, plays, media_id, volume) -> None:
        """Speak a TTS media-source id on every target player."""
        for p in plays:
            await self._speak(p["mp"], media_id, p["announce"], volume)

    def _schedule_announce_chime(self, notif_id, p, chime, chime_len, start_delay, volume):
        """Play `chime` on `p` after `start_delay`, then every `chime_len` until stopped."""
        loop: dict = {}

        @callback
        def _tick(_now=None):
            if notif_id not in self._active:
                return
            self.hass.async_create_task(self._chime_play(notif_id, p, chime, volume))
            loop["cancel"] = async_call_later(self.hass, chime_len, _tick)

        loop["cancel"] = async_call_later(self.hass, start_delay, _tick)
        return loop

    async def _chime_play(self, notif_id, p, chime, volume) -> None:
        """One announce-chime iteration, re-checking liveness so a dismiss between the
        tick and this play doesn't fire an extra trailing chime."""
        if notif_id not in self._active:
            return
        if p["announce"]:
            await self._announce(p["mp"], chime, volume)
        else:
            await self._play_once(p["mp"], chime, volume)

    async def _speak(self, mp, media_id, announce, volume) -> None:
        """Play a TTS media-source id on `mp` (announce-ducked when supported)."""
        data = {
            "entity_id": mp,
            "media_content_id": media_id,
            "media_content_type": "music",
        }
        try:
            if announce:
                data["announce"] = True
                if volume is not None:
                    data["extra"] = {"announce_volume": int(float(volume))}
            elif volume is not None:
                level = max(0.0, min(1.0, (float(volume or 0)) / 100.0))
                await self.hass.services.async_call(
                    "media_player", "volume_set",
                    {"entity_id": mp, "volume_level": level}, blocking=False,
                )
            await self.hass.services.async_call(
                "media_player", "play_media", data, blocking=False,
            )
        except Exception:  # noqa: BLE001 - a bad media_player must not break announcing
            pass

    def _tts_media_id(self, message, engine):
        """Build a TTS media-source id for `message` (None when TTS is unavailable)."""
        try:
            from homeassistant.components.tts import media_source as tts_ms  # noqa: PLC0415

            return tts_ms.generate_media_source_id(self.hass, message, engine=engine or None)
        except Exception as ex:  # noqa: BLE001 - TTS is best-effort
            _LOGGER.warning("Ted's Cards announce: could not build TTS media id: %s", ex)
            return None

    @staticmethod
    def _estimate_speech(message) -> float:
        """Rough spoken length (seconds) of `message` — ~0.45s/word, clamped 3-30s.

        Only a fallback when the real TTS audio can't be generated/measured.
        """
        words = len((message or "").split())
        return min(30.0, max(3.0, words * 0.45 + 1.0))

    async def _tts_duration(self, message, engine) -> float:
        """Exact spoken length (seconds) of `message`.

        Pre-generates the TTS audio via HA's tts helper and measures it, which ALSO
        warms HA's TTS cache so the subsequent play_media starts without a synthesis
        pause. Cached per (engine, message); falls back to a word-count estimate when
        TTS generation or measurement is unavailable.
        """
        key = f"{engine or ''}\n{message}"
        if key in self._tts_durations:
            return self._tts_durations[key]
        dur = None
        media_id = self._tts_media_id(message, engine)
        if media_id is not None:
            try:
                from homeassistant.components import tts  # noqa: PLC0415

                _ext, data = await tts.async_get_media_source_audio(self.hass, media_id)
                dur = await self.hass.async_add_executor_job(self._measure_audio, data)
            except Exception as ex:  # noqa: BLE001 - measurement is best-effort
                _LOGGER.debug("Ted's Cards announce: TTS duration measure failed: %s", ex)
        if dur is None:
            dur = self._estimate_speech(message)
        self._tts_durations[key] = dur
        return dur

    @staticmethod
    def _measure_audio(data) -> float | None:
        """Length in seconds of in-memory audio `data` (via mutagen), or None."""
        try:
            import mutagen  # noqa: PLC0415 - optional dep, only needed for durations

            audio = mutagen.File(io.BytesIO(data))
            if audio and audio.info and audio.info.length:
                return round(float(audio.info.length), 2)
        except Exception as ex:  # noqa: BLE001 - duration is best-effort
            _LOGGER.debug("Could not measure TTS audio: %s", ex)
        return None

    def _inspect(self, mp):
        """Return (announce_supported, snapshot) for a media player.

        `snapshot` (for non-announce players) captures the current volume and, if
        playing, the media to resume afterwards. Announce players need no snapshot.
        """
        st = self.hass.states.get(mp)
        attrs = st.attributes if st else {}
        announce = bool(int(attrs.get("supported_features", 0) or 0) & MEDIA_ANNOUNCE)
        snapshot = None
        if not announce and st is not None:
            snapshot = {
                "volume": attrs.get("volume_level"),
                "content_id": attrs.get("media_content_id") if st.state == "playing" else None,
                "content_type": attrs.get("media_content_type") or "music",
            }
        return announce, snapshot

    async def _run(self, plays, notif_id, repeat, timeout) -> None:
        """Fire the initial play on each target and set up repeat/restore handling."""
        # Replacing an active alert on the same id: drop its timers (no restore —
        # we're about to play again on the same players).
        if notif_id:
            old = self._active.pop(notif_id, None)
            if old:
                self._cancel_timers(old)

        for p in plays:
            if p["announce"]:
                await self._announce(p["mp"], p["sound"], p["volume"])
            else:
                await self._play_once(p["mp"], p["sound"], p["volume"])

        if not notif_id:
            return

        entry = {"plays": plays, "loops": [], "cancels": []}

        if repeat:
            # Repeat by re-playing the sound every mp3 length (announce players
            # re-announce; others re-play). Native `repeat_set` isn't honoured for a
            # one-shot media URL on most players, so we drive the loop here.
            # Auto-stop after the notification lifetime.
            for p in plays:
                duration = await self._sound_duration(p["sound"])
                entry["loops"].append(self._schedule_reloop(notif_id, p, duration))
            if timeout:
                entry["cancels"].append(
                    async_call_later(self.hass, float(timeout), self._auto_stop(notif_id))
                )
            self._active[notif_id] = entry
            return

        # One-shot: non-announce players that we touched need their volume/media
        # restored once the sound has played out (announce players auto-resume).
        restorable = [p for p in plays if not p["announce"] and p["snapshot"]]
        if restorable:
            durations = [await self._sound_duration(p["sound"]) for p in restorable]
            delay = max(durations) + 0.5
            entry["cancels"].append(
                async_call_later(self.hass, delay, self._auto_stop(notif_id))
            )
            self._active[notif_id] = entry

    def _auto_stop(self, notif_id):
        @callback
        def _cb(_now=None):
            self.stop(notif_id)

        return _cb

    def _schedule_reloop(self, notif_id, p, duration):
        """Re-play `p` every `duration` seconds until the alert is stopped."""
        loop: dict = {}

        @callback
        def _tick(_now=None):
            if notif_id not in self._active:
                return
            self.hass.async_create_task(self._loop_play(notif_id, p))
            loop["cancel"] = async_call_later(self.hass, duration, _tick)

        loop["cancel"] = async_call_later(self.hass, duration, _tick)
        return loop

    async def _loop_play(self, notif_id, p) -> None:
        """One loop iteration, re-checking liveness so a dismiss that lands between
        the tick and this play doesn't fire an extra (jarring) trailing sound."""
        if notif_id not in self._active:
            return
        if p["announce"]:
            await self._announce(p["mp"], p["sound"], p.get("volume"))
        else:
            await self._replay(p["mp"], p["sound"])

    async def _announce(self, mp, sound, volume=None) -> None:
        """Play `sound` as an announcement (native duck + auto-resume).

        `volume` (0-100) is forwarded as `announce_volume` so announce-capable
        players honour the configured alert volume; players that don't support it
        simply ignore the extra and announce at their current volume.
        """
        data = {
            "entity_id": mp,
            "media_content_id": sound,
            "media_content_type": "music",
            "announce": True,
        }
        if volume is not None:
            data["extra"] = {"announce_volume": int(float(volume))}
        try:
            await self.hass.services.async_call(
                "media_player", "play_media", data, blocking=False,
            )
        except Exception:  # noqa: BLE001 - a bad media_player must not break playback
            pass

    async def _play_once(self, mp, sound, volume) -> None:
        """Set volume and play `sound` directly (non-announce path)."""
        try:
            level = max(0.0, min(1.0, (float(volume or 0)) / 100.0))
            await self.hass.services.async_call(
                "media_player", "volume_set",
                {"entity_id": mp, "volume_level": level}, blocking=False,
            )
            await self.hass.services.async_call(
                "media_player", "play_media",
                {"entity_id": mp, "media_content_id": sound, "media_content_type": "music"},
                blocking=False,
            )
        except Exception:  # noqa: BLE001 - a bad media_player must not break playback
            pass

    async def _replay(self, mp, sound) -> None:
        """Re-play `sound` (a loop iteration; volume is already set)."""
        try:
            await self.hass.services.async_call(
                "media_player", "play_media",
                {"entity_id": mp, "media_content_id": sound, "media_content_type": "music"},
                blocking=False,
            )
        except Exception:  # noqa: BLE001 - a bad media_player must not break playback
            pass

    async def _stop_and_restore(self, p) -> None:
        """Stop a non-announce player immediately and restore its prior state."""
        mp = p["mp"]
        try:
            await self.hass.services.async_call(
                "media_player", "repeat_set",
                {"entity_id": mp, "repeat": "off"}, blocking=False,
            )
            await self.hass.services.async_call(
                "media_player", "media_stop",
                {"entity_id": mp}, blocking=False,
            )
            snap = p.get("snapshot") or {}
            if snap.get("volume") is not None:
                await self.hass.services.async_call(
                    "media_player", "volume_set",
                    {"entity_id": mp, "volume_level": snap["volume"]}, blocking=False,
                )
            if snap.get("content_id"):
                await self.hass.services.async_call(
                    "media_player", "play_media",
                    {
                        "entity_id": mp,
                        "media_content_id": snap["content_id"],
                        "media_content_type": snap.get("content_type") or "music",
                    },
                    blocking=False,
                )
        except Exception:  # noqa: BLE001 - a bad media_player must not break dismissal
            pass

    @staticmethod
    def _cancel_timers(entry) -> None:
        for loop in entry.get("loops", []):
            if loop.get("cancel"):
                loop["cancel"]()
        for cancel in entry.get("cancels", []):
            if cancel:
                cancel()

    def stop(self, notif_id) -> None:
        entry = self._active.pop(notif_id, None)
        if not entry:
            return
        self._cancel_timers(entry)
        # Announce players auto-resume once their current clip ends; only the
        # non-announce (repeat_set / replaced) players need an explicit restore.
        for p in entry["plays"]:
            if not p["announce"]:
                self.hass.async_create_task(self._stop_and_restore(p))

    def shutdown(self) -> None:
        for notif_id in list(self._active):
            self.stop(notif_id)

    async def _sound_duration(self, url) -> float:
        """Length of `url` in seconds (cached). Bundled files are read from disk;
        custom URLs are fetched. Falls back to DEFAULT_DURATION on any failure."""
        if url in self._durations:
            return self._durations[url]
        duration = await self.hass.async_add_executor_job(self._read_duration, url)
        self._durations[url] = duration
        return duration

    def _read_duration(self, url) -> float:
        try:
            import mutagen  # noqa: PLC0415 - optional dep, only needed for durations

            if url.startswith(SOUNDS_URL_PREFIX):
                path = os.path.join(self._sounds_dir, os.path.basename(url))
                audio = mutagen.File(path)
            else:
                import requests  # noqa: PLC0415

                fetch = url
                if url.startswith("/"):
                    try:
                        fetch = f"{get_url(self.hass)}{url}"
                    except NoURLAvailableError:
                        return DEFAULT_DURATION
                resp = requests.get(fetch, timeout=10)
                audio = mutagen.File(io.BytesIO(resp.content))
            if audio and audio.info and audio.info.length:
                return round(float(audio.info.length), 2)
        except Exception as ex:  # noqa: BLE001 - duration is best-effort
            _LOGGER.debug("Could not read duration for %s: %s", url, ex)
        return DEFAULT_DURATION
