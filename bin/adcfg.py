"""adcfg — shared configuration layer for Audio Downloader.

Both `audio-downloader` (the web app) and `record-audio` (the capture engine)
import this. Everything machine-specific lives in ONE json file so the same
code runs on any Mac.

Resolution order for the config file:
    1. $AUDIO_DOWNLOADER_CONFIG                 (explicit path)
    2. ~/Library/Application Support/Audio Downloader/config.json
    3. built-in defaults (and the file is written on first run)

Every path value supports ~ and $VARS. Every "tools"/"helpers" value may be
left empty ("") to mean "find it yourself" — PATH, then Homebrew arm64
(/opt/homebrew/bin), then Homebrew Intel (/usr/local/bin), then the directory
this module lives in.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

APP_NAME = "Audio Downloader"
BIN_DIR = Path(__file__).resolve().parent
SUPPORT_DIR = Path.home() / "Library" / "Application Support" / APP_NAME
DEFAULT_CONFIG_PATH = SUPPORT_DIR / "config.json"

DEFAULTS = {
    "_comment": "Audio Downloader configuration. Paths accept ~ and $VARS. "
                "Empty tool/helper strings are auto-discovered on PATH.",

    # where finished audio lands
    "storage_dir": "~/Music/Audio Downloader",

    # optional archive copies; null or "" disables
    "mirrors": {
        "captures": None,      # Spotify captures mirror here
        "downloads": None,     # yt-dlp / SoundCloud rips mirror here
    },

    # runtime scratch: pidfile, queue, segment dir, silenced-from marker
    "state_dir": "~/Library/Application Support/Audio Downloader/state",
    "log_file": "~/Library/Application Support/Audio Downloader/capture.log",

    # audio routing
    "silent_output_device": "BlackHole 2ch",
    "multi_output_name": "Audio Downloader",

    # external tools ("" = auto-discover)
    "tools": {
        "switchaudiosource": "",
        "ffmpeg": "",
        "ffprobe": "",
        "yt_dlp": "",
    },

    # bundled helper binaries ("" = look next to this file)
    "helpers": {
        "avrec": "",
        "avrec_app": "",
        "spotify_ctl": "",
        "multiout": "",
        "ensure_rate": "",
    },

    # reverse-DNS prefix for the throwaway launchd jobs that own each
    # avrec/avrec-app process (see record-audio: TCC responsible-process fix)
    "launchd_prefix": "com.audiodownloader",

    # web app
    # Loopback only. Nothing outside this Mac can reach the app; there is no
    # authentication, so a wildcard bind would expose settings and the
    # capture queue to everything on the network.
    "server": {"host": "127.0.0.1", "port": 8765},

    # free-text search / version steering. All blank means "yt-dlp ytsearch
    # only" — the baseline that works with no credentials. Supplying Spotify
    # Client-Credentials turns on the richer Spotify arm of the resolver.
    "search": {
        "spotify": {"client_id": "", "client_secret": ""},
        # Which source wins when scores are tied. "spotify" (default) favours
        # the richer metadata; "soundcloud" favours unofficial/VIP edits.
        "prefer": "spotify",
    },

    # DJ pools (BPMSupreme / DJcity / Hypeddit). Cookie-authenticated, so
    # paste exported browser cookies into these files. All off until a path
    # is set AND enabled.
    "pools": {
        "bpmsupreme": {"enabled": False, "cookies_file": ""},
        "djcity":     {"enabled": False, "cookies_file": ""},
        "hypeddit":   {"enabled": False, "cookies_file": ""},
    },

    # notifications — all OFF by default. Nothing leaves the machine.
    "notify": {
        "prefix": "",
        # macOS Notification Centre banners. The only channel on by default —
        # it is local, needs no credentials and nothing leaves the machine.
        "desktop": {"enabled": True},
        "telegram": {
            "enabled": False,
            "token": "",
            "chat_id": "",
            "token_file": "",
            "chat_id_file": "",
            "send_audio_helper": "",
            "autosend_captures": False,
            # Inbound control: when on, the server long-polls getUpdates from
            # the same bot token and accepts URLs/commands from the locked
            # chat_id only. Independent of `enabled` so people can keep the
            # outbound pipe without opening the inbound channel.
            "control_enabled": False,
            # Push-side watcher. When control_enabled is on, a background
            # thread posts "capturing: ...", "done: ..." and failure messages
            # as the state machine advances, so an iPhone sees progress the
            # way the web UI does. Opt out with false if you prefer to poll
            # by messaging `status` yourself.
            "push_updates": True,
        },
        "imessage": {
            "enabled": False,
            "recipient": "",
            "attach_helper": "",
            "send_lock": "",
        },
    },
}

_SEARCH_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin")


def _expand(v):
    if v in (None, "", False):
        return None
    return Path(os.path.expandvars(os.path.expanduser(str(v))))


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def config_path() -> Path:
    env = os.environ.get("AUDIO_DOWNLOADER_CONFIG")
    return Path(os.path.expanduser(env)) if env else DEFAULT_CONFIG_PATH


def load(write_if_missing: bool = True) -> dict:
    p = config_path()
    raw = {}
    if p.exists():
        try:
            raw = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            raw = {}
    elif write_if_missing:
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(DEFAULTS, indent=2) + "\n")
        except OSError:
            pass
    return _deep_merge(DEFAULTS, raw)


def find_tool(name: str, configured: str = "") -> str:
    """Resolve an external tool. Configured path wins; else PATH; else the
    usual Homebrew prefixes (arm64 and Intel); else bare name."""
    if configured:
        p = _expand(configured)
        if p and p.exists():
            return str(p)
    w = shutil.which(name)
    if w:
        return w
    for d in _SEARCH_DIRS:
        c = Path(d) / name
        if c.exists():
            return str(c)
    return name


def find_helper(name: str, configured: str = "") -> Path:
    """Resolve a bundled helper binary (avrec-app, spotify-ctl)."""
    if configured:
        p = _expand(configured)
        if p:
            return p
    local = BIN_DIR / name
    if local.exists():
        return local
    w = shutil.which(name)
    return Path(w) if w else local


class Settings:
    """Flat, resolved view of the config. Construct once at import time."""

    def __init__(self, cfg: dict):
        self.raw = cfg
        self.path = config_path()

        self.storage = _expand(cfg["storage_dir"]) or (Path.home() / "Music" / APP_NAME)
        self.state_dir = _expand(cfg["state_dir"]) or (SUPPORT_DIR / "state")
        self.log_file = _expand(cfg["log_file"]) or (SUPPORT_DIR / "capture.log")

        m = cfg.get("mirrors") or {}
        self.mirror_captures = _expand(m.get("captures"))
        self.mirror_downloads = _expand(m.get("downloads"))

        self.silent_output = cfg.get("silent_output_device") or "BlackHole 2ch"
        self.launchd_prefix = cfg.get("launchd_prefix") or "com.audiodownloader"
        self.multi_output = cfg.get("multi_output_name") or APP_NAME

        t = cfg.get("tools") or {}
        self.switch = find_tool("SwitchAudioSource", t.get("switchaudiosource", ""))
        self.ffmpeg = find_tool("ffmpeg", t.get("ffmpeg", ""))
        self.ffprobe = find_tool("ffprobe", t.get("ffprobe", ""))
        self.ytdlp = find_tool("yt-dlp", t.get("yt_dlp", ""))

        h = cfg.get("helpers") or {}
        self.avrec = find_helper("avrec", h.get("avrec", ""))
        self.avrec_app = find_helper("avrec-app", h.get("avrec_app", ""))
        self.spotify_ctl = find_helper("spotify-ctl", h.get("spotify_ctl", ""))
        self.multiout = find_helper("ad-multiout", h.get("multiout", ""))
        self.ensure_rate = find_helper("ensure-blackhole-rate", h.get("ensure_rate", ""))

        s = cfg.get("server") or {}
        self.host = os.environ.get("AUDIO_DOWNLOADER_HOST", s.get("host") or "127.0.0.1")
        self.port = int(os.environ.get("AUDIO_DOWNLOADER_PORT", s.get("port") or 8765))

        n = cfg.get("notify") or {}
        self.prefix = n.get("prefix") or ""
        self.desktop_enabled = bool((n.get("desktop") or {}).get("enabled", True))
        tg = n.get("telegram") or {}
        self.tg_enabled = bool(tg.get("enabled"))
        self.tg_token = tg.get("token") or ""
        self.tg_chat = tg.get("chat_id") or ""
        self.tg_token_file = _expand(tg.get("token_file"))
        self.tg_chat_file = _expand(tg.get("chat_id_file"))
        self.tg_audio_helper = _expand(tg.get("send_audio_helper"))
        self.tg_autosend = bool(tg.get("autosend_captures"))
        self.tg_control_enabled = bool(tg.get("control_enabled"))
        im = n.get("imessage") or {}
        self.im_enabled = bool(im.get("enabled"))
        self.im_recipient = im.get("recipient") or ""
        self.im_attach_helper = _expand(im.get("attach_helper"))
        self.im_send_lock = _expand(im.get("send_lock"))

    def telegram_creds(self):
        """(token, chat_id) or (None, None). Inline values win over files."""
        if not self.tg_enabled:
            return None, None
        token, chat = self.tg_token, self.tg_chat
        try:
            if not token and self.tg_token_file and self.tg_token_file.exists():
                token = self.tg_token_file.read_text().strip()
            if not chat and self.tg_chat_file and self.tg_chat_file.exists():
                chat = self.tg_chat_file.read_text().strip()
        except OSError:
            pass
        return (token or None), (chat or None)

    def ensure_dirs(self):
        for d in (self.storage, self.state_dir, self.log_file.parent):
            try:
                d.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass


SETTINGS = Settings(load())


def _describe():
    s = SETTINGS
    rows = [
        ("config file", s.path),
        ("storage", s.storage),
        ("mirror: captures", s.mirror_captures or "(off)"),
        ("mirror: downloads", s.mirror_downloads or "(off)"),
        ("state dir", s.state_dir),
        ("log file", s.log_file),
        ("silent device", s.silent_output),
        ("multi-output", s.multi_output),
        ("launchd prefix", s.launchd_prefix),
        ("SwitchAudioSource", s.switch),
        ("ffmpeg", s.ffmpeg),
        ("ffprobe", s.ffprobe),
        ("yt-dlp", s.ytdlp),
        ("avrec", s.avrec),
        ("avrec-app", s.avrec_app),
        ("spotify-ctl", s.spotify_ctl),
        ("ad-multiout", s.multiout),
        ("ensure-blackhole-rate", s.ensure_rate),
        ("server", f"{s.host}:{s.port}"),
        ("desktop notify", "on" if s.desktop_enabled else "off"),
        ("telegram", "on" if s.tg_enabled else "off"),
        ("imessage", "on" if s.im_enabled else "off"),
    ]
    w = max(len(k) for k, _ in rows)
    out = [f"{k.rjust(w)} : {v}" for k, v in rows]
    missing = [n for n, p in (("SwitchAudioSource", s.switch), ("ffmpeg", s.ffmpeg),
                              ("ffprobe", s.ffprobe), ("yt-dlp", s.ytdlp))
               if "/" not in str(p)]
    for n, p in (("avrec", s.avrec), ("avrec-app", s.avrec_app), ("spotify-ctl", s.spotify_ctl),
                 ("ad-multiout", s.multiout), ("ensure-blackhole-rate", s.ensure_rate)):
        if not Path(p).exists():
            missing.append(n)
    if missing:
        out.append("")
        out.append("MISSING: " + ", ".join(missing))
    return "\n".join(out)


if __name__ == "__main__":
    print(_describe())
