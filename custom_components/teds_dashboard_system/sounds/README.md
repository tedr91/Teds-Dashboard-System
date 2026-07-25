# Bundled alert sounds

Files placed here are served at `/teds_dashboard_system/sounds/<file>` and used as the
**default** alert sounds by the Ted's Cards settings system.

Provide these files (any short `.mp3`) to enable the built-in defaults:

- `timer.mp3` — default Timer alert sound
- `alarm.mp3` — default Alarm alert sound
- `notification.mp3` — default Notification sound

When a setting's sound is left as `default`, the engine plays
`/teds_dashboard_system/sounds/<kind>.mp3`. To use a different sound, set the
`*_alert_sound` / `notification_sound` setting to any media URL (e.g. a
`media-source://…` URL or an `https://…` file).
