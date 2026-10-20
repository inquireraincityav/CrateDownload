# Audio Downloader

Paste a Spotify link, get the track. Albums and playlists too. Everything
happens on this Mac — no account, no server, nothing leaves the machine.

---

## Install

```
./install.sh
```

That's it. It will tell you what's missing and offer to install it.

Options:

```
./install.sh --storage ~/Music/Rips     put captures somewhere else
./install.sh --port 8790                different port
./install.sh --yes                      don't ask before installing packages
./install.sh --uninstall                remove the app (keeps your music)
```

**Requires macOS 14.4 or newer.** The audio tap uses ScreenCaptureKit
per-app capture, which doesn't exist before that.

**Intel and Apple Silicon both supported.** Every Swift helper ships as a
universal binary (x86_64 + arm64) — no Xcode, no build step on either.
The installer checks each helper for this machine's slice anyway and
rebuilds from the bundled sources in `src/` if one is ever missing.

---

## What gets installed

| what | where |
|---|---|
| engine + web app | `~/Library/Application Support/Audio Downloader/bin` |
| your settings | `~/Library/Application Support/Audio Downloader/config.json` |
| python venv (for ID3 tags) | `~/Library/Application Support/Audio Downloader/venv` |
| launcher | `/Applications/Audio Downloader.app` |
| background service | `~/Library/LaunchAgents/com.audiodownloader.server.plist` |
| your music | `~/Music/Audio Downloader` (changeable) |

Shared tools it installs through Homebrew: `ffmpeg`, `yt-dlp`,
`switchaudio-osx`, and the `blackhole-2ch` audio driver.

---

## Using it

Open **Audio Downloader** from Launchpad. Paste one or more links, hit Capture.

- **Spotify** track / album / playlist — recorded from the Spotify app
- **YouTube / SoundCloud** — downloaded directly, no recording needed
- **1001tracklists** set URL — parses the tracklist and searches each entry
- **Free text** like "Fisher - Losing It VIP Mix" — resolved via the
  search layer; a version marker steers the pick (SoundCloud wins for VIPs
  that Spotify doesn't carry)
- **BPMSupreme / DJcity / Hypeddit** URL — if that pool is enabled in config
  with a cookies file, the download uses your logged-in session

Finished files show at the bottom of the window with a 🔍 button that jumps
straight to them in Finder, and a waveform preview under the row.

### yt-dlp names, cleaned up

Downloads inherit the uploader's title — "ARTIST - TRACK (Official Audio)
[FREE DL] [320kbps]" and friends. The engine strips the boilerplate on
save, splits into Artist / Title / Version, rewrites ID3, and renames the
file to `Artist - Title (Version).mp3`.

### Settings

Click the **gear** in the top right. Every path is editable there:

- capture folder
- optional mirror folders (a second copy of everything, e.g. into iCloud)
- working folder and log file
- which output device counts as "silent"
- port

Saving restarts the background service. No file editing, no terminal.

---

## How captures behave

**They're silent.** The Mac's output is parked on BlackHole for the duration,
so nothing plays through your speakers. Your previous output device is
restored when the capture finishes — including if it crashes or you quit
mid-run.

**They're full quality.** The audio is tapped from the Spotify app directly
via ScreenCaptureKit, not scraped off the system mix. Mail, Slack and
everything else keeps playing on your real output and none of it bleeds in.

**Spotify's own volume is pushed to 100%** before each track and put back
afterwards. With a per-app tap, the app's volume slider *is* the recorded
level — at 50% you'd get a track 6 dB quiet forever.

**Track boundaries are tight.** Spotify's reported play position rolls over
to zero about 1.4 s before the audio actually stops, so the recorder keeps
running past the rollover for exactly that long and then pauses playback
before the next track's first sample. No clipped tail, no next-song bleed.

**Interrupted runs resume.** Re-fire the same album or playlist URL and it
skips every track already on disk (verified by duration, not just filename)
and picks up where it stopped.

---

## Permissions

Two one-time prompts, both asked for during install:

1. **Screen Recording** — for `avrec-app`. This is the audio tap. Without it
   captures produce silence.
2. **Automation → Spotify** — for `spotify-ctl`. This is how tracks get
   navigated to, played and paused.

Grant them at
`System Settings ▸ Privacy & Security ▸ Screen Recording / Automation`.

---

## Speed, honestly

Recording is real-time. A four-minute song takes four minutes; a sixty-track
playlist is about four hours. Commercial tools advertise "10X" — that comes
from decoding the web player's stream rather than recording playback, which
is a different (and legally louder) technique. This one plays the song and
records it.

Fire a playlist and go do something else. It runs unattended and silent.

---

## Notifications

Off-machine notifications are **disabled by default** — no accounts, no
tokens, nothing phones home. A local Notification Centre banner appears when
a capture finishes. Optional Telegram and iMessage hooks exist in
`config.json` if you want them; both are blank and off until you fill them in.

### Telegram control from your phone

With `notify.telegram.control_enabled` turned on (same token and `chat_id`
as the outbound notifier), the server long-polls the Bot API and accepts
commands from your locked chat only. Any Spotify / YouTube / SoundCloud link
you message the bot is queued exactly as if you'd pasted it into the web UI.
Short commands too:

- `status` — current capture + queue
- `recent` — last 10 finished files
- `stop` — kill the current capture
- `resend <name substring>` — re-upload a recent file to Telegram
- `help`

Messages from any chat other than the configured `chat_id` are ignored.

---

## Troubleshooting

**Nothing happens when I paste a link** — only Spotify, YouTube and
SoundCloud links are accepted. Anything else is ignored on purpose.

**The capture is silent / empty** — Screen Recording permission. Check
`System Settings ▸ Privacy & Security ▸ Screen Recording` and make sure
`avrec-app` is listed and ticked.

**My speakers stayed on BlackHole** — click
the **stop current capture** link at the bottom of the app window. From a
terminal, call it through the bundled interpreter (the path has a space in it,
so the script's own shebang cannot be used):

```
"$HOME/Library/Application Support/Audio Downloader/venv/bin/python3" \
  "$HOME/Library/Application Support/Audio Downloader/bin/record-audio" stop
```

That always restores the previous device.

**Where's the log** — `~/Library/Application Support/Audio Downloader/capture.log`,
and the service's own output next to it in `audio-downloader.err.log`.

**It stopped starting at login** —
`launchctl kickstart -k gui/$(id -u)/com.audiodownloader.server`

---

## The one real risk: your Spotify account

The commercial tools in this category generate a steady stream of users
reporting **suspended or deleted Spotify accounts** — often within hours of a
big batch. The trigger is behavioural: their fast modes pull tracks far
quicker than a human could listen, which is trivially detectable server-side.
Several of those reports come from people who swear they were running at 1X,
so it is not a guarantee either way.

This tool plays each song through the real Spotify app at real speed, which
is the least conspicuous profile available — but it still means a very long
unbroken run of back-to-back complete plays. If you care about the account:
break large playlists up, don't run captures around the clock, and don't do
it on an account you can't afford to lose.

## A word on what this is

This records audio that's playing on your own machine. That's the same thing
a tape deck did. It is also, under the DMCA and its equivalents, arguably
circumvention — every commercial tool in this category is published by an
offshore shell company for exactly that reason. Keep it personal, don't
redistribute what you capture, and don't put your name on a storefront.
