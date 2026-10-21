#!/bin/bash
# Audio Downloader installer.
#
#   ./install.sh                      install with defaults
#   ./install.sh --storage ~/Music/Rips
#   ./install.sh --port 8790
#   ./install.sh --yes                don't ask about Homebrew packages, just do it
#   ./install.sh --prefix /tmp/nc     install somewhere else entirely (testing)
#   ./install.sh --label com.x.y      different launchd job name
#   ./install.sh --uninstall           remove the app; keeps your music
#   ./install.sh --uninstall --purge-music --remove-deps
#                                     remove absolutely everything
#
# Everything lands under ~/Library/Application Support/Audio Downloader plus
# the app itself in /Applications (or ~/Applications if that is not writable).
# Nothing else is written outside your home folder except the Homebrew
# packages, which are shared system tools.
#
# Written for /bin/bash 3.2 (the one macOS ships). No arrays, no ${x,,}.

set -uo pipefail

APP_NAME="Audio Downloader"
LABEL="${AUDIO_DOWNLOADER_LABEL:-com.audiodownloader.server}"
SUPPORT="${AUDIO_DOWNLOADER_PREFIX:-$HOME/Library/Application Support/$APP_NAME}"
BIN="$SUPPORT/bin"
VENV="$SUPPORT/venv"
CONFIG="$SUPPORT/config.json"
# /Applications is group-writable by admin users, so this normally needs no
# sudo. Spotlight indexes ~/Applications too, but an app people expect to find
# by name belongs in the folder they actually look in.
if [ -w /Applications ]; then
  APPDIR="/Applications/$APP_NAME.app"
else
  APPDIR="$HOME/Applications/$APP_NAME.app"
fi
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UID_N=$(id -u)

STORAGE_OPT=""
PORT_OPT=""
APP_SET=0
LABEL_SET=0
DO_UNINSTALL=0
ASSUME_YES=0
PURGE_MUSIC=0
REMOVE_DEPS=0

while [ $# -gt 0 ]; do
  case "$1" in
    --storage) STORAGE_OPT="${2:-}"; shift 2 ;;
    --port)    PORT_OPT="${2:-}";    shift 2 ;;
    --yes|-y)  ASSUME_YES=1; shift ;;
    --prefix)  SUPPORT="${2:-}"; shift 2
               BIN="$SUPPORT/bin"; VENV="$SUPPORT/venv"; CONFIG="$SUPPORT/config.json" ;;
    --label)   LABEL="${2:-}"; shift 2; LABEL_SET=1
               PLIST="$HOME/Library/LaunchAgents/$LABEL.plist" ;;
    --app)     APPDIR="${2:-}"; shift 2; APP_SET=1 ;;
    --uninstall) DO_UNINSTALL=1; shift ;;
    --purge-music) PURGE_MUSIC=1; shift ;;
    --remove-deps) REMOVE_DEPS=1; shift ;;
    -h|--help) sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1"; exit 2 ;;
  esac
done

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m*\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31mx\033[0m %s\n' "$*"; }

# ------------------------------------------------------------------ uninstall
# Reverses everything install.sh created, in the order that keeps the machine
# usable at every step: stop capture (restores audio) -> stop service -> remove
# the audio device -> remove files. Captured music and the shared Homebrew
# tools are the two things it will not touch without being told to.
if [ "$DO_UNINSTALL" = 1 ]; then
  say "Uninstalling $APP_NAME"

  # What this install actually created. Without it an uninstall triggered from
  # the app has to GUESS the app bundle path from defaults — which, for an
  # install made with --app or --prefix, means deleting somebody else's copy.
  MANIFEST="$SUPPORT/.install-manifest"
  if [ -f "$MANIFEST" ]; then
    if [ "$APP_SET" = 0 ]; then
      V=$(grep '^APPDIR=' "$MANIFEST" | head -1 | cut -d= -f2-)
      [ -n "$V" ] && APPDIR="$V"
    fi
    if [ "$LABEL_SET" = 0 ]; then
      V=$(grep '^LABEL=' "$MANIFEST" | head -1 | cut -d= -f2-)
      if [ -n "$V" ]; then LABEL="$V"; PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"; fi
    fi
    ok "read install manifest"
  fi

  # Refuse to delete an app bundle that is not ours. Cheap, and the only thing
  # standing between a mis-detected path and somebody's Applications folder.
  if [ -d "$APPDIR" ] && [ ! -f "$APPDIR/Contents/MacOS/AudioDownloader" ]; then
    warn "$APPDIR does not look like $APP_NAME — leaving it alone"
    APPDIR=""
  fi

  # Read what we need out of the config while it still exists.
  STORE=""; SILENT="BlackHole 2ch"; MULTI="$APP_NAME"
  if [ -f "$CONFIG" ]; then
    STORE=$(/usr/bin/python3 -c "import json,os;print(os.path.expanduser(json.load(open('$CONFIG')).get('storage_dir','') or ''))" 2>/dev/null)
    V=$(/usr/bin/python3 -c "import json;print(json.load(open('$CONFIG')).get('silent_output_device','') or '')" 2>/dev/null); [ -n "$V" ] && SILENT="$V"
    V=$(/usr/bin/python3 -c "import json;print(json.load(open('$CONFIG')).get('multi_output_name','') or '')" 2>/dev/null); [ -n "$V" ] && MULTI="$V"
  fi

  # Helper locations. Normally these sit in $BIN, but the app passes its own
  # resolved paths when it triggers an uninstall from the UI, because a
  # hand-rolled install can keep the engine somewhere else entirely.
  RECBIN="${AD_UNINSTALL_RECORD_AUDIO:-$BIN/record-audio}"
  MULTIBIN="${AD_UNINSTALL_MULTIOUT:-$BIN/ad-multiout}"
  if [ ! -x "$MULTIBIN" ] && [ -f "$CONFIG" ]; then
    V=$(/usr/bin/python3 -c "import json,os;print(os.path.expanduser(os.path.expandvars(json.load(open('$CONFIG')).get('helpers',{}).get('multiout','') or '')))" 2>/dev/null)
    [ -n "$V" ] && MULTIBIN="$V"
  fi

  # Interpreter to run the engine with. Never rely on its shebang here: see
  # the long note at the shebang-pinning step for why one cannot name a path
  # containing a space.
  RECPY="${AD_UNINSTALL_PYTHON:-}"
  if [ -z "$RECPY" ] && [ -f "$MANIFEST" ]; then
    RECPY=$(grep '^INTERP=' "$MANIFEST" | head -1 | cut -d= -f2-)
  fi
  [ -n "$RECPY" ] && [ ! -x "$RECPY" ] && RECPY=""
  [ -z "$RECPY" ] && [ -x "$VENV/bin/python3" ] && RECPY="$VENV/bin/python3"
  [ -z "$RECPY" ] && RECPY=/usr/bin/python3

  # 1. A capture mid-flight owns the audio device. Stopping it properly is how
  #    the previous output gets restored — do this before anything is removed.
  if [ -f "$RECBIN" ]; then
    "$RECPY" "$RECBIN" stop >/dev/null 2>&1 && ok "stopped a capture in progress"
  fi

  # 2. Background service.
  launchctl bootout "gui/$UID_N/$LABEL" 2>/dev/null && ok "stopped the background service"
  if [ -f "$PLIST" ]; then rm -f "$PLIST"; ok "removed the launch agent"; fi

  # 3. The aggregate ("Multi-Output") device, which otherwise sits in Audio
  #    MIDI Setup forever with no app left to explain it.
  if [ -x "$MULTIBIN" ]; then
    if AD_MULTIOUT_NAME="$MULTI" "$MULTIBIN" --destroy >/dev/null 2>&1; then
      ok "removed the \"$MULTI\" audio device"
    fi
  fi

  # 4. If output is still parked on the loopback or on the device we just
  #    destroyed, hand the Mac back a real one so sound isn't silently gone.
  SAS="$(command -v SwitchAudioSource 2>/dev/null)"
  [ -z "$SAS" ] && [ -x /opt/homebrew/bin/SwitchAudioSource ] && SAS=/opt/homebrew/bin/SwitchAudioSource
  [ -z "$SAS" ] && [ -x /usr/local/bin/SwitchAudioSource ] && SAS=/usr/local/bin/SwitchAudioSource
  if [ -n "$SAS" ] && [ -x "$SAS" ]; then
    CUR=$("$SAS" -c 2>/dev/null)
    if [ "$CUR" = "$SILENT" ] || [ "$CUR" = "$MULTI" ] || [ -z "$CUR" ]; then
      NEW=$("$SAS" -a -t output 2>/dev/null | grep -v -x "$SILENT" | grep -v -x "$MULTI" | head -1)
      if [ -n "$NEW" ]; then
        "$SAS" -s "$NEW" >/dev/null 2>&1 && ok "audio output put back to \"$NEW\""
      fi
    fi
  fi

  # 5. The app bundle.
  if [ -n "$APPDIR" ] && [ -d "$APPDIR" ]; then rm -rf "$APPDIR"; ok "removed $APPDIR"; fi

  # 6. Engine, venv, settings, logs, working state — everything the installer
  #    created under Application Support.
  if [ -d "$SUPPORT" ]; then rm -rf "$SUPPORT"; ok "removed $SUPPORT"; fi

  # 7. Shared Homebrew tools. Other software uses these, so they only go when
  #    explicitly asked for.
  if [ "$REMOVE_DEPS" = 1 ]; then
    BREW="$(command -v brew 2>/dev/null || true)"
    [ -z "$BREW" ] && [ -x /opt/homebrew/bin/brew ] && BREW=/opt/homebrew/bin/brew
    [ -z "$BREW" ] && [ -x /usr/local/bin/brew ] && BREW=/usr/local/bin/brew
    if [ -n "$BREW" ]; then
      say "Removing shared tools"
      for pkg in ffmpeg yt-dlp switchaudio-osx; do
        "$BREW" uninstall "$pkg" >/dev/null 2>&1 && ok "removed $pkg" || warn "$pkg not removed (not installed, or another package needs it)"
      done
      warn "blackhole-2ch is an audio driver — removing it asks for your password"
      "$BREW" uninstall --cask blackhole-2ch 2>/dev/null && ok "removed blackhole-2ch" || warn "blackhole-2ch left in place"
    else
      warn "Homebrew not found — nothing to remove"
    fi
  fi

  # 8. Captured music is the user's own data. Never implicit.
  if [ "$PURGE_MUSIC" = 1 ] && [ -n "$STORE" ] && [ -d "$STORE" ]; then
    rm -rf "$STORE"; ok "deleted your captures at $STORE"
  fi

  say "Done"
  if [ "$PURGE_MUSIC" != 1 ] && [ -n "$STORE" ] && [ -d "$STORE" ]; then
    echo "  Your captured music was left alone:"
    echo "     $STORE"
    echo "     (re-run with --purge-music to delete it too)"
  fi
  if [ "$REMOVE_DEPS" != 1 ]; then
    echo "  Shared tools left installed: ffmpeg, yt-dlp, switchaudio-osx, blackhole-2ch"
    echo "     (re-run with --remove-deps to uninstall those too)"
  fi
  echo
  echo "  macOS keeps two permission entries that no installer can revoke."
  echo "  Remove them by hand if you want a clean slate:"
  echo "     System Settings > Privacy & Security > Screen Recording  -> avrec-app"
  echo "     System Settings > Privacy & Security > Automation        -> spotify-ctl"
  exit 0
fi

say "Installing $APP_NAME"

# ------------------------------------------------------------ 0. sanity checks
[ "$(uname -s)" = "Darwin" ] || { bad "macOS only."; exit 1; }
MACOS=$(sw_vers -productVersion)
MAJOR=$(echo "$MACOS" | cut -d. -f1)
if [ "$MAJOR" -lt 14 ]; then
  bad "macOS 14.4 (Sonoma) or newer required — this is $MACOS."
  bad "Per-app audio capture (ScreenCaptureKit) does not exist before 14.4."
  exit 1
fi
ARCH=$(uname -m)
ok "macOS $MACOS on $ARCH"

# Port pre-flight, BEFORE anything is written. Installing over a port that is
# already taken leaves launchd holding a job that can never bind; with
# KeepAlive it respawns forever, and the app still looks healthy because the
# other server is the one answering /health.
PRE_PORT="${PORT_OPT:-}"
if [ -z "$PRE_PORT" ] && [ -f "$CONFIG" ]; then
  PRE_PORT=$(/usr/bin/python3 -c "import json;print(json.load(open('$CONFIG'))['server']['port'])" 2>/dev/null)
fi
[ -n "$PRE_PORT" ] || PRE_PORT=8765
if curl -s -m 1 "http://127.0.0.1:$PRE_PORT/health" >/dev/null 2>&1 \
   && ! launchctl print "gui/$UID_N/$LABEL" >/dev/null 2>&1; then
  bad "port $PRE_PORT is already serving something else."
  echo "     Installing here would leave a background service that can never"
  echo "     start. Re-run with --port <n>, or stop the other service first."
  echo "     Nothing has been changed."
  exit 1
fi
ok "port $PRE_PORT is free (or already ours)"

# Every Swift helper now ships as a universal binary (x86_64 + arm64), so
# Intel and Apple Silicon both run the shipped bits with no build step. We
# verify that claim instead of trusting it: if any helper is missing this
# machine's slice, fall back to rebuilding from the bundled sources.
NEED_BUILD=0
for h in avrec avrec-app spotify-ctl ad-multiout ensure-blackhole-rate; do
  [ -f "$SRC/bin/$h" ] || continue
  if ! lipo -archs "$SRC/bin/$h" 2>/dev/null | tr ' ' '\n' | grep -qx "$ARCH"; then
    NEED_BUILD=1
    warn "$h has no $ARCH slice — will rebuild from source."
  fi
done
[ "$NEED_BUILD" = 0 ] && ok "helpers are universal (x86_64 + arm64)"

PY=""
for cand in /usr/bin/python3 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
  [ -x "$cand" ] && { PY="$cand"; break; }
done
[ -n "$PY" ] || PY="$(command -v python3 2>/dev/null)"
if [ -z "$PY" ]; then
  bad "No python3 found. Run: xcode-select --install"
  exit 1
fi
ok "python3 -> $PY ($("$PY" -V 2>&1))"

# --------------------------------------------------------- 1. homebrew deps
BREW="$(command -v brew 2>/dev/null || true)"
[ -z "$BREW" ] && [ -x /opt/homebrew/bin/brew ] && BREW=/opt/homebrew/bin/brew
[ -z "$BREW" ] && [ -x /usr/local/bin/brew ] && BREW=/usr/local/bin/brew
BREW_BIN="/opt/homebrew/bin"
[ -n "$BREW" ] && BREW_BIN="$(dirname "$BREW")"

have() { command -v "$1" >/dev/null 2>&1 || [ -x "$BREW_BIN/$1" ]; }

MISSING=""
have ffmpeg            || MISSING="$MISSING ffmpeg"
have yt-dlp            || MISSING="$MISSING yt-dlp"
have SwitchAudioSource || MISSING="$MISSING switchaudio-osx"

BLACKHOLE=0
system_profiler SPAudioDataType 2>/dev/null | grep -qi "BlackHole 2ch" && BLACKHOLE=1
[ -d "/Library/Audio/Plug-Ins/HAL/BlackHole2ch.driver" ] && BLACKHOLE=1

say "Dependencies"
for f in ffmpeg yt-dlp SwitchAudioSource; do
  have "$f" && ok "$f" || bad "$f - missing"
done
if [ "$BLACKHOLE" = 1 ]; then ok "BlackHole 2ch audio driver"
else bad "BlackHole 2ch audio driver - missing (this is how captures stay silent)"; fi
[ -d /Applications/Spotify.app ] && ok "Spotify.app" || warn "Spotify.app not found - needed for Spotify captures"

if [ -n "$MISSING" ] || [ "$BLACKHOLE" = 0 ]; then
  echo
  if [ -z "$BREW" ]; then
    bad "Homebrew isn't installed and it's the sane way to get these."
    echo "     Install it from https://brew.sh, then run this again."
    exit 1
  fi
  echo "  Needs installing:"
  [ -n "$MISSING" ]    && echo "     brew install$MISSING"
  [ "$BLACKHOLE" = 0 ] && echo "     brew install --cask blackhole-2ch    (asks for your login password)"
  echo
  DOIT=0
  if [ "$ASSUME_YES" = 1 ]; then DOIT=1; else
    printf '  Install them now? [y/N] '; read -r yn
    case "$yn" in [yY]*) DOIT=1 ;; esac
  fi
  if [ "$DOIT" = 1 ]; then
    [ -n "$MISSING" ] && "$BREW" install $MISSING
    if [ "$BLACKHOLE" = 0 ]; then
      "$BREW" install --cask blackhole-2ch
      # the pkg restarts coreaudiod itself; give the HAL a beat to publish it
      sleep 2
      system_profiler SPAudioDataType 2>/dev/null | grep -qi "BlackHole 2ch" \
        && ok "BlackHole 2ch is live" \
        || warn "BlackHole installed but not showing yet - log out and back in"
    fi
  else
    warn "Skipped. Captures will fail (or play out loud) until these are in."
  fi
fi

# --------------------------------------------------- 2. python side-dependency
# mutagen writes the ID3 tags and cover art. A venv off /usr/bin/python3 keeps
# it working even when Homebrew upgrades its own python out from under us.
say "Python environment"
if [ ! -x "$VENV/bin/python3" ]; then
  "$PY" -m venv "$VENV" >/dev/null 2>&1
fi
if [ -x "$VENV/bin/python3" ]; then
  if "$VENV/bin/python3" -c "import mutagen" 2>/dev/null; then
    ok "mutagen present"
  else
    "$VENV/bin/pip" install -q --disable-pip-version-check mutagen >/dev/null 2>&1
    if "$VENV/bin/python3" -c "import mutagen" 2>/dev/null; then ok "mutagen installed"
    else warn "mutagen unavailable - files still capture, just without ID3 tags/artwork"; fi
  fi
  RUNPY="$VENV/bin/python3"
else
  warn "couldn't build a venv - falling back to $PY"
  RUNPY="$PY"
fi
ok "interpreter -> $RUNPY"

# ------------------------------------------------------------- 3. copy files
say "Installing files"
mkdir -p "$BIN" "$SUPPORT/state" "$HOME/Applications" "$HOME/Library/LaunchAgents"
for f in audio-downloader record-audio adcfg.py \
         tg_control.py waveform.py naming.py tracklist.py search.py pools.py; do
  cp "$SRC/bin/$f" "$BIN/$f" || { bad "missing payload: bin/$f"; exit 1; }
done
for f in avrec avrec-app spotify-ctl ad-multiout ensure-blackhole-rate; do
  if [ -f "$SRC/bin/$f" ]; then cp "$SRC/bin/$f" "$BIN/$f"; else warn "payload missing helper: $f"; fi
done
chmod +x "$BIN"/audio-downloader "$BIN"/record-audio 2>/dev/null
for f in avrec avrec-app spotify-ctl ad-multiout ensure-blackhole-rate; do
  [ -f "$BIN/$f" ] && chmod +x "$BIN/$f"
done
xattr -dr com.apple.quarantine "$BIN" 2>/dev/null
ok "engine + web app -> $BIN"

# Keep a copy of this script next to the settings. The kit folder in Downloads
# is the first thing people delete, and without this the app has no uninstaller
# to call and the user has nothing to run.
cp "$SRC/install.sh" "$SUPPORT/install.sh" 2>/dev/null && chmod +x "$SUPPORT/install.sh" \
  && ok "uninstaller kept at $SUPPORT/install.sh"

# Give the two scripts a shebang that actually works from a shell.
#
# A shebang CANNOT contain a space: the kernel splits on the first whitespace.
# The default install path has one ("Application Support"), so pinning to
# "$VENV/bin/python3" produced `bad interpreter: /Users/you/Library/Application`
# — and python surfaces that as ENOENT naming the *script*, which sends you
# looking in completely the wrong place. (macOS `env -S` does not rescue this;
# it mangles the quoted path. A /bin/sh exec trampoline does work, but it has
# to be the module docstring, and these files use `from __future__ import
# annotations`, which must follow at most ONE string literal.)
#
# So: pin to the venv only when its path is space-free, else fall back to
# /usr/bin/python3, which is always present on a supported macOS and never has
# a space. The CLI then works either way. Nothing is lost by the fallback —
# the venv exists only for mutagen (ID3 tags), record-audio imports it lazily
# and logs "mutagen not importable — skipping ID3" if absent, and the paths
# that actually capture (the launch agent and the app) name the venv
# interpreter explicitly rather than relying on this line.
"$RUNPY" - "$BIN" "$RUNPY" <<'PYEOF'
import sys, pathlib
bindir, interp = pathlib.Path(sys.argv[1]), sys.argv[2]
shebang = interp if " " not in interp else "/usr/bin/python3"
for name in ("audio-downloader", "record-audio"):
    p = bindir / name
    lines = p.read_text().split("\n")
    if lines and lines[0].startswith("#!"):
        lines[0] = "#!" + shebang
        p.write_text("\n".join(lines))
if shebang == interp:
    print("  \033[32m*\033[0m shebangs pinned to the venv")
else:
    print("  \033[32m*\033[0m shebangs -> %s (venv path has a space, which a "
          "shebang cannot express)" % shebang)
PYEOF
INTERP="$RUNPY"

if [ "$NEED_BUILD" = 1 ] && command -v swiftc >/dev/null 2>&1; then
  for t in avrec avrec-app spotify-ctl ad-multiout ensure-blackhole-rate; do
    [ -f "$SRC/src/$t/main.swift" ] || { warn "no source for $t, leaving as shipped"; continue; }
    if swiftc -O -target "$ARCH-apple-macos14.0" -o "$BIN/$t" "$SRC/src/$t/main.swift" \
         -framework Foundation -framework AVFoundation -framework CoreAudio \
         -framework AudioToolbox -framework ScreenCaptureKit -framework CoreMedia 2>/dev/null; then
      ok "rebuilt $t for $ARCH"
    else
      bad "could not rebuild $t"
    fi
  done
elif [ "$NEED_BUILD" = 1 ]; then
  bad "swiftc missing. Run: xcode-select --install, then re-run this script."
fi

rm -rf "$APPDIR"
mkdir -p "$APPDIR/Contents"
cp -R "$SRC"/app/Contents/* "$APPDIR/Contents/"
chmod +x "$APPDIR/Contents/MacOS/AudioDownloader"
xattr -dr com.apple.quarantine "$APPDIR" 2>/dev/null
# point the launcher at this install (matters when --prefix/--label were used)
LAUNCHBIN="$APPDIR/Contents/MacOS/AudioDownloader"
"$PY" - "$LAUNCHBIN" "$SUPPORT" "$LABEL" <<'PYEOF'
import sys, pathlib
p, support, label = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
t = p.read_text()
t = t.replace('SUPPORT="$HOME/Library/Application Support/Audio Downloader"',
              'SUPPORT="%s"' % support)
t = t.replace('LABEL="com.audiodownloader.server"', 'LABEL="%s"' % label)
p.write_text(t)
PYEOF
touch "$APPDIR"
# Register with LaunchServices so Spotlight can find it immediately rather
# than whenever the indexer next gets round to /Applications.
LSREG=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister
[ -x "$LSREG" ] && "$LSREG" -f "$APPDIR" 2>/dev/null
ok "launcher -> $APPDIR"

# Record what this install owns, so the uninstaller never has to infer it.
# Plain KEY=value, read back with grep — no sourcing, nothing executed.
{
  echo "APPDIR=$APPDIR"
  echo "LABEL=$LABEL"
  echo "SUPPORT=$SUPPORT"
  echo "INTERP=$INTERP"
  echo "INSTALLED=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} > "$SUPPORT/.install-manifest" 2>/dev/null && ok "install manifest written"

# ------------------------------------------------------------ 4. config file
say "Settings"
if [ -f "$CONFIG" ]; then
  ok "kept your existing config"
else
  cp "$SRC/config.example.json" "$CONFIG"
  # a fresh config keeps its scratch next to itself, wherever --prefix put us
  "$PY" - "$CONFIG" "$SUPPORT" <<'PYEOF'
import json, sys
path, support = sys.argv[1], sys.argv[2]
cfg = json.load(open(path))
cfg["state_dir"] = support + "/state"
cfg["log_file"] = support + "/capture.log"
with open(path, "w") as fh:
    json.dump(cfg, fh, indent=2); fh.write("\n")
PYEOF
  ok "wrote a fresh config"
fi

"$RUNPY" - "$CONFIG" "${STORAGE_OPT:-}" "${PORT_OPT:-}" "$BIN" <<'PYEOF'
import json, os, sys
path, storage, port, bindir = sys.argv[1:5]
cfg = json.load(open(path))
if storage:
    cfg["storage_dir"] = storage
if port:
    cfg.setdefault("server", {})["port"] = int(port)
cfg.setdefault("helpers", {})
for key, name in (("avrec", "avrec"), ("avrec_app", "avrec-app"),
                  ("spotify_ctl", "spotify-ctl"), ("multiout", "ad-multiout"),
                  ("ensure_rate", "ensure-blackhole-rate")):
    # Never clobber a helper that already resolves. This used to overwrite
    # the whole block unconditionally, which silently repointed an existing
    # install at this kit's copies even while the installer reported that it
    # had kept the user's config.
    existing = cfg["helpers"].get(key) or ""
    if existing and os.path.exists(os.path.expanduser(os.path.expandvars(existing))):
        continue
    p = os.path.join(bindir, name)
    cfg["helpers"][key] = p if os.path.exists(p) else ""
with open(path, "w") as fh:
    json.dump(cfg, fh, indent=2); fh.write("\n")
d = os.path.expanduser(os.path.expandvars(cfg["storage_dir"]))
os.makedirs(d, exist_ok=True)
print("  \033[32m*\033[0m captures land in " + d)
print("  \033[32m*\033[0m " + path)
PYEOF

# ----------------------------------------------------------- 5. launch agent
say "Background service"
WANT_PORT=$("$RUNPY" -c "import json;print(json.load(open('$CONFIG'))['server']['port'])" 2>/dev/null || echo 8765)
if curl -s -m 1 "http://127.0.0.1:$WANT_PORT/health" >/dev/null 2>&1; then
  if ! launchctl print "gui/$UID_N/$LABEL" >/dev/null 2>&1; then
    # Should be unreachable: section 0 pre-flights this before writing
    # anything. Kept as a belt-and-braces guard in case the port was taken
    # while the install was running.
    bad "port $WANT_PORT was taken during install — service not started."
    echo "     Free it, then re-run with --port <n> or restart the installer."
    exit 1
  fi
fi
launchctl bootout "gui/$UID_N/$LABEL" 2>/dev/null
sed -e "s|__PYTHON__|$RUNPY|g" -e "s|__BIN__|$BIN|g" \
    -e "s|__SUPPORT__|$SUPPORT|g" -e "s|__BREW__|$BREW_BIN|g" \
    -e "s|com.audiodownloader.server|$LABEL|g" -e "s|__CONFIG__|$CONFIG|g" \
    "$SRC/launchd/com.audiodownloader.server.plist.template" > "$PLIST"
if launchctl bootstrap "gui/$UID_N" "$PLIST" 2>/dev/null; then
  ok "service installed and started"
else
  warn "bootstrap returned non-zero - kickstarting instead"
  launchctl kickstart -k "gui/$UID_N/$LABEL" 2>/dev/null
fi

PORT_LIVE=$("$RUNPY" -c "import json;print(json.load(open('$CONFIG'))['server']['port'])" 2>/dev/null || echo 8765)
UP=0
for _ in $(seq 1 60); do
  curl -s -m 1 "http://127.0.0.1:$PORT_LIVE/health" >/dev/null 2>&1 && { UP=1; break; }
  sleep 0.25
done
if [ "$UP" = 1 ]; then ok "answering on http://localhost:$PORT_LIVE"
else bad "not answering - see $SUPPORT/audio-downloader.err.log"; fi

# ------------------------------------------------------- 6. permission priming
say "Permissions"
if [ "${AD_SKIP_PERMS:-0}" = "1" ]; then
  warn "skipped (AD_SKIP_PERMS=1)"
else
echo "  macOS gates two things. Getting the prompts now beats hitting them"
echo "  halfway through your first album."
echo
if [ -x "$BIN/spotify-ctl" ] && [ -d /Applications/Spotify.app ]; then
  "$BIN/spotify-ctl" raw 'tell application "Spotify" to get name' >/dev/null 2>&1
  ok "asked for Automation (controlling Spotify)"
fi
if [ -x "$BIN/avrec-app" ]; then
  "$BIN/avrec-app" com.apple.finder /tmp/nc-tcc-probe.caf >/dev/null 2>&1 &
  PROBE=$!; sleep 3; kill "$PROBE" 2>/dev/null; wait "$PROBE" 2>/dev/null
  rm -f /tmp/nc-tcc-probe.caf
  ok "asked for Screen Recording (this is the audio tap)"
fi
echo
echo "  If you missed a prompt, add them by hand:"
echo "    System Settings > Privacy & Security > Screen Recording  -> $BIN/avrec-app"
echo "    System Settings > Privacy & Security > Automation        -> spotify-ctl"
fi

# ------------------------------------------------------------------ 7. wrap up
say "Resolved settings"
AUDIO_DOWNLOADER_CONFIG="$CONFIG" "$RUNPY" "$BIN/adcfg.py" | sed 's/^/  /'

cat <<EOF

$(printf '\033[1mDone.\033[0m') Launchpad -> "$APP_NAME", or open
  http://localhost:$PORT_LIVE

Change any path from the gear icon in the app. No file editing needed.

Uninstall:  $SRC/install.sh --uninstall
EOF
