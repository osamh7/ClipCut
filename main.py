"""
Video Cutter — Entry point.

Checks for FFmpeg, downloads it if needed, then launches the GUI.
"""

import sys
import os
import tkinter as tk
import customtkinter as ctk
from tkinter import messagebox

# Ensure imports work when running from any cwd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Add mpv.net to PATH so python-mpv can find libmpv-2.dll
_MPV_PATHS = [
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "mpv.net"),
    os.path.join(os.environ.get("ProgramFiles", ""), "mpv.net"),
    os.path.join(os.environ.get("ProgramFiles", ""), "mpv"),
]
for _p in _MPV_PATHS:
    if os.path.isdir(_p) and _p not in os.environ.get("PATH", ""):
        os.environ["PATH"] = _p + os.pathsep + os.environ.get("PATH", "")
        break

from ffmpeg_manager import get_ffmpeg_paths, verify_hevc_support
from video_processor import VideoProcessor
from gui import VideoCutterApp


def main():
    # Create a temporary root to show startup messages
    splash = tk.Tk()
    splash.title("ClipCut — Starting")
    splash.geometry("400x100")
    splash.configure(bg="#1e1e1e")
    splash.resizable(False, False)

    lbl = tk.Label(
        splash, text="Checking for FFmpeg...",
        bg="#1e1e1e", fg="#e0e0e0", font=("Segoe UI", 11),
    )
    lbl.pack(expand=True)

    def update_splash(msg: str):
        lbl.config(text=msg)
        splash.update_idletasks()

    try:
        ffmpeg_path, ffprobe_path = get_ffmpeg_paths(progress_callback=update_splash)
    except Exception as e:
        splash.destroy()
        err_root = tk.Tk()
        err_root.withdraw()
        messagebox.showerror(
            "FFmpeg Not Found",
            f"Could not find or download FFmpeg.\n\n{e}\n\n"
            "Please install FFmpeg manually:\nhttps://ffmpeg.org/download.html",
        )
        err_root.destroy()
        sys.exit(1)

    has_hevc = verify_hevc_support(ffmpeg_path)
    codec_note = "H.265 (HEVC)" if has_hevc else "H.264 (HEVC not available)"
    update_splash(f"FFmpeg ready — encoder: {codec_note}")

    splash.destroy()

    # --- Launch main app ---
    root = ctk.CTk()
    processor = VideoProcessor(ffmpeg_path, ffprobe_path)

    app = VideoCutterApp(root, processor, has_hevc)

    def on_close():
        try:
            app.cleanup()
        except Exception:
            pass
        try:
            root.destroy()
        except Exception:
            pass

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
