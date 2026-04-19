"""
Discord bot-based uploader — authenticates via bot token, lists servers/channels,
and sends video files to a selected channel.

Setup (one-time, ~2 minutes):
  1. Go to https://discord.com/developers/applications → New Application
  2. Bot tab → Reset Token → copy the token
  3. OAuth2 → URL Generator → select "bot" scope → permissions: Send Messages, Attach Files
  4. Open the generated URL → add the bot to your server(s)
  5. Paste the bot token into the app
"""

import os
import threading
import requests

_API = "https://discord.com/api/v10"

# Discord file size limits (bytes)
DISCORD_FREE_LIMIT = 10 * 1024 * 1024        # 10 MB (non-Nitro)
DISCORD_NITRO_BASIC_LIMIT = 50 * 1024 * 1024  # 50 MB (Nitro Basic)
DISCORD_NITRO_LIMIT = 500 * 1024 * 1024       # 500 MB (Nitro)

UPLOAD_LIMITS = {
    "Free (10 MB)": DISCORD_FREE_LIMIT,
    "Nitro Basic (50 MB)": DISCORD_NITRO_BASIC_LIMIT,
    "Nitro (500 MB)": DISCORD_NITRO_LIMIT,
}

# Channel types we can send messages to
_TEXT_CHANNEL_TYPES = {0, 5}  # GUILD_TEXT, GUILD_ANNOUNCEMENT


# ======================================================================
# Bot token validation
# ======================================================================

def validate_bot_token(token: str) -> tuple[bool, dict]:
    """
    Validate a Discord bot token.

    Returns (ok, bot_info_dict).  bot_info_dict has 'username', 'id', etc.
    """
    try:
        resp = requests.get(
            f"{_API}/users/@me",
            headers=_auth(token),
            timeout=10,
        )
        if resp.status_code == 200:
            return True, resp.json()
        return False, {}
    except Exception:
        return False, {}


# ======================================================================
# Guild & channel listing
# ======================================================================

def list_guilds(token: str) -> list[dict]:
    """
    Return guilds the bot is a member of.

    Each dict has at least 'id' and 'name'.
    """
    try:
        resp = requests.get(
            f"{_API}/users/@me/guilds",
            headers=_auth(token),
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return []


def list_text_channels(token: str, guild_id: str) -> list[dict]:
    """
    Return text channels in *guild_id* that the bot can see.

    Each dict has 'id', 'name', 'type', 'position'.
    Results are sorted by position (same order as Discord sidebar).
    """
    try:
        resp = requests.get(
            f"{_API}/guilds/{guild_id}/channels",
            headers=_auth(token),
            timeout=10,
        )
        if resp.status_code == 200:
            channels = [
                c for c in resp.json()
                if c.get("type") in _TEXT_CHANNEL_TYPES
            ]
            channels.sort(key=lambda c: c.get("position", 0))
            return channels
    except Exception:
        pass
    return []


# ======================================================================
# File size check
# ======================================================================

def check_file_size(file_path: str, tier: str = "Free (10 MB)") -> tuple[bool, int, int]:
    """
    Check if a file is within the Discord upload limit for the given tier.

    Returns (ok, file_size_bytes, limit_bytes).
    """
    limit = UPLOAD_LIMITS.get(tier, DISCORD_FREE_LIMIT)
    size = os.path.getsize(file_path)
    return size <= limit, size, limit


# ======================================================================
# Upload
# ======================================================================

def upload_to_channel(
    token: str,
    channel_id: str,
    file_path: str,
    message: str = "",
    progress_callback=None,
    done_callback=None,
):
    """
    Upload a file to a Discord channel via bot token.

    Runs in a background thread.

    *progress_callback(status_str)* — status updates.
    *done_callback(success, message_str)* — called when finished.
    """
    t = threading.Thread(
        target=_upload_worker,
        args=(token, channel_id, file_path, message,
              progress_callback, done_callback),
        daemon=True,
    )
    t.start()
    return t


def _upload_worker(token, channel_id, file_path, message,
                   progress_callback, done_callback):
    try:
        if not os.path.isfile(file_path):
            if done_callback:
                done_callback(False, f"File not found: {file_path}")
            return

        file_size = os.path.getsize(file_path)
        filename = os.path.basename(file_path)

        if progress_callback:
            progress_callback(
                f"Uploading {filename} ({_human_size(file_size)}) to Discord...")

        payload = {}
        if message:
            payload["content"] = message

        with open(file_path, "rb") as f:
            resp = requests.post(
                f"{_API}/channels/{channel_id}/messages",
                headers=_auth(token),
                data=payload,
                files={"file": (filename, f, "video/mp4")},
                timeout=300,
            )

        if resp.status_code == 200:
            if done_callback:
                done_callback(True, f"Uploaded {filename} to Discord!")
        elif resp.status_code == 413:
            if done_callback:
                done_callback(
                    False,
                    "File too large for this server's upload limit.\n"
                    "Reduce quality or clip duration.")
        elif resp.status_code == 403:
            if done_callback:
                done_callback(
                    False,
                    "Bot lacks permission to post in this channel.\n"
                    "Make sure it has Send Messages + Attach Files permissions.")
        else:
            detail = ""
            try:
                detail = resp.json().get("message", resp.text[:300])
            except Exception:
                detail = resp.text[:300]
            if done_callback:
                done_callback(
                    False,
                    f"Discord API error (HTTP {resp.status_code}): {detail}")

    except requests.exceptions.Timeout:
        if done_callback:
            done_callback(False, "Upload timed out. Check your connection.")
    except requests.exceptions.ConnectionError:
        if done_callback:
            done_callback(False, "Could not reach Discord. Check your internet.")
    except Exception as e:
        if done_callback:
            done_callback(False, f"Upload error: {e}")


# ======================================================================
# Helpers
# ======================================================================

def _auth(token: str) -> dict:
    return {"Authorization": f"Bot {token}"}


def _human_size(nbytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if nbytes < 1024:
            return f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} TB"
