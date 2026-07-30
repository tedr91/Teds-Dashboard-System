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

### v0.9.24

- Improves the "Assign devices to a room" dialog: a non-admin (kiosk) account now sees only the current screen's device, while admins see the current device pinned to the top with a clear "This device" badge and highlight. Bundles Ted's Cards v0.9.21. Pairs with Ted's Cards v0.9.21+.

### v0.9.23

- Fixes a false "Assign this screen to a room" prompt that could appear on panels already assigned to a room (it was triggered by other unassigned devices). The prompt is now scoped to this panel's own device, and the fix dialog marks this screen. Bundles Ted's Cards v0.9.20. Pairs with Ted's Cards v0.9.20+.

### v0.9.22

- **Voice timers own the dashboard.** "Set a timer" spoken to a Ted's Dashboard panel now creates a Ted's timer with a live countdown on the Timers view — with full voice control (cancel, pause, resume, add/remove time). Phones and other devices keep Home Assistant's native timers. (Replaces the previous experimental timer bridge.)
- **Room‑aware setup.** An un‑scoped panel is nudged to pick a room (voice commands are area‑scoped by the device), with a one‑tap fix dialog and a privileged `set_device_area` command so even kiosk accounts can self‑assign (gated by a new setting). Admins also get a Repair listing Companion‑app devices without an area.
- Bundles Ted's Cards v0.9.19. Pairs with Ted's Cards v0.9.19+.

### v0.9.21

- **Full voice Assist support.** Adds voice commands to play music, make announcements, control the thermostat (smart heat/cool with an optional auto-on and a minimum gap between setpoints), and ask for your next alarm, running timers, or next calendar appointment — spoken answers also appear on the Assist-Response view. Timers started with Home Assistant's built-in voice now mirror onto the Timers view. New Settings → Thermostats options: voice zone names, auto turn on, and a minimum heat/cool gap. Bundles Ted's Cards v0.9.18. Pairs with Ted's Cards v0.9.18+.

### v0.9.20

- Fires a `dashboard_updated` event when a dashboard update installs, so devices can auto-refresh. Bundles Ted's Cards v0.9.17 (new “Auto-refresh on update” setting). Pairs with Ted's Cards v0.9.17+.

### v0.9.19

- Serves the MDI icon-name list locally (`frontend/mdi-names.txt`) so the Calendar card can resolve non-MDI icons to their `mdi:` equivalent offline. Bundles Ted's Cards v0.9.16. Pairs with Ted's Cards v0.9.16+.

### v0.9.18

- Bundles Ted's Cards v0.9.15 — consistent Camera/Photo Viewer empty states and a “Multi” default camera layout. Pairs with Ted's Cards v0.9.15+.

### v0.9.17

- **Fixed: Welcome-page setup tips reappear** (the layout suggestions + “register this device” prompt were hidden by HA's native card visibility). Bundles Ted's Cards v0.9.14. Pairs with Ted's Cards v0.9.14+.
- The integration's setup description and the sidebar panel icon (temporary teddy-bear placeholder) were refreshed.

### v0.9.16

- **Welcome page:** the “Re-check requirements” box now hides itself once every requirement is met, and its wording is clearer. Bundles Ted's Cards v0.9.13 (tidier kiosk-mode prompt). Pairs with Ted's Cards v0.9.13+.

### v0.9.15

- **Welcome page layout fix:** it now uses a full-width panel layout (instead of a narrow column) while still rendering without Layout Card.

### v0.9.14

- **Welcome page improvements:** a **Re-check requirements** button (forces an immediate re-scan after installing a plugin); a clearer notice when **UIX** is downloaded but not yet added as an integration; reworded “needs setup” boxes; and the welcome page now renders even when **Layout Card** isn't installed yet (so its own setup guidance is always reachable).

### v0.9.13

- **Kiosk mode is now opt-in** (off by default). A registered device shows a one-time prompt to enable it. Bundles Ted's Cards v0.9.12. Pairs with Ted's Cards v0.9.12+.

### v0.9.12

- Bundles Ted's Cards v0.9.11 (kiosk mode now also hides HA's header; Automatic Night Mode defaults off and turns on for Nightstand-type devices; night schedule can follow the Sun integration; and the Bing attribution icon clears when switching slideshow albums). Pairs with Ted's Cards v0.9.11+.
- **Automatic Night Mode now defaults to off**, and a new **night schedule source** setting (Manual / Sun sunset→sunrise / Sun dusk→dawn) is available.

### v0.9.11

- **Favorites, imported wallpapers, and the Bing “removed” list now survive integration updates.** Favorited photos (Bing + Photos page) are stored under `media/Ted Dash System/Favorites`, set-as-wallpaper imports under `media/Ted Dash System/Wallpapers`, and the Bing removed-photo list in a config-level data folder — so a HACS update no longer wipes them. (Anything saved before this update in the old location is lost once; re-save it.)
- Bundles Ted's Cards v0.9.10 (no Single-Image background flicker when navigating between views). Pairs with Ted's Cards v0.9.10+.

### v0.9.10

- Bundles Ted's Cards v0.9.9 ("Enable music on this device" is now admin-only with a note, and the Climate empty state uses the standard message card). Pairs with Ted's Cards v0.9.9+.
- Weather view: the clock-weather forecast card is now centered and width-capped so it no longer stretches with an oversized "today" section.

### v0.9.9

- Bundles Ted's Cards v0.9.8 (registering a device also syncs its Browser ID to the login session, and "Update Name / Area" is now admin-only with a clear note for non-admins). Pairs with Ted's Cards v0.9.8+.

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
