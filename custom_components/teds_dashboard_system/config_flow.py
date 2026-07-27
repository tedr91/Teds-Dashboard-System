"""Config flow for Ted's Dashboard System."""

from __future__ import annotations

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback

from .const import (
    CONF_DASHBOARD_BRANCH,
    CONF_DASHBOARD_REPO,
    DEFAULT_DASHBOARD_BRANCH,
    DEFAULT_DASHBOARD_REPO,
    DOMAIN,
)


class TedsBackendConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Single-instance config flow — nothing to configure at add time."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        if user_input is not None:
            return self.async_create_entry(title="Ted's Dashboard System", data={})
        return self.async_show_form(step_id="user")

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Return the options flow (advanced content-source overrides)."""
        return TedsDashboardOptionsFlow()


def _options_schema(options) -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional(
                CONF_DASHBOARD_REPO,
                default=options.get(CONF_DASHBOARD_REPO, DEFAULT_DASHBOARD_REPO),
            ): str,
            vol.Optional(
                CONF_DASHBOARD_BRANCH,
                default=options.get(CONF_DASHBOARD_BRANCH, DEFAULT_DASHBOARD_BRANCH),
            ): str,
        }
    )


class TedsDashboardOptionsFlow(config_entries.OptionsFlow):
    """Let the user override the Ted's Dashboard content repo/branch."""

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(
                data={
                    CONF_DASHBOARD_REPO: user_input[CONF_DASHBOARD_REPO].strip(),
                    CONF_DASHBOARD_BRANCH: user_input[CONF_DASHBOARD_BRANCH].strip(),
                }
            )
        return self.async_show_form(
            step_id="init", data_schema=_options_schema(self.config_entry.options)
        )
