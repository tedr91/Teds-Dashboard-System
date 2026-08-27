"""Prune-candidate reporting for the TDS device registry.

`register_device()` records a dashboard client (device_id -> area/name/media_player/
last_seen) and its per-device settings overrides, but nothing ever removes them. Browser
Mod IDs live in origin-scoped `localStorage`, so changing Home Assistant's port, scheme,
or host makes every browser re-register under a brand-new random ID and strands the old
entries permanently. Left alone they accumulate forever and bloat the settings snapshot.

**Reporting only.** Nothing here deletes: it classifies entries and hands back enough
context for a human to judge each one. Availability is deliberately NOT treated as
evidence of an orphan — a sleeping phone or a locked tablet reads exactly like a dead
registration, so an automatic reaper keyed on presence or a last-seen timeout would
destroy live devices' settings and area assignments. Removal happens only against an
explicitly supplied list of IDs (see `TedsManager.prune_devices`).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

# TDS keys a dashboard client as "bm:<browser_mod browser id>" (see `calendar_scope`).
BROWSER_ID_PREFIX = "bm:"

# Browser Mod's own auto-generated browser IDs, which a human never chose and which are
# therefore the likeliest orphans after an origin change. Two formats exist: the modern
# underscore form and a legacy dash form (which slugifies to underscores in entity IDs).
_AUTO_ID_PATTERNS = (
    re.compile(r"^browser_mod_[0-9a-f]+_[0-9a-f]+$", re.IGNORECASE),
    re.compile(r"^[0-9a-f]{8}-[0-9a-f]{8}$", re.IGNORECASE),
)

# Sort sentinel for entries that have never recorded a `last_seen`.
_NEVER_SEEN = float("inf")


def browser_id(device_id: Any) -> str | None:
    """The Browser Mod browser ID behind a TDS device key, if it is one."""
    if not isinstance(device_id, str) or not device_id.startswith(BROWSER_ID_PREFIX):
        return None
    return device_id[len(BROWSER_ID_PREFIX):] or None


def is_auto_browser_id(device_id: Any) -> bool:
    """True for a Browser Mod auto-generated (never human-named) browser ID."""
    bid = browser_id(device_id)
    if not bid:
        return False
    return any(pattern.match(bid) for pattern in _AUTO_ID_PATTERNS)


def _age_seconds(last_seen: Any, now: datetime) -> float | None:
    """Seconds since `last_seen` (an ISO timestamp), or None when unusable."""
    if not isinstance(last_seen, str) or not last_seen:
        return None
    try:
        parsed = datetime.fromisoformat(last_seen)
    except ValueError:
        return None
    if parsed.tzinfo is None and now.tzinfo is not None:
        parsed = parsed.replace(tzinfo=now.tzinfo)
    elif parsed.tzinfo is not None and now.tzinfo is None:
        parsed = parsed.replace(tzinfo=None)
    try:
        return (now - parsed).total_seconds()
    except TypeError:
        return None


def _approx_bytes(*payloads: Any) -> int:
    """Rough serialized size of the given fragments, for reporting payload cost."""
    total = 0
    for payload in payloads:
        try:
            total += len(json.dumps(payload, default=str))
        except (TypeError, ValueError):
            total += 0
    return total


def candidate_rows(
    registry: Mapping[str, Mapping[str, Any]],
    device_settings: Mapping[str, Mapping[str, Any]],
    live_ids: Iterable[str],
    now: datetime,
    ttl: float,
) -> list[dict[str, Any]]:
    """One report row per registered device, most-likely-orphan first.

    `live_ids` are the TDS device keys ("bm:<id>") that currently exist as real devices
    in Home Assistant's registry. `device_exists` is None for keys we cannot check that
    way (anything not in the "bm:" namespace), so an unknown never reads as an orphan.
    """
    live = {device_id for device_id in live_ids if isinstance(device_id, str)}
    registry = registry or {}
    device_settings = device_settings or {}
    # Union of both structures: an entry that survives only in the settings overrides is
    # still prunable, so it has to be reportable too.
    device_ids = list(registry) + [d for d in device_settings if d not in registry]
    rows: list[dict[str, Any]] = []
    for device_id in device_ids:
        raw_entry = registry.get(device_id)
        entry = raw_entry if isinstance(raw_entry, Mapping) else {}
        overrides = device_settings.get(device_id) or {}
        age = _age_seconds(entry.get("last_seen"), now)
        present = age is not None and age <= ttl
        exists: bool | None = (device_id in live) if browser_id(device_id) else None
        auto_id = is_auto_browser_id(device_id)
        rows.append({
            "device_id": device_id,
            "name": entry.get("name"),
            "area": entry.get("area"),
            "media_player": entry.get("media_player"),
            "last_seen": entry.get("last_seen"),
            "last_seen_seconds_ago": None if age is None else int(age),
            "present": present,
            "device_exists": exists,
            "auto_id": auto_id,
            "override_keys": len(overrides),
            "approx_bytes": _approx_bytes(entry, overrides),
            # Advisory only — an operator still has to confirm each ID before pruning.
            "likely_orphan": auto_id and exists is False and not present,
        })
    rows.sort(
        key=lambda row: (
            not row["likely_orphan"],
            row["present"],
            row["device_exists"] is not False,
            -(_NEVER_SEEN if row["last_seen_seconds_ago"] is None
              else row["last_seen_seconds_ago"]),
            row["device_id"],
        )
    )
    return rows


def summarize(rows: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """Headline counts/bytes for a candidate list."""
    rows = list(rows)
    orphans = [row for row in rows if row.get("likely_orphan")]
    return {
        "total": len(rows),
        "likely_orphans": len(orphans),
        "likely_orphan_bytes": sum(int(row.get("approx_bytes") or 0) for row in orphans),
        "total_bytes": sum(int(row.get("approx_bytes") or 0) for row in rows),
    }
