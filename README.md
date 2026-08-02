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

### v0.9.66

- Bundles Ted's Cards v0.9.62. The portrait **Home (Wall Panel)** calendar now always shows a day ahead and uses a fixed 24vh height (dropping the earlier time-of-day sizing).

### v0.9.65

- Bundles Ted's Cards v0.9.61 — the Calendar card gains `agenda_day_height` / `agenda_evening_height`. The portrait **Home (Wall Panel)** view uses them so the agenda is 40vh before 5pm and 80vh from 5pm (pairing with its evening 2-day lookahead).

### v0.9.64

- Reverts the portrait **Home (Wall Panel)** calendar to its filled layout — the auto-height change made the embedded agenda card render at full intrinsic height, pushing the Room Card and Music player off-screen. The calendar and Room Card again share the flexible column space.

### v0.9.63

- Bundles Ted's Cards v0.9.60 — the **Tablet — Portrait** device profile's navbar now omits the weather and clock items by default. The portrait **Home (Wall Panel)** view's calendar is now auto-height (sizes to its agenda), letting the Room Card take the remaining space.

### v0.9.62

- Bundles Ted's Cards v0.9.59 — Room Card editor refreshes after Auto-populate (no stale fields), the Music card's mini mode shows a non-interactive playback strip along the card's bottom edge, and the Calendar card gains `agenda_evening_lookahead`. The portrait **Home (Wall Panel)** view now uses it so the agenda extends through tomorrow only from 5pm.

### v0.9.61

- Bundles Ted's Cards v0.9.58 — Room Card auto-populate strips the room's area name from **Controls** and **Scenes** item names (e.g. in a “Kitchen” room, “Kitchen Ceiling Lights” becomes “Ceiling Lights”). Light/Cover cards default the Neumorphic effect off, and light-name header text stays legible over a top-scrimmed header photo.

### v0.9.60

- Bundles Ted's Cards v0.9.57 — device type presets: Landscape tablet auto-hides the navbar, Portrait tablet keeps it shown. Adds the single-column portrait **Home (Wall Panel)** view (clock, 2-day agenda calendar, area-aware Room Card, compact Music).

### v0.9.59

- Bundles Ted's Cards v0.9.56 — the Settings card hides the Global tab from non-administrator users (forcing the This device scope).

### v0.9.58

- Bundles Ted's Cards v0.9.55 — a global **Theme** setting (Settings → General) now drives all dashboard-integrated cards (replacing the per-Calendars Theme setting). Room Card defaults to HA theme; auto-populate routes Browser Mod entities to Others, orders Controls lights by role, and styles Scenes as icon + name.

### v0.9.57

- Bundles Ted's Cards v0.9.54 — the Calendar card's header is now colored to match each theme (steel blue for Ted's, theme primary for HA), lightly translucent.

### v0.9.56

- Bundles Ted's Cards v0.9.53 — the Calendar card gains three consistent theme options (Home Assistant, Ted's, SuperDingo's). The landscape Home wall-panel now hides the calendar's navigation controls.

### v0.9.55

- Bundles Ted's Cards v0.9.52 — Room Card gains a 1.5x button height and independently scrolling button sections; the Calendar card's “Calendar source” is replaced by a `dashboard_integration` flag and its Header editor gains a Show controls toggle. The Calendar views now use `dashboard_integration: true`.

### v0.9.54

- Bundles Ted's Cards v0.9.51 — auto-populated Room Card light and cover tiles are now double-height with default name/icon/state styling.

### v0.9.53

- Bundles Ted's Cards v0.9.50 — the clock's custom time format now follows HA's 12/24-hour setting, and the self-contained confirmation/prompt dialogs are now interactive above the card editor. Also ships the new landscape **Home (Wall Panel)** view: an area-aware Room Card + compact Music player alongside a clock, with a full-height agenda calendar.

### v0.9.52

- Bundles Ted's Cards v0.9.49 — Room Card auto-populate refinements (compact light/cover tiles, name+state buttons, multi-speaker Media icon, tabbed layout for 2+ sections) and the editor Auto-populate confirmation now renders above the card editor.

### v0.9.51

- Bundles Ted's Cards v0.9.48 — the Room Card can auto-populate from a room's entities: a standalone **Auto-populate from area** editor button, and a `dashboard_integration` mode that adopts the device's area and fills in status items + control sections automatically (hiding when the device has no area).

### v0.9.50

- Bundles Ted's Cards v0.9.47 — Camera Vision notifications now carry their event reference, so clicking one in the navbar notifications popover plays the event's clip (resolved by id to handle two-pass analysis). Notifications gained a generic `data` payload to support this.

### v0.9.49

- Bundles Ted's Cards v0.9.46 — the Vision card gains right-aligned “mark all reviewed” and “clear all” buttons, and marking an old event reviewed no longer reorders it to the top of the list.

### v0.9.48

- Bundles Ted's Cards v0.9.45 — Vision false-alarm fixes: events are no longer all tagged as false alarms (boolean coercion bug), “Drop” now discards false alarms flagged during the detailed pass, and the global-only Vision Analysis settings are hidden from the “This device” tab.

### v0.9.47

- Bundles Ted's Cards v0.9.44 — collapsible list rows now show the chevron at the far right with a trash-can delete button immediately to its left (consistent across Settings/Navbar editors), and the Settings, Announce, and Notification Center cards are hidden from the “Add card” chooser.

### v0.9.46

- Bundles Ted's Cards v0.9.43 — Vision Analysis gains false-alarm filtering (Off / Log only / Drop) with a “False alarm” tag/filter in the card, real camera-stream recording (default, falls back to stitched frames), and optional two-pass analysis (fast quick pass + detailed refine) with separate AI Task entities per pass and smart provider defaults. Capture window default is now 10s.

### v0.9.45

- Bundles Ted's Cards v0.9.42 — Vision “Display live feed” now navigates targeted screens to the Cameras view and focuses the triggering camera (primary + live stream), plus Settings list-row height fixes.

### v0.9.44

- Bundles Ted's Cards v0.9.41 — vision trigger actions reworked into four fixed on/off sections (Display live feed, Toast, Push, Custom). Backend now skips vision actions whose section is toggled off.

### v0.9.43

- Bundles Ted's Cards v0.9.40 — Vision “Additional actions” reworked into consistent collapsible lists (type chosen from the “+” popup; Toast/Live feed target areas, Push targets notify services by friendly name, Custom adds Automation/Script/Scene/Action). Empty target list means everywhere. Backend action dispatch updated to match.

### v0.9.42

- Bundles Ted's Cards v0.9.39 — thermostat voice aliases moved into the Thermostats list (expand a thermostat → collapsible “Aliases” section); the separate “Voice zone names” section was removed.

### v0.9.41

- Bundles Ted's Cards v0.9.38 — consistent list-row heights/buttons across the Cameras, Vision, Announce, and Navbar editors; “+ Add” for typed items (Vision triggers/actions, navbar items) opens a type-picker popup; the Vision severity-filter description moved above its checkboxes.

### v0.9.40

- Bundles Ted's Cards v0.9.37 — consistent collapsible-section headers across Settings (section icon + name on the left; count pill, icon-only action buttons, chevron, and delete “X” on the right), applied to the Cameras/Calendars/Thermostats lists, the per-camera Vision triggers/actions, and the Announce Predefined messages.

### v0.9.39

- **Vision Analysis rebuilt around per-camera triggers.** Each camera is configured inline in the Cameras list with a list of triggers (detection type + a multi-select severity filter + cooldown), and each trigger runs its own actions — Toast notification, Push notification, Display live feed, or a Custom action — only when the analyzed severity matches. Bundles Ted's Cards v0.9.36.

### v0.9.38

- **New: Camera Vision Analysis.** Opt cameras into Vision Analysis (Settings → Vision) and their motion / person / animal / vehicle detections are captured and classified by Home Assistant's built-in AI Task (OpenAI, Ollama, …) into a severity plus a short and long summary, with a best-frame thumbnail and a short clip. Adds a Vision timeline card and dashboard view, a `calendar.teds_vision_timeline` entity so you can ask Assist what happened, per-camera opt-in with severity thresholds and on-trigger notifications/actions, and `analyze_camera` / `delete_vision_event` / `clear_vision_events` services. No third-party vision integration required. Bundles Ted's Cards v0.9.35.

### v0.9.37

- Bundles Ted's Cards v0.9.34 — the navbar Assist mic now auto-disables on devices loaded over HTTP (where the browser can't use the microphone) and explains, on tap, that voice needs an HTTPS connection.

### v0.9.36

- Bundles Ted's Cards v0.9.33 — the wallpaper now crossfades when a view first loads and when you switch albums (fading in over the theme background), instead of snapping. Plain navigation with the same wallpaper still repaints instantly.

### v0.9.35

- Bundles Ted's Cards v0.9.32 — the Nightstand device type trims the navbar to the Home/Music/Alarms-Timers launcher buttons and shows only the Center section. Pairs with Ted's Cards v0.9.32+.

### v0.9.30

- Bundles Ted's Cards v0.9.27 — the navbar's sections and items are now editable in **Settings → Navbar → Navbar sections** (with a default that matches the dashboard's existing bar), and vertical navbars self-heal the first-load overflow collapse. Adds the `navbar_sections` setting default. Pairs with Ted's Cards v0.9.27+.

### v0.9.29

- Bundles Ted's Cards v0.9.26 — the navbar **Float** mode now works on **Left/Right** (vertical) bars: a floating side bar detaches from the edge with rounded corners, centers along its height, and hugs its content. Pairs with Ted's Cards v0.9.26+.

### v0.9.28

- **Auto-recovers dashboard clients after a Home Assistant restart** — a few seconds after HA finishes starting, every `browser_mod` screen is refreshed once, so panels don't get stuck on a loading spinner. Renames the sidebar dashboard to **TDS** and the settings screen to **TDS Settings**. New defaults: Calendars use the **Ted's Style** theme and **Automatic Night Mode** follows the Sun (dusk → dawn). Bundles Ted's Cards v0.9.25 (room-scoping fix for area-less devices + wallpaper slideshow crossfade). Pairs with Ted's Cards v0.9.25+.

### v0.9.27

- Assist-Response conversation history. Persists a rolling per-device/area conversation (last 20 turns, with the recognized question) and exposes it so the Assist-Response view can scroll back through past answers. Bundles Ted's Cards v0.9.24 (scroll-back conversation log).

### v0.9.26

- Voice Assist fixes. Recognizes the dashboard (`browser_mod`) device as a Ted's voice satellite, so voice timers create a Ted's timer on the panel instead of erroring. Bundles Ted's Cards v0.9.23 — voice requests now carry the panel's device id (room-aware "show the cameras" navigation, correct room, timers), a listening chime, a single accumulating conversation box, a longer on-screen linger past the spoken answer, and every answer mirrored to the Assist-Response view.

### v0.9.25

- Bundles Ted's Cards v0.9.22 — browser-based voice Assist: a device runs the Home Assistant Assist pipeline in the dashboard with a self-dismissing voice overlay (push-to-talk mic button + experimental continuous wake word), replacing the Companion app's native Assist dialog. New Voice settings group. Requires the dashboard served over HTTPS (browser microphone requirement). Pairs with Ted's Dashboard v0.9.7+ (navbar mic button).

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
