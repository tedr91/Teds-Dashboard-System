"""Gradual wake-up light for alarms: a parabolic sunrise ramp plus a safeguard fade-back.

An alarm can name a light that ramps from ~1% up to its target brightness so it reaches
full exactly when the alarm rings. Some time after ringing a safeguard fade-back returns
the light to whatever it was before the ramp — but only if the light is still sitting at
the ramp's target (i.e. the user hasn't already changed or switched it off).
"""

from __future__ import annotations

import asyncio
import logging

_LOGGER = logging.getLogger(__name__)

# How often the ramp rewrites the light's brightness.
FADE_STEP_SECONDS = 4
# Fade-in never starts from a hard 0 (that would turn the light off).
FADE_START_PCT = 1
# The safeguard fade-back fires this long after the ramp completes (i.e. after the alarm
# rings), so a light isn't accidentally left on all day if the user never turns it off.
RESTORE_SAFEGUARD_SECONDS = 60 * 60
# Duration of the safeguard fade-back.
RESTORE_FADE_SECONDS = 60
# The fade-back no-ops unless the light is still within this many percent of the ramp's
# target — otherwise the user has changed it since it rang and we leave it alone.
RESTORE_TOLERANCE_PCT = 5


def _brightness_to_pct(brightness) -> int:
    if not brightness:
        return 0
    return round(int(brightness) / 255 * 100)


def _clamp_pct(value, default: int) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(100, v))


class LightFadeEngine:
    """Runs per-alarm sunrise fades and a safeguard fade-back, entirely server-side."""

    def __init__(self, manager) -> None:
        self._m = manager
        self.hass = manager.hass
        # alarm_id -> {"entity_id", "snapshot", "target", "fade_task", "restore_task"}
        self._sessions: dict[str, dict] = {}

    # ── public API ──────────────────────────────────────────
    def start_wake_fade(self, alarm: dict) -> None:
        """Begin a sunrise ramp that reaches the target brightness at the alarm time."""
        entity_id = alarm.get("light_entity")
        fade_minutes = int(alarm.get("light_fade_minutes") or 0)
        if not entity_id or fade_minutes <= 0 or alarm["id"] in self._sessions:
            return
        target = _clamp_pct(alarm.get("light_target_pct"), default=100)
        session = {
            "entity_id": entity_id,
            "snapshot": self._snapshot(entity_id),
            "target": target,
            "fade_task": None,
            "restore_task": None,
        }
        self._sessions[alarm["id"]] = session
        session["fade_task"] = self.hass.async_create_task(
            self._run_fade(alarm["id"], entity_id, target, fade_minutes * 60)
        )

    def cancel(self, alarm_id: str) -> None:
        """Stop any fade/restore for an alarm and put the light back (remove/disable)."""
        session = self._sessions.pop(alarm_id, None)
        if not session:
            return
        self._cancel_tasks(session)
        self.hass.async_create_task(
            self._apply_snapshot(session["entity_id"], session["snapshot"], RESTORE_FADE_SECONDS)
        )

    def shutdown(self) -> None:
        for session in self._sessions.values():
            self._cancel_tasks(session)
        self._sessions.clear()

    @staticmethod
    def _cancel_tasks(session: dict) -> None:
        for key in ("fade_task", "restore_task"):
            task = session.get(key)
            if task and not task.done():
                task.cancel()

    # ── fade internals ──────────────────────────────────────
    async def _run_fade(self, alarm_id, entity_id, target, duration_s) -> None:
        try:
            steps = max(1, duration_s // FADE_STEP_SECONDS)
            await self._set_brightness(entity_id, FADE_START_PCT)
            for i in range(1, steps + 1):
                await asyncio.sleep(FADE_STEP_SECONDS)
                p = i / steps
                # Parabolic ease-in: dim for longer, then rise quickly toward the target.
                await self._set_brightness(entity_id, target * p * p)
            await self._set_brightness(entity_id, target)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Wake-up fade failed for %s", entity_id)
        finally:
            session = self._sessions.get(alarm_id)
            if session is not None:
                session["fade_task"] = None
                session["restore_task"] = self.hass.async_create_task(
                    self._run_restore(alarm_id, entity_id, target)
                )

    async def _run_restore(self, alarm_id, entity_id, target) -> None:
        try:
            await asyncio.sleep(RESTORE_SAFEGUARD_SECONDS)
            cur = self._current_pct(entity_id)
            # Only restore if the light is still where the ramp left it (user untouched).
            if cur is None or abs(cur - target) > RESTORE_TOLERANCE_PCT:
                return
            session = self._sessions.get(alarm_id)
            snapshot = session["snapshot"] if session else {"on": False, "pct": 0}
            await self._apply_snapshot(entity_id, snapshot, RESTORE_FADE_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Wake-up restore failed for %s", entity_id)
        finally:
            self._sessions.pop(alarm_id, None)

    async def _apply_snapshot(self, entity_id, snapshot, duration_s) -> None:
        if snapshot.get("on"):
            await self._fade_to(entity_id, max(FADE_START_PCT, snapshot.get("pct", 0)), duration_s)
        else:
            await self._fade_to(entity_id, FADE_START_PCT, duration_s)
            await self.hass.services.async_call(
                "light", "turn_off", {"entity_id": entity_id}, blocking=False
            )

    async def _fade_to(self, entity_id, target, duration_s) -> None:
        start = self._current_pct(entity_id)
        if start is None:
            start = 0
        steps = max(1, duration_s // FADE_STEP_SECONDS)
        for i in range(1, steps + 1):
            p = i / steps
            await self._set_brightness(entity_id, start + (target - start) * p * p)
            await asyncio.sleep(FADE_STEP_SECONDS)
        await self._set_brightness(entity_id, target)

    # ── light helpers ───────────────────────────────────────
    def _snapshot(self, entity_id) -> dict:
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unavailable", "unknown"):
            return {"on": False, "pct": 0}
        on = state.state == "on"
        pct = _brightness_to_pct(state.attributes.get("brightness")) if on else 0
        return {"on": on, "pct": pct}

    def _current_pct(self, entity_id):
        state = self.hass.states.get(entity_id)
        if state is None or state.state != "on":
            return None
        return _brightness_to_pct(state.attributes.get("brightness"))

    async def _set_brightness(self, entity_id, pct) -> None:
        await self.hass.services.async_call(
            "light",
            "turn_on",
            {"entity_id": entity_id, "brightness_pct": int(max(1, min(100, round(pct))))},
            blocking=False,
        )
