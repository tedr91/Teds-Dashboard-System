"""Ted's Cards Backend — alarms & timers for Ted's Cards."""

DOMAIN = "teds_cards_backend"

STORAGE_VERSION = 1
STORAGE_KEY = DOMAIN

# How many most-recent timers to remember for quick re-start.
RECENT_TIMERS_MAX = 5

# How many most-recent announcements to remember for quick re-send.
RECENT_ANNOUNCEMENTS_MAX = 10

# How many notifications to keep in the store (FIFO, newest kept).
NOTIFICATIONS_MAX = 50

EVENT_ALARM_RINGING = f"{DOMAIN}_alarm_ringing"
EVENT_TIMER_FINISHED = f"{DOMAIN}_timer_finished"
EVENT_NOTIFICATION = f"{DOMAIN}_notification"
EVENT_SETTINGS = f"{DOMAIN}_settings"
EVENT_NAVIGATE = f"{DOMAIN}_navigate"
EVENT_ASSIST_RESPONSE = f"{DOMAIN}_assist_response"

# Custom Assist intent type names (registered in intents.py + sentences/en.yaml).
INTENT_ADD_ALARM = "TedsAddAlarm"
INTENT_LIST_ALARMS = "TedsListAlarms"
INTENT_ENABLE_ALARM = "TedsEnableAlarm"
INTENT_DISABLE_ALARM = "TedsDisableAlarm"
INTENT_REMOVE_ALARM = "TedsRemoveAlarm"
INTENT_READ_NOTIFICATIONS = "TedsReadNotifications"
INTENT_CLEAR_NOTIFICATIONS = "TedsClearNotifications"
INTENT_MARK_NOTIFICATIONS_READ = "TedsMarkNotificationsRead"
INTENT_NAVIGATE = "TedsNavigate"
INTENT_WEATHER = "TedsWeather"

# Sentinel meaning "use the bundled default sound for this alert kind".
DEFAULT_SOUND = "default"

# Dedicated folder created under HA's local "My media" source for Ted's Cards
# wallpaper uploads (and where the Background image/folder pickers open by default).
MEDIA_FOLDER_NAME = "Ted Dash System"

# How long (seconds) a registered device is considered "present" for server-side
# playback targeting after its last heartbeat.
DEVICE_PRESENCE_TTL = 900

# Global settings baseline. Per-device overrides layer on top of these; a card's
# effective value = device override (if set) else global (if set) else default.
SETTINGS_DEFAULTS = {
    # Timers
    "timer_snooze_enabled": True,
    "timer_snooze_minutes": 1,
    "timer_alert_sound": DEFAULT_SOUND,
    "timer_alert_volume": 60,
    "timer_alert_repeat": True,
    # Alarms
    "alarm_snooze_enabled": True,
    "alarm_snooze_minutes": 9,
    "alarm_alert_sound": DEFAULT_SOUND,
    "alarm_alert_volume": 70,
    "alarm_alert_repeat": True,
    # Notifications
    "notification_sound": DEFAULT_SOUND,
    "notification_volume": 50,
    # Per-severity notification sounds ("default" → use notification_sound).
    "notification_sound_info": DEFAULT_SOUND,
    "notification_sound_success": DEFAULT_SOUND,
    "notification_sound_warning": DEFAULT_SOUND,
    "notification_sound_danger": DEFAULT_SOUND,
    "notification_sound_tip": DEFAULT_SOUND,
    # Media
    # `system_sound_player` = alarms/timers/alerts/notifications; `music_player` =
    # the Music view / Music Assistant. Both are per-device (fall back to the
    # device's own registered player).
    "system_sound_player": None,
    "music_player": None,
    "music_volume": 5,
    # Cameras — ordered list of camera entity ids. Global = the available allow-list;
    # per-device = the curated subset that device shows (empty inherits the global list).
    "cameras_list": [],
    # How this device arranges its cameras on the Cameras view (single/quad/big-small/auto).
    "cameras_layout": "auto",
    # Temperatures — ordered list of climate entity ids. Global = the available allow-list;
    # per-device = the curated subset that device shows (empty inherits the global list).
    "climate_list": [],
    # How this device arranges its thermostats on the Climate view (auto/tabbed/vertical/horizontal).
    "climate_layout": "auto",
    # Calendars — ordered list of calendar entity ids. Global = the available allow-list;
    # per-device = the curated subset that device shows (empty inherits the global list).
    "calendars_list": [],
    # Per-calendar display options keyed by calendar entity id (global/calendar-wide):
    # {entity_id: {name?, readonly?, person?, icon?, icon_source?, color?}}. Applied by
    # Ted's Calendar card in `calendar_source: settings` mode.
    "calendar_options": {},
    # Card-level Calendar appearance (used by Ted's Calendar card in settings mode when
    # the card's own YAML doesn't set the corresponding option).
    "calendar_name": "Family Calendar",  # calendar title ("" = no title)
    "calendar_theme": "ha",       # ha | ted-style
    "calendar_view": "month",     # month | week | schedule | agenda
    # When true, add a day_styles rule that dims weekends so weekdays stand out.
    "calendar_emphasize_weekdays": True,
    # Navbar (per-device navbar behavior; empty/false means "follow the card's YAML").
    "navbar_auto_hide": False,
    "navbar_auto_hide_delay": 5,
    "navbar_float": False,
    "navbar_position": "bottom",
    "navbar_size": 48,
    # View Launcher — auto-discovered, Settings-driven navbar buttons that navigate to the
    # dashboard's views (shown on navbars with `backend_integration: true`).
    "launcher_enabled": True,
    # Which of the five fixed navbar sections the launcher buttons are prepended into.
    "launcher_section": "center",  # left | mid-left | center | mid-right | right
    # Combine views whose path/title share a prefix (e.g. Home-*) into one expandable button.
    "launcher_combine_groups": True,
    # Quick-launch groups: single tap on a group navigates to its dashboard; hold opens the
    # group selector popout. When off, a tap opens the popout. Requires combine groups.
    "launcher_quick_launch": True,
    # Ordered list of view paths. Global = the available allow-list; per-device = the curated
    # subset that device shows (empty inherits the global list).
    "launcher_list": [],
    # Per-view button options keyed by view path (global): {path: {nav_button_size?, name?,
    # icon?, badge?, highlight?}}.
    "launcher_options": {},
    # Highlight the launcher button for the currently-open view (or its group).
    "launcher_highlight_active": True,
    # Tint/icon color of every launcher button.
    "launcher_button_color": "white",
    # Ring color marking the current view's button.
    "launcher_highlight_color": "accent",
    # Announce — spoken announcements broadcast to Ted's Dashboard devices/areas.
    # Global list of predefined messages: [{id, label, text, icon?}].
    "announce_messages": [],
    # TTS engine entity (tts.*) used to speak announcements. None = HA's default engine.
    "announce_tts_engine": None,
    # Spoken preface before the title/message ("" = no preface, go straight to the title).
    "announce_intro_phrase": "Incoming announcement",
    # Alert sound looped after the spoken message on persistent announcements
    # ("default" = the bundled notification chime).
    "announce_sound": DEFAULT_SOUND,
    # Volume (0-100) for announcement speech + alert sound.
    "announce_volume": 80,
    # Default auto-dismiss timeout (seconds) for "Play once" announcements.
    "announce_timeout_default": 30,
    # General
    "do_not_disturb": False,
    "debug_mode": False,
    # Icon set used by Ted's built-in icons (Status/Settings etc.). "auto" = the best
    # installed set by priority; otherwise force a specific set (falls back to mdi).
    "icon_set": "auto",
    # Default weather entity used by Ted's weather/clock cards that opt in via
    # `backend_integration: true`. None = the card falls back to its own default.
    "weather_entity": None,
    # Automatic Night Mode — dims the background, lowers screen brightness, and switches to a
    # night font color on a nightly schedule, restoring day values in the morning.
    "night_enabled": True,
    "night_start": "21:00:00",           # night begins (local time, HH:MM:SS)
    "night_end": "07:00:00",             # night ends (local time)
    "night_dim_brightness": 75,          # target screen brightness percent at night
    "night_dim_background": 25,          # target background brightness percent at night
    "night_font_color": "red",           # font color used during night mode
    "night_transition_seconds": 30,      # transition duration into/out of night (seconds)
    # Switch the device to Dark mode at night (via browser_mod), restoring the prior Auto/Light/Dark
    # setting in the morning.
    "night_dark_mode": True,
    # Per-device screen-brightness entity (light/number/input_number). None = auto-resolve the
    # browser_mod screen light for the device.
    "night_brightness_entity": None,
    # Internal: per-device "day" snapshot the frontend stores while night mode is active so it can
    # restore brightness/color temp/on-off in the morning (or on disable). Not a user-facing field.
    "night_day_snapshot": None,
    # Internal: per-device saved maximized state of Fullscreen cards, keyed by each card's
    # `state_key`. Shape: { <state_key>: bool }. Not a user-facing field.
    "fullscreen_states": {},
    # Per-device profile: None | "nightstand" | "tablet-landscape" | "tablet-portrait" | "handheld".
    # Picking one cascades a preset of navbar/home/fullscreen device settings (frontend applies it).
    "device_type": None,
    # Internal: default maximized state for content Fullscreen cards on this device, seeded by the
    # device type. Whitelisted for writes.
    "fullscreen_default": False,
    # Background Wallpaper — applied by the invisible ted-background-card.
    # mode: solid | image | slideshow | theme (theme = defer to the HA theme's background).
    "background_mode": "slideshow",
    # Common (solid/image/slideshow) — background_scroll false = fixed (attachment).
    "background_scroll": False,
    "background_size": "fill",        # original | fill (cover) | fit (contain)
    "background_align": "center",     # 9 positions: top-left … bottom-right
    "background_repeat": "tile",      # tile (repeat) | no-repeat
    # Solid
    "background_color": "#57608E",
    "background_gradient": True,
    # Single image (URL or media-source:// uri); recents = MRU of last picks (cap 5).
    "background_image": None,
    "background_recent_images": [],
    # Slideshow
    "background_album": "bing_pod",   # builtin | folder | bing_pod
    "background_folder": None,        # media-source:// folder uri when album = folder
    "background_type_pref": "match",  # match | all | light | dark  (UI "Mood matching")
    "background_shuffle": True,
    "background_cycle_minutes": 30,
    # Max Bing "Photo of the Day" images kept in the bing_pod cache (oldest pruned).
    "background_bing_cache_size": 100,
    # Readability — tone the wallpaper toward theme contrast via a luminance scrim.
    "background_enhance_readability": True,
    "background_readability_strength": 45,  # 0–100 (caps the scrim opacity)
    # Base background brightness (0–100). 100 = full brightness; lower dims the wallpaper at all times.
    "background_brightness": 75,
    # Photos (Ted's Photo Viewer card + Photos view)
    "photos_folder": None,               # media-source:// folder uri for the Photos view album
    "photos_auto_open_last": True,       # re-open the last viewed photo when the Photos view loads
    "photos_last_viewed": None,          # per-device: ref of the last opened photo (internal)
    "photos_slideshow_transition": "crossfade",   # crossfade | none
    "photos_slideshow_crossfade_seconds": 2,
    # Navigation
    "dashboard_root": "ted-dashboard",
    "home_dashboard": "[root]/home-welcome",
    "alarms_dashboard": "[root]/alarms-timers?tab=alarms",
    "timers_dashboard": "[root]/alarms-timers?tab=timers",
    "weather_dashboard": "[root]/weather",
    "calendar_dashboard": "[root]/calendar-month",
    "cameras_dashboard": "[root]/cameras",
    "climate_dashboard": "[root]/climate",
    "music_dashboard": "[root]/music",
    "photos_dashboard": "[root]/photos",
    "assist_response_dashboard": "[root]/assist-response",
    "announce_dashboard": "[root]/announce",
    "auto_return_home_after": 0,
    # When true, a voice-driven climate/music action nudges this device's screen to the
    # matching view (Climate/Music) via the navigation signal (server-side, area-scoped).
    "nav_follow_actions": True,
}

# Only keys present in SETTINGS_DEFAULTS may be written (guards the services/WS).
SETTINGS_KEYS = frozenset(SETTINGS_DEFAULTS)

