"""Bridge Home Assistant's native voice timers into Ted's timers.

Home Assistant only starts a native voice timer on a device that has a registered
timer handler (``intent.async_register_timer_handler``). By registering a handler
for each registered Ted's Dashboard *screen* device, we make those devices
timer-capable AND intercept every timer event, mirroring it into a read-only Ted's
timer so it shows on the Timers view — while the native timer stays the
authoritative clock (so spoken "add two minutes" / "cancel timer" keep working).

Only Ted's Dashboard browser_mod devices are registered (never shared speaker
satellites, whose own on-device timer handler we must not clobber).
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from homeassistant.components.intent.timers import (
    TimerEventType,
    async_register_timer_handler,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.start import async_at_started

from .const import EVENT_NAVIGATE, EVENT_SETTINGS

_LOGGER = logging.getLogger(__name__)


def _ha_device_id_for(hass: HomeAssistant, ted_device_id: str) -> str | None:
    """Map a Ted's device id (``bm:<browserID>``) to its HA device-registry id."""
    if not ted_device_id.startswith("bm:"):
        return None  # only browser_mod screens have an HA device to bridge
    browser_id = ted_device_id[3:]
    device = dr.async_get(hass).async_get_device(identifiers={("browser_mod", browser_id)})
    return device.id if device else None


class TimerBridge:
    """Registers native timer handlers for Ted's screen devices and mirrors events."""

    def __init__(self, hass: HomeAssistant, manager) -> None:
        self.hass = hass
        self.manager = manager
        self._handlers: dict[str, Callable[[], None]] = {}  # ha_device_id -> unregister

    def _enabled(self) -> bool:
        return self.manager.effective_settings().get("timer_bridge_enabled", True) is not False

    @callback
    def reconcile(self, *_) -> None:
        """(Re)register handlers to match the current registered-devices + setting."""
        desired: dict[str, str] = {}  # ha_device_id -> ted_device_id
        if self._enabled():
            for ted_id in list(self.manager.device_registry):
                ha_id = _ha_device_id_for(self.hass, ted_id)
                if ha_id:
                    desired[ha_id] = ted_id
        for ha_id in list(self._handlers):
            if ha_id not in desired:
                self._unregister(ha_id)
        for ha_id in desired:
            if ha_id in self._handlers:
                continue
            try:
                self._handlers[ha_id] = async_register_timer_handler(
                    self.hass, ha_id, self._make_handler(ha_id)
                )
            except Exception:  # noqa: BLE001 - never let a bad device break setup
                _LOGGER.debug("Timer bridge: could not register handler for %s", ha_id)

    def _unregister(self, ha_id: str) -> None:
        unreg = self._handlers.pop(ha_id, None)
        if unreg is None:
            return
        try:
            unreg()
        except Exception:  # noqa: BLE001
            pass

    def _make_handler(self, ha_device_id: str):
        manager = self.manager
        hass = self.hass

        @callback
        def handler(event_type, timer) -> None:
            try:
                if event_type == TimerEventType.STARTED:
                    manager.mirror_timer_started(
                        timer.id, timer.name, timer.seconds, timer.area_id
                    )
                    hass.bus.async_fire(
                        EVENT_NAVIGATE,
                        {
                            "dashboard": "timers_dashboard",
                            "area": timer.area_id,
                            "device_id": ha_device_id,
                        },
                    )
                elif event_type == TimerEventType.UPDATED:
                    manager.mirror_timer_updated(
                        timer.id, timer.seconds_left, not timer.is_active
                    )
                elif event_type == TimerEventType.CANCELLED:
                    manager.mirror_timer_cancelled(timer.id)
                elif event_type == TimerEventType.FINISHED:
                    manager.mirror_timer_finished(timer.id)
            except Exception:  # noqa: BLE001 - a handler exception must not crash HA
                _LOGGER.exception("Timer bridge handler failed")

        return handler

    def shutdown(self) -> None:
        for ha_id in list(self._handlers):
            self._unregister(ha_id)


def async_setup_timer_bridge(
    hass: HomeAssistant, entry: ConfigEntry, manager
) -> None:
    """Start the timer bridge: reconcile at startup and on device/settings changes."""
    bridge = TimerBridge(hass, manager)
    manager.timer_bridge = bridge
    entry.async_on_unload(async_at_started(hass, bridge.reconcile))
    entry.async_on_unload(hass.bus.async_listen(EVENT_SETTINGS, bridge.reconcile))
    entry.async_on_unload(bridge.shutdown)
