"""Locally-served Room Card header photos.

The Room Card ships a curated set of room header photos. Rather than have every
browser fetch them from a public CDN on every dashboard load (a third-party
request, with a referrer, per card per load), they are bundled with this
integration and served locally.

Two directories are merged:
  * bundled    -- shipped in this folder; re-extracted on every update.
  * downloaded -- config-root fallback, used only if the bundle is missing
                  (see download()). MUST live outside the integration folder,
                  which HACS replaces wholesale on update.
Bundled wins on a filename collision: it is version-matched to this release.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from urllib.parse import quote

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)

DIRNAME = "room_photos"
BUNDLED_URL = "/teds_dashboard_system/room_photos"
DOWNLOAD_URL = "/teds_dashboard_system/room_photos_dl"

# Fallback CDN source, used ONLY when the bundled folder is unusable.
# MUST point at a tag containing the .webp assets (see spec) and stay in sync
# with PHOTO_CDN_BASE in Teds-Cards/src/cards/room-card/const.ts.
_CDN_BASE = (
    "https://cdn.jsdelivr.net/gh/tedr91/Teds-Cards@v0.9.115/images/room-header-photos/"
)

_ALLOWED_EXTS = (".jpg", ".jpeg", ".png", ".webp")
_MAX_FILES = 64                  # sanity ceiling on one request
_MAX_BYTES = 12 * 1024 * 1024    # per-file cap; largest real photo is 784 KB
_TIMEOUT = 30                    # seconds per file
_CONCURRENCY = 4

# Reject anything that isn't a plain filename: no separators, no traversal, no
# control characters. Spaces and parens ARE legal ("Bathroom Alt 2.webp").
_SAFE_NAME = re.compile(r"^[A-Za-z0-9 ()._-]{1,120}$")

_data_dir: str | None = None
_lock = asyncio.Lock()


def set_data_dir(path: str | None) -> None:
    """Point the download fallback at the config-root data dir (survives updates)."""
    global _data_dir
    _data_dir = path


def bundled_dir() -> str:
    return os.path.join(os.path.dirname(__file__), DIRNAME)


def download_dir() -> str | None:
    return os.path.join(_data_dir, DIRNAME) if _data_dir else None


def _is_safe(name: str) -> bool:
    if not _SAFE_NAME.match(name or ""):
        return False
    if name != os.path.basename(name) or name in (".", ".."):
        return False
    return name.lower().endswith(_ALLOWED_EXTS)


def _list(path: str | None, url: str) -> dict[str, str]:
    if not path:
        return {}
    try:
        names = sorted(os.listdir(path))
    except OSError:
        return {}
    return {n: f"{url}/{quote(n)}" for n in names if _is_safe(n)}


def scan() -> dict[str, str]:
    """Map of available filename -> served URL (blocking I/O).

    Downloaded first, bundled second so the bundled copy wins on collision.
    """
    out = _list(download_dir(), DOWNLOAD_URL)
    out.update(_list(bundled_dir(), BUNDLED_URL))
    return out


async def download(hass: HomeAssistant, names: list[str]) -> dict:
    """Fetch missing photos from the pinned CDN into the config-root fallback dir.

    Only needed when the bundled folder is missing/incomplete. Returns
    {"downloaded": int, "skipped": int, "failed": [name, ...],
     "photos": {name: url}}. Idempotent -- present files are skipped.
    """
    path = download_dir()
    if not path:
        return {"downloaded": 0, "skipped": 0, "failed": list(names),
                "photos": scan(), "error": "no_data_dir"}

    wanted = [n for n in dict.fromkeys(names) if _is_safe(n)][:_MAX_FILES]
    if not wanted:
        return {"downloaded": 0, "skipped": 0, "failed": [], "photos": scan()}

    # Serialise: two dashboards clicking the button at once must not interleave.
    async with _lock:
        await hass.async_add_executor_job(lambda: os.makedirs(path, exist_ok=True))

        have = await hass.async_add_executor_job(scan)
        todo = [n for n in wanted if n not in have]
        skipped = len(wanted) - len(todo)

        session = async_get_clientsession(hass)
        sem = asyncio.Semaphore(_CONCURRENCY)
        failed: list[str] = []

        async def _one(name: str) -> None:
            async with sem:
                url = _CDN_BASE + quote(name)
                try:
                    async with session.get(url, timeout=_TIMEOUT) as resp:
                        if resp.status != 200:
                            _LOGGER.warning("Room photo %s: HTTP %s", name, resp.status)
                            failed.append(name)
                            return
                        blob = await resp.content.read(_MAX_BYTES + 1)
                        if not blob or len(blob) > _MAX_BYTES:
                            _LOGGER.warning("Room photo %s: bad size", name)
                            failed.append(name)
                            return
                except Exception as err:  # noqa: BLE001 - network is best-effort
                    _LOGGER.warning("Room photo %s failed: %s", name, err)
                    failed.append(name)
                    return

                # Atomic: never leave a half-written file that scan() reports
                # as present.
                final = os.path.join(path, name)
                tmp = final + ".part"

                def _write() -> None:
                    with open(tmp, "wb") as fh:
                        fh.write(blob)
                    os.replace(tmp, final)

                try:
                    await hass.async_add_executor_job(_write)
                except OSError as err:
                    _LOGGER.warning("Room photo %s write failed: %s", name, err)
                    failed.append(name)

        await asyncio.gather(*(_one(n) for n in todo))

        return {
            "downloaded": len(todo) - len(failed),
            "skipped": skipped,
            "failed": failed,
            "photos": await hass.async_add_executor_job(scan),
        }
