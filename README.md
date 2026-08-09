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
- Per-view extras: **yet-another-media-player** and **Music Assistant** (+ `mass_queue`) for the Music view; **weather-forecast-card** (`troinine/ha-weather-forecast-card`) and **windy-card** (`timmaurice/lovelace-windy-card`) for the Weather view.

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

### v0.9.136

- Bundles Ted's Cards v0.9.114 (night mode's "Switch to Dark Mode" now uses Home Assistant's real user-scoped dark theme and restores the prior preference at dawn; enabling it warns that it cascades to the account's other devices, listing them).

### v0.9.135

- Bundles Ted's Cards v0.9.113 (Calendar Card now follows the Home Assistant dark-mode toggle on the HA theme: white font and recessed event tints in dark mode; event tint softened to 85%).

### v0.9.134

- Bundles Ted's Cards v0.9.112 (Calendar Card on HA theme: forces the wrapped Daylight calendar's custom cell/agenda surfaces fully transparent so the card no longer reads milkier than other cards on translucent themes).

### v0.9.133

- Bundles Ted's Cards v0.9.111 (night mode redesigned into "Dynamic night mode": independently opt-in screen/background/font effects, day/night brightness levels, and a hide-background-at-night option).

### v0.9.132

- Bundles Ted's Cards v0.9.110 (Settings → General: Personalization section now sits directly under Kiosk mode, above Automatic night mode).

### v0.9.131

- Bundles Ted's Cards v0.9.109 (Settings → General reorganized: Kiosk mode under Device type, new Personalization section for Theme/Icon set/Background, Weather moved to Advanced, night-mode settings stay visible when off).

### v0.9.130

- Bundles Ted's Cards v0.9.108 (timer, settings, and vision dialogs now open as top-layer modals so they can't be clipped or pushed off-screen).

### v0.9.129

- Bundles Ted's Cards v0.9.107 (fixes the alarm add/edit dialog running off-screen with unreachable buttons).

### v0.9.128

- Bundles Ted's Cards v0.9.106 (alarm dialog now uses proper searchable entity pickers for the wake-up light and presence sensor).

### v0.9.127

- **Alarm presence gate.** An alarm can now name an optional presence sensor; when nobody's present at the alarm time it skips both the wake-up light ramp and the alarm ring. Fails open, so a missing or unavailable sensor never suppresses an alarm. Bundles Ted's Cards v0.9.105.

### v0.9.126

- **Alarm wake-up light.** Alarms can now gradually brighten a chosen light so it reaches full brightness exactly when the alarm rings, using a smooth parabolic ramp. An hour after ringing the light fades back to its previous state as an energy safeguard — but only if it's still where the ramp left it (so it won't override a change you made). Bundles Ted's Cards v0.9.104.

### v0.9.125

- **Weather view: larger, bold Forecast text.** The Forecast card's fonts are now bigger and bold for better wall-panel readability (the on-chart temperature numbers enlarge but stay regular weight, as they're canvas-drawn).

### v0.9.124

- **Weather view fixes.** The Radar overlay loop now dwells 20s per layer (was 10s). The Forecast card no longer clips the right-most day — removed a column-width override that conflicted with the card's fit-to-width math, so days spread cleanly across the tab.

### v0.9.123

- **Weather view: Forecast fonts now scale with the display.** The Forecast card's text auto-scales using a blend of viewport height and the card's own width (via the card's documented font-size variables), so it stays readable on large wall panels without ballooning on phones.

### v0.9.122

- **Weather view: Radar map now fills the whole tab.** The windy-card Radar map was a fixed 450px height, leaving blank space below it; it now stretches to fill the full tab height.

### v0.9.121

- **Fixed: Vision events could freeze at “analyzing” when the AI provider hung.** The primary analysis pass called `ai_task.generate_data` with no timeout, so a wedged provider (e.g. an Ollama server stalled by another model) stranded the event forever — it had to be deleted by hand. Every ai_task call now has a 180s ceiling, and the whole analysis stage has a 300s ceiling, so an event always reaches a terminal state (`complete`, showing “Analysis unavailable”) instead of hanging. In two-pass mode, if the detailed pass fails but pass 1 already produced a real summary, that summary is now kept rather than blanked. The 0.9.118 diagnostic A/B detach is unchanged.

### v0.9.120

- **Weather view: Radar map zoom increased.** The windy-card Radar tab now defaults to zoom level 7 (was 5) for a tighter regional view.

### v0.9.119

- **Weather view: new Forecast and Radar cards.** The Forecast tab now uses **weather-forecast-card** (`troinine/ha-weather-forecast-card`) in chart mode with condition effects, and the Radar tab now uses **windy-card** (`timmaurice/lovelace-windy-card`) with an auto-cycling rain/satellite/temp overlay loop. Install both via HACS.

### v0.9.118

- **Fixed: the diagnostic A/B model could freeze event processing.** When analysis debugging's A/B entity **hung** (rather than erroring — e.g. an Ollama model that never loads), the event stayed at “analyzing” forever even though the real analysis had already succeeded. The A/B comparison now runs fully detached from the real pass with a 120s ceiling, so a slow or stuck A/B model can never delay or block an event; a timed-out A/B pass logs a warning and posts a FAILED debug notification. Analysis debugging is off by default, so most installs were unaffected.

### v0.9.117

- **Animated Vision previews.** The stills that were analyzed for each Frigate event are now retained (in the served vision cache) so the Cameras view's Vision timeline can **loop them like Frigate's own preview thumbnails**, showing the motion at a glance instead of one frozen frame. The frames are pruned along with their event (on clear, delete, or when the event ages out), and the detail sheet falls back to the same loop when an event has no clip. Bundles Ted's Cards v0.9.103.

### v0.9.116

- **Clearing or deleting Vision events now also clears their notifications.** Marking an event reviewed already cleared its notification, but the Cameras view's **Clear all** and deleting a single event left the matching notifications behind. Both now dismiss the notifications a vision event's toast created, on every device (sticky ones are marked read).

### v0.9.115

- **Analysis debugging now raises a notification per pass.** With **Enable analysis debugging** on, each analysis pass (including each A/B pass) posts a **silent** Info notification titled with the AI Task entity's friendly name and carrying its images/timing/severity and full summary — a timing-independent way to compare models straight from the notification center. The notifications are silent (a two-pass A/B event emits four), unscoped, and survive marking the event reviewed. Note they churn the notification list while debugging is on. Bundles Ted's Cards v0.9.102.

### v0.9.114

- **Compare vision models side by side, live.** The **Enable analysis debugging** setting (renamed; now works in single-pass mode) gains an **AI Task entity — A/B pass**: when set, every analysis pass is also run in parallel against that entity with the exact same images, recorded in the event detail alongside the real result. The A/B result is never published — it can't change an event's summary, severity, or false-alarm handling — so you can benchmark a candidate model against production on genuinely identical input with no replay harness. Bundles Ted's Cards v0.9.101.

### v0.9.113

- **More reliable Vision descriptions of direction and sequence.** Frigate's labelled reference snapshot can come from the middle or end of an event, but it was presented as image #1 ahead of the chronological frames — so the model saw timestamps out of order and sometimes flipped the direction of travel or “loading vs unloading” between identical runs. The analysis prompt now tells the model that image #1 is a reference outside the sequence and to use only the remaining, chronologically-ordered frames for direction and order of events.

### v0.9.112

- **Vision analysis costs several-fold less, predictably across models.** Every image is now downscaled before it's sent (longest edge 1024 px by default), the number of images is a single total budget (default 5, including Frigate's labelled snapshot) instead of a growing pile, Pass 1 now sends only the live frames where Frigate tracked the object (down from all ~11), and both passes send fewer images on quiet events (scaled to how much the object actually moved). Set **Max image size** to 0 to restore full resolution. Bundles Ted's Cards v0.9.100.

### v0.9.111

- **Vision Pass 2 now extracts frames where the object actually was.** Instead of sampling stills at a fixed interval across the whole clip (which mostly captured empty pre/post-capture padding), TDS reads Frigate's tracked-object path for the event and pulls frames from those moments — self-calibrating the clip alignment and falling back to even sampling if Frigate is unreachable. Measured ~+65% more distinct object positions per event at the same frame budget, so the AI describes the action rather than the static scene.

### v0.9.110

- **Vision Pass 1 now runs live, at the start of the event — and both passes analyze the right object.** Previously the “quick” pass only ran after the clip finalized (so a summary didn't appear until the event ended), and a review that absorbed an earlier-started object could hand the detailed pass the *wrong* object's clip and snapshot. Pass 1 now samples the live camera the moment the alert fires (a real summary at ~T+12s), and the event that raised the alert is pinned so the clip, thumbnail, and labelled snapshot always match. Also fixes video-capable provider detection (Gemini) and gives each pass its own image set. Bundles Ted's Cards v0.9.99.

### v0.9.109

- **Vision two-pass diagnostics.** With **Enable analysis debugging** on (Settings → Cameras → Vision Analysis), each event now retains the quick and detailed pass results separately — entity, timing, attachment count, and text — viewable in the event detail so you can tell which pass wrote a given summary. Bundles Ted's Cards v0.9.98.

### v0.9.108

- Welcome view: the setup/requirement prompts (Register device, Re-check requirements, Frigate, backend-not-installed) now use the same default card styling as the rest of the boxes instead of the darker Ted's Style surface.

### v0.9.107

- Bundles Ted's Cards v0.9.97. The **Cameras and Vision views are now combined** — the Vision timeline is a tab inside the Cameras view that disappears when Vision Analysis is off (deep-link with `?tab=cameras` / `?tab=vision`). Also nudges the Calendar Agenda day-row spacing to 2px.

### v0.9.105

- Bundles Ted's Cards v0.9.95 (Automatic Night Mode dark theme is now applied per-browser instead of via HA's account-wide theme, so devices sharing one HA user account no longer darken each other).

### v0.9.104

- Bundles Ted's Cards v0.9.94 (notification severity colors — each severity now drives a shared accent color, danger warmed to a clearer red, and the auto-hide navbar pill pulsates in the highest-severity unread notification's color).

### v0.9.103

- Bundles Ted's Cards v0.9.93 (Calendar Card Agenda view: tighter day rows — the blank strip above each day trimmed to 1px).

### v0.9.102

- Bundles Ted's Cards v0.9.92 (Vision card **context-aware empty states** — the timeline now explains why it's empty and offers **Show all events** when filters hide everything).

### v0.9.101

- **Fixed real activity being mislabeled as a false alarm.** The Vision AI was over-applying the “false alarm” flag — sometimes tagging a clip its own summary described as a car arriving or a person walking. The analysis prompt no longer invites it, now treats a Frigate-tracked object as proof something happened, and a code backstop clears a contradictory flag when Frigate independently tracked an object (logged so the rate is visible). This prevents a real event from being silently downgraded.

### v0.9.100

- Bundles Ted's Cards v0.9.91 (Vision card **“Hide viewed”** toggle — the timeline hides events you've already reviewed by default).

### v0.9.99

- **Vision “reviewed” and its notification now stay in sync, everywhere.** Marking a Vision event reviewed (e.g. on your desktop) clears its notification from the notification center on every device, including wall panels; and reading, dismissing, or clearing that notification marks the Vision timeline entry reviewed. Vision events and notifications are global, so either action reflects on all your screens.

### v0.9.98

- **Marking a Vision event reviewed now marks it reviewed in Frigate too.** For a Frigate-native event, tapping “Mark reviewed” in the Vision timeline also clears it from Frigate's review inbox (via Frigate's API), so the two stay in sync. Best-effort — if Frigate is unreachable the TDS review still succeeds.

### v0.9.97

- **No more silently-dropped Frigate alerts.** Frigate already decides what's alert-worthy, so TDS no longer re-filters its alerts against a hand-kept per-object trigger list. Frigate-native cameras now always have a built-in **“Any Frigate alert”** catch-all (server-side too, so existing configs are covered without any migration); object triggers are optional refinements for per-object cooldowns/actions. Previously an alert for an object with no matching trigger (e.g. a `car` on a camera set up only for `person`) produced no timeline entry and no notification.
- Fixed two related edge cases: the notification bridge could pick the wrong event from a multi-object review (now uses the earliest-started one), and it now falls back to notifying if Vision ever declines a review — while still respecting cooldown suppression.
- Bundles Ted's Cards v0.9.90 (the “Any Frigate alert” settings UI).

### v0.9.96

- **More accurate Vision descriptions.** Fixed three issues that made AI summaries describe the wrong thing: the prompt's example sentences were being copied verbatim (removed them); the model wasn't told which object Frigate actually tracked (it's now given the tracked object, its zones, and Frigate's labelled bounding-box snapshot as a reference frame); and the analyzed clip could be picked from the wrong detection (now always the earliest-started one that opened the review).

### v0.9.106

- Bundles Ted's Cards v0.9.96 (navbar: the voice Assist mic moved to the Left section, after the weather item; the Nightstand device type no longer auto-hides its navbar).

### v0.9.95

- Bundles Ted's Cards v0.9.89 (Calendar Card Agenda view: long event titles now truncate with “…” inside the card instead of overflowing past its right edge).

### v0.9.94

- Bundles Ted's Cards v0.9.88 — voice: interrupt a long spoken answer by saying a stop word ("stop", "no", "cancel"…) or tapping the navbar mic (now a stop button while speaking); it stops and acknowledges with "Okay". The full-screen Assist-Response view now scrolls so long answers aren't clipped.

### v0.9.93

- Bundles Ted's Cards v0.9.87 (Nightstand device type now defaults the navbar to the right with the Left/Right sections kept but weather and date/time items removed; Calendar Card Agenda view groups the weekday/forecast tightly around a slightly larger date number).

### v0.9.92

- Bundles Ted's Cards v0.9.86 (Calendar Card Agenda view tuned to a Calendar Card Pro look: larger day date, high-temp-only forecast with a matched condition icon, tighter day/event spacing, and slightly larger event titles).

### v0.9.91

- **Vision analysis is now staged, with live progress.** A camera event is logged and its actions (live feed, chime, notifications) fire the **instant** it's detected — from a fast placeholder snapshot, before any AI runs — then the row upgrades from **In progress…** to **Analyzing…** to its final summary. The clip is captured once and, with two-pass on, a quick early-window pass and the detailed full-clip pass run concurrently (the quick one posts a preliminary summary only if it beats the detailed one). Applies to both Frigate-native and binary-sensor cameras.
- Sharpened the analysis prompt to describe what changes throughout the clip and to avoid inventing a story or flagging static objects.
- Bundles Ted's Cards v0.9.85 (the In progress / Analyzing status badge).

### v0.9.90

- **Vision: act immediately, discard after analysis.** Trigger actions (live feed, chime, notifications) now fire the instant an event is detected instead of waiting on severity. Once the full AI analysis completes, the event is discarded if it turns out to be a false alarm or its final severity matches the trigger's new **“Discard events if severity matches”** list — discarded events are logged-only or dropped per your **Filter out false alarms** setting. Applies to both Frigate-native and binary-sensor Vision paths.
- Bundles Ted's Cards v0.9.84 (the discard-severity trigger UI).

### v0.9.89

- **Frigate-native Vision, done right.** For Frigate cameras, TDS now drives Vision from Frigate's own tracked events instead of re-recording: it logs the event using **Frigate's thumbnail + clip** (nothing captured or stored locally) and runs the AI only to describe that clip. The entry appears **the instant Frigate reports the object** — firing your “Display live feed” / chime actions immediately, while the activity is happening — then upgrades in place with the clip and a detailed summary when the event ends.
- **Fewer false detections.** Fixed a classifier bug where a camera named after an object (e.g. `front_door_package`) misfiled its motion sensor as that object, and de-primed the AI prompt so it treats the sensor's object type as an unverified hint and reports only what's actually visible.
- Bundles Ted's Cards v0.9.83 (the “Analyzing…” badge).

### v0.9.88

- **Tighter Frigate integration.** When Frigate is the adopted camera source, TDS now taps its native detection and events: Frigate review **alerts** become Ted's notifications with the event thumbnail and clip; for two-pass Vision, Frigate's own object detection **seeds the quick pass** so only the detailed pass calls the AI (still a full-clip summary); and the Cameras view gains Frigate **controls** (detect/recordings/snapshots), a **Recordings** link, and **status chips**. All toggleable under Settings → Cameras → Frigate. Bundles Ted's Cards v0.9.82.

### v0.9.87

- **Direct Frigate support.** When the Frigate integration is installed and exposing cameras, Ted's Dashboard can use those cameras as its camera source. If this dashboard has no cameras configured yet, it adopts your Frigate cameras automatically; if you already have a camera list, it offers a one-tap switch (on the Welcome page, in Settings → Cameras, and via a one-time notification) that does a one-time clear of your list and replaces it with your Frigate cameras. Bundles Ted's Cards v0.9.81.

### v0.9.86

- Updated the bundled Ted's Cards to v0.9.80: removing a Bing “Photo of the Day” now removes it from every device's wallpaper slideshow live — a Remove on one tablet drops the photo on all the others at once, instead of it lingering until each device reloaded.

### v0.9.85

- Updated the bundled Ted's Cards to v0.9.79: notifications now show a thumbnail image when provided (e.g. a vision event snapshot) in the navbar notifications popup, the Notification Center card, and the toast.

### v0.9.84

- Updated the bundled Ted's Cards to v0.9.78: Settings → Cameras auto-populate now collapses high/medium/low substream channels into one entry per camera (keeping the highest-res) and cleans up redundant substreams, and the “Add a camera” picker hides redundant substreams.

### v0.9.83

- Updated the bundled Ted's Cards to v0.9.77: camera card editor substream polish — Medium/Low pickers show the auto-detected feed as a muted placeholder, and the main “Camera entity” picker hides redundant medium/low substreams (keeping main feeds).

### v0.9.82

- Updated the bundled Ted's Cards to v0.9.76: camera card substream matching is now device-aware — auto-detection and Auto populate only pair feeds on the same parent device with a related entity name, correctly linking renamed cameras to their substreams while keeping distinct feeds (e.g. doorbell package cameras) separate.

### v0.9.81

- Updated the bundled Ted's Cards to v0.9.75: the camera card's “Auto populate” now collapses high/medium/low substream variants of the same camera into one entry (recognizing Reolink `_high/medium/low_resolution_channel` naming), and the device-Settings “Camera source” selector is now YAML-only.

### v0.9.80

- Updated the bundled Ted's Cards to v0.9.74: the camera card now auto-detects lower-resolution substream entities by naming convention (UniFi Protect `_high/_medium/_low`, Reolink `_clear/_balanced/_fluent`, generic `_main/_sub`), so small tiles use lighter feeds with no manual setup.

### v0.9.79

- Updated the bundled Ted's Cards to v0.9.73: the camera card can now use lower-resolution substreams for small feeds — low for the small Multi tiles, medium for the Multi primary and Quad/Auto-grid tiles, and the full feed for Single, with fallback to the main camera.

### v0.9.78

- Updated the bundled Ted's Cards to v0.9.72: tapping a notification in the Notification Center card now opens the full event in a centered detail modal (matching the navbar notifications popup) instead of only marking it read.

### v0.9.77

- Stopped `sensor.teds_vision_events`, `sensor.teds_settings`, and the other Teds status sensors from writing their large JSON attribute blobs to the recorder database — clears the repeated "State attributes … exceed maximum size of 16384 bytes" log warnings. These sensors are live push channels for the cards, so their attribute history had no value.

### v0.9.76

- Updated the bundled Ted's Cards to v0.9.71 and added a `subscribe_dashboard_updated` WebSocket command so non-admin (kiosk/Wallpanel) panels can auto-refresh on update without triggering repeated `Unauthorized` / `Refusing to allow … to subscribe to event` log errors.

### v0.9.75

- Updated the bundled Ted's Cards to v0.9.70: the mini Music Player vertical volume slider now fills its popout.

### v0.9.74

- Updated the bundled Ted's Cards to v0.9.69: the mini Music Player volume popout is now a vertical slider.

### v0.9.73

- Updated the bundled Ted's Cards to v0.9.68: fixed the mini Music Player “…” menu opening off-screen (it now opens upward on a bottom-pinned player).

### v0.9.72

- Updated the bundled Ted's Cards to v0.9.67: the Music Player “mini” mode’s “…” menu gained a “Party Mode!” item that opens Music Assistant’s fullscreen party dashboard for the current player (requires MA 2.8+ Party plugin), plus a reusable Web View card.

### v0.9.71

- Updated the bundled Ted's Cards to v0.9.66: the Music Player “mini” mode now has a “…” menu (Media, Queue, Volume) in place of the volume button. Media and Queue open a pop-up of the full player’s tab, and Volume opens a compact slider (tap the speaker to mute).

### v0.9.70

- Vision “False alarm” is now flagged only when the analysis concludes no genuine activity was detected (a spurious trigger like shadows, rain, or a swaying tree) — not merely uninteresting activity.
- Stopped the recorder warnings about `sensor.teds_vision_events` and `sensor.teds_settings` attributes exceeding 16 KB: their large payloads (read live by the cards over WebSocket) are now excluded from database recording.

### v0.9.69

- Bundles Ted's Cards v0.9.65 — fixes a full-screen black overlay that could appear on devices whose browser lacks Popover API support (regression from the v0.9.64 Vision live-feed overlay).

### v0.9.68

- Bundles Ted's Cards v0.9.64 — Vision “Display live feed” now pops open a muted full-screen live stream of the triggering camera on targeted screens (no navigation, WebRTC-preferred, auto-closes after 60s or on tap).

### v0.9.67

- Bundles Ted's Cards v0.9.63. The wall-panel **Home** views' Room Cards now use `auto_max_rows`, so each section shows only as many rows of controls as fit the card and tucks the rest into the "…" menu.

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
