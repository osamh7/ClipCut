"""
FFmpeg binary manager — detects ffmpeg/ffprobe on PATH or downloads them automatically.
"""

import os
import platform
import shutil
import subprocess
import sys
import zipfile
import io
import tempfile

# Where we store a local copy of ffmpeg if we need to download it
_LOCAL_FFMPEG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ffmpeg")

# BtbN release — Windows GPL shared build (includes libx265)
_FFMPEG_DOWNLOAD_URL = (
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/"
    "latest/ffmpeg-master-latest-win64-gpl.zip"
)


def _find_on_path(name: str) -> str | None:
    """Return full path if *name* (.exe on Windows) is on PATH."""
    return shutil.which(name)


def _find_in_local(name: str) -> str | None:
    """Return full path if *name* exists in our local ffmpeg/ folder."""
    if platform.system() == "Windows":
        name += ".exe"
    # The zip extracts to a subfolder like ffmpeg-master-latest-win64-gpl/bin/
    for root, _dirs, files in os.walk(_LOCAL_FFMPEG_DIR):
        if name in files:
            return os.path.join(root, name)
    return None


def _download_ffmpeg(progress_callback=None):
    """Download and extract FFmpeg to the local ffmpeg/ directory."""
    import requests

    if progress_callback:
        progress_callback("Downloading FFmpeg (~80 MB) — this is a one-time setup...")

    os.makedirs(_LOCAL_FFMPEG_DIR, exist_ok=True)

    resp = requests.get(_FFMPEG_DOWNLOAD_URL, stream=True, timeout=60)
    resp.raise_for_status()

    total = int(resp.headers.get("content-length", 0))
    downloaded = 0
    buf = io.BytesIO()

    for chunk in resp.iter_content(chunk_size=1024 * 256):
        buf.write(chunk)
        downloaded += len(chunk)
        if progress_callback and total:
            pct = int(downloaded / total * 100)
            progress_callback(f"Downloading FFmpeg... {pct}%")

    if progress_callback:
        progress_callback("Extracting FFmpeg...")

    buf.seek(0)
    with zipfile.ZipFile(buf) as zf:
        zf.extractall(_LOCAL_FFMPEG_DIR)

    if progress_callback:
        progress_callback("FFmpeg ready.")


def get_ffmpeg_paths(progress_callback=None) -> tuple[str, str]:
    """
    Return (ffmpeg_path, ffprobe_path).

    Checks PATH first, then local ffmpeg/ folder.
    If neither exists, downloads FFmpeg automatically.

    *progress_callback* is an optional callable(str) for status updates.
    """
    ffmpeg = _find_on_path("ffmpeg") or _find_in_local("ffmpeg")
    ffprobe = _find_on_path("ffprobe") or _find_in_local("ffprobe")

    if ffmpeg and ffprobe:
        return ffmpeg, ffprobe

    # Need to download
    _download_ffmpeg(progress_callback)

    ffmpeg = _find_in_local("ffmpeg")
    ffprobe = _find_in_local("ffprobe")

    if not ffmpeg or not ffprobe:
        raise RuntimeError(
            "Failed to locate ffmpeg/ffprobe after download. "
            "Please install FFmpeg manually and add it to your PATH."
        )

    return ffmpeg, ffprobe


def verify_hevc_support(ffmpeg_path: str) -> bool:
    """Check if the ffmpeg binary has any HEVC encoder support (hardware or software)."""
    try:
        result = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=10,
        )
        for enc in ("hevc_nvenc", "hevc_qsv", "hevc_amf", "libx265"):
            if enc in result.stdout:
                return True
        return False
    except Exception:
        return False
