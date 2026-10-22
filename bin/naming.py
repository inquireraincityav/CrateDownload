"""naming — clean up messy track titles from yt-dlp.

yt-dlp's `%(title)s` is whatever the uploader typed. For an SoundCloud / YouTube
rip that's typically something like:

    "ARTIST — TRACK NAME (Official Audio) [FREE DL] [320kbps]"
    "Fisher - Losing It (Extended Mix) [FREE DOWNLOAD]"
    "[Premiere] Artist - Title [LABEL001]"

This module normalises those into a clean (artist, title, version) tuple so the
file can be renamed and its ID3 tags rewritten. Pure string work — no network
calls, so it can't fail or stall. A MusicBrainz lookup is a separate opt-in
layer that runs on top when enabled.

Deliberately conservative. If the parser can't confidently pull an artist out,
it returns `artist=""` and leaves the whole string as the title — better to
keep the uploader's wording than to invent one.
"""
from __future__ import annotations

import re
from pathlib import Path

# Fragments that are almost always noise and never part of a real title.
# Matched case-insensitively, with surrounding brackets stripped later.
_NOISE_PATTERNS = [
    r"official\s*(?:music\s*)?video",
    r"official\s*audio",
    r"official\s*lyric\s*video",
    r"lyric\s*video",
    r"official\s*visualizer",
    r"audio\s*only",
    r"hd(?:\s*audio)?",
    r"4k",
    r"full\s*song",
    r"full\s*track",
    r"free\s*(?:download|dl|d/?l)",
    r"free\s*download\s*in\s*description",
    r"d/l\s*in\s*description",
    r"mp3(?:\s*320(?:kbps)?)?",
    r"320\s*kbps",
    r"premiere",
    r"out\s*now",
    r"video\s*clip",
    r"with\s*lyrics",
    r"audio",
]

_NOISE_RE = re.compile(
    r"\s*[\[\(][^\]\)]*?(?:" + "|".join(_NOISE_PATTERNS) + r")[^\]\)]*?[\]\)]",
    re.IGNORECASE,
)

# Catalogue / label tags like [SPINNIN042], [MONSTERCAT], (ANJ123). We strip
# these aggressively — they're never part of the artistic title. Heuristic:
# ALL-CAPS or CamelCase token followed by digits, inside brackets.
_LABEL_TAG_RE = re.compile(r"\s*[\[\(]\s*[A-Z][A-Z0-9]{2,}\s*\d{2,}\s*[\]\)]")

# Version markers we DO want to keep but as a separate field for downstream
# consumers (version steering). Order matters — longer forms first.
_VERSION_RE = re.compile(
    r"[\(\[\s]*("
    r"vip\s*(?:mix|edit)?"
    r"|extended\s*(?:mix|version|edit)?"
    r"|club\s*(?:mix|edit)?"
    r"|radio\s*(?:mix|edit|version)?"
    r"|dub\s*(?:mix)?"
    r"|instrumental"
    r"|acapella"
    r"|a\s*cappella"
    r"|original\s*mix"
    r"|remix(?:\s+by\s+[^)\]]+)?"
    r"|(?:[^()\[\]]+?\s+remix)"
    r"|edit"
    r"|bootleg"
    r")[\)\]\s]*",
    re.IGNORECASE,
)

# Several dash variants show up in uploaded titles.
_DASH_RE = re.compile(r"\s+[\-‐‑‒–—―~⁃]\s+")

# "feat. X" / "ft. X" moves onto the title side if it slipped onto the artist.
_FEAT_RE = re.compile(r"\s+(?:feat\.?|ft\.?|featuring)\s+", re.IGNORECASE)


def _strip_brackets(s: str) -> str:
    """Remove empty bracket pairs left over after noise/label removal."""
    s = re.sub(r"\s*\(\s*\)|\s*\[\s*\]", "", s)
    return re.sub(r"\s{2,}", " ", s).strip(" -–—")


def clean_raw(raw: str) -> str:
    """Strip boilerplate noise and label tags. Keeps version markers intact
    for `split()` to lift into its own field."""
    s = raw.strip()
    # Repeat because overlapping brackets can leave fragments after one pass.
    for _ in range(3):
        s = _NOISE_RE.sub("", s)
        s = _LABEL_TAG_RE.sub("", s)
    return _strip_brackets(s)


def _extract_version(s: str) -> tuple[str, str]:
    """Pull a trailing version marker off the title. Returns (title_core,
    version). The version keeps its original casing where possible."""
    # Prefer a bracketed version at the very end (common case).
    m = re.search(r"[\(\[]\s*([^\(\)\[\]]+?)\s*[\)\]]\s*$", s)
    if m and _VERSION_RE.fullmatch(m.group(1).strip()):
        return s[:m.start()].rstrip(" -–—"), m.group(1).strip()
    # Fallback: unbracketed trailing version marker (e.g. "- Extended Mix").
    m2 = re.search(r"\s+[\-–—]\s+([A-Za-z][A-Za-z ']+?(?:mix|edit|version|remix|bootleg|instrumental|dub|vip))\s*$",
                   s, re.IGNORECASE)
    if m2 and _VERSION_RE.fullmatch(m2.group(1).strip()):
        return s[:m2.start()].rstrip(), m2.group(1).strip()
    return s, ""


def split(raw: str) -> tuple[str, str, str]:
    """Split a messy title into (artist, title, version).

    Rules:
    - Strip boilerplate noise and catalogue tags.
    - If the result contains an " - " separator, left is artist, right is title.
    - Otherwise return the whole thing as the title with no artist.
    - Pull a trailing version marker (Extended Mix, VIP, …) into its own field.
    """
    cleaned = clean_raw(raw)
    if not cleaned:
        return "", raw.strip(), ""

    core, version = _extract_version(cleaned)
    parts = _DASH_RE.split(core, maxsplit=1)
    if len(parts) == 2:
        artist = parts[0].strip()
        title = parts[1].strip()
    else:
        artist, title = "", core.strip()

    # If the artist side was blank after stripping, treat the whole thing as title.
    if not title:
        title, artist = artist, ""

    return artist, title, version


def filename_for(artist: str, title: str, version: str, ext: str = ".mp3",
                 sanitize=None, index=None) -> str:
    """Build a filesystem name from parsed fields. Version lives in parens
    on the title side so filesystem-sort by filename still groups a track's
    variants together. `index` (1-based) prefixes the name with a zero-padded
    "NN - " so a DJ set's tracks sort in play order."""
    base = title
    if version:
        base = f"{title} ({version})"
    name = f"{artist} - {base}" if artist else base
    if sanitize is not None:
        name = sanitize(name)
    if index is not None:
        try:
            name = f"{int(index):02d} - {name}"
        except (TypeError, ValueError):
            pass
    return f"{name}{ext}"


def retag_file(path: Path, artist: str, title: str, version: str,
               log=lambda m: None) -> bool:
    """Rewrite TIT2 / TPE1 on an mp3 in-place. Keeps every other tag
    (artwork, year, track number) untouched. Returns True on success."""
    try:
        from mutagen.id3 import ID3, TIT2, TPE1, ID3NoHeaderError
    except ImportError:
        log("naming.retag_file: mutagen not installed; skipping")
        return False
    try:
        try:
            tags = ID3(str(path))
        except ID3NoHeaderError:
            tags = ID3()
        tit = f"{title} ({version})" if version else title
        if tit:
            tags["TIT2"] = TIT2(encoding=3, text=tit)
        if artist:
            tags["TPE1"] = TPE1(encoding=3, text=artist)
        tags.save(str(path), v2_version=3)
        return True
    except Exception as e:
        log(f"naming.retag_file error for {path}: {type(e).__name__}: {e}")
        return False


def rename_and_retag(path: Path, raw_title: str, sanitize=None,
                     log=lambda m: None, index=None) -> Path:
    """Clean `raw_title`, retag `path` in place, and rename it to the clean
    name. Returns the new path (same as input if nothing changed, or if the
    cleaned name collides with another file in the same folder — in that case
    we tag but don't rename, to avoid clobbering a resume target).

    `index` (1-based) prefixes the saved filename with "NN - " so a DJ set's
    tracks sort by play order on disk.
    """
    artist, title, version = split(raw_title)
    if not (artist or title):
        return path
    retag_file(path, artist, title, version, log=log)
    new_name = filename_for(artist, title, version, ext=path.suffix,
                            sanitize=sanitize, index=index)
    if new_name == path.name:
        return path
    dest = path.with_name(new_name)
    if dest.exists() and dest != path:
        log(f"naming: skipping rename of {path.name}: {dest.name} already exists")
        return path
    try:
        path.rename(dest)
        return dest
    except OSError as e:
        log(f"naming: rename failed {path} -> {dest}: {e}")
        return path
