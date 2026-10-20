"""search — resolve a free-text track query to a playable URL.

Takes a string like "Fisher - Losing It VIP Mix" and returns one of:

    {"src": "spotify",    "url": "https://open.spotify.com/track/..."}
    {"src": "youtube",    "url": "https://www.youtube.com/watch?v=..."}
    {"src": "soundcloud", "url": "https://soundcloud.com/..."}

…so the result can be fed straight into the existing submit() entrypoint
without the dispatcher learning anything new. Resolution is deliberately
tiered — the fastest source that scores well wins, so a Spotify-grade
mainstream track never pays the cost of a SoundCloud scrape.

Version-aware. If the query carries a version marker (VIP, Extended Mix,
<Artist> Remix, …), each candidate is scored on whether its own title also
carries it. A perfect title match beats a plain track every time, so the
"Fisher - Losing It VIP" query doesn't fall through to the original.

Spotify lookup uses the public /search endpoint via a short-lived
Client-Credentials token — opt-in via config.search.spotify_client_id /
client_secret. Without those we skip Spotify and go straight to yt-dlp's
`ytsearch`, which needs no credentials and already ships for the rip side.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.parse
import urllib.request

import naming

# How many search results to consider per source before picking.
TOP_N = 5

# How long a Spotify Client-Credentials token lasts; we re-fetch after this.
_SP_TOKEN_TTL = 3300    # Spotify grants 3600s; refresh a touch earlier.
_SP_TOKEN_CACHE: dict = {"token": "", "exp": 0.0}

# Version words that bias scoring. Lower-cased for compare; order-agnostic.
_VERSION_WORDS = {
    "vip", "extended", "club", "radio", "dub", "instrumental", "acapella",
    "original", "remix", "edit", "bootleg", "mix",
}


def _norm(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _score(query_tokens: set[str], candidate: str, want_version: str) -> float:
    """Simple token-overlap scorer, with a bonus for version-word match.
    1.0 is a perfect token match + matching version; 0 is unrelated."""
    cand_norm = _norm(candidate)
    cand_tokens = set(cand_norm.split())
    if not cand_tokens:
        return 0.0
    overlap = len(query_tokens & cand_tokens) / max(1, len(query_tokens))
    bonus = 0.0
    want = _norm(want_version)
    if want:
        want_toks = set(want.split()) & _VERSION_WORDS
        got_toks = cand_tokens & _VERSION_WORDS
        if want_toks and want_toks.issubset(got_toks):
            bonus = 0.4
        elif want_toks and not (want_toks & got_toks):
            # Penalise: user asked VIP, candidate is the plain mix.
            bonus = -0.25
    return min(1.0, overlap + bonus)


# --- Spotify ----------------------------------------------------------------


def _sp_token(client_id: str, client_secret: str) -> str | None:
    now = time.time()
    if _SP_TOKEN_CACHE["token"] and _SP_TOKEN_CACHE["exp"] > now:
        return _SP_TOKEN_CACHE["token"]
    try:
        import base64
        creds = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        req = urllib.request.Request(
            "https://accounts.spotify.com/api/token",
            data=b"grant_type=client_credentials",
            headers={"Authorization": f"Basic {creds}",
                     "Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode())
        tok = data.get("access_token") or ""
        if tok:
            _SP_TOKEN_CACHE["token"] = tok
            _SP_TOKEN_CACHE["exp"] = now + _SP_TOKEN_TTL
        return tok or None
    except Exception:
        return None


def _spotify_candidates(query: str, token: str) -> list[dict]:
    try:
        qs = urllib.parse.urlencode({"q": query, "type": "track", "limit": TOP_N})
        req = urllib.request.Request(
            f"https://api.spotify.com/v1/search?{qs}",
            headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode())
    except Exception:
        return []
    out = []
    for t in (data.get("tracks") or {}).get("items") or []:
        artists = ", ".join(a.get("name", "") for a in t.get("artists") or [])
        label = f"{artists} - {t.get('name','')}"
        out.append({"label": label, "url": t.get("external_urls", {}).get("spotify") or ""})
    return out


# --- YouTube / SoundCloud via yt-dlp ---------------------------------------


def _ytdlp_search(prefix: str, query: str, ytdlp: str) -> list[dict]:
    """Shell out to `yt-dlp "ytsearch5:QUERY" --dump-json --flat-playlist`.
    Works for ytsearch (YouTube) and scsearch (SoundCloud). Returns [] on any
    non-zero exit or empty stdout."""
    needle = f"{prefix}search{TOP_N}:{query}"
    try:
        r = subprocess.run(
            [ytdlp, "--dump-json", "--flat-playlist", "--skip-download",
             "--no-warnings", needle],
            capture_output=True, text=True, timeout=25)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if r.returncode != 0:
        return []
    out = []
    for line in r.stdout.splitlines():
        if not line.strip():
            continue
        try:
            m = json.loads(line)
        except json.JSONDecodeError:
            continue
        title = m.get("title") or ""
        url = m.get("url") or m.get("webpage_url") or ""
        if not url and m.get("id") and prefix == "yt":
            url = f"https://www.youtube.com/watch?v={m['id']}"
        if title and url:
            out.append({"label": title, "url": url})
    return out


# --- public entrypoint ------------------------------------------------------


def resolve(query: str, ytdlp: str, settings=None,
            prefer: str = "spotify",
            log=lambda m: None) -> dict | None:
    """Resolve `query` to one playable {src, url, label, score}. Returns
    None if no source yielded a candidate.

    `prefer` picks which source wins ties: "spotify" (default) favours
    reproducible metadata; "soundcloud" favours VIPs / unofficial edits
    that often don't exist on Spotify.
    """
    q = (query or "").strip()
    if not q:
        return None

    artist, title, version = naming.split(q)
    enriched = f"{artist} {title} {version}".strip() if (artist or title) else q
    log(f"search: {q!r} -> ({artist!r}, {title!r}, {version!r})")

    tokens = set(_norm(enriched).split())
    want_version = version

    results: list[tuple[float, str, dict]] = []

    sp_cid = sp_secret = ""
    if settings is not None:
        raw = (settings.raw.get("search") or {}).get("spotify") or {}
        sp_cid = raw.get("client_id") or ""
        sp_secret = raw.get("client_secret") or ""

    if sp_cid and sp_secret:
        tok = _sp_token(sp_cid, sp_secret)
        if tok:
            for c in _spotify_candidates(enriched, tok):
                s = _score(tokens, c["label"], want_version)
                results.append((s, "spotify", c))

    for c in _ytdlp_search("yt", enriched, ytdlp):
        s = _score(tokens, c["label"], want_version)
        results.append((s, "youtube", c))

    # Only pay for SoundCloud if nothing else scored well, OR a VIP/remix
    # version was asked for — those live on SoundCloud far more often than
    # on YouTube or Spotify.
    best_so_far = max((s for s, _, _ in results), default=0.0)
    if want_version or best_so_far < 0.7:
        for c in _ytdlp_search("sc", enriched, ytdlp):
            s = _score(tokens, c["label"], want_version)
            results.append((s, "soundcloud", c))

    if not results:
        return None

    # Prefer-source tiebreak: if two candidates are within 0.05 of top, the
    # preferred source wins.
    results.sort(key=lambda r: r[0], reverse=True)
    top_score = results[0][0]
    near_top = [r for r in results if r[0] >= top_score - 0.05]
    for r in near_top:
        if r[1] == prefer:
            results = [r]
            break

    score, src, cand = results[0]
    if score < 0.25:
        log(f"search: best candidate scored {score:.2f} — not confident, skipping")
        return None
    return {"src": src, "url": cand["url"], "label": cand["label"],
            "score": round(score, 2)}
