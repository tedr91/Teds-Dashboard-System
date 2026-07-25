"""Bing "Photo of the Day" wallpaper source for Ted's Dashboard System.

Downloads Bing's daily images into an isolated ``backgrounds/bing_pod/`` cache
(kept separate from the bundled Built-in wallpapers) and serves them from the
existing static path, so the frontend can analyse their luminance (Mood
matching / Readability scrim) without cross-origin canvas tainting.

The cache accumulates over time up to a configurable cap
(``background_bing_cache_size``, default 100); the oldest images are pruned
beyond it. A small ``index.json`` sidecar persists each day's title/copyright so
attribution survives restarts even for days no longer in Bing's 8-day archive.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
from urllib.parse import urlparse

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.network import NoURLAvailableError, get_url

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

_BING_HOST = "https://www.bing.com"
_BING_ARCHIVE = "/HPImageArchive.aspx"
_CACHE_DIRNAME = "bing_pod"
_FAVORITES_DIRNAME = "favorites"
_STORED_DIRNAME = "stored"
_INDEX_NAME = "index.json"
_REMOVED_NAME = "removed.json"
_URL_BASE = f"/teds_dashboard_system/backgrounds/{_CACHE_DIRNAME}"
_DEFAULT_CACHE_SIZE = 100
_FETCH_DAYS = 8  # Bing's archive exposes up to the last 8 days.
_RESOLUTIONS = ("_UHD.jpg", "_1920x1080.jpg")  # try UHD first, then 1080p.


def _cache_dir() -> str:
    return os.path.join(os.path.dirname(__file__), "backgrounds", _CACHE_DIRNAME)


def _index_path() -> str:
    return os.path.join(_cache_dir(), _INDEX_NAME)


def _removed_path() -> str:
    return os.path.join(_cache_dir(), _REMOVED_NAME)


def _bing_mkt(hass: HomeAssistant) -> str:
    """Derive Bing's market (mkt) code from HA's configured locale."""
    lang = (getattr(hass.config, "language", None) or "en").split("-")[0]
    country = getattr(hass.config, "country", None)
    if country:
        return f"{lang}-{country}"
    return "en-US"


def _cache_size(hass: HomeAssistant) -> int:
    """The effective (global) cache cap, clamped to at least 1."""
    mgr = next(iter((hass.data.get(DOMAIN) or {}).values()), None)
    if mgr is None:
        return _DEFAULT_CACHE_SIZE
    try:
        val = int(mgr.effective_settings().get("background_bing_cache_size", _DEFAULT_CACHE_SIZE))
    except (TypeError, ValueError):
        return _DEFAULT_CACHE_SIZE
    return max(1, val)


# ── blocking file helpers (run in the executor) ───────────────────────────────
def _ensure_dir() -> None:
    os.makedirs(_cache_dir(), exist_ok=True)


def _write_file(dest: str, content: bytes) -> None:
    with open(dest, "wb") as fh:
        fh.write(content)


def _load_index() -> dict:
    try:
        with open(_index_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_index(index: dict) -> None:
    try:
        with open(_index_path(), "w", encoding="utf-8") as fh:
            json.dump(index, fh)
    except OSError:
        pass


def _load_removed() -> set[str]:
    """The set of startdates the user explicitly removed (so they're never
    re-downloaded by the 8-day fetch, on any device)."""
    try:
        with open(_removed_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        return {str(x) for x in data} if isinstance(data, list) else set()
    except (OSError, ValueError):
        return set()


def _save_removed(removed: set[str]) -> None:
    try:
        with open(_removed_path(), "w", encoding="utf-8") as fh:
            json.dump(sorted(removed), fh)
    except OSError:
        pass


def cache_has_images() -> bool:
    """True when the Bing cache already holds at least one image (blocking)."""
    try:
        return any(n.lower().endswith(".jpg") for n in os.listdir(_cache_dir()))
    except OSError:
        return False


def _reconcile_and_prune(index: dict, cap: int) -> list[dict]:
    """Reconcile the metadata index with the files on disk, prune to ``cap``,
    and return the kept entries newest-first (blocking)."""
    directory = _cache_dir()
    try:
        on_disk = {
            os.path.splitext(name)[0]
            for name in os.listdir(directory)
            if name.lower().endswith(".jpg")
        }
    except OSError:
        on_disk = set()

    # Drop index entries whose file is gone; add bare entries for orphan files.
    index = {k: v for k, v in index.items() if k in on_disk}
    for startdate in on_disk:
        if startdate not in index:
            index[startdate] = {
                "url": f"{_URL_BASE}/{startdate}.jpg",
                "title": "",
                "copyright": "",
                "startdate": startdate,
            }

    # Newest first (YYYYMMDD sorts lexicographically).
    ordered = sorted(index.values(), key=lambda e: e["startdate"], reverse=True)
    cap = max(1, cap)
    keep, prune = ordered[:cap], ordered[cap:]
    for entry in prune:
        try:
            os.remove(os.path.join(directory, f"{entry['startdate']}.jpg"))
        except OSError:
            pass
    _save_index({e["startdate"]: e for e in keep})
    return keep


def _clear_cache_files() -> None:
    directory = _cache_dir()
    try:
        names = os.listdir(directory)
    except OSError:
        return
    for name in names:
        # Keep the blocklist so removed photos stay gone even after a cache clear.
        if name == _REMOVED_NAME:
            continue
        try:
            os.remove(os.path.join(directory, name))
        except OSError:
            pass


# ── network ───────────────────────────────────────────────────────────────────
async def _download_image(
    session: aiohttp.ClientSession, urlbase: str, dest: str, hass: HomeAssistant
) -> bool:
    """Download a day's image, trying UHD then 1080p. Returns True on success."""
    for suffix in _RESOLUTIONS:
        url = f"{_BING_HOST}{urlbase}{suffix}"
        try:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status != 200:
                    continue
                content = await resp.read()
        except Exception as err:  # noqa: BLE001 - best-effort per resolution
            _LOGGER.debug("Bing PoD download failed for %s (%s)", url, err)
            continue
        try:
            await hass.async_add_executor_job(_write_file, dest, content)
            return True
        except OSError as err:
            _LOGGER.debug("Bing PoD write failed for %s (%s)", dest, err)
            return False
    return False


async def fetch_and_cache_bing(hass: HomeAssistant) -> list[dict]:
    """Ensure recent Bing images are cached, prune to the cap, and return their
    metadata newest-first as ``[{url, title, copyright, startdate}, ...]``.

    Best-effort: on any network error, returns whatever is already cached.
    """
    session = async_get_clientsession(hass)
    mkt = _bing_mkt(hass)
    index = await hass.async_add_executor_job(_load_index)
    removed = await hass.async_add_executor_job(_load_removed)

    try:
        params = {"format": "js", "idx": "0", "n": str(_FETCH_DAYS), "mkt": mkt}
        async with session.get(
            f"{_BING_HOST}{_BING_ARCHIVE}",
            params=params,
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)
        images = data.get("images") or []
    except Exception as err:  # noqa: BLE001 - offline / Bing hiccup: use cache
        _LOGGER.debug("Bing PoD archive fetch failed (%s); using cached images", err)
        images = []

    await hass.async_add_executor_job(_ensure_dir)

    fetched_dates: list[str] = []
    for img in images:
        startdate = str(img.get("startdate") or "").strip()
        urlbase = str(img.get("urlbase") or "").strip()
        if not startdate or not urlbase:
            continue
        fetched_dates.append(startdate)
        # Skip images the user explicitly removed — never re-download them.
        if startdate in removed:
            continue
        filename = f"{startdate}.jpg"
        dest = os.path.join(_cache_dir(), filename)
        if not await hass.async_add_executor_job(os.path.exists, dest):
            if not await _download_image(session, urlbase, dest, hass):
                continue
        index[startdate] = {
            "url": f"{_URL_BASE}/{filename}",
            "title": str(img.get("title") or "").strip(),
            "copyright": str(img.get("copyright") or "").strip(),
            "startdate": startdate,
        }

    # Bound the blocklist: drop entries older than Bing's current 8-day window
    # (Bing won't serve them again, so they can't reappear anyway).
    if fetched_dates:
        oldest = min(fetched_dates)
        pruned = {d for d in removed if d >= oldest}
        if pruned != removed:
            await hass.async_add_executor_job(_save_removed, pruned)

    return await hass.async_add_executor_job(_reconcile_and_prune, index, _cache_size(hass))


async def clear_bing_cache(hass: HomeAssistant) -> None:
    """Delete every cached Bing image and the metadata sidecar."""
    await hass.async_add_executor_job(_clear_cache_files)


# ── favorite / remove a single photo ──────────────────────────────────────────
def _favorites_dir() -> str:
    return os.path.join(os.path.dirname(__file__), "backgrounds", _FAVORITES_DIRNAME)


def _safe_bing_name(name: str | None) -> str | None:
    """Validate a cached-photo filename (guards against path traversal). The cache
    stores strictly ``<startdate>.jpg`` (all-digit stem), so accept only that shape."""
    base = os.path.basename(name or "")
    if not base.lower().endswith(".jpg"):
        return None
    stem = base[:-4]
    if not stem.isdigit():
        return None
    return base


def _do_favorite(name: str) -> bool:
    src = os.path.join(_cache_dir(), name)
    if not os.path.isfile(src):
        return False
    try:
        os.makedirs(_favorites_dir(), exist_ok=True)
        shutil.copyfile(src, os.path.join(_favorites_dir(), name))
        return True
    except OSError:
        return False


def _do_remove(name: str) -> bool:
    removed = False
    try:
        os.remove(os.path.join(_cache_dir(), name))
        removed = True
    except OSError:
        removed = False
    stem = name[:-4]
    index = _load_index()
    if stem in index:
        del index[stem]
        _save_index(index)
    # Blocklist it so the 8-day fetch never re-downloads it (on any device).
    blocked = _load_removed()
    if stem not in blocked:
        blocked.add(stem)
        _save_removed(blocked)
    return removed


async def favorite_bing_photo(hass: HomeAssistant, name: str) -> bool:
    """Copy a cached Bing image into the ``favorites`` folder (for a future album)."""
    safe = _safe_bing_name(name)
    if not safe:
        return False
    return await hass.async_add_executor_job(_do_favorite, safe)


async def remove_bing_photo(hass: HomeAssistant, name: str) -> bool:
    """Delete a single cached Bing image and drop it from the metadata index."""
    safe = _safe_bing_name(name)
    if not safe:
        return False
    return await hass.async_add_executor_job(_do_remove, safe)


# ── import an arbitrary photo (favorite / set-as-background) ───────────────────
_IMPORT_EXTS = (".webp", ".jpg", ".jpeg", ".png", ".gif", ".avif")
_CT_EXT = {
    "image/webp": ".webp",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/avif": ".avif",
}


def _stored_dir() -> str:
    return os.path.join(os.path.dirname(__file__), "backgrounds", _STORED_DIRNAME)


def _import_dirname(dest: str) -> str:
    return _STORED_DIRNAME if dest == "stored" else _FAVORITES_DIRNAME


def _import_ext(url: str, content_type: str | None) -> str:
    """Pick a file extension from the response Content-Type, else the URL path."""
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in _CT_EXT:
        return _CT_EXT[ct]
    _, ext = os.path.splitext(urlparse(url).path.lower())
    if ext == ".jpeg":
        return ".jpg"
    if ext in _IMPORT_EXTS:
        return ext
    return ".jpg"


def _readable_stem(url: str) -> str:
    """A short, filesystem-safe name derived from the source URL's basename
    (e.g. ".../oar2.jpg?x=1" -> "oar2"). Falls back to "photo"."""
    base = os.path.basename(urlparse(url).path)
    stem = os.path.splitext(base)[0]
    cleaned = "".join(c if (c.isalnum() or c in "-_") else "-" for c in stem)
    cleaned = "-".join(part for part in cleaned.split("-") if part).strip("-_")
    return cleaned[:40] or "photo"


def _write_import(dirname: str, stem: str, short: str, ext: str, content: bytes) -> str | None:
    """Write ``content`` into backgrounds/<dirname>/<stem>-<short><ext>. Deduped by
    the ``short`` content hash: if any file already ends with ``-<short><ext>`` (any
    readable prefix), reuse it. Blocking — run in the executor."""
    dest_dir = os.path.join(os.path.dirname(__file__), "backgrounds", dirname)
    base = f"/teds_dashboard_system/backgrounds/{dirname}"
    suffix = f"-{short}{ext}"
    try:
        os.makedirs(dest_dir, exist_ok=True)
        for existing in os.listdir(dest_dir):
            if existing.endswith(suffix):
                return f"{base}/{existing}"
        filename = f"{stem}{suffix}"
        with open(os.path.join(dest_dir, filename), "wb") as fh:
            fh.write(content)
        return f"{base}/{filename}"
    except OSError:
        return None


async def import_photo(hass: HomeAssistant, ref: str, dest: str = "favorites") -> str | None:
    """Download the image at ``ref`` and store it under backgrounds/favorites (or
    backgrounds/stored when ``dest == 'stored'``), named ``<name>-<shorthash><ext>``
    and deduped by the content hash.

    ``ref`` may be an absolute http(s) URL or a HA-relative path (e.g. a resolved
    media-source or our own served wallpaper). Returns the served URL, or None.
    """
    if not ref or not isinstance(ref, str):
        return None
    url = ref
    if url.startswith("/"):
        try:
            base = get_url(hass, prefer_external=False)
        except NoURLAvailableError:
            base = None
        if not base:
            return None
        url = base.rstrip("/") + url
    elif not url.startswith(("http://", "https://")):
        return None

    session = async_get_clientsession(hass)
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                return None
            content = await resp.read()
            content_type = resp.headers.get("Content-Type")
    except (aiohttp.ClientError, TimeoutError):
        return None
    if not content:
        return None

    # SHA-1 is used purely to dedupe identical images (not for security).
    short = hashlib.sha1(content, usedforsecurity=False).hexdigest()[:10]
    return await hass.async_add_executor_job(
        _write_import,
        _import_dirname(dest),
        _readable_stem(url),
        short,
        _import_ext(url, content_type),
        content,
    )


def _list_favorites() -> list[str]:
    try:
        names = sorted(os.listdir(_favorites_dir()))
    except OSError:
        return []
    base = f"/teds_dashboard_system/backgrounds/{_FAVORITES_DIRNAME}"
    return [f"{base}/{n}" for n in names if n.lower().endswith(_IMPORT_EXTS)]


async def list_favorites(hass: HomeAssistant) -> list[str]:
    """Return the served URLs of every favorited photo (newest-last)."""
    return await hass.async_add_executor_job(_list_favorites)

