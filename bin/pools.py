"""pools — download tracks from cookie-authenticated DJ pools.

BPMSupreme, DJcity and Hypeddit all gate downloads behind a paid or free
account whose session lives in a browser cookie. yt-dlp doesn't know these
sites, so we handle them here: find the download URL on the page we're
pointed at, re-fetch it with the user's cookies, save it under STORAGE.

Config shape (in config.json):

    "pools": {
        "bpmsupreme": {"enabled": true,  "cookies_file": "~/private/bpm.cookies"},
        "djcity":     {"enabled": false, "cookies_file": ""},
        "hypeddit":   {"enabled": true,  "cookies_file": "~/private/hype.cookies"}
    }

`cookies_file` is a Netscape-format cookies.txt that the browser extensions
"Get cookies.txt" (Chrome) and "cookies.txt" (Firefox) export in one click.
Same format yt-dlp accepts, so if yt-dlp works with them, these will too.

Each adapter is intentionally thin: find a download link on the page, fetch
it with cookies attached, save it. If a pool's HTML changes, you fix one
adapter — the dispatcher stays stable.

Nothing here raises. On failure, returns (None, "reason") so the caller
can log and move on to the next item in the queue.
"""
from __future__ import annotations

import http.cookiejar
import re
import urllib.parse
import urllib.request
from pathlib import Path

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 "
       "(KHTML, like Gecko) Version/17.4 Safari/605.1.15")

POOL_HOSTS = {
    "bpmsupreme.com": "bpmsupreme",
    "www.bpmsupreme.com": "bpmsupreme",
    "djcity.com": "djcity",
    "www.djcity.com": "djcity",
    "hypeddit.com": "hypeddit",
    "www.hypeddit.com": "hypeddit",
}


def classify(url: str) -> str | None:
    """Return pool key for a URL, or None if it isn't a pool link."""
    try:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
    except ValueError:
        return None
    return POOL_HOSTS.get(host)


def is_pool_url(url: str) -> bool:
    return classify(url) is not None


def _opener(cookies_path: Path | None) -> urllib.request.OpenerDirector:
    cj = http.cookiejar.MozillaCookieJar()
    if cookies_path and cookies_path.is_file():
        try:
            cj.load(str(cookies_path), ignore_discard=True, ignore_expires=True)
        except (OSError, http.cookiejar.LoadError):
            pass
    return urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cj),
        urllib.request.HTTPRedirectHandler(),
    )


def _fetch_html(opener, url: str, timeout: int = 20) -> str | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA,
                                                   "Accept-Language": "en-US,en;q=0.9"})
        with opener.open(req, timeout=timeout) as r:
            return r.read(5_000_000).decode("utf-8", "replace")
    except Exception:
        return None


def _save_audio(opener, url: str, dest_dir: Path, hint_name: str,
                log=lambda m: None) -> Path | None:
    """Stream the audio to disk under dest_dir. Uses the Content-Disposition
    filename when the server supplies one; otherwise falls back to hint_name."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA,
                                                   "Referer": url})
        with opener.open(req, timeout=60) as r:
            disp = r.headers.get("Content-Disposition", "")
            ctype = (r.headers.get("Content-Type") or "").lower()
            m = re.search(r'filename\*?="?([^";]+)"?', disp)
            name = (m.group(1) if m else hint_name or "pool-track").strip()
            if not name.lower().endswith((".mp3", ".wav", ".aiff", ".m4a", ".flac")):
                ext = (".mp3" if "mpeg" in ctype or "mp3" in ctype else
                       ".wav" if "wav"  in ctype else
                       ".m4a" if "m4a"  in ctype or "aac" in ctype else
                       ".mp3")
                name = f"{name}{ext}"
            dest_dir.mkdir(parents=True, exist_ok=True)
            out = dest_dir / name
            # Avoid clobber: index collisions as " (2)", " (3)", …
            i = 2
            while out.exists():
                stem = out.stem
                stem = re.sub(r"\s*\(\d+\)$", "", stem)
                out = dest_dir / f"{stem} ({i}){out.suffix}"
                i += 1
            with open(out, "wb") as fh:
                while True:
                    chunk = r.read(1 << 16)
                    if not chunk:
                        break
                    fh.write(chunk)
            return out
    except Exception as e:
        log(f"pools._save_audio failed for {url}: {type(e).__name__}: {e}")
        return None


# --- adapters ---------------------------------------------------------------
# Each `_<pool>_find` function reads one page and returns the direct audio URL
# (if any). All of these are deliberately simple regex scrapes — a brittle
# but one-file approach, so a layout change touches one function at most.


def _bpmsupreme_find(html: str) -> str | None:
    """BPMSupreme embeds the stream URL in a data-src/download attribute on the
    row for the track, and the download link resolves to a signed CDN URL."""
    m = re.search(r'href="(https://[^"]+?download[^"]*)"', html, re.IGNORECASE)
    return m.group(1) if m else None


def _djcity_find(html: str) -> str | None:
    """DJcity exposes a direct <a class="download" href="…"> on each track row."""
    m = re.search(r'<a[^>]+class="[^"]*download[^"]*"[^>]+href="([^"]+)"',
                  html, re.IGNORECASE)
    if m:
        return urllib.parse.urljoin("https://www.djcity.com/", m.group(1))
    return None


def _hypeddit_find(html: str) -> str | None:
    """Hypeddit free-gates reveal the download URL inline in a JS blob after
    the gate is passed (the cookie we carry stands in for having passed it)."""
    m = re.search(r'"download_url"\s*:\s*"([^"]+)"', html)
    if m:
        return m.group(1).replace("\\/", "/")
    m = re.search(r'href="(https://[^"]+\.(?:mp3|wav|m4a))"', html, re.IGNORECASE)
    return m.group(1) if m else None


_FINDERS = {
    "bpmsupreme": _bpmsupreme_find,
    "djcity": _djcity_find,
    "hypeddit": _hypeddit_find,
}


# --- public entrypoint ------------------------------------------------------


def download(url: str, settings, storage: Path,
             log=lambda m: None) -> tuple[Path | None, str]:
    """Download the file at `url` from its DJ pool. Returns (saved_path, note).
    Note is a short status string the caller can show the user."""
    pool = classify(url)
    if not pool:
        return None, "not a DJ-pool URL"

    cfg = (settings.raw.get("pools") or {}).get(pool) or {}
    if not cfg.get("enabled"):
        return None, f"{pool} not enabled in config"

    cookies_path = cfg.get("cookies_file") or ""
    cookies_path = Path(cookies_path).expanduser() if cookies_path else None
    if not (cookies_path and cookies_path.is_file()):
        return None, f"{pool} cookies_file missing"

    opener = _opener(cookies_path)
    html = _fetch_html(opener, url)
    if not html:
        return None, f"{pool} page fetch failed"
    finder = _FINDERS.get(pool)
    if not finder:
        return None, f"no adapter for {pool}"
    audio_url = finder(html)
    if not audio_url:
        return None, f"{pool} download link not found (cookies expired?)"

    # Build a filename hint from the page's <title> if the server doesn't send
    # Content-Disposition, so we don't end up with "track" everywhere.
    hint = ""
    m = re.search(r"<title>([^<]+)</title>", html, re.IGNORECASE)
    if m:
        hint = re.sub(r"\s*[\|\-]\s*(bpmsupreme|djcity|hypeddit).*$", "",
                      m.group(1).strip(), flags=re.IGNORECASE).strip()
    dest = storage / pool
    saved = _save_audio(opener, audio_url, dest, hint_name=hint, log=log)
    if not saved:
        return None, f"{pool} save failed"
    return saved, f"{pool}: {saved.name}"
