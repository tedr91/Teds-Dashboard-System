"""Smart voice climate control for Ted's Dashboard System.

A single entry point, :func:`apply_climate`, encapsulates all of the "smart"
thermostat behavior so that BOTH the Assist intents (voice) and the on-screen
interactive prompt buttons (which call the ``teds_dashboard_system.apply_climate``
service) run exactly the same logic, recomputed from fresh state each time.

Behavior (see the plan):
  * OFF thermostat -> turn on automatically when the ``climate_auto_on`` setting is
    on, otherwise post an interactive "turn it on?" notification and speak the
    question. When turning on, pick a heat_cool/auto mode if available, else the
    single mode that matches current-vs-target.
  * Single mode (heat XOR cool): relative shifts the single setpoint; absolute sets
    it. A relative request with no amount posts a "how many degrees?" prompt.
  * heat_cool / auto (range) mode: relative shifts BOTH setpoints; absolute computes
    a comfort band centered on the target, biased by current + outside temperature,
    always honoring the ``climate_min_delta`` setting and clamped/stepped to the
    entity's limits.
"""

from __future__ import annotations

import logging

from homeassistant.components.climate import (
    ATTR_CURRENT_TEMPERATURE,
    ATTR_HVAC_MODES,
    ATTR_MAX_TEMP,
    ATTR_MIN_TEMP,
    ATTR_PRESET_MODE,
    ATTR_PRESET_MODES,
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    ATTR_TARGET_TEMP_STEP,
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_HVAC_MODE,
    SERVICE_SET_PRESET_MODE,
    SERVICE_SET_TEMPERATURE,
    SERVICE_TURN_ON,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_SUPPORTED_FEATURES,
    ATTR_TEMPERATURE,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

_LOGGER = logging.getLogger(__name__)

# Default step/limits when an entity doesn't advertise them.
_DEFAULT_STEP = 0.5
_DEFAULT_MIN_TEMP = 7.0
_DEFAULT_MAX_TEMP = 35.0
# How far outside temperature must differ from the target before it biases the band.
_OUTSIDE_BIAS = 5.0

_ON_RANGE_MODES = {HVACMode.HEAT_COOL, HVACMode.AUTO}


# ── entity resolution ───────────────────────────────────────


def _entity_area(hass: HomeAssistant, entity_id: str) -> str | None:
    """Resolve a climate entity's area (its own, else its device's)."""
    entry = er.async_get(hass).async_get(entity_id)
    if entry is None:
        return None
    if entry.area_id:
        return entry.area_id
    if entry.device_id:
        device = dr.async_get(hass).async_get(entry.device_id)
        if device and device.area_id:
            return device.area_id
    return None


def resolve_climate_entity(
    hass: HomeAssistant, manager, zone: str | None, area_id: str | None
) -> str | None:
    """Resolve a spoken zone / area to a climate entity id.

    Priority: configured alias -> climate friendly-name (exact then substring) ->
    a climate entity in the target area -> the only climate entity (if just one).
    """
    if zone:
        wanted = zone.strip().casefold()
        aliases = manager.effective_settings().get("climate_aliases") or []
        for alias in aliases:
            if not isinstance(alias, dict):
                continue
            name = (alias.get("name") or "").strip().casefold()
            if name and name == wanted and alias.get("entity"):
                return str(alias["entity"])
        exact: str | None = None
        partial: str | None = None
        for state in hass.states.async_all(CLIMATE_DOMAIN):
            friendly = (state.attributes.get("friendly_name") or state.entity_id).casefold()
            if friendly == wanted:
                exact = state.entity_id
                break
            if partial is None and wanted in friendly:
                partial = state.entity_id
        if exact or partial:
            return exact or partial
    if area_id:
        for state in hass.states.async_all(CLIMATE_DOMAIN):
            if _entity_area(hass, state.entity_id) == area_id:
                return state.entity_id
    climates = hass.states.async_all(CLIMATE_DOMAIN)
    if len(climates) == 1:
        return climates[0].entity_id
    return None


# ── numeric helpers ─────────────────────────────────────────


def _as_float(value) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _round_step(value: float, step: float) -> float:
    if step <= 0:
        step = _DEFAULT_STEP
    rounded = round(value / step) * step
    # Avoid float noise like 69.99999999.
    return round(rounded, 2)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _outside_temp(hass: HomeAssistant, manager) -> float | None:
    """Best-effort outdoor temperature from the configured weather entity."""
    entity_id = (manager.effective_settings() or {}).get("weather_entity")
    if not entity_id:
        for state in hass.states.async_all("weather"):
            entity_id = state.entity_id
            break
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None:
        return None
    return _as_float(state.attributes.get("temperature"))


def _fmt_temp(value: float) -> str:
    """Format a temperature for speech without a trailing .0."""
    if float(value).is_integer():
        return str(int(value))
    return f"{value:g}"


def _choose_on_mode(modes: list[str], target: float | None, current: float | None) -> str | None:
    """Pick the HVAC mode to turn an off thermostat on to."""
    if HVACMode.HEAT_COOL in modes:
        return HVACMode.HEAT_COOL
    if HVACMode.AUTO in modes:
        return HVACMode.AUTO
    has_heat = HVACMode.HEAT in modes
    has_cool = HVACMode.COOL in modes
    if has_heat and has_cool:
        if target is not None and current is not None:
            return HVACMode.HEAT if target >= current else HVACMode.COOL
        return HVACMode.HEAT
    if has_heat:
        return HVACMode.HEAT
    if has_cool:
        return HVACMode.COOL
    return None


def _compute_band(
    target: float, current: float | None, outside: float | None, min_delta: float
) -> tuple[float, float]:
    """Compute (low, high) setpoints centered on the target, biased by temps."""
    half = min_delta / 2.0
    need_cool = (current is not None and current > target + 0.5) or (
        outside is not None and outside >= target + _OUTSIDE_BIAS
    )
    need_heat = (current is not None and current < target - 0.5) or (
        outside is not None and outside <= target - _OUTSIDE_BIAS
    )
    if need_cool and not need_heat:
        # Wants cooling: pin the cool setpoint near the target, heat below.
        high = target
        low = target - min_delta
    elif need_heat and not need_cool:
        low = target
        high = target + min_delta
    else:
        low = target - half
        high = target + half
    return low, high


# ── interactive prompt notifications ────────────────────────


def _base_request(entity_id: str, req: dict) -> dict:
    """Service-data payload replaying a request (used by prompt action buttons)."""
    data = {"entity_id": entity_id, "kind": req["kind"]}
    for key in ("temperature", "amount", "direction", "hvac_mode", "preset"):
        if req.get(key) is not None:
            data[key] = req[key]
    return data


async def _prompt_turn_on(
    hass: HomeAssistant, manager, entity_id: str, name: str, area_id: str | None, req: dict
) -> str:
    """Post an interactive "turn it on?" notification and return the spoken prompt."""
    confirm = _base_request(entity_id, req)
    confirm["force_on"] = True
    actions = [
        {
            "label": "Turn it on",
            "action": "call-service",
            "service": "teds_dashboard_system.apply_climate",
            "service_data": confirm,
            "variant": "primary",
        },
        {"label": "Not now", "action": "dismiss"},
    ]
    await manager.notify(
        f"Turn on {name}?",
        f"{name} is currently off. Would you like to turn it on?",
        severity="info",
        icon="mdi:thermostat",
        area=area_id,
        actions=actions,
        timeout=120,
    )
    return f"{name} is currently off. Would you like me to turn it on?"


async def _prompt_amount(
    hass: HomeAssistant,
    manager,
    entity_id: str,
    name: str,
    area_id: str | None,
    direction: str,
    req: dict,
) -> str:
    """Post a "how many degrees?" notification and return the spoken prompt."""
    actions = []
    for amount in (1, 2, 3, 5):
        data = _base_request(entity_id, {**req, "amount": amount})
        actions.append(
            {
                "label": f"{amount}\u00b0",
                "action": "call-service",
                "service": "teds_dashboard_system.apply_climate",
                "service_data": data,
                "variant": "primary" if amount == 2 else "default",
            }
        )
    word = "warmer" if direction == "warmer" else "cooler"
    await manager.notify(
        f"How much {word}?",
        f"How many degrees {word} would you like {name}?",
        severity="info",
        icon="mdi:thermostat",
        area=area_id,
        actions=actions,
        timeout=120,
    )
    return (
        f"How many degrees {word} would you like {name}? "
        f"Tap an amount on the screen, or say make it a few degrees {word}."
    )


# ── main entry point ────────────────────────────────────────


async def apply_climate(
    hass: HomeAssistant,
    manager,
    *,
    entity_id: str,
    kind: str = "absolute",
    temperature: float | None = None,
    amount: float | None = None,
    direction: str | None = None,
    hvac_mode: str | None = None,
    preset: str | None = None,
    force_on: bool = False,
) -> str:
    """Apply a climate request to ``entity_id`` and return a spoken confirmation.

    ``kind`` is one of ``absolute`` | ``relative`` | ``mode`` | ``preset``.
    """
    state = hass.states.get(entity_id)
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return "That thermostat is unavailable right now."
    attrs = state.attributes
    name = attrs.get("friendly_name") or entity_id
    features = int(attrs.get(ATTR_SUPPORTED_FEATURES) or 0)
    modes = list(attrs.get(ATTR_HVAC_MODES) or [])
    current_temp = _as_float(attrs.get(ATTR_CURRENT_TEMPERATURE))
    min_t = _as_float(attrs.get(ATTR_MIN_TEMP)) or _DEFAULT_MIN_TEMP
    max_t = _as_float(attrs.get(ATTR_MAX_TEMP)) or _DEFAULT_MAX_TEMP
    step = _as_float(attrs.get(ATTR_TARGET_TEMP_STEP)) or _DEFAULT_STEP

    # ── mode change ─────────────────────────────────────────
    if kind == "mode":
        if hvac_mode not in modes:
            return f"{name} doesn't support {str(hvac_mode).replace('_', ' ')} mode."
        await hass.services.async_call(
            CLIMATE_DOMAIN, SERVICE_SET_HVAC_MODE,
            {ATTR_ENTITY_ID: entity_id, "hvac_mode": hvac_mode}, blocking=True,
        )
        return f"Set {name} to {str(hvac_mode).replace('_', ' ')} mode."

    # ── preset change ───────────────────────────────────────
    if kind == "preset":
        presets = list(attrs.get(ATTR_PRESET_MODES) or [])
        match = _match_choice(preset, presets)
        if match is None:
            return f"{name} doesn't have a {preset} preset."
        await hass.services.async_call(
            CLIMATE_DOMAIN, SERVICE_SET_PRESET_MODE,
            {ATTR_ENTITY_ID: entity_id, ATTR_PRESET_MODE: match}, blocking=True,
        )
        return f"Set {name} to {match}."

    req = {
        "kind": kind, "temperature": temperature, "amount": amount,
        "direction": direction, "hvac_mode": hvac_mode, "preset": preset,
    }
    area_id = _entity_area(hass, entity_id)
    mode = state.state

    # ── off handling ────────────────────────────────────────
    if mode == HVACMode.OFF:
        auto_on = manager.effective_settings().get("climate_auto_on", False) is True
        if not force_on and not auto_on:
            return await _prompt_turn_on(hass, manager, entity_id, name, area_id, req)
        target_hint = temperature
        if target_hint is None and kind == "relative" and current_temp is not None:
            target_hint = current_temp + (amount or 0) * (1 if direction == "warmer" else -1)
        on_mode = _choose_on_mode(modes, target_hint, current_temp)
        if on_mode is None:
            return f"I couldn't turn {name} on."
        if features & ClimateEntityFeature.TURN_ON:
            await hass.services.async_call(
                CLIMATE_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: entity_id}, blocking=True,
            )
        await hass.services.async_call(
            CLIMATE_DOMAIN, SERVICE_SET_HVAC_MODE,
            {ATTR_ENTITY_ID: entity_id, "hvac_mode": on_mode}, blocking=True,
        )
        mode = on_mode
        # Re-read setpoints after turning on so relative math uses live values.
        state = hass.states.get(entity_id) or state
        attrs = state.attributes

    # ── relative with no amount -> prompt ───────────────────
    if kind == "relative" and amount is None:
        return await _prompt_amount(
            hass, manager, entity_id, name, area_id, direction or "warmer", req
        )

    is_range = bool(features & ClimateEntityFeature.TARGET_TEMPERATURE_RANGE) and (
        mode in _ON_RANGE_MODES
    )

    # ── range (heat_cool / auto) ────────────────────────────
    if is_range:
        min_delta = _as_float(manager.effective_settings().get("climate_min_delta")) or 5.0
        low = _as_float(attrs.get(ATTR_TARGET_TEMP_LOW))
        high = _as_float(attrs.get(ATTR_TARGET_TEMP_HIGH))
        if kind == "relative":
            if low is None or high is None:
                return f"I couldn't read {name}'s current setpoints."
            delta = (amount or 0) * (1 if direction == "warmer" else -1)
            low += delta
            high += delta
        else:
            if temperature is None:
                return "What temperature would you like?"
            outside = _outside_temp(hass, manager)
            low, high = _compute_band(temperature, current_temp, outside, min_delta)
        # Enforce the minimum delta, clamp, and round to the entity's step.
        if high - low < min_delta:
            center = (high + low) / 2.0
            low = center - min_delta / 2.0
            high = center + min_delta / 2.0
        low = _round_step(_clamp(low, min_t, max_t), step)
        high = _round_step(_clamp(high, min_t, max_t), step)
        if high - low < min_delta:
            high = _round_step(_clamp(low + min_delta, min_t, max_t), step)
        await hass.services.async_call(
            CLIMATE_DOMAIN, SERVICE_SET_TEMPERATURE,
            {
                ATTR_ENTITY_ID: entity_id,
                ATTR_TARGET_TEMP_LOW: low,
                ATTR_TARGET_TEMP_HIGH: high,
            },
            blocking=True,
        )
        return (
            f"Set {name} to heat to {_fmt_temp(low)} and cool to {_fmt_temp(high)} degrees."
        )

    # ── single setpoint (heat / cool) ───────────────────────
    if kind == "relative":
        cur_target = _as_float(attrs.get(ATTR_TEMPERATURE))
        if cur_target is None:
            return f"I couldn't read {name}'s current temperature setting."
        target = cur_target + (amount or 0) * (1 if direction == "warmer" else -1)
    else:
        if temperature is None:
            return "What temperature would you like?"
        target = temperature
    target = _round_step(_clamp(target, min_t, max_t), step)
    await hass.services.async_call(
        CLIMATE_DOMAIN, SERVICE_SET_TEMPERATURE,
        {ATTR_ENTITY_ID: entity_id, ATTR_TEMPERATURE: target}, blocking=True,
    )
    return f"Set {name} to {_fmt_temp(target)} degrees."


def _match_choice(spoken: str | None, choices: list[str]) -> str | None:
    """Case-insensitive match of a spoken word against a list of option strings."""
    if not spoken:
        return None
    wanted = spoken.strip().casefold()
    for choice in choices:
        if str(choice).casefold() == wanted:
            return choice
    for choice in choices:
        if wanted in str(choice).casefold():
            return choice
    return None
