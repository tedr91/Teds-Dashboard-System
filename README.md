# Ted's Dashboard System

A Home Assistant integration that installs and serves the whole **Ted's Dashboard** ecosystem from a single HACS install — and backs the interactive cards server-side.

## What it does

- **Serves Ted's Cards** — bundles and auto-loads `ted-cards.js` (via `add_extra_js_url`); automatically defers if you already have a standalone **Ted's Cards** HACS install (detected in `www/community` or as a Lovelace resource).
- **Installs Ted's Themes** into `<config>/themes/` — never overwriting your own or a HACS-installed theme file.
- **Installs & registers the generic Ted's Dashboard** as a yaml-mode dashboard at `/ted-dashboard` (no `configuration.yaml` edit needed), with a **user override layer** (`dashboards/ted-dashboard-user/`) so your customizations survive updates.
- **Auto-updates** the dashboard content from GitHub (an Update entity + optional auto-update).
- **Backs the cards server-side** — alarms, timers, notifications, announcements, Assist-Response, per-device settings, and media/sound/background serving.

## Install

1. Add this repo to HACS as a custom repository (category **Integration**) and install it.
2. Restart Home Assistant.
3. Add **Ted's Dashboard System** from Settings → Devices & Services.

The **Ted's Dashboard** panel then appears in the sidebar. Manage its views from **Ted's Settings → Dashboards → Dashboard views** (customize / revert / add / remove / hide / reorder, with upstream-drift badges).

## Prerequisites (front-end plugins)

The integration serves the cards + themes itself, but the shipped **Ted's Dashboard** views use a few third-party front-end plugins. Install these via HACS for the full experience — the Welcome view and `sensor.teds_requirements` flag any that are missing:

- **Browser Mod** (`thomasloven/hass-browser_mod`) — device registration, the Refresh action, per-browser screen light (night mode), and the native kiosk toggle.
- **Layout Card** (`thomasloven/lovelace-layout-card`) — the `custom:grid-layout` used by every view.
- **card-mod** (`thomasloven/lovelace-card-mod`) — styling on a few views.
- **Custom Icons** — an icon pack so Ted's icons can render as Streamline / Fluent / Pepicons (they fall back to built-in MDI when absent).
- **Daylight Calendar Card** (`superdingo101/daylight-calendar-card`) — the Calendar views.
- Per-view extras: **yet-another-media-player** and **Music Assistant** (+ `mass_queue`) for the Music view; **clock-weather-card** and a weather-radar card for the Weather view.

Kiosk mode uses Home Assistant's **built-in** kiosk (2026.1+), driven per-device from Ted's Settings — no third-party kiosk plugin is required.

## Themes

For the bundled Ted's Themes to appear in the theme picker, Home Assistant must load the themes directory. Add this to `configuration.yaml` once, then restart:

```yaml
frontend:
  themes: !include_dir_merge_named themes/
```

(HA only auto-loads `<config>/themes/` when this include is present.)

## Services & events

- **Alarms** — `add_alarm` / `update_alarm` / `remove_alarm`; fires `teds_dashboard_system_alarm_ringing`.
- **Timers** — `start_timer` / `cancel_timer` / pause / resume / update; fires `teds_dashboard_system_timer_finished`.
- **Dashboard views** — `dashboard_customize_view` / `dashboard_revert_view` / `dashboard_add_custom_view` / `dashboard_remove_custom_view` / `dashboard_set_layout` (also driven by the Settings UI above).
- Plus `announce`, `assist_response`, `notify`, and settings services.

## Changelog

### v0.9.8

- Bundles Ted's Cards v0.9.7 (one-tap Browser Mod device registration from the welcome page and the status card, plus layout tips that only appear once a device is registered). Pairs with Ted's Cards v0.9.7+.
- Welcome page: a new helper to register this browser with Browser Mod in one tap (no trip to the Browser Mod panel), and the "better layout for this screen" tips now appear only once the device is registered.

### v0.9.7

- Bundles Ted's Cards v0.9.6 (navbar alarm/timer icon fix, weekday-default and one-shot alarms, Announce settings deep-link, and a device presence heartbeat so a device stays online for announcements). Pairs with Ted's Cards v0.9.6+.
- **One-shot alarms.** An alarm with no repeat days now rings once at the next matching time and then disables itself, instead of being saved as if every day were selected.
- **No more stray alarm sound after a timer.** Fixed a case where dismissing a timer could play a single burst of the alarm sound — the system no longer “resumes” its own alert sounds as if they were your media.

### v0.9.6

- Bundles Ted's Cards v0.9.5 (music setup now runs proactively on load, so a device that can finish automatically no longer shows a prompt or needs a "Try again" click). Pairs with Ted's Cards v0.9.5+.

### v0.9.5

- **Zero-config music setup on the add-on.** When Music Assistant runs as the Home Assistant add-on, a device now sets itself up as a Music Assistant player with no token needed — the backend authorizes the setup as the current Home Assistant admin (over Music Assistant's ingress). An admin token is only needed for an external Music Assistant server. Pairs with Ted's Cards v0.9.4+.

### v0.9.4

- Bundles Ted's Cards v0.9.4 (clearer music-setup guidance: short status-row state, with the full step-by-step guidance delivered as a persistent notification). Pairs with Ted's Cards v0.9.4+.

### v0.9.3

- **Music Assistant admin token for reliable auto-setup.** Music Assistant gates player setup behind an admin role, so **Ted's Dashboard System → Configure** now takes an optional Music Assistant admin token. When set, the backend uses it (over Music Assistant's HTTP API) to expose a device as a Music Assistant player automatically; when empty, the cards guide you through the one-time manual step. Pairs with Ted's Cards v0.9.3+.

### v0.9.2

- **Music auto-setup logging.** The backend now logs each step of exposing a device as a Music Assistant player (prefixed `teds MA auto-create:`) and logs failures, so setup issues are diagnosable from the Home Assistant log. Bundles Ted's Cards v0.9.2. Pairs with Ted's Cards v0.9.2+.

### v0.9.1

- **Auto-set-up music on a device.** The backend can now expose a device as a Music Assistant player on its own — it adds Music Assistant's Home Assistant player provider automatically when Music Assistant runs as the add-on (no manual provider setup), and serializes requests so several devices can't clash. Pairs with Ted's Cards v0.9.1+.

### v0.9.0

- Initial public preview release — the baseline we are using for real-world testing ahead of a v1.0.0 release.
