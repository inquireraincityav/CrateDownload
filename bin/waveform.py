"""waveform — generate a small PNG waveform preview for a finished audio file.

Called from `record-audio` at the end of a successful Spotify capture and
yt-dlp download, and surfaced by the web UI as the inline preview under
each recent-files row. Writes the PNG beside the audio file as
`<basename>.waveform.png` so it moves with the track if the user drags it,
and so the ledger of "finished files" is still the audio itself (the UI
builds waveform paths deterministically from each row's path).

Fast: ffmpeg's showwavespic filter decodes once and writes a PNG in well
under a second for a four-minute track. Never raises — a missing ffmpeg,
a corrupt file or a bad codec just logs and returns None, so a waveform
failure never jeopardises the capture.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

WIDTH = 900
HEIGHT = 90
# A muted teal that reads well on both the light and dark app chrome.
# Expressed as RGBA; ffmpeg's showwavespic understands 0xRRGGBBAA.
COLORS = "0x4FD1C5CC|0x4FD1C5CC"


def path_for(audio: Path) -> Path:
    """Deterministic waveform PNG path next to the audio file."""
    return audio.with_suffix(audio.suffix + ".waveform.png")


def generate(audio: Path, ffmpeg: str, log=lambda m: None,
             width: int = WIDTH, height: int = HEIGHT) -> Path | None:
    """Render a PNG waveform for `audio` using `ffmpeg`. Returns the PNG
    path on success, None on failure. Overwrites any existing PNG so
    re-encodes refresh their preview."""
    audio = Path(audio)
    if not audio.is_file():
        return None
    out = path_for(audio)
    cmd = [
        ffmpeg, "-nostdin", "-y", "-v", "error",
        "-i", str(audio),
        "-filter_complex",
        f"aformat=channel_layouts=mono,"
        f"showwavespic=s={width}x{height}:colors={COLORS}",
        "-frames:v", "1",
        str(out),
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"waveform: ffmpeg failed for {audio.name}: {type(e).__name__}: {e}")
        return None
    if r.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        log(f"waveform: ffmpeg rc={r.returncode} for {audio.name}: "
            f"{(r.stderr or '').strip()[:200]}")
        try:
            out.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    return out
