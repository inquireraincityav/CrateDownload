"""tg_control — inbound Telegram control loop for Audio Downloader.

Long-polls the configured bot's getUpdates and feeds URLs and short commands
back into the local server. The bot token and chat_id are the same ones the
outbound notifier already uses; only messages from the configured chat_id
are honoured, so the "nothing leaves the machine without you asking" posture
holds — if your token or chat ID weren't in config.json, nothing happens.

Opt-in via notify.telegram.control_enabled. Independent of
notify.telegram.enabled so you can keep the outbound notifier without the
inbound command channel, or vice versa.

Commands (case-insensitive, slash optional):
    status            current capture + queue
    recent            last 10 finished files
    stop              kill the current capture
    resend <name>     re-fire the Telegram upload for a recent file (substring match)
    help              list these

Anything containing a Spotify / YouTube / SoundCloud URL is submitted the
same way a paste into the web UI would be.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

API_TIMEOUT = 30          # getUpdates long-poll timeout
POLL_IDLE = 2.0           # sleep between polls on transient errors
OFFSET_FILENAME = "tg_control.offset"


def _state_offset_path(state_dir: Path) -> Path:
    return state_dir / OFFSET_FILENAME


def _load_offset(state_dir: Path) -> int:
    try:
        return int(_state_offset_path(state_dir).read_text().strip())
    except (OSError, ValueError):
        return 0


def _save_offset(state_dir: Path, offset: int) -> None:
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        _state_offset_path(state_dir).write_text(str(offset))
    except OSError:
        pass


def _api(token: str, method: str, params: dict | None = None, timeout: int = API_TIMEOUT):
    """One synchronous Telegram Bot API call. Returns parsed JSON or None."""
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(params or {}).encode() if params else None
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=timeout + 5) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _send(token: str, chat_id: str, text: str) -> None:
    try:
        _api(token, "sendMessage",
             {"chat_id": chat_id, "text": text[:3900],
              "disable_web_page_preview": "true"}, timeout=15)
    except Exception:
        pass


def _bar(percent: float, width: int = 20) -> str:
    """Monospace progress bar — renders cleanly in Telegram's default font."""
    try:
        p = max(0.0, min(100.0, float(percent or 0)))
    except (TypeError, ValueError):
        p = 0.0
    filled = int(round(p / 100.0 * width))
    return "[" + "█" * filled + "░" * (width - filled) + "]"


def _fmt_secs(s) -> str:
    try:
        s = int(round(float(s)))
    except (TypeError, ValueError):
        return "?"
    m, s = divmod(max(0, s), 60)
    return f"{m}:{s:02d}"


def _format_progress(progress) -> str:
    """Multi-line status reply with a progress bar. Format:

        ▶ capturing: Fisher - Losing It (Extended Mix)
        [████████░░░░░░░░░░░░] 42% · 1:35 / 3:42
        3 of 20 tracks done · in: My Favorite Playlist
    """
    if not progress:
        return "idle."
    track = progress.get("track") or "(reading log…)"
    stage = progress.get("stage") or "capturing"
    pct = progress.get("percent")
    done = progress.get("done_count") or 0
    container = progress.get("container")
    elapsed = progress.get("elapsed")
    duration = progress.get("duration")

    lines = [f"▶ {stage}: {track}"]
    if pct is not None:
        time_tag = f" · {_fmt_secs(elapsed)} / {_fmt_secs(duration)}" if duration else ""
        lines.append(f"{_bar(pct)} {pct}%{time_tag}")
    tail_bits = []
    if done:
        tail_bits.append(f"{done} track{'s' if done != 1 else ''} done")
    if container:
        tail_bits.append(f"in: {container}")
    if tail_bits:
        lines.append(" · ".join(tail_bits))
    return "\n".join(lines)


def _format_recent(files, limit=10) -> str:
    if not files:
        return "(no finished captures yet)"
    lines = []
    for f in files[:limit]:
        name = f.get("name") or Path(f.get("path", "?")).name
        when = f.get("when") or ""
        lines.append(f"• {name}{(' — ' + when) if when else ''}")
    return "\n".join(lines)


def _find_recent_match(files, needle: str):
    needle = needle.lower()
    for f in files:
        name = (f.get("name") or Path(f.get("path", "")).name).lower()
        if needle in name:
            return f
    return None


def _run_loop(settings, submit_fn, progress_fn, recent_fn, stop_fn,
              resend_fn, log_fn):
    """The polling loop. Runs forever on its daemon thread; exits only on
    process shutdown. All exceptions are swallowed and logged so a flaky
    network or a transient 502 can never take the main server down."""
    token, chat_id = settings.telegram_creds()
    if not (token and chat_id):
        log_fn("tg_control: no credentials; aborting")
        return
    chat_id = str(chat_id)
    state_dir = settings.state_dir
    offset = _load_offset(state_dir)
    log_fn(f"tg_control: listening (chat {chat_id}, offset {offset})")

    while True:
        try:
            resp = _api(token, "getUpdates", {
                "timeout": API_TIMEOUT,
                "offset": offset,
                "allowed_updates": json.dumps(["message"]),
            })
            if not resp or not resp.get("ok"):
                log_fn(f"tg_control: getUpdates not ok: {resp}")
                time.sleep(POLL_IDLE)
                continue
            for upd in resp.get("result", []):
                offset = max(offset, upd["update_id"] + 1)
                msg = upd.get("message") or {}
                m_chat = str((msg.get("chat") or {}).get("id") or "")
                text = (msg.get("text") or "").strip()
                if m_chat != chat_id:
                    log_fn(f"tg_control: ignored message from chat {m_chat}")
                    continue
                if not text:
                    continue
                _handle(settings, token, chat_id, text,
                        submit_fn, progress_fn, recent_fn, stop_fn,
                        resend_fn, log_fn)
            _save_offset(state_dir, offset)
        except Exception as e:
            log_fn(f"tg_control: loop error: {type(e).__name__}: {e}")
            time.sleep(POLL_IDLE)


_URL_HINT = ("send a Spotify / YouTube / SoundCloud link, or:\n"
             "status · recent · stop · resend <name> · help")


def _handle(settings, token, chat_id, text,
            submit_fn, progress_fn, recent_fn, stop_fn, resend_fn, log_fn):
    body = text.strip()
    low = body.lstrip("/").strip().lower()
    first = low.split(None, 1)[0] if low else ""

    if first in ("status", "s"):
        _send(token, chat_id, _format_progress(progress_fn()))
        return
    if first == "recent":
        _send(token, chat_id, _format_recent(recent_fn()))
        return
    if first == "stop":
        ok, note = stop_fn()
        _send(token, chat_id, note if note else ("stopped." if ok else "nothing to stop."))
        return
    if first == "resend":
        needle = body.split(None, 1)[1].strip() if len(body.split(None, 1)) > 1 else ""
        if not needle:
            _send(token, chat_id, "usage: resend <filename substring>")
            return
        match = _find_recent_match(recent_fn(), needle)
        if not match:
            _send(token, chat_id, f"no recent file matches {needle!r}.")
            return
        resend_fn(match.get("path") or "")
        _send(token, chat_id, f"re-firing: {Path(match.get('path','')).name}")
        return
    if first in ("help", "h", "?"):
        _send(token, chat_id, _URL_HINT)
        return

    # Anything else: let submit() hunt for URLs. Its own regex decides what's valid.
    found = submit_fn(body)
    if not found:
        _send(token, chat_id, _URL_HINT)
        return
    rejected = [f for f in found if f["status"] == "rejected"]
    accepted = [f for f in found if f["status"] != "rejected"]
    bits = []
    if accepted:
        bits.append("queued " + ", ".join(
            f"{f['src']}:{f['kind']}" for f in accepted))
    if rejected:
        bits.append("rejected: " + ", ".join(
            Path(f["url"]).name or f["url"] for f in rejected))
    _send(token, chat_id, " · ".join(bits) or "accepted.")


PUSH_POLL = 8.0           # seconds between watcher polls — low enough to feel
                          # live on a phone, high enough not to tax the bot API


def _watcher_loop(settings, progress_fn, submits_fn, log_fn):
    """Push live updates to Telegram as the capture state changes. Fires at
    track transitions and on errors — NOT on every poll — so the chat doesn't
    get spammed by heartbeats.

    State we track between polls (local to this thread):
    - last track string we announced
    - last done_count we announced
    - set of submit entries we've already pinged a failure for
    """
    token, chat_id = settings.telegram_creds()
    if not (token and chat_id):
        return
    chat_id = str(chat_id)

    last_track: str | None = None
    last_done: int = 0
    last_container: str | None = None
    seen_errors: set = set()
    was_capturing: bool = False

    log_fn("tg_control: push watcher started")

    while True:
        time.sleep(PUSH_POLL)
        try:
            # --- capture-side transitions --------------------------------------
            p = progress_fn()
            if p:
                container = p.get("container")
                track = p.get("track")
                done = int(p.get("done_count") or 0)

                # Album/playlist boundary: a new container shows up, reset
                # per-batch counters so a "finished" line fires fresh later.
                if container and container != last_container:
                    last_container = container
                    last_done = 0

                # Each new track worth announcing exactly once.
                if track and track != last_track:
                    _send(token, chat_id, f"▶ Capturing: {track}"
                          + (f"\n  in: {container}" if container else ""))
                    last_track = track

                # A single-track completion inside a batch — only report when
                # done_count has advanced AND we've just moved off a track.
                if done > last_done and last_track is not None:
                    last_done = done

                was_capturing = True

            elif was_capturing:
                # Transition: capture just finished. Report once per batch.
                tag = f" from {last_container}" if last_container else ""
                _send(token, chat_id,
                      f"✅ Done{tag}" +
                      (f" · {last_done} track{'s' if last_done != 1 else ''}"
                       if last_done else ""))
                last_track = None
                last_done = 0
                last_container = None
                was_capturing = False

            # --- submit-side failures ------------------------------------------
            for s in (submits_fn() or [])[:30]:
                if s.get("status") not in ("error", "rejected"):
                    continue
                # Dedup by (url, t) — each submit entry's timestamp is unique.
                key = (s.get("url") or "", s.get("t") or 0)
                if key in seen_errors:
                    continue
                seen_errors.add(key)
                reason = (s.get("result") or "")[:200] or "unknown reason"
                _send(token, chat_id,
                      f"⚠️ Failed: {s.get('url')}\n  {reason}")

            # Keep the dedup set small.
            if len(seen_errors) > 500:
                seen_errors = set(list(seen_errors)[-200:])

        except Exception as e:
            log_fn(f"tg_control watcher error: {type(e).__name__}: {e}")


def start(settings, submit_fn, progress_fn, recent_fn, stop_fn, resend_fn,
          log_fn, submits_fn=None):
    """Fire the control loop in a daemon thread if config permits.

    `settings` is the shared adcfg.SETTINGS. The server passes its own
    callables for everything that touches server state (submit, recent files,
    live progress, stop, resend, submits_fn for the push watcher) so this
    module stays free of imports from `audio-downloader` — nothing to
    circular-import, nothing to lock across.
    """
    if not getattr(settings, "tg_control_enabled", False):
        return None
    token, chat_id = settings.telegram_creds()
    if not (token and chat_id):
        log_fn("tg_control: enabled but no token/chat_id in config; skipping")
        return None
    threads = []
    t = threading.Thread(
        target=_run_loop,
        args=(settings, submit_fn, progress_fn, recent_fn, stop_fn,
              resend_fn, log_fn),
        name="tg-control", daemon=True)
    t.start()
    threads.append(t)

    # Push watcher is opt-in-but-default-on when control is enabled, so
    # most users get the "feels like a notification stream" behaviour.
    push_enabled = (settings.raw.get("notify", {})
                            .get("telegram", {})
                            .get("push_updates", True))
    if push_enabled and submits_fn is not None:
        tw = threading.Thread(
            target=_watcher_loop,
            args=(settings, progress_fn, submits_fn, log_fn),
            name="tg-push", daemon=True)
        tw.start()
        threads.append(tw)

    return threads
