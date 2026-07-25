# Ted's Cards Backend

Home Assistant integration backing **Ted's Cards** alarms & timers — so the Alarm and Timer cards can add/manage them without you creating helpers by hand.

- **Alarms** — label, description, time, repeat days, enabled; fires a `teds_cards_backend_alarm_ringing` event you can automate.
- **Timers** — countdown (h/m/s) + name, pause/resume, edit (rename/change duration), view/cancel in-progress, last 5 recent for one-tap restart; fires `teds_cards_backend_timer_finished`.

Install via HACS (custom repository, category **Integration**), restart, then add **Ted's Cards Backend** from Settings → Devices & Services. Pair with the cards in [Teds-Cards](https://github.com/tedr91/Teds-Cards).

## Changelog

### v1.0.82

- **Assist-Response service.** New `teds_cards_backend.assist_response` action pushes a title + message (and optional image) to targeted areas/devices for the new Ted's Cards Assist-Response view — the visual counterpart to a spoken Assist answer. It stores the latest answer per target (streamed via `subscribe_assist_responses`, exposed on `sensor.teds_assist_responses` for reload restore) and reuses the navigation signal to switch the targeted screens to the view (`navigate: false` to skip). Renamed the `info_dashboard` setting default to `assist_response_dashboard`. Pairs with Ted's Cards v1.0.333+.

### v1.0.81

- **Readable favorite / stored photo filenames.** Photos imported by the Photo Viewer card (Favorite / Set-as-background) are now saved as `<name>-<shorthash>.ext` (e.g. `oar2-3f9c1a2b4d.jpg`) instead of a bare hash, so they're recognizable on disk. Deduplication still works by the content hash, so the same image is never stored twice. Pairs with Ted's Cards v1.0.319+.

### v1.0.80

- **Photo import for the new Photo Viewer card.** Added `favorite_photo` and `store_background_photo` WebSocket commands that download the current photo (local file or URL), dedupe it by content hash, and store it in the `favorites` / `stored` background folders; plus `list_favorites` to enumerate favorited photos (powering the wallpaper **Slideshow → Favorites** album). Added the `photos_*` settings defaults (album folder, re-open-last, slideshow transition/crossfade). Pairs with Ted's Cards v1.0.319+.

### v1.0.79

- **Device Type profiles.** Added `device_type` and `fullscreen_default` per-device setting defaults so Ted's Cards can store each device's profile (nightstand / tablet / handheld) and its default fullscreen state. Pairs with Ted's Cards v1.0.316+.

### v1.0.78

- **Hardened announcement playback.** Some idle Alexa/cast speakers (e.g. an Echo Show) occasionally dropped the first play, so an announcement produced no sound. The backend now waits briefly for an idle target to become available, then verifies the clip actually started — re-issuing it up to twice if the device shows no sign of playing. Retries are gated so a dismissed announcement won't replay and devices that are merely slow to begin audio won't double-play.

### v1.0.77

- **Fullscreen card saved state.** Added an internal `fullscreen_states` per-device setting so the new Ted's Cards **Fullscreen Card** can remember whether each card is normal or maximized across reloads. Pairs with Ted's Cards v1.0.303+.

### v1.0.76

- **Recent announcements are now per-device.** Each sending device keeps its own recent-announcement history (deduped and capped independently), so a busy device no longer evicts another device's recents. Pairs with Ted's Cards v1.0.302+.

### v1.0.75

- **Bundled sounds are now listable.** A new `list_sounds` WebSocket endpoint reports the built-in alert sounds (with friendly names + categories), powering the new sound-picker dropdown + preview in Ted's Cards Settings. Pairs with Ted's Cards v1.0.301+.

### v1.0.74

- **Less jarring dismissals.** Repeating alert sounds (timers, alarms, announcement chimes) now re-check that the alert is still active immediately before each repeat, so dismissing one no longer lets a stray extra sound slip through from a repeat that was already queued. Pairs with Ted's Cards v1.0.300+.

### v1.0.73

- **Announcements speak a preface and the title.** The spoken sequence is now **chime → preface → title → 0.5s pause → message → chime**. The preface comes from a new **Spoken preface** setting (default “Incoming announcement”, blank = none), and a predefined announcement's title is read aloud before the message (free-text sends with no real title are unaffected). Pairs with Ted's Cards v1.0.299+.

### v1.0.72

- **Announcements: repeat chime now behaves like a finished timer.** The finishing chime is always baked into the spoken clip, and “until dismissed” announcements now start their repeating alert chime **exactly when the clip ends** — the loop is timed off the clip's *measured* length, so the long silence before the repeat (and the drawn-out gaps between chimes) are gone. It now rings on repeat right after the announcement, just like a timer going off. Pairs with Ted's Cards v1.0.298+.

### v1.0.71

- **Announcements: no duplicate Dismiss, seamless repeat chime.** “Until dismissed” announcements previously showed **two Dismiss buttons** and left a noticeable **gap before the alert chime started repeating**. The extra button is gone (the message box's own Dismiss handles it), and the stitched clip now ends right at the message so the repeating alert chime begins seamlessly at its finish — no gap. Pairs with Ted's Cards v1.0.298+.

### v1.0.70

- **Announcement playback drops the middle chime.** The spoken sequence is now **chime → “Announcement incoming” → 0.5s pause → message → chime** (previously there was an extra chime between the preface and the message). The pause makes the message easier to catch. Existing cached clips regenerate automatically when a message changes.

### v1.0.69

- **Recent announcements remember the sender.** Each recent announcement now records the device that sent it, so the Announce card can show “from &lt;device&gt;” and offer a reply. Pairs with Ted's Cards v1.0.297+.

### v1.0.68

- **Announcements: one stitched clip fixes the stutter.** On high-latency speakers (some tablets/cast devices) each separate sound/TTS call took several seconds to start, so the chime→speech→chime→message sequence dragged on. Now the whole sequence is stitched into a single audio clip (via ffmpeg) and played with **one** call, so the device's startup lag is paid once and it plays gaplessly. Falls back to separate clips if stitching isn't available. Timing diagnostics are logged when Debug mode is on.
- **Simpler repeat + unified timeout.** Every "Until dismissed" announcement repeats its alert chime (the separate `repeat_sound` option is gone). The **timeout** now auto-dismisses the on-screen message **and** stops the repeating sound together — and dismissing the message (by tap or timeout) always stops the sound. Pairs with Ted's Cards v1.0.296+.

### v1.0.67

- **Announcement prep-then-show.** An incoming announcement now fully prepares its audio first — the "Announcement incoming" preface and the message are generated and measured up front (server-side) — and only then does the on-screen message appear and the sequence play. This keeps the visual and audio in sync and removes the last of the timing glitches; brand-new messages have a brief one-time prep before they show (repeats are instant).

### v1.0.66

- **Announcement timing now uses real audio lengths.** Reverted the v1.0.65 estimate tweak (which could cut the spoken message off) and instead measures the *actual* length of each spoken clip — the TTS audio is generated and measured up front (during the opening chime), which also warms Home Assistant's TTS cache so speech starts without a synthesis pause. No more cut-offs or dead gaps between the chime and the message.

### v1.0.65

- **Snappier announcement sequence.** Trimmed the dead gap between the alert chime and the spoken steps: on announce-capable speakers the next clip now starts as the chime tail plays (its speech synthesis overlaps the chime instead of adding a pause), and the per-step timing estimates are tighter.

### v1.0.64

- **Announcements now chime, preface, then speak.** Each received announcement plays an ordered sequence on the target speaker: alert chime → “Announcement incoming” → alert chime → the message → the alert chime once more (or on repeat until dismissed for a persistent announcement). Dismissing mid-sequence stops it.
- **Reply to announcements.** The `announce` service accepts a `source_device`, carried on the toast so a recipient can reply straight back to the sender (used by the Ted's Announce card's new Reply button). Pairs with Ted's Cards v1.0.294+.

### v1.0.63

- **Announcements** — a new `teds_cards_backend.announce` service (and the Ted's Announce card) speaks a message aloud on the targeted rooms/devices and shows a prominent, centered toast on their screens. Speech uses each speaker's best method — Music Assistant / Sonos / Alexa-style players duck and auto-resume; others play directly — via the TTS engine set in Settings → Announce. Persistent announcements can loop an alert chime until dismissed. Adds a global predefined-message list, per-announcement targeting by area and/or registered device, a `sensor.teds_announcements` recent list, and a `remove_announcement` service. Pairs with Ted's Cards v1.0.287+.

### v1.0.62

- **Removed Bing photos stay removed** — deleting a Bing Photo of the Day from the info flyout now records it in a persistent blocklist (`bing_pod/removed.json`), so the daily 8-day fetch no longer re-downloads it and it won't reappear on any device. The blocklist self-bounds to Bing's 8-day window and survives a cache clear.

### v1.0.61

- **Voice: navigate the dashboard** — new Assist intents to show a view (*"show cameras"*, *"go to music"*, *"open climate"*, …) covering Cameras, Climate, Weather, Music, Calendar and Home. Changing a thermostat or starting music by voice also nudges that room's screen to the matching view (new `nav_follow_actions` setting, on by default), and *"what's the weather"* answers **and** opens the Weather view. Navigation is scoped to the room you ask from. Pairs with Ted's Cards v1.0.247+.

### v1.0.60

- **Fix: integration failed to set up** — a voice-intent handler assigned its slot schema incorrectly, which crashed setup on v1.0.58/v1.0.59 (the integration showed as "not installed"). Fixed; the integration now loads normally.

### v1.0.59

- **Whole-home alarms by voice** — you can now explicitly create a house-wide alarm regardless of which device you ask from, e.g. *"set a whole home alarm for 7am"*, *"set an everywhere alarm for 6:30"*, or *"set an unscoped alarm for 8"*. (Without this, alarms are scoped to the requesting device's area.)

### v1.0.58

- **Voice control with LLM agents** — the alarm and notification intents now expose typed parameters (`slot_schema`), so LLM-based conversation agents (OpenAI, Gemini, etc.) can actually call them as tools. Requests are auto-scoped to the calling device's area. Requires the agent to have *Control Home Assistant* set to *Assist*.

### v1.0.57

- **Voice control (Assist)** — added custom Assist intents for **alarms** (add, list, enable, disable, remove) and **notifications** (read, clear, mark read), backed by the existing services. Requests are scoped to the voice satellite's area, so the same phrase works from any device. English sentences are installed automatically into `custom_sentences/en/`. Examples: *"set an alarm for 7am on weekdays"*, *"what alarms do I have"*, *"disable my morning alarm"*, *"read my notifications"*, *"clear my notifications"*. (First install may require one Home Assistant restart before voice works.)

### v1.0.56

- **Wallpaper defaults** — the Background Wallpaper now defaults to **Slideshow** mode with the **Bing Photo of the Day** album (`background_mode: slideshow`, `background_album: bing_pod`) for new/unset devices. Pairs with Ted's Cards v1.0.245+.

### v1.0.55

- **Bing photo actions** — added the `favorite_bing_photo` and `remove_bing_photo` WebSocket commands. Favorite copies a cached Bing image into a new `backgrounds/favorites/` folder (isolated from Built-in, for a future album); Remove deletes a single cached image and drops it from the index. Pairs with Ted's Cards v1.0.239+.

### v1.0.54

- **Background brightness default** — the base `background_brightness` default is now **75** (was 100). Pairs with Ted's Cards v1.0.238+.

### v1.0.53

- **Night mode transition in seconds** — renamed `night_transition_minutes` → `night_transition_seconds` (default 30). Pairs with Ted's Cards v1.0.237+.

### v1.0.52

- **Night mode Dark-mode setting** — added the `night_dark_mode` setting (default on) that lets Automatic Night Mode switch the device to Dark theme mode at night and restore the prior Auto/Light/Dark setting in the morning. Pairs with Ted's Cards v1.0.234+.

### v1.0.51

- **Background brightness + night dim settings** — added the `background_brightness` (base wallpaper brightness) and `night_dim_background` (night-time background target) settings, and updated the Automatic Night Mode defaults (transition 1 min, screen dim 75%, background dim 25%). Pairs with Ted's Cards v1.0.233+.

### v1.0.50

- **Night mode day-value storage** — added the internal per-device `night_day_snapshot` setting that Ted's Cards uses to store your daytime screen values (brightness, color temperature, on/off) while Automatic Night Mode is active, so they can be restored in the morning or when you disable night mode — surviving browser cache clears. Pairs with Ted's Cards v1.0.232+.

### v1.0.49

- **Automatic night mode settings** — added the `night_*` settings (enabled, start/end time, dim brightness, night font color, transition duration, and a per-device screen-brightness entity) that back Ted's Cards' new **Settings → General → Automatic night mode**. Pairs with Ted's Cards v1.0.230+.

### v1.0.48

- **Bing Photo of the Day source** — new `list_bing_photos` / `clear_bing_photos_cache` (admin) WebSocket commands and a `bing_pod` image cache that downloads Bing's daily wallpapers (last 8 days, market auto-detected from your HA locale) into an isolated `backgrounds/bing_pod/` folder and serves them locally with title/copyright attribution. A daily refresh keeps it current, and the new `background_bing_cache_size` setting (default 100) caps how many photos are kept. Pairs with Ted's Cards v1.0.227+.

### v1.0.47

- **Quick launch groups setting** — added the `launcher_quick_launch` setting (default **on**) so launcher groups can single-tap to their dashboard and hold to open the popout. Pairs with Ted's Cards v1.0.224+.

### v1.0.46

- **Home dashboard default** — `home_dashboard` now defaults to **`[root]/home-welcome`**, matching the renamed Home-Welcome landing view. Pairs with Ted's Cards v1.0.220+.

### v1.0.45

- **View Launcher colors** — replaced `launcher_active_color` with **`launcher_button_color`** (default white) and **`launcher_highlight_color`** (default accent), backing the Button/Highlight color controls in Settings → Navbar → Launcher Buttons. Pairs with Ted's Cards v1.0.209+.

### v1.0.44

- **View Launcher settings** — added the global **`launcher_enabled`**, **`launcher_section`**, **`launcher_combine_groups`**, **`launcher_list`**, **`launcher_options`**, **`launcher_highlight_active`**, and **`launcher_active_color`** settings backing the new auto-discovered View Launcher navbar buttons (Settings → Navbar → Launcher Buttons). Pairs with Ted's Cards v1.0.206+.

### v1.0.43

- **Emphasize weekdays default** — `calendar_emphasize_weekdays` now defaults to **on**. Pairs with Ted's Cards v1.0.196+.

### v1.0.42

- **Emphasize weekdays setting** — added the global **`calendar_emphasize_weekdays`** setting backing the Settings → Calendars option that dims weekends so weekdays stand out. Pairs with Ted's Cards v1.0.195+.

### v1.0.41

- **Default calendar title** — the **`calendar_name`** setting now defaults to **“Family Calendar”**, so a settings-mode calendar shows that title out of the box. Pairs with Ted's Cards v1.0.190+.

### v1.0.40

- **Calendar appearance settings** — added global **`calendar_name`**, **`calendar_theme`**, and **`calendar_view`** settings backing the new Name/Theme/Default view controls in Settings → Calendars. Pairs with Ted's Cards v1.0.190+.

### v1.0.39

- **Fixed saving object-valued settings** — the `set_setting` service now accepts dictionary values, so per-calendar options (`calendar_options`) save correctly from Settings → Calendars. Previously saving them failed with "not a valid value for dictionary value". Pairs with Ted's Cards v1.0.188+.

### v1.0.38

- **Per-calendar options setting** — added the global **`calendar_options`** setting (a map keyed by calendar entity id) that stores per-calendar display options (name, read-only, person, icon, badge source, color) edited in Settings → Calendars and applied by Ted's Calendar card. Pairs with Ted's Cards v1.0.188+.

### v1.0.37

- **Calendars setting** — added the per-device/global **`calendars_list`** setting that backs the new Ted's Calendar card (Settings → Calendars). Pairs with Ted's Cards v1.0.172+.

### v1.0.36

- **Alert volume honored on announce-capable players** — notification/alarm/timer volumes are now applied when the target player uses the announce path (e.g. Music Assistant players), via `announce_volume`. Previously only non-announce players honored the configured volume; announce-capable players played at their current volume. Players that don't support it ignore the extra (no change for them).

### v1.0.35

- **Default Music volume 5%** — the `music_volume` setting now defaults to 5 (was 50). Pairs with Ted's Cards v1.0.168+.

### v1.0.34

- **Icon set setting** — added the per-device/global **`icon_set`** setting (default `auto`) that lets Ted's Cards choose which icon family its built-in icons use. Pairs with Ted's Cards v1.0.161+.

### v1.0.33

- **Separate music vs. system-sound players** — the alert engine now plays alarms/timers/notifications on the per-device **`system_sound_player`** setting (renamed from `media_player`), leaving the new **`music_player`** setting for the Music view. Both fall back to the device's own registered player. Pairs with Ted's Cards v1.0.153+.

### v1.0.32

- **Card-first Background support** — added `background_enhance_readability` and `background_readability_strength` settings (readability scrim), and a committed `backgrounds/index.json` catalogue so the bundled wallpapers can be served over CDN to card-only users (Ted Background Card without this integration). Pairs with Ted's Cards v1.0.147+.

### v1.0.31

- **“Ted Dash System” media folder** — on setup the integration creates a `Ted Dash System` folder under Home Assistant's local **My media** source (the first configured `media_dirs` path) and exposes its media-source URI via a new `teds_cards_backend/media_folder` WebSocket command. Ted's Cards uses it so Background wallpaper uploads land in that folder and the image/folder pickers open into it. Pairs with Ted's Cards v1.0.146+.

### v1.0.30

- **Background Wallpaper default** — the default Solid Color is now a muted indigo (`#57608E`). Pairs with Ted's Cards v1.0.132+.

### v1.0.29

- **Background Wallpapers** — added the `background_*` settings (mode, size, alignment, repeat, scroll, color + gradient, single image + recents, slideshow album/folder/type/shuffle/cycle) to the settings store. Serves the bundled wallpapers at `/teds_cards_backend/backgrounds/*` and adds a `list_backgrounds` WebSocket command that returns the built-in image catalogue grouped by general/light/dark. Pairs with Ted's Cards v1.0.131+.

### v1.0.28

- **Temperatures settings** — added `climate_list` and `climate_layout` to the settings store so the new Climate Card's per-device/global thermostat list and layout persist. Pairs with Ted's Cards v1.0.115+.

### v1.0.27

- **Weather entity setting** — added `weather_entity` to the settings store so the Clock Weather Card's opt-in (`backend_integration: true`) can source its weather entity from a single per-device/global setting. Pairs with Ted's Cards v1.0.110+.

### v1.0.26

- **Camera layout setting** — added `cameras_layout` to the settings store so the Camera Card's per-device/global layout choice persists. Pairs with Ted's Cards v1.0.108+.

### v1.0.25

- **Debug mode setting** — added `debug_mode` to the settings store so the Ted's Cards per-device/global debug-outline toggle persists. Pairs with Ted's Cards v1.0.100+.

### v1.0.24

- **Cameras list setting** — added the `cameras_list` setting and allowed list values in `set_setting`, so the Camera Card can store the available camera allow-list (global) and each device's curated camera subset. Pairs with Ted's Cards v1.0.96+.

### v1.0.23

- **Navbar size setting** — added `navbar_size` to the settings store so the Ted's Cards navbar thickness can be set per-device (via the navbar's long-press menu or Settings → Navbar). Pairs with Ted's Cards v1.0.92+.

### v1.0.22

- **Navbar settings** — added `navbar_auto_hide`, `navbar_auto_hide_delay`, `navbar_float`, and `navbar_position` to the settings store so the Ted's Cards navbar can be configured per-device (and driven by the navbar's long-press menu). Pairs with Ted's Cards v1.0.90+.

### v1.0.21

- **Integration version exposed** — `sensor.teds_requirements` now includes a `version` attribute (the integration's manifest version), so dashboards and the new Ted's Cards Status Card can display the installed backend version. Pairs with Ted's Cards v1.0.86+.

### v1.0.20

- **Global settings are admin-only** — the `set_setting` and `clear_setting` services now reject writes at **global** scope unless the calling user is an administrator (device-scope writes stay open for everyone, e.g. kiosk devices). Pairs with Ted's Cards v1.0.84+, which shows Global settings read-only for non-admins.

### v1.0.19

- **Expanded Navigation settings** — added dashboard-path settings for **Weather, Calendar, Cameras, Climate, Music, Photos, Info, and Announce**, and corrected the **Home dashboard** default to `[root]/welcome`. (New keys must exist here because the backend whitelists which settings may be written.) Pairs with Ted's Cards v1.0.83+.

### v1.0.18

- **Server-side dependency detection** — a new `sensor.teds_requirements` reports which optional Ted Dashboard dependencies are present (HACS, browser_mod, Custom Icons, card-mod/UIX, layout-card, Ted's Cards, Daylight Calendar Card, Kiosk-Mode, and a weather entity). Detection runs after Home Assistant starts and re-checks when dashboards change, every 10 minutes, or via the new `check_requirements` service. Each requirement is exposed as an attribute (`ok`/`missing`/`unknown`) so dashboards can surface targeted "missing dependency" prompts with no fragile front-end detection. Pairs with the Ted Dashboard Welcome page.

### v1.0.17

- **Devices report their screen on registration** — `register_device` (service + WebSocket) now accepts and stores each device's `client_width`, `client_height`, `client_orientation`, and `client_form_factor`, exposed on the device registry via `sensor.teds_settings`. The Ted's Cards frontend reports these automatically (and re-reports, throttled, when the screen changes). Pairs with Ted's Cards v1.0.80+.

### v1.0.16

- **Repeating alarm/timer sounds actually repeat now** — the engine re-plays the sound every mp3-length (announce players re-announce; others re-play) instead of relying on `media_player.repeat_set`, which most players ignore for a one-shot media URL — so a repeating alert previously played only **once** on those players. It keeps looping until dismissed or the notification times out.

### v1.0.15

- **Alarms / Timers dashboard settings** — added `alarms_dashboard` (default `[root]/alarms-timers?tab=alarms`) and `timers_dashboard` (default `[root]/alarms-timers?tab=timers`) to the settings store, so the Alarms/Timers navbar status items know where to navigate. Pairs with Ted's Cards v1.0.74+.

### v1.0.14

- **New default navigation settings** — the **Home dashboard** now defaults to `[root]/home` (was `[root]/home-tablet`) and **Auto-return home after** defaults to `0` (never). Pairs with Ted's Cards v1.0.73+.

### v1.0.13

- **Pause-and-resume playback** — alerts no longer talk over (or clobber) whatever's playing. Announce-capable media players get a native `announce` (duck/pause the current media, play the alert, auto-resume); other players (BrowserMod, Squeezelite, …) snapshot their volume + current media, play the alert, then restore and resume it. Stopping is **immediate** (`media_stop`, not at the end of the current loop), and players are always returned to their prior state. Repeating alarm/timer sounds now loop for the **actual length of the sound** — announce players re-announce each length; others loop natively via `repeat_set` — so the old fixed 6-second interval and `*_alert_max_repeats` settings are gone (repeat is now just an on/off flag, bounded by the notification's timeout). Sound length is read with `mutagen`.
- **One place drives every sound** — sound-triggering is centralized in the notification pipeline and mapped by **source + severity**, so alarms/timers use their own alert sounds while everything else uses the per-severity notification sound. New per-severity notification sounds: `notification_sound_info` / `_success` / `_warning` / `_danger` / `_tip` (each falls back to the general `notification_sound`).
- **Notification persistence** — the `notify` service's `sticky` boolean is replaced by a `persistence` field: **`transient`** (toast/sound only, never stored), **`normal`** (stored, auto-cleared when read/dismissed), or **`sticky`** (stored, marked read on interaction and kept until cleared). Pairs with Ted's Cards v1.0.72+.

### v1.0.12

- **Playback falls back to a device's own media player** — `register_device` now accepts a `media_player`, and the playback engine uses it when no per-device or global media player is set, so a device plays alerts on its own client speaker by default. Pairs with Ted's Cards v1.0.71+.

### v1.0.11

- **Settings system + sound playback** — a backend settings store (**global** baseline + **per-device** overrides) with services (`set_setting`, `clear_setting`, `register_device`), a `teds_cards_backend/subscribe_settings` WebSocket command, a device registry, and **`sensor.teds_settings`**. A server-side **playback engine** plays alert sounds for finished timers / ringing alarms / notifications on the firing area's devices' media players (deduped, DND-aware), repeating up to the configured max and cancelling on dismiss. Snooze is now a client-resolved payload on completion notifications (each device uses its own snooze settings). Bundled default sounds are served from `/teds_cards_backend/sounds/` (drop `timer.mp3` / `alarm.mp3` / `notification.mp3` in the `sounds/` folder, or set a custom URL in settings). Pairs with Ted's Cards v1.0.69+.

### v1.0.10

- **Snooze / Dismiss buttons on completion notifications** — finished-timer and ringing-alarm notifications now include action buttons: **Snooze (1min)** / **Dismiss** for timers and **Snooze (9min)** / **Dismiss** for alarms. Snooze starts a new timer for the snooze duration, keeping the original name and room (so alarm snoozes stay area-scoped). Pairs with Ted's Cards v1.0.34+.

### v1.0.9

- **Dismissals sync across devices** — `mark_read`, `dismiss_notification`, and `clear_notifications` now broadcast a dismissal signal (a `teds_cards_backend_notification` event with `dismissed: true`) for each affected notification, so a toast dismissed on one device closes on every device showing it (e.g. a house-wide alarm cleared everywhere at once). Pairs with Ted's Cards v1.0.65+.

### v1.0.8

- **Change an alarm/timer's scope (incl. house-wide)** — `update_alarm` and `update_timer` now accept **`location`** and apply it even when set to `null`, so an existing alarm or timer can be moved to a room or made **house-wide** (cleared location). Previously `update_alarm` ignored `null`/unset fields, so a location could never be cleared, and `update_timer` didn't take a location at all. Pairs with Ted's Cards v1.0.62+ (per-item "This room / House-wide" scope).

### v1.0.7

- **Notifications work for non-admin users** — added a `teds_cards_backend/subscribe_notifications` WebSocket command so kiosk/Wallpanel (non-admin) dashboards can receive notifications. Home Assistant blocks non-admin users from subscribing to custom events via `subscribe_events`, which caused repeated "Unauthorized" errors; cards now use this command instead. The `teds_cards_backend_notification` event still fires, so event-triggered automations are unaffected. Pairs with Ted's Cards v1.0.49+.

### v1.0.6

- **Notifications (foundation)** — a new server-side notification store: a **`notify`** service (title, message, severity, icon, area, timeout, sticky) plus `dismiss_notification`, `mark_read`, and `clear_notifications`. Exposes **`sensor.teds_notifications`** (unread count + list) and fires a `teds_cards_backend_notification` event. Finished timers and ringing alarms now also create notifications, so they flow through one path. Pairs with Ted's Cards v1.0.34+.

### v1.0.5

- **New `remove_recent` service** — removes a preset from the Recent timers list (matched by name, duration, and area). Powers the Timer card's long-press "Delete" on recent presets.

### v1.0.4

- **Finished timers are removed reliably** — the timer-finished handler now runs on the event loop (as a proper callback), so a completed timer is dropped from the active list and the sensor updates immediately instead of occasionally lingering at 0:00.
- The `teds_cards_backend_timer_finished` event now also includes the timer's **`duration`** (seconds), so the cards can show how long the finished timer ran.

### v1.0.3

- **Location-aware alarms & timers** — `add_alarm` and `start_timer` now accept an optional **`location`** (an HA area) that is stored on each alarm/timer (and recent preset). The `teds_cards_backend_alarm_ringing` and `teds_cards_backend_timer_finished` events now include `location` (area id) and `area_name`, so automations can announce in the room the item belongs to. Unset `location` behaves as before (global).

### v1.0.2

- **Reliable card refresh** — the alarms and timers sensors now expose fresh copies of their data on every read instead of the manager's live list. Home Assistant compares old vs. new state by value, so returning the live (already-mutated) reference could make attribute-only changes — such as adding or editing an alarm — fail to push an update to the cards. They now refresh immediately.

### v1.0.1

- **Timers** — added **pause**, **resume**, and **edit** (rename / change duration) via new `pause_timer`, `resume_timer`, and `update_timer` services. The timers sensor now exposes each active timer's `duration`, `remaining`, and `paused` state so the Timer card can render progress and paused timers.

### v1.0.0

- Initial release — alarms and timers backend for Ted's Cards.
