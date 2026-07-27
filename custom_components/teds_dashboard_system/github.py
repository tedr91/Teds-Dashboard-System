"""GitHub download client for the Ted's Dashboard System installer.

Fetches files, directories, ``versions.json`` manifests, and release metadata
from public GitHub repositories (the Ted's Dashboard content repo, and the
Ted's Cards / Ted's Themes release assets). Uses the REST contents API to list
directories and ``raw.githubusercontent.com`` to fetch file bytes.

If HACS is installed, its personal-access token is reused to lift the
unauthenticated rate limit; otherwise anonymous requests are used.
"""

from __future__ import annotations

import logging
import urllib.parse
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)

# Guard against pathological recursion when mirroring a repo directory tree.
MAX_DIR_DEPTH = 6
_TIMEOUT = 30


class GitHubError(Exception):
    """Base error for GitHub client failures."""


class GitHubNotFound(GitHubError):
    """Raised when a requested path or release does not exist (HTTP 404)."""


class GitHubClient:
    """Download files and directories from a public GitHub repo."""

    def __init__(self, hass: HomeAssistant, repo: str, branch: str = "main") -> None:
        """Initialise the client for ``owner/repo`` on ``branch``."""
        self.hass = hass
        self.repo = repo.strip().strip("/")
        self.branch = branch.strip()
        self._api = f"https://api.github.com/repos/{self.repo}"
        self._raw = f"https://raw.githubusercontent.com/{self.repo}/{self.branch}"

    # -- low-level helpers --------------------------------------------------

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/vnd.github+json"}
        if token := self._token():
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _token(self) -> str | None:
        """Reuse a HACS personal access token, if available, to lift limits."""
        if hacs := self.hass.data.get("hacs"):
            try:
                return hacs.configuration.token
            except AttributeError:
                return None
        return None

    async def _get_json(self, url: str) -> Any:
        session = async_get_clientsession(self.hass)
        async with session.get(
            url, headers=self._headers(), timeout=_TIMEOUT
        ) as resp:
            if resp.status == 404:
                raise GitHubNotFound(url)
            if resp.status != 200:
                raise GitHubError(f"GET {url} returned HTTP {resp.status}")
            return await resp.json()

    async def async_get_bytes(self, repo_path: str) -> bytes:
        """Return the raw bytes of a file at ``repo_path`` in the repo."""
        url = self.raw_url(repo_path)
        session = async_get_clientsession(self.hass)
        async with session.get(url, timeout=_TIMEOUT) as resp:
            if resp.status == 404:
                raise GitHubNotFound(url)
            if resp.status != 200:
                raise GitHubError(f"GET {url} returned HTTP {resp.status}")
            return await resp.read()

    async def async_get_text(self, repo_path: str) -> str:
        """Return the UTF-8 text of a file at ``repo_path``."""
        return (await self.async_get_bytes(repo_path)).decode("utf-8")

    def raw_url(self, path: str) -> str:
        """Return the ``raw.githubusercontent.com`` URL for a repo path."""
        return f"{self._raw}/{urllib.parse.quote(path)}"

    # -- directory listing / mirroring -------------------------------------

    async def async_list_dir(self, repo_path: str) -> list[dict[str, Any]]:
        """Return the contents-API entries for a repo directory."""
        url = f"{self._api}/contents/{urllib.parse.quote(repo_path)}?ref={self.branch}"
        data = await self._get_json(url)
        if not isinstance(data, list):
            raise GitHubError(f"{repo_path} is not a directory")
        return data

    async def async_download_dir(
        self, repo_path: str, dest: Path, depth: int = 1
    ) -> int:
        """Recursively download a repo directory into ``dest``.

        Returns the number of files written.
        """
        count = 0
        for entry in await self.async_list_dir(repo_path):
            name = entry["name"]
            if entry["type"] == "dir":
                if depth >= MAX_DIR_DEPTH:
                    continue
                count += await self.async_download_dir(
                    entry["path"], dest / name, depth + 1
                )
            elif entry["type"] == "file":
                data = await self.async_get_bytes(entry["path"])
                await self.hass.async_add_executor_job(
                    self._write_file, dest / name, data
                )
                count += 1
        return count

    @staticmethod
    def _write_file(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    # -- release metadata ---------------------------------------------------

    async def async_latest_release(self) -> tuple[str, str] | None:
        """Return ``(tag, body)`` of the latest published release, or None."""
        try:
            data = await self._get_json(f"{self._api}/releases/latest")
        except GitHubNotFound:
            return None
        tag = data.get("tag_name")
        if not tag:
            return None
        return tag, data.get("body") or ""
