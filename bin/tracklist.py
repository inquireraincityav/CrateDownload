"""tracklist — parse DJ-set tracklists out of YouTube descriptions / pinned
comments, and (opportunistically) 1001tracklists set pages.

The source material is almost always one of three shapes:

    1. Timestamped list in a YouTube description or pinned comment:

           00:00 Fisher - Losing It
           03:12 Chris Lake - Turn Off The Lights (Extended Mix)
           ...

    2. Numbered list without timestamps:

           1. Artist - Title
           2. Artist - Title

    3. A 1001tracklists.com set page (HTML). Harder — the markup varies
       per-skin and the site rate-limits aggressively. We extract what
       OpenGraph+JSON-LD gives us for free; anything deeper is on the
       caller to do by hand.

Returns a list of {"artist": ..., "title": ..., "version": ..., "query": ...}
dicts. The `query` is the "Artist - Title (Version)" string that goes
straight into the free-text search layer (see search.py).

Pure stdlib. The 1001tracklists call uses urllib so it inherits the system
proxy. All exceptions are swallowed; the caller sees an empty list on
failure rather than a crashing dispatcher.
"""
from __future__ import annotations

import json
import re
import urllib.request
from pathlib import Path

import naming

_YT_WATCH_RE = re.compile(r"(?:youtu\.be/|youtube\.com/watch\?v=)([A-Za-z0-9_-]{6,})")
_1001_RE = re.compile(r"1001tracklists\.com/")

# "00:00", "0:00", "1:02:03" — the leading timestamp of a tracklist line.
_TS_RE = re.compile(r"^\s*\[?(?:\d{1,2}:)?\d{1,2}:\d{2}\]?\s+")
# "1.", "01)", "#1", "1 -" numbering.
_NUM_RE = re.compile(r"^\s*(?:#\s*)?\d{1,3}\s*[\.\):\-]\s*")

# A line that is just whitespace, or a URL, or clearly a credit, isn't a track.
_SKIP_RE = re.compile(
    r"^\s*(?:https?://\S+|follow|subscribe|instagram|facebook|twitter|discord|"
    r"soundcloud|spotify|apple music|track\s*list|tracklist[:\s]*$|"
    r"support\s+the\s+artist|download\s+link)",
    re.IGNORECASE,
)


def _looks_like_track(line: str) -> bool:
    s = line.strip()
    if not s or len(s) < 5 or len(s) > 240:
        return False
    if _SKIP_RE.match(s):
        return False
    return " - " in s or " – " in s or " — " in s


def _strip_leader(line: str) -> str:
    s = _TS_RE.sub("", line)
    s = _NUM_RE.sub("", s)
    return s.strip()


def parse_text(text: str) -> list[dict]:
    """Pull tracks out of a free-form chunk of text (YT description, pinned
    comment, pasted tracklist). Uses the shared `naming.split` so the artist
    / title / version fields look exactly like the ones a filename-cleaned
    yt-dlp download would carry."""
    out = []
    seen = set()
    for raw_line in (text or "").splitlines():
        line = _strip_leader(raw_line)
        if not _looks_like_track(line):
            continue
        artist, title, version = naming.split(line)
        if not (artist and title):
            continue
        query = f"{artist} - {title}" + (f" ({version})" if version else "")
        key = query.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({"artist": artist, "title": title, "version": version,
                    "query": query})
    return out


# --- 1001tracklists ----------------------------------------------------------

_1001_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/17.4 Safari/605.1.15")


def _fetch(url: str, timeout: int = 20) -> str | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _1001_UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read(2_000_000)   # cap at 2MB
            return data.decode("utf-8", "replace")
    except Exception:
        return None


_JSONLD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE)


def _tracks_from_jsonld(html: str) -> list[dict]:
    """1001tracklists set pages embed a Schema.org MusicPlaylist with a
    `track` array. When the markup is intact, this is the clean path."""
    out = []
    for m in _JSONLD_RE.finditer(html):
        try:
            blob = json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            continue
        items = blob if isinstance(blob, list) else [blob]
        for item in items:
            if not isinstance(item, dict):
                continue
            tracks = item.get("track") or item.get("itemListElement") or []
            if isinstance(tracks, dict):
                tracks = [tracks]
            for t in tracks:
                if not isinstance(t, dict):
                    continue
                inner = t.get("item") if "item" in t else t
                name = (inner or {}).get("name") or ""
                perf = (inner or {}).get("byArtist") or ""
                if isinstance(perf, dict):
                    perf = perf.get("name") or ""
                if isinstance(perf, list):
                    perf = ", ".join(p.get("name", "") if isinstance(p, dict) else str(p)
                                      for p in perf)
                raw = f"{perf} - {name}".strip(" -") if perf else name
                if not raw:
                    continue
                artist, title, version = naming.split(raw)
                if not (artist and title):
                    continue
                out.append({"artist": artist, "title": title,
                            "version": version,
                            "query": f"{artist} - {title}" +
                                     (f" ({version})" if version else "")})
    return out


def parse_1001tracklists(url: str) -> list[dict]:
    """Fetch a 1001tracklists set page and extract its tracks. Returns [] on
    network failure, blocked response or layout we can't parse — never raises."""
    if not _1001_RE.search(url or ""):
        return []
    html = _fetch(url)
    if not html:
        return []
    tracks = _tracks_from_jsonld(html)
    if tracks:
        return tracks
    # Fallback: plaintext scrape of visible <meta description> if present.
    m = re.search(r'<meta name="description" content="([^"]+)"', html,
                  re.IGNORECASE)
    if m:
        return parse_text(m.group(1).replace(" | ", "\n"))
    return []


# --- top-level dispatcher ----------------------------------------------------


def parse(source: str) -> list[dict]:
    """Accept a URL, raw text, or a file path and return tracks found. The
    caller is expected to feed each returned `query` into search.py.

    - 1001tracklists URL        -> parse_1001tracklists
    - YouTube URL + no text     -> caller must fetch description first;
                                   we don't shell out to yt-dlp here to
                                   keep this file free of side effects.
    - Everything else           -> parse_text on the string itself (reads
                                   from disk first if it's a path).
    """
    s = (source or "").strip()
    if not s:
        return []
    if _1001_RE.search(s):
        return parse_1001tracklists(s)
    p = Path(s)
    if len(s) < 1024 and p.is_file():
        try:
            s = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
    return parse_text(s)


# --- optional YouTube description fetch via yt-dlp --------------------------


def youtube_description(url: str, ytdlp: str) -> str:
    """Pull the description + top pinned comment of a YouTube URL via yt-dlp
    (which already ships with Audio Downloader for the actual downloads).
    Returns the concatenated text, or "" on any failure."""
    if not _YT_WATCH_RE.search(url or ""):
        return ""
    import subprocess
    try:
        r = subprocess.run(
            [ytdlp, "--skip-download", "--dump-single-json",
             "--write-comments", "--extractor-args", "youtube:max_comments=200",
             url],
            capture_output=True, text=True, timeout=90)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if r.returncode != 0 or not r.stdout:
        return ""
    try:
        meta = json.loads(r.stdout)
    except json.JSONDecodeError:
        return ""
    chunks = [meta.get("description") or ""]
    for c in (meta.get("comments") or []):
        # Pinned or hearted comments by the channel owner are the most likely
        # home for a tracklist posted after upload.
        if c.get("is_pinned") or c.get("author_is_uploader"):
            chunks.append(c.get("text") or "")
    return "\n\n".join(chunk for chunk in chunks if chunk)
