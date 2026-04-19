"""
Video Cutter — CustomTkinter GUI with embedded mpv playback and visual range selection.
"""

import json
import os
import subprocess
import sys
import threading
import tkinter as tk
import customtkinter as ctk
from tkinter import filedialog, messagebox

from video_processor import VideoProcessor
from discord_uploader import (
    validate_bot_token, list_guilds, list_text_channels,
    check_file_size, upload_to_channel,
    UPLOAD_LIMITS, DISCORD_FREE_LIMIT,
)

# Optional: tkinterdnd2 for drag-and-drop file opening
try:
    import tkinterdnd2
    HAS_DND = True
except ImportError:
    HAS_DND = False

# Optional: mpv for video playback with audio and precise seeking
try:
    import mpv as mpv_module

    HAS_MPV = True
except Exception:
    HAS_MPV = False

# Optional: PIL for fallback thumbnail display (no mpv)
try:
    from PIL import Image, ImageTk

    HAS_PIL = True
except ImportError:
    HAS_PIL = False

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


# ======================================================================
# Tooltip helper
# ======================================================================

class _Tooltip:
    """Hover tooltip for any tkinter/CTk widget."""

    def __init__(self, widget, text="", delay=400, anchor="left"):
        self._widget = widget
        self._text = text
        self._delay = delay
        self._anchor = anchor  # "left" = near cursor, "center" = centred beneath widget
        self._tip_win = None
        self._after_id = None
        widget.bind("<Enter>", self._on_enter, add="+")
        widget.bind("<Leave>", self._on_leave, add="+")

    @property
    def text(self):
        return self._text

    @text.setter
    def text(self, value):
        self._text = value

    def _on_enter(self, _ev):
        self._after_id = self._widget.after(self._delay, self._show)

    def _on_leave(self, _ev):
        if self._after_id:
            self._widget.after_cancel(self._after_id)
            self._after_id = None
        self._hide()

    def _show(self):
        if not self._text:
            return
        self._tip_win = tw = tk.Toplevel(self._widget)
        tw.wm_overrideredirect(True)
        tw.wm_attributes("-topmost", True)
        tw.configure(bg="#1a1a1a")
        lbl = tk.Label(
            tw, text=self._text, justify=tk.LEFT,
            bg="#1a1a1a", fg="#e0e0e0", relief=tk.SOLID, borderwidth=1,
            font=("Segoe UI", 10), padx=8, pady=4, wraplength=320,
        )
        lbl.pack()
        tw.update_idletasks()
        if self._anchor == "center":
            wx = self._widget.winfo_rootx()
            ww = self._widget.winfo_width()
            tw_w = tw.winfo_reqwidth()
            x = wx + (ww - tw_w) // 2
            y = self._widget.winfo_rooty() + self._widget.winfo_height() + 4
        else:
            x = self._widget.winfo_rootx() + 20
            y = self._widget.winfo_rooty() + self._widget.winfo_height() + 4
        tw.wm_geometry(f"+{x}+{y}")

    def _hide(self):
        if self._tip_win:
            self._tip_win.destroy()
            self._tip_win = None


# ======================================================================
# Helper
# ======================================================================

def _fmt_time(sec: float) -> str:
    if sec < 0:
        sec = 0
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    if h:
        return f"{h}:{m:02d}:{s:05.2f}"
    return f"{m}:{s:05.2f}"


# ======================================================================
# Custom range‑timeline widget
# ======================================================================

class RangeTimeline(tk.Canvas):
    """
    Full‑width timeline with:
      - dark track (entire video)
      - blue highlighted region (confirmed clip: both in and out set)
      - grey "pending max" zone (in is set, out is not yet — shows the 60s window)
      - green start handle / red end handle (draggable once clip is confirmed)
      - white playhead line
    """

    MIN_CLIP = 5.0    # minimum clip length in seconds

    _PAD = 20
    _TRACK_H = 10
    _HANDLE_W = 4
    _HANDLE_H = 20

    def __init__(self, parent, *, on_seek=None, on_range_change=None,
                 on_drag_start=None, on_drag_end=None, **kw):
        kw.setdefault("height", 80)
        kw.setdefault("bg", "#242424")
        kw.setdefault("highlightthickness", 0)
        kw.setdefault("cursor", "hand2")
        super().__init__(parent, **kw)

        self._on_seek = on_seek
        self._on_range_change = on_range_change
        self._on_drag_start = on_drag_start
        self._on_drag_end = on_drag_end

        self.duration = 0.0
        self.position = 0.0

        # Clip state (single-clip mode)
        self.clip_start = -1.0   # -1 = not set
        self.clip_end = -1.0     # -1 = not set
        self.pending_max = -1.0  # -1 = no ghost zone

        # Multi-clip state
        self.multi_clip = False
        self.clips: list[tuple[float, float]] = []     # confirmed (start, end) pairs
        self._active_clip_idx: int | None = None        # index of clip being dragged

        # Zoom / pan state
        self._zoom = 1.0         # 1.0 = full video visible
        self._view_center = 0.0  # centre of visible window in seconds

        self._dragging = None

        # Seek throttling for smooth scrubbing
        self._SEEK_THROTTLE_MS = 50
        self._seek_pending = None     # queued seek time (seconds)
        self._seek_timer = None       # after() id

        # Persistent canvas item IDs (created lazily, moved with coords())
        self._id_playhead = None
        self._id_mm_playhead = None   # minimap playhead tick
        self._base_dirty = True       # full redraw needed

        self.bind("<Configure>", self._on_configure)
        self.bind("<Button-1>", self._mouse_down)
        self.bind("<B1-Motion>", self._mouse_move)
        self.bind("<ButtonRelease-1>", self._mouse_up)
        self.bind("<Motion>", self._hover)
        self.bind("<MouseWheel>", self._mouse_wheel)
        self.bind("<Button-3>", self._right_click)

    @property
    def has_clip(self):
        if self.multi_clip:
            return len(self.clips) > 0
        return self.clip_start >= 0 and self.clip_end >= 0

    @property
    def is_pending(self):
        """Mark In set, but Mark Out not yet."""
        return self.clip_start >= 0 and self.clip_end < 0

    @property
    def all_clips(self) -> list[tuple[float, float]]:
        """Return all clip ranges (works in both single and multi mode)."""
        if self.multi_clip:
            return list(self.clips)
        if self.clip_start >= 0 and self.clip_end >= 0:
            return [(self.clip_start, self.clip_end)]
        return []

    # -- public -----------------------------------------------------------

    def set_duration(self, dur):
        self.duration = dur
        self.clip_start = -1.0
        self.clip_end = -1.0
        self.pending_max = -1.0
        self.clips.clear()
        self._active_clip_idx = None
        self.position = 0.0
        self._zoom = 1.0
        self._view_center = dur / 2.0
        self._invalidate()

    def set_position(self, sec):
        self.position = sec
        self._move_playhead()

    def set_range(self, start, end):
        self.clip_start = start
        self.clip_end = end
        self.pending_max = -1.0
        self._invalidate()

    def clear_clip(self):
        self.clip_start = -1.0
        self.clip_end = -1.0
        self.pending_max = -1.0
        self.clips.clear()
        self._active_clip_idx = None
        self._invalidate()

    def _invalidate(self):
        """Mark base layer dirty and do full redraw."""
        self._base_dirty = True
        self._draw()

    def _on_configure(self, _ev):
        self._invalidate()

    # -- visible window ---------------------------------------------------

    def _view_span(self):
        """Duration of the visible portion of the timeline."""
        return self.duration / self._zoom

    def _view_start(self):
        span = self._view_span()
        vs = self._view_center - span / 2.0
        vs = max(0.0, min(vs, self.duration - span))
        return vs

    def _clamp_view(self):
        span = self._view_span()
        half = span / 2.0
        self._view_center = max(half, min(self._view_center, self.duration - half))

    # -- coords (zoom-aware) ----------------------------------------------

    def _s2x(self, sec):
        w = self.winfo_width() - 2 * self._PAD
        if self.duration <= 0 or w <= 0:
            return self._PAD
        vs = self._view_start()
        span = self._view_span()
        if span <= 0:
            return self._PAD
        return self._PAD + (sec - vs) / span * w

    def _x2s(self, x):
        w = self.winfo_width() - 2 * self._PAD
        if self.duration <= 0 or w <= 0:
            return 0.0
        vs = self._view_start()
        span = self._view_span()
        sec = vs + (x - self._PAD) / w * span
        return max(0.0, min(self.duration, sec))

    # -- drawing ----------------------------------------------------------

    @staticmethod
    def _nice_tick_interval(span):
        """Pick a human-friendly tick interval for the visible time span."""
        # Target roughly 6-10 ticks across the visible area
        raw = span / 8
        # Round to a nice value from this table
        nice = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600]
        for n in nice:
            if raw <= n:
                return n
        return 3600

    def _draw_ticks(self, cw, ty1, ty2):
        """Draw subtle tick marks and timestamps along the track."""
        if self.duration <= 0:
            return
        span = self._view_span()
        interval = self._nice_tick_interval(span)
        vs = self._view_start()
        ve = vs + span

        # First tick at or after view start, aligned to interval
        first = (int(vs / interval) + 1) * interval if vs % interval else int(vs / interval) * interval
        if first < interval:
            first = 0

        t = first
        tick_font = ("Segoe UI", 7)
        while t <= min(ve, self.duration):
            x = self._s2x(t)
            if self._PAD <= x <= cw - self._PAD:
                # Small tick line above the track
                self.create_line(x, ty1 - 4, x, ty1, fill="#555", width=1)
                # Timestamp label
                self.create_text(x, ty1 - 6, text=_fmt_time(t),
                                 fill="#555", font=tick_font, anchor="s")
            t += interval

    def _draw(self):
        """Full redraw — base layer + playhead. Use _move_playhead() for fast updates."""
        self.delete("all")
        self._id_playhead = None
        self._id_mm_playhead = None
        cw = self.winfo_width()
        ch = self.winfo_height()
        cy = ch // 2 - 6   # shift track up to leave room for labels below

        if self.duration <= 0:
            self.create_text(cw // 2, ch // 2, text="Open a video to begin",
                             fill="#555", font=("Segoe UI", 9))
            return

        ty1 = cy - self._TRACK_H // 2
        ty2 = cy + self._TRACK_H // 2

        # full track
        self.create_rectangle(self._PAD, ty1, cw - self._PAD, ty2,
                              fill="#333", outline="")

        # Scrub gutter indicators (subtle lines above and below the track)
        self.create_line(self._PAD, ty1 - 14, cw - self._PAD, ty1 - 14,
                         fill="#2a2a2a", width=1, dash=(2, 4))
        self.create_line(self._PAD, ty2 + 14, cw - self._PAD, ty2 + 14,
                         fill="#2a2a2a", width=1, dash=(2, 4))

        # --- timeline tick marks / timestamps ---
        self._draw_ticks(cw, ty1, ty2)

        hw, hh = self._HANDLE_W, self._HANDLE_H

        # --- ghost zone (pending: in set, out not yet) ---
        if self.is_pending and self.pending_max > 0:
            xs = self._s2x(self.clip_start)
            xm = self._s2x(self.pending_max)
            self.create_rectangle(xs, ty1 - 1, xm, ty2 + 1,
                                  fill="#444", outline="#555")
            self.create_line(xs, ty1 - 10, xs, ty2 + 10, fill="#4CAF50", width=2)
            self.create_line(xm, ty1 - 6, xm, ty2 + 6, fill="#666", width=1, dash=(3, 3))
            self.create_text(xs, cy + hh // 2 + 6, text=f"IN {_fmt_time(self.clip_start)}",
                             fill="#4CAF50", font=("Segoe UI", 8), anchor="n")
            self.create_text(xm, cy + hh // 2 + 6, text=_fmt_time(self.pending_max),
                             fill="#666", font=("Segoe UI", 8), anchor="n")

        # --- multi-clip: draw all confirmed clips ---
        if self.multi_clip and self.clips:
            _CLIP_COLORS = [
                "#0078d4", "#e67e22", "#9b59b6", "#2ecc71",
                "#e74c3c", "#1abc9c", "#f39c12", "#3498db",
            ]
            # First pass: draw clip regions and handles
            for idx, (cs, ce) in enumerate(self.clips):
                color = _CLIP_COLORS[idx % len(_CLIP_COLORS)]
                xs = self._s2x(cs)
                xe = self._s2x(ce)
                self.create_rectangle(xs, ty1, xe, ty2, fill=color, outline="")
                self.create_rectangle(xs - hw, cy - hh // 2, xs, cy + hh // 2,
                                      fill="#4CAF50", outline="#2E7D32")
                self.create_line(xs, ty1 - 3, xs, ty2 + 3, fill="#4CAF50", width=2)
                self.create_rectangle(xe, cy - hh // 2, xe + hw, cy + hh // 2,
                                      fill="#f44336", outline="#b71c1c")
                self.create_line(xe, ty1 - 3, xe, ty2 + 3, fill="#f44336", width=2)

            # Duration labels centred on each clip
            dur_font = ("Segoe UI", 7)
            for idx, (cs, ce) in enumerate(self.clips):
                xs = self._s2x(cs)
                xe = self._s2x(ce)
                mid = (xs + xe) / 2
                if xe - xs > 30:  # only if clip region is wide enough
                    dur_s = ce - cs
                    dur_txt = f"{dur_s:.1f}s" if dur_s < 60 else _fmt_time(dur_s)
                    self.create_text(mid, cy, text=dur_txt,
                                     fill="#fff", font=dur_font, anchor="center")

            # Second pass: draw labels with collision avoidance
            label_y = cy + hh // 2 + 8
            label_font = ("Segoe UI", 8)
            min_label_gap = 50  # minimum px between label centers
            # Collect all candidate labels: (x_pos, text, color)
            candidates = []
            for idx, (cs, ce) in enumerate(self.clips):
                xs = self._s2x(cs)
                xe = self._s2x(ce)
                candidates.append((xs, _fmt_time(cs), "#4CAF50"))
                candidates.append((xe, _fmt_time(ce), "#f44336"))
            # Sort by x position
            candidates.sort(key=lambda c: c[0])
            # Greedily place labels, skipping those too close to the last placed
            last_x = -999
            for x, text, color in candidates:
                if x - last_x >= min_label_gap:
                    self.create_text(x, label_y, text=text,
                                     fill=color, font=label_font, anchor="n")
                    last_x = x

        # --- confirmed clip (single mode, both in and out are set) ---
        elif not self.multi_clip and self.has_clip:
            xs = self._s2x(self.clip_start)
            xe = self._s2x(self.clip_end)

            self.create_rectangle(xs, ty1, xe, ty2, fill="#0078d4", outline="")

            self.create_rectangle(xs - hw, cy - hh // 2, xs, cy + hh // 2,
                                  fill="#4CAF50", outline="#2E7D32", tags="h_s")
            self.create_line(xs, ty1 - 3, xs, ty2 + 3, fill="#4CAF50", width=2)

            self.create_rectangle(xe, cy - hh // 2, xe + hw, cy + hh // 2,
                                  fill="#f44336", outline="#b71c1c", tags="h_e")
            self.create_line(xe, ty1 - 3, xe, ty2 + 3, fill="#f44336", width=2)

            # Duration label centred on the clip
            dur_font = ("Segoe UI", 7)
            mid = (xs + xe) / 2
            if xe - xs > 30:
                dur_s = self.clip_end - self.clip_start
                dur_txt = f"{dur_s:.1f}s" if dur_s < 60 else _fmt_time(dur_s)
                self.create_text(mid, cy, text=dur_txt,
                                 fill="#fff", font=dur_font, anchor="center")

            # Labels with collision avoidance (same logic as multi-clip)
            label_y = cy + hh // 2 + 8
            label_font = ("Segoe UI", 8)
            min_label_gap = 50
            candidates = [
                (xs, _fmt_time(self.clip_start), "#4CAF50"),
                (xe, _fmt_time(self.clip_end), "#f44336"),
            ]
            candidates.sort(key=lambda c: c[0])
            last_x = -999
            for x, text, color in candidates:
                if x - last_x >= min_label_gap:
                    self.create_text(x, label_y, text=text,
                                     fill=color, font=label_font, anchor="n")
                    last_x = x

        # playhead — persistent item, moved with coords() later
        xp = self._s2x(self.position)
        self._id_playhead = self.create_line(
            xp, ty1 - 10, xp, ty2 + 10, fill="#fff", width=2)

        # minimap (thin overview bar at top when zoomed in)
        if self._zoom > 1.05:
            mm_y = 4
            mm_h = 6
            mm_x1 = self._PAD
            mm_x2 = cw - self._PAD
            mm_w = mm_x2 - mm_x1

            self.create_rectangle(mm_x1, mm_y, mm_x2, mm_y + mm_h,
                                  fill="#2a2a2a", outline="#444")

            vs = self._view_start()
            span = self._view_span()
            vx1 = mm_x1 + (vs / self.duration) * mm_w
            vx2 = mm_x1 + ((vs + span) / self.duration) * mm_w
            self.create_rectangle(vx1, mm_y, vx2, mm_y + mm_h,
                                  fill="#555", outline="")

            px = mm_x1 + (self.position / self.duration) * mm_w
            self._id_mm_playhead = self.create_line(
                px, mm_y, px, mm_y + mm_h, fill="#fff", width=1)

            if self.multi_clip and self.clips:
                _CLIP_COLORS = [
                    "#0078d4", "#e67e22", "#9b59b6", "#2ecc71",
                    "#e74c3c", "#1abc9c", "#f39c12", "#3498db",
                ]
                for idx, (cs, ce) in enumerate(self.clips):
                    color = _CLIP_COLORS[idx % len(_CLIP_COLORS)]
                    cx1 = mm_x1 + (cs / self.duration) * mm_w
                    cx2 = mm_x1 + (ce / self.duration) * mm_w
                    self.create_rectangle(cx1, mm_y + 1, cx2, mm_y + mm_h - 1,
                                          fill=color, outline="")
            elif not self.multi_clip and self.has_clip:
                cx1 = mm_x1 + (self.clip_start / self.duration) * mm_w
                cx2 = mm_x1 + (self.clip_end / self.duration) * mm_w
                self.create_rectangle(cx1, mm_y + 1, cx2, mm_y + mm_h - 1,
                                      fill="#0078d4", outline="")
            elif self.is_pending:
                cx1 = mm_x1 + (self.clip_start / self.duration) * mm_w
                self.create_line(cx1, mm_y, cx1, mm_y + mm_h, fill="#4CAF50", width=1)

        self._base_dirty = False

    def _move_playhead(self):
        """Fast path — move only the playhead line(s) without redrawing anything else."""
        if self._id_playhead is None or self.duration <= 0:
            self._draw()
            return

        ch = self.winfo_height()
        cy = ch // 2 - 6
        ty1 = cy - self._TRACK_H // 2
        ty2 = cy + self._TRACK_H // 2

        xp = self._s2x(self.position)
        self.coords(self._id_playhead, xp, ty1 - 10, xp, ty2 + 10)

        # minimap playhead tick
        if self._id_mm_playhead is not None:
            mm_y = 4
            mm_h = 6
            mm_x1 = self._PAD
            mm_w = (self.winfo_width() - 2 * self._PAD)
            px = mm_x1 + (self.position / self.duration) * mm_w
            self.coords(self._id_mm_playhead, px, mm_y, px, mm_y + mm_h)

    # -- mouse interaction ------------------------------------------------

    def _hit(self, x, y=None):
        hw = self._HANDLE_W + 4  # tight hit zone around slim handles
        # Only match handles if mouse Y is within the handle's vertical extent
        ch = self.winfo_height()
        cy = ch // 2 - 6
        hh = self._HANDLE_H
        handle_y1 = cy - hh // 2
        handle_y2 = cy + hh // 2
        if y is not None and not (handle_y1 - 4 <= y <= handle_y2 + 4):
            return None  # mouse is above or below handles → always scrub
        # Multi-clip mode: find the CLOSEST handle to the mouse
        if self.multi_clip and self.clips:
            best = None
            best_dist = hw + 1
            for idx, (cs, ce) in enumerate(self.clips):
                ds = abs(x - self._s2x(cs))
                de = abs(x - self._s2x(ce))
                if ds <= hw and ds < best_dist:
                    best = ("mc_start", idx)
                    best_dist = ds
                if de <= hw and de < best_dist:
                    best = ("mc_end", idx)
                    best_dist = de
            return best
        # Single-clip mode
        if not self.has_clip:
            return None
        if abs(x - self._s2x(self.clip_start)) <= hw:
            return "start"
        if abs(x - self._s2x(self.clip_end)) <= hw:
            return "end"
        return None

    def _mouse_down(self, ev):
        if self._on_drag_start:
            self._on_drag_start()
        h = self._hit(ev.x, ev.y)
        if h:
            self._dragging = h
            if h == "start":
                self._throttled_seek(self.clip_start)
            elif h == "end":
                self._throttled_seek(self.clip_end)
            elif isinstance(h, tuple):
                kind, idx = h
                self._active_clip_idx = idx
                cs, ce = self.clips[idx]
                self._throttled_seek(cs if kind == "mc_start" else ce)
        else:
            self._dragging = "playhead"
            sec = self._x2s(ev.x)
            self.position = sec
            self._move_playhead()
            self._throttled_seek(sec)

    def _mouse_move(self, ev):
        if not self._dragging:
            return
        sec = self._x2s(ev.x)
        if self._dragging == "playhead":
            sec = max(0.0, min(self.duration, sec))
            self.position = sec
            self._move_playhead()
            self._throttled_seek(sec)
        elif self._dragging == "start":
            sec = max(0.0, min(sec, self.clip_end - self.MIN_CLIP))
            self.clip_start = sec
            self._draw()
            if self._on_range_change:
                self._on_range_change(self.clip_start, self.clip_end)
            self._throttled_seek(sec)
        elif self._dragging == "end":
            sec = min(self.duration, max(sec, self.clip_start + self.MIN_CLIP))
            self.clip_end = sec
            self._draw()
            if self._on_range_change:
                self._on_range_change(self.clip_start, self.clip_end)
            self._throttled_seek(sec)
        elif isinstance(self._dragging, tuple):
            kind, idx = self._dragging
            if 0 <= idx < len(self.clips):
                cs, ce = self.clips[idx]
                max_dur = 60.0  # multi-clip max
                if kind == "mc_start":
                    sec = max(0.0, min(sec, ce - self.MIN_CLIP))
                    if ce - sec > max_dur:
                        sec = ce - max_dur
                    self.clips[idx] = (sec, ce)
                elif kind == "mc_end":
                    sec = min(self.duration, max(sec, cs + self.MIN_CLIP))
                    if sec - cs > max_dur:
                        sec = cs + max_dur
                    self.clips[idx] = (cs, sec)
                self._draw()
                if self._on_range_change:
                    self._on_range_change(self.clips[idx][0], self.clips[idx][1])
                self._throttled_seek(sec)

    def _mouse_up(self, _ev):
        # Final precise seek on release
        if self._dragging and self._seek_pending is not None:
            if self._seek_timer:
                self.after_cancel(self._seek_timer)
                self._seek_timer = None
            if self._on_seek:
                self._on_seek(self._seek_pending)
            self._seek_pending = None
        self._dragging = None
        self._active_clip_idx = None
        if self._on_drag_end:
            self._on_drag_end()

    def _throttled_seek(self, sec):
        """Queue a VLC seek; only fire at most every _SEEK_THROTTLE_MS."""
        self._seek_pending = sec
        if self._seek_timer is None:
            self._flush_seek()

    def _flush_seek(self):
        if self._seek_pending is not None and self._on_seek:
            self._on_seek(self._seek_pending)
            self._seek_pending = None
        if self._dragging:
            self._seek_timer = self.after(self._SEEK_THROTTLE_MS, self._flush_seek)
        else:
            self._seek_timer = None

    def _right_click(self, ev):
        """Right-click to delete a clip in multi-clip mode."""
        if not self.multi_clip or not self.clips:
            return
        sec = self._x2s(ev.x)
        for idx, (cs, ce) in enumerate(self.clips):
            if cs <= sec <= ce:
                self.clips.pop(idx)
                self._draw()
                if self._on_range_change:
                    self._on_range_change()
                return

    def _hover(self, ev):
        h = self._hit(ev.x, ev.y)
        if h == "start" or h == "end" or (isinstance(h, tuple)):
            self.config(cursor="sb_h_double_arrow")
        else:
            self.config(cursor="hand2")

    def _mouse_wheel(self, ev):
        if self.duration <= 0:
            return
        factor = 1.3
        if ev.delta > 0:
            self._zoom = min(self._zoom * factor, 80.0)
        else:
            self._zoom = max(self._zoom / factor, 1.0)

        # Focus on markers if they exist, otherwise fall back to playhead position
        if self.multi_clip and self.clips:
            # Focus on the clip closest to the mouse cursor
            mouse_sec = self._x2s(ev.x)
            best_dist = float("inf")
            best_center = mouse_sec
            for cs, ce in self.clips:
                mid = (cs + ce) / 2.0
                dist = abs(mouse_sec - mid)
                if dist < best_dist:
                    best_dist = dist
                    best_center = mid
            self._view_center = best_center
        elif self.has_clip and self.clip_start >= 0 and self.clip_end >= 0:
            self._view_center = (self.clip_start + self.clip_end) / 2.0
        elif self.is_pending:
            self._view_center = self.clip_start
        else:
            self._view_center = self.position

        self._clamp_view()
        self._invalidate()


# ======================================================================
# Main application
# ======================================================================

# ======================================================================
# Persistent config
# ======================================================================

def _config_path() -> str:
    d = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "VideoCutter")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "config.json")

def _load_config() -> dict:
    try:
        with open(_config_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def _save_config(cfg: dict) -> None:
    with open(_config_path(), "w", encoding="utf-8") as f:
        json.dump(cfg, f)


class VideoCutterApp:
    _POLL_MS = 100

    def __init__(self, root: tk.Tk, processor: VideoProcessor, has_hevc: bool):
        self.root = root
        self.processor = processor
        self.has_hevc = has_hevc

        self.video_path: str | None = None
        self.video_info: dict | None = None
        self._encoding = False
        self._uploading = False
        self._was_playing_before_drag = False
        self._closing = False
        self._config = _load_config()

        # mpv player
        self._mpv = None
        self._mpv_ok = False

        # fallback thumb ref (prevent GC)
        self._thumb_ref = None

        self._build_ui()
        self._bind_keys()
        self._init_mpv()

    # ------------------------------------------------------------------
    # mpv init
    # ------------------------------------------------------------------

    def _init_mpv(self):
        if not HAS_MPV:
            return
        try:
            self._mpv = mpv_module.MPV(
                wid=str(int(self.video_frame.winfo_id())),
                hr_seek="yes",
                hr_seek_framedrop="yes",
                hwdec="auto",
                keep_open="yes",
                keep_open_pause="yes",
                terminal="no",
                pause=True,
                volume=70,
            )
            self._mpv_ok = True

            # Keep references to observers so we can unobserve on cleanup
            @self._mpv.property_observer("time-pos")
            def _time_handler(_name, value):
                if self._closing:
                    return
                if value is not None and not self.timeline._dragging:
                    self.root.after(0, self._on_mpv_time, value)

            @self._mpv.property_observer("eof-reached")
            def _eof_handler(_name, value):
                if self._closing:
                    return
                if value:
                    self.root.after(50, self._on_mpv_eof)

            @self._mpv.property_observer("pause")
            def _pause_handler(_name, value):
                if self._closing:
                    return
                self.root.after(0, self._sync_play_btn)

            self._mpv_observers = [
                ("time-pos", _time_handler),
                ("eof-reached", _eof_handler),
                ("pause", _pause_handler),
            ]

        except Exception as e:
            print(f"mpv init error: {e}", file=sys.stderr)
            self._mpv_ok = False

    def _on_mpv_time(self, sec):
        if not self.video_path:
            return
        tl = self.timeline
        tl.set_position(sec)
        self._update_time(sec)
        if self._loop_var.get() and not self._mpv.pause:
            if tl.multi_clip and tl.clips:
                # Loop within whichever clip the playhead is currently in
                for cs, ce in tl.clips:
                    if cs <= sec <= ce + 0.15:
                        if sec >= ce:
                            self._mpv.seek(cs, "absolute", "exact")
                        break
            elif not tl.multi_clip and tl.has_clip and tl.clip_end >= 0:
                if sec >= tl.clip_end:
                    self._mpv.seek(tl.clip_start, "absolute", "exact")

    def _on_mpv_eof(self):
        if self.video_path and self._mpv_ok:
            self._close_video()

    def _sync_play_btn(self):
        if self._mpv_ok and self._mpv:
            if self._mpv.pause:
                self.btn_play.configure(text="▶  Play")
            else:
                self.btn_play.configure(text="❚❚ Pause")

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self):
        self.root.title("ClipCut")
        self.root.minsize(780, 620)
        # Default window size — centered on screen
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        w, h = min(1280, sw - 100), min(800, sh - 100)
        x, y = (sw - w) // 2, (sh - h) // 2
        self.root.geometry(f"{w}x{h}+{x}+{y}")

        # Main container
        main = ctk.CTkFrame(self.root, fg_color="transparent")
        main.pack(fill=tk.BOTH, expand=True, padx=16, pady=16)

        # ---- top bar ----
        top = ctk.CTkFrame(main, fg_color="transparent")
        top.pack(fill=tk.X, pady=(0, 8))

        self.lbl_file = ctk.CTkLabel(
            top, text="No file loaded", text_color="#888",
            font=ctk.CTkFont("Segoe UI", 12),
        )
        self.lbl_file.pack(side=tk.LEFT, padx=16)

        # Help button
        self.btn_help = ctk.CTkButton(
            top, text="?  Help", command=self._show_shortcuts,
            width=72, height=32, corner_radius=8,
            fg_color="#444", hover_color="#555",
            font=ctk.CTkFont("Segoe UI", 13, "bold"),
        )
        self.btn_help.pack(side=tk.RIGHT, padx=(4, 0))

        # ---- video area ----
        # Raw tk.Frame for mpv window embedding (needs real HWND)
        self.video_frame = tk.Frame(main, bg="black", cursor="hand2")
        self.video_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 8))
        self.video_frame.bind("<Button-1>", self._on_video_area_click)

        self.lbl_preview = tk.Label(
            self.video_frame, bg="black", fg="#888",
            text="Click to open video or drag directly here",
            font=("Segoe UI", 13), cursor="hand2",
        )
        self.lbl_preview.place(relx=0.5, rely=0.5, anchor="center")
        self.lbl_preview.bind("<Button-1>", self._on_video_area_click)

        # ---- playback controls ----
        ctrl = ctk.CTkFrame(main, fg_color="transparent")
        ctrl.pack(fill=tk.X, pady=(0, 8))

        self.btn_play = ctk.CTkButton(
            ctrl, text="▶  Play", command=self._toggle_play,
            width=110, height=32, corner_radius=8,
            font=ctk.CTkFont("Segoe UI", 13),
        )
        self.btn_play.pack(side=tk.LEFT, padx=(0, 10))

        self.btn_clear = ctk.CTkButton(
            ctrl, text="✕  Clear", command=self._clear_markers,
            width=90, height=32, corner_radius=8,
            fg_color="#444", hover_color="#555",
            font=ctk.CTkFont("Segoe UI", 12),
        )
        self.btn_clear.pack(side=tk.LEFT, padx=(0, 8))

        self._loop_var = tk.BooleanVar(value=False)
        self.chk_loop = ctk.CTkCheckBox(
            ctrl, text="Loop Clip", variable=self._loop_var,
            font=ctk.CTkFont("Segoe UI", 12),
            checkbox_width=20, checkbox_height=20, corner_radius=4,
        )
        self.chk_loop.pack(side=tk.LEFT, padx=(0, 12))

        self._multi_clip_var = tk.BooleanVar(value=False)
        self.chk_multi = ctk.CTkCheckBox(
            ctrl, text="Multi Clips", variable=self._multi_clip_var,
            command=self._on_multi_clip_toggle,
            font=ctk.CTkFont("Segoe UI", 12),
            checkbox_width=20, checkbox_height=20, corner_radius=4,
        )
        self.chk_multi.pack(side=tk.LEFT, padx=(0, 12))

        self.lbl_time = ctk.CTkLabel(
            ctrl, text="0:00.00 / 0:00.00", text_color="#888",
            font=ctk.CTkFont("Segoe UI", 12),
        )
        self.lbl_time.pack(side=tk.LEFT, padx=(12, 0))

        # volume (right-aligned)
        self._vol_slider = ctk.CTkSlider(
            ctrl, from_=0, to=100, number_of_steps=100,
            command=self._on_volume, width=100,
        )
        self._vol_slider.set(70)
        self._vol_slider.pack(side=tk.RIGHT)

        ctk.CTkLabel(ctrl, text="🔊", font=ctk.CTkFont(size=14)).pack(
            side=tk.RIGHT, padx=(8, 4))

        # ---- range timeline ----
        self.timeline = RangeTimeline(
            main, on_seek=self._tl_seek, on_range_change=self._tl_range_changed,
            on_drag_start=self._tl_drag_start, on_drag_end=self._tl_drag_end,
        )
        self.timeline.pack(fill=tk.X, pady=(0, 10))

        # ---- action bar (clip info + quality + cut) ----
        act = ctk.CTkFrame(main, fg_color="transparent")
        act.pack(fill=tk.X, pady=(0, 10))

        self.lbl_clip = ctk.CTkLabel(
            act, text="No clip set", text_color="#888",
            font=ctk.CTkFont("Segoe UI", 12),
        )
        self.lbl_clip.pack(side=tk.LEFT)
        self._tip_clip = _Tooltip(self.lbl_clip, "")

        self.lbl_estimate = ctk.CTkLabel(
            act, text="", text_color="#888",
            font=ctk.CTkFont("Segoe UI", 11),
        )
        self.lbl_estimate.pack(side=tk.LEFT, padx=(12, 0))
        self._tip_estimate = _Tooltip(self.lbl_estimate, "")

        self.btn_cut = ctk.CTkButton(
            act, text="✂  Export", command=self._start_cut,
            width=140, height=36, corner_radius=8,
            font=ctk.CTkFont("Segoe UI", 13, "bold"),
            fg_color="#0078d4", hover_color="#1a8ae8",
        )
        self.btn_cut.pack(side=tk.RIGHT)

        self.combo_q = ctk.CTkComboBox(
            act, values=list(VideoProcessor.QUALITY_PRESETS.keys()),
            state="readonly", width=140, command=self._on_quality_changed,
            font=ctk.CTkFont("Segoe UI", 12),
        )
        self.combo_q.pack(side=tk.RIGHT, padx=(0, 12))
        self.combo_q.set("Medium")

        ctk.CTkLabel(
            act, text="Quality:",
            font=ctk.CTkFont("Segoe UI", 12),
        ).pack(side=tk.RIGHT, padx=(0, 6))

        # ---- Discord integration panel ----
        self._build_discord_panel(main)

        # ---- status / progress ----
        self.progress = ctk.CTkProgressBar(main, height=6, corner_radius=3)
        self.progress.set(0)
        self._progress_visible = False

        self._status_bar = ctk.CTkFrame(main, fg_color="transparent")
        self._status_bar.pack(fill=tk.X, pady=(4, 0))

        self.lbl_status = ctk.CTkLabel(
            self._status_bar, text="Ready", text_color="#888",
            font=ctk.CTkFont("Segoe UI", 11),
        )
        self.lbl_status.pack(side=tk.LEFT)

        self.lbl_shortcuts = ctk.CTkLabel(
            self._status_bar,
            text="Q = In    E = Out    C = Clear    Scroll = Zoom    ? = Help",
            text_color="#666", font=ctk.CTkFont("Segoe UI", 10),
        )
        self.lbl_shortcuts.pack(side=tk.RIGHT)

        # ---- tooltips ----
        self._setup_tooltips()

        # ---- drag-and-drop ----
        self._setup_dnd()

    # ------------------------------------------------------------------
    # Tooltips
    # ------------------------------------------------------------------

    def _setup_tooltips(self):
        """Attach descriptive tooltips to every interactive control."""
        _Tooltip(self.btn_help,
                 "Open the Help guide — shows all shortcuts,\n"
                 "workflow tips, and feature explanations.\n"
                 "Shortcut: ?")
        _Tooltip(self.btn_play,
                 "Play or pause the video.\n"
                 "Shortcut: Space")
        _Tooltip(self.btn_clear,
                 "Clear all clip markers.\n"
                 "Shortcut: C")
        _Tooltip(self.chk_loop,
                 "When enabled, playback loops within\n"
                 "the current clip boundaries.")
        _Tooltip(self.chk_multi,
                 "Multi Clip mode: mark several clips on\n"
                 "the timeline, then export them all at once.\n"
                 "Q = add clip start, E = confirm clip end.")
        _Tooltip(self.lbl_time,
                 "Current playhead position / total duration.")
        _Tooltip(self._vol_slider,
                 "Adjust playback volume.")
        _Tooltip(self.timeline,
                 "Click or drag to scrub.\n"
                 "Drag handles to adjust clip boundaries.\n"
                 "Scroll to zoom. Q = Mark In, E = Mark Out.\n"
                 "A/D = ±5s, Shift+A/D = ±1s, Alt+A/D = ±100ms.",
                 anchor="center")
        _Tooltip(self.btn_cut,
                 "Export the selected clip(s) to a file.\n"
                 "In Multi Clip mode you can name each clip.")
        _Tooltip(self.combo_q,
                 "Choose encoding quality.\n"
                 "Higher quality = larger file size.")
        # Discord panel tooltips
        _Tooltip(self.chk_discord,
                 "Enable automatic upload to Discord\n"
                 "after each export finishes.")
        _Tooltip(self.entry_token,
                 "Paste your Discord bot token here.\n"
                 "Get one at discord.com/developers →\n"
                 "New Application → Bot → Copy Token.")
        _Tooltip(self.btn_show_token,
                 "Show or hide the bot token text.")
        _Tooltip(self.btn_connect,
                 "Connect to Discord using the bot token\n"
                 "to populate the server and channel lists.")
        _Tooltip(self.combo_guild,
                 "Select the Discord server to upload to.\n"
                 "Connect your bot first.")
        _Tooltip(self.combo_channel,
                 "Select the text channel within the server.")
        _Tooltip(self.combo_tier,
                 "Set your Nitro tier to match Discord's\n"
                 "upload size limit (10 MB – 500 MB).")
        _Tooltip(self.btn_discord_upload,
                 "Manually pick a file and upload it\n"
                 "to the selected Discord channel.")

    # ------------------------------------------------------------------
    # Discord integration panel
    # ------------------------------------------------------------------

    def _build_discord_panel(self, parent):
        """Build the Discord upload panel with bot token auth and server/channel pickers."""
        self._discord_guilds: list[dict] = []     # cached guild list
        self._discord_channels: list[dict] = []   # cached channel list
        self._discord_connected = False

        discord_frame = ctk.CTkFrame(parent, fg_color="#1e1e1e", corner_radius=8)
        discord_frame.pack(fill=tk.X, pady=(6, 4))

        # ── Header row: auto-upload checkbox + warning ──
        header = ctk.CTkFrame(discord_frame, fg_color="transparent")
        header.pack(fill=tk.X, padx=10, pady=(8, 4))

        self._discord_auto_var = tk.BooleanVar(
            value=self._config.get("discord_auto_upload", False))
        self.chk_discord = ctk.CTkCheckBox(
            header, text="Upload to Discord",
            variable=self._discord_auto_var,
            command=self._on_discord_toggle,
            font=ctk.CTkFont("Segoe UI", 13, "bold"),
            checkbox_width=22, checkbox_height=22, corner_radius=4,
        )
        self.chk_discord.pack(side=tk.LEFT)

        self.lbl_discord_warn = ctk.CTkLabel(
            header, text="", text_color="#F44336",
            font=ctk.CTkFont("Segoe UI", 11, "bold"),
        )
        self.lbl_discord_warn.pack(side=tk.RIGHT)

        self.lbl_discord_status = ctk.CTkLabel(
            header, text="", text_color="#888",
            font=ctk.CTkFont("Segoe UI", 11),
        )
        self.lbl_discord_status.pack(side=tk.RIGHT, padx=(0, 12))

        # ── Settings area (hidden when unchecked) ──
        self._discord_settings = ctk.CTkFrame(discord_frame, fg_color="transparent")

        # Row 1: Bot token + Connect button
        token_row = ctk.CTkFrame(self._discord_settings, fg_color="transparent")
        token_row.pack(fill=tk.X, padx=10, pady=(2, 4))

        ctk.CTkLabel(
            token_row, text="Bot token:",
            font=ctk.CTkFont("Segoe UI", 12), width=80, anchor="w",
        ).pack(side=tk.LEFT)

        self._token_var = tk.StringVar(
            value=self._config.get("discord_bot_token", ""))
        self.entry_token = ctk.CTkEntry(
            token_row, textvariable=self._token_var,
            placeholder_text="Paste your bot token here",
            font=ctk.CTkFont("Segoe UI", 11), height=30, show="•",
        )
        self.entry_token.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 6))

        self._show_token_var = tk.BooleanVar(value=False)
        self.btn_show_token = ctk.CTkButton(
            token_row, text="👁", width=32, height=30, corner_radius=6,
            fg_color="#333", hover_color="#444",
            command=self._toggle_token_visibility,
            font=ctk.CTkFont("Segoe UI", 13),
        )
        self.btn_show_token.pack(side=tk.LEFT, padx=(0, 6))

        self.btn_connect = ctk.CTkButton(
            token_row, text="Connect", command=self._connect_discord,
            width=90, height=30, corner_radius=8,
            fg_color="#5865F2", hover_color="#4752C4",
            font=ctk.CTkFont("Segoe UI", 12),
        )
        self.btn_connect.pack(side=tk.LEFT)

        self.lbl_bot_status = ctk.CTkLabel(
            token_row, text="", width=20,
            font=ctk.CTkFont("Segoe UI", 12),
        )
        self.lbl_bot_status.pack(side=tk.LEFT, padx=(4, 0))

        # Row 2: Server + Channel dropdowns + tier + upload button
        sel_row = ctk.CTkFrame(self._discord_settings, fg_color="transparent")
        sel_row.pack(fill=tk.X, padx=10, pady=(0, 4))

        ctk.CTkLabel(
            sel_row, text="Server:",
            font=ctk.CTkFont("Segoe UI", 12), width=80, anchor="w",
        ).pack(side=tk.LEFT)

        self.combo_guild = ctk.CTkComboBox(
            sel_row, values=["(connect first)"],
            state="readonly", width=200,
            command=self._on_guild_selected,
            font=ctk.CTkFont("Segoe UI", 11),
        )
        self.combo_guild.set("(connect first)")
        self.combo_guild.pack(side=tk.LEFT, padx=(6, 10))

        ctk.CTkLabel(
            sel_row, text="Channel:",
            font=ctk.CTkFont("Segoe UI", 12), anchor="w",
        ).pack(side=tk.LEFT)

        self.combo_channel = ctk.CTkComboBox(
            sel_row, values=["(select server)"],
            state="readonly", width=200,
            command=self._on_channel_selected,
            font=ctk.CTkFont("Segoe UI", 11),
        )
        self.combo_channel.set("(select server)")
        self.combo_channel.pack(side=tk.LEFT, padx=(6, 0))

        # Row 3: Tier + manual upload button
        tier_row = ctk.CTkFrame(self._discord_settings, fg_color="transparent")
        tier_row.pack(fill=tk.X, padx=10, pady=(0, 8))

        ctk.CTkLabel(
            tier_row, text="Nitro tier:",
            font=ctk.CTkFont("Segoe UI", 12), width=80, anchor="w",
        ).pack(side=tk.LEFT)

        self._tier_var = tk.StringVar(
            value=self._config.get("discord_tier", "Free (10 MB)"))
        self.combo_tier = ctk.CTkComboBox(
            tier_row, values=list(UPLOAD_LIMITS.keys()),
            variable=self._tier_var,
            state="readonly", width=170,
            command=self._on_tier_changed,
            font=ctk.CTkFont("Segoe UI", 12),
        )
        self.combo_tier.pack(side=tk.LEFT, padx=(6, 12))

        self.btn_discord_upload = ctk.CTkButton(
            tier_row, text="📤  Upload Now", command=self._manual_discord_upload,
            width=130, height=30, corner_radius=8,
            fg_color="#5865F2", hover_color="#4752C4",
            font=ctk.CTkFont("Segoe UI", 12),
        )
        self.btn_discord_upload.pack(side=tk.RIGHT)

        # Setup link
        ctk.CTkLabel(
            tier_row,
            text="Need a bot? discord.com/developers → New App → Bot → copy token",
            text_color="#555", font=ctk.CTkFont("Segoe UI", 10),
        ).pack(side=tk.RIGHT, padx=(0, 12))

        # Show/hide based on saved state
        if self._discord_auto_var.get():
            self._discord_settings.pack(fill=tk.X)

        # Auto-connect if we have a saved token
        if self._token_var.get().strip():
            self.root.after(500, self._connect_discord)

    # -- Discord panel logic ----------------------------------------------

    def _toggle_token_visibility(self):
        if self.entry_token.cget("show") == "•":
            self.entry_token.configure(show="")
            self.btn_show_token.configure(text="🔒")
        else:
            self.entry_token.configure(show="•")
            self.btn_show_token.configure(text="👁")

    def _on_discord_toggle(self):
        """Show/hide Discord settings and save preference."""
        if self._discord_auto_var.get():
            self._discord_settings.pack(fill=tk.X)
        else:
            self._discord_settings.pack_forget()
        self._config["discord_auto_upload"] = self._discord_auto_var.get()
        _save_config(self._config)
        self._update_discord_warning()

    def _connect_discord(self):
        """Validate the bot token and populate the server list."""
        token = self._token_var.get().strip()
        if not token:
            self.lbl_bot_status.configure(text="✕", text_color="#F44336")
            return

        self.btn_connect.configure(state="disabled", text="...")
        self.lbl_bot_status.configure(text="", text_color="#888")

        def _work():
            ok, info = validate_bot_token(token)
            if ok:
                guilds = list_guilds(token)
                self.root.after(0, self._on_connect_ok, info, guilds, token)
            else:
                self.root.after(0, self._on_connect_fail)

        threading.Thread(target=_work, daemon=True).start()

    def _on_connect_ok(self, bot_info, guilds, token):
        self._discord_connected = True
        self._discord_guilds = guilds

        # Save token
        self._config["discord_bot_token"] = token
        _save_config(self._config)

        bot_name = bot_info.get("username", "Bot")
        self.lbl_bot_status.configure(text=f"✓ {bot_name}", text_color="#4CAF50")
        self.btn_connect.configure(state="normal", text="Connect")

        # Populate server dropdown
        if guilds:
            names = [g["name"] for g in guilds]
            self.combo_guild.configure(values=names)
            # Restore last selected guild
            saved_guild = self._config.get("discord_guild_name", "")
            if saved_guild in names:
                self.combo_guild.set(saved_guild)
                self._on_guild_selected(saved_guild)
            else:
                self.combo_guild.set(names[0])
                self._on_guild_selected(names[0])
        else:
            self.combo_guild.configure(values=["(bot not in any servers)"])
            self.combo_guild.set("(bot not in any servers)")

    def _on_connect_fail(self):
        self._discord_connected = False
        self.lbl_bot_status.configure(text="✕ Invalid", text_color="#F44336")
        self.btn_connect.configure(state="normal", text="Connect")
        self.combo_guild.configure(values=["(connect first)"])
        self.combo_guild.set("(connect first)")
        self.combo_channel.configure(values=["(select server)"])
        self.combo_channel.set("(select server)")

    def _on_guild_selected(self, guild_name):
        """Fetch and populate channels for the selected guild."""
        # Find guild id
        guild = next((g for g in self._discord_guilds if g["name"] == guild_name), None)
        if not guild:
            return

        self._config["discord_guild_name"] = guild_name
        _save_config(self._config)

        token = self._token_var.get().strip()
        guild_id = guild["id"]

        self.combo_channel.configure(values=["Loading..."])
        self.combo_channel.set("Loading...")

        def _work():
            channels = list_text_channels(token, guild_id)
            self.root.after(0, self._on_channels_loaded, channels)

        threading.Thread(target=_work, daemon=True).start()

    def _on_channels_loaded(self, channels):
        self._discord_channels = channels
        if channels:
            names = [f"# {c['name']}" for c in channels]
            self.combo_channel.configure(values=names)
            # Restore last selected channel
            saved_ch = self._config.get("discord_channel_name", "")
            if saved_ch in names:
                self.combo_channel.set(saved_ch)
            else:
                self.combo_channel.set(names[0])
                self._config["discord_channel_name"] = names[0]
                _save_config(self._config)
        else:
            self.combo_channel.configure(values=["(no text channels)"])
            self.combo_channel.set("(no text channels)")

    def _on_channel_selected(self, channel_display_name):
        """Save selected channel."""
        self._config["discord_channel_name"] = channel_display_name
        _save_config(self._config)

    def _get_selected_channel_id(self) -> str | None:
        """Return the Discord channel ID for the currently selected channel."""
        display = self.combo_channel.get()
        if not display.startswith("# "):
            return None
        ch_name = display[2:]
        ch = next((c for c in self._discord_channels if c["name"] == ch_name), None)
        return ch["id"] if ch else None

    def _on_tier_changed(self, *_args):
        """Save tier and update size warning."""
        self._config["discord_tier"] = self._tier_var.get()
        _save_config(self._config)
        self._update_discord_warning()

    def _get_discord_limit(self) -> int:
        return UPLOAD_LIMITS.get(self._tier_var.get(), DISCORD_FREE_LIMIT)

    def _update_discord_warning(self):
        """Show a warning if the estimated file size exceeds the Discord limit."""
        if not self._discord_auto_var.get():
            self.lbl_discord_warn.configure(text="")
            return
        if not self.video_info or not self.timeline.has_clip:
            self.lbl_discord_warn.configure(text="")
            return

        est_bytes = self._estimate_output_bytes()
        limit = self._get_discord_limit()

        if est_bytes > limit:
            limit_mb = limit / (1024 * 1024)
            est_mb = est_bytes / (1024 * 1024)
            self.lbl_discord_warn.configure(
                text=f"⚠ ~{est_mb:.1f} MB > {limit_mb:.0f} MB limit — won't upload",
                text_color="#F44336",
            )
        else:
            self.lbl_discord_warn.configure(
                text="✓ Within Discord limit",
                text_color="#4CAF50",
            )

    def _estimate_output_bytes(self) -> float:
        """Return estimated output file size in bytes."""
        if not self.video_info or not self.timeline.has_clip:
            return 0
        tl = self.timeline
        if tl.multi_clip:
            clip_dur = sum(ce - cs for cs, ce in tl.clips)
        else:
            clip_dur = tl.clip_end - tl.clip_start
        if clip_dur <= 0:
            return 0

        w = self.video_info["width"]
        h = self.video_info["height"]
        fps = self.video_info["fps"]
        pixels_per_sec = w * h * fps

        preset = self.combo_q.get()
        quality, audio_br_str = VideoProcessor.QUALITY_PRESETS.get(preset, (26, "128k"))

        if self.has_hevc:
            bpp = 0.22 * (2.0 ** (-quality / 7.0))
        else:
            bpp = 0.33 * (2.0 ** (-quality / 7.0))

        video_bps = pixels_per_sec * bpp / 8
        src_bps = self.video_info["file_size_bytes"] / max(self.video_info["duration"], 1)
        video_bps = min(video_bps, src_bps)

        audio_bps = int(audio_br_str.rstrip("k")) * 1000 / 8
        return (video_bps + audio_bps) * clip_dur

    def _manual_discord_upload(self):
        """Pick a file and upload it to the selected Discord channel."""
        if self._uploading:
            return
        channel_id = self._get_selected_channel_id()
        if not channel_id:
            messagebox.showwarning(
                "No Channel Selected",
                "Connect your bot and select a server/channel first.")
            return

        last_dir = self._config.get("last_save_dir", "")
        file_path = filedialog.askopenfilename(
            title="Select Video to Upload to Discord",
            initialdir=last_dir if last_dir and os.path.isdir(last_dir) else None,
            filetypes=[("MP4 video", "*.mp4"), ("All files", "*.*")],
        )
        if not file_path:
            return

        tier = self._tier_var.get()
        ok, file_size, limit = check_file_size(file_path, tier)
        if not ok:
            limit_mb = limit / (1024 * 1024)
            size_mb = file_size / (1024 * 1024)
            proceed = messagebox.askyesno(
                "File Too Large",
                f"The file is {size_mb:.1f} MB but your Discord limit is "
                f"{limit_mb:.0f} MB.\n\nUpload anyway?")
            if not proceed:
                return

        self._do_discord_upload(file_path)

    def _do_discord_upload(self, file_path: str):
        """Start the Discord upload in background using bot token."""
        token = self._token_var.get().strip()
        channel_id = self._get_selected_channel_id()
        if not token or not channel_id:
            messagebox.showwarning(
                "Discord Not Ready",
                "Connect your bot and select a channel first.")
            return

        self._uploading = True
        self.btn_discord_upload.configure(state="disabled")
        self.lbl_discord_status.configure(text="Uploading...", text_color="#5865F2")

        upload_to_channel(
            token=token,
            channel_id=channel_id,
            file_path=file_path,
            progress_callback=lambda s: self.root.after(0, self._discord_prog, s),
            done_callback=lambda ok, m: self.root.after(0, self._discord_done, ok, m),
        )

    def _discord_prog(self, status):
        self.lbl_discord_status.configure(text=status, text_color="#5865F2")

    def _discord_done(self, success, msg):
        self._uploading = False
        self.btn_discord_upload.configure(state="normal")
        if success:
            self.lbl_discord_status.configure(text="✓ Uploaded!", text_color="#4CAF50")
            self.lbl_status.configure(text="Discord upload complete!")
        else:
            self.lbl_discord_status.configure(text="✕ Failed", text_color="#F44336")
            messagebox.showerror("Discord Upload Failed", msg)

    # ------------------------------------------------------------------
    # Keyboard shortcuts
    # ------------------------------------------------------------------

    def _bind_keys(self):
        self.root.bind_all("<space>", self._key_toggle_play)
        self.root.bind_all("q", lambda e: self._mark_in() if not self._in_entry() else None)
        self.root.bind_all("e", lambda e: self._mark_out() if not self._in_entry() else None)
        self.root.bind_all("c", lambda e: self._clear_markers() if not self._in_entry() else None)
        self.root.bind_all("[", lambda e: self._mark_in() if not self._in_entry() else None)
        self.root.bind_all("]", lambda e: self._mark_out() if not self._in_entry() else None)
        self.root.bind_all("<Left>", lambda e: self._seek_rel(-5) if not self._in_entry() else None)
        self.root.bind_all("<Right>", lambda e: self._seek_rel(5) if not self._in_entry() else None)
        self.root.bind_all("<Shift-Left>", lambda e: self._seek_rel(-1) if not self._in_entry() else None)
        self.root.bind_all("<Shift-Right>", lambda e: self._seek_rel(1) if not self._in_entry() else None)
        self.root.bind_all("a", lambda e: self._seek_rel(-5) if not self._in_entry() else None)
        self.root.bind_all("d", lambda e: self._seek_rel(5) if not self._in_entry() else None)
        self.root.bind_all("A", lambda e: self._seek_rel(-1) if not self._in_entry() else None)
        self.root.bind_all("D", lambda e: self._seek_rel(1) if not self._in_entry() else None)
        self.root.bind_all("<Alt-a>", lambda e: self._seek_rel(-0.1) if not self._in_entry() else None)
        self.root.bind_all("<Alt-d>", lambda e: self._seek_rel(0.1) if not self._in_entry() else None)
        self.root.bind_all("?", lambda e: self._show_shortcuts() if not self._in_entry() else None)

    # ------------------------------------------------------------------
    # Help guide overlay
    # ------------------------------------------------------------------

    def _show_shortcuts(self):
        if hasattr(self, "_shortcut_win") and self._shortcut_win and self._shortcut_win.winfo_exists():
            self._shortcut_win.destroy()
            return
        W, H = 520, 560
        win = ctk.CTkToplevel(self.root)
        self._shortcut_win = win
        win.title("Help — ClipCut")
        win.resizable(False, False)
        win.attributes("-topmost", True)
        win.geometry(f"{W}x{H}")
        win.update_idletasks()
        px = self.root.winfo_x() + (self.root.winfo_width() - W) // 2
        py = self.root.winfo_y() + (self.root.winfo_height() - H) // 2
        win.geometry(f"+{px}+{py}")

        # ── Tab bar ──
        tab_bar = ctk.CTkFrame(win, fg_color="transparent")
        tab_bar.pack(fill=tk.X, padx=16, pady=(12, 0))

        body = ctk.CTkFrame(win, fg_color="transparent")
        body.pack(fill=tk.BOTH, expand=True, padx=16, pady=(8, 0))

        tab_buttons: list[ctk.CTkButton] = []
        pages: dict[str, list] = {}

        def _switch(name):
            for child in body.winfo_children():
                child.destroy()
            for btn in tab_buttons:
                btn.configure(fg_color="#333", hover_color="#444")
            tab_buttons[list(pages.keys()).index(name)].configure(
                fg_color="#0078d4", hover_color="#1a8ae8")
            _render(body, pages[name])

        def _render(parent, items):
            scroll = ctk.CTkScrollableFrame(parent, fg_color="transparent")
            scroll.pack(fill=tk.BOTH, expand=True)
            for item in items:
                kind = item[0]
                if kind == "heading":
                    ctk.CTkLabel(
                        scroll, text=item[1],
                        font=ctk.CTkFont("Segoe UI", 14, "bold"),
                    ).pack(anchor="w", pady=(10, 4))
                elif kind == "text":
                    ctk.CTkLabel(
                        scroll, text=item[1], wraplength=460,
                        justify="left", anchor="w",
                        text_color="#bbb",
                        font=ctk.CTkFont("Segoe UI", 12),
                    ).pack(anchor="w", pady=(2, 2))
                elif kind == "shortcut":
                    row = ctk.CTkFrame(scroll, fg_color="transparent")
                    row.pack(fill=tk.X, pady=2)
                    ctk.CTkLabel(
                        row, text=item[1], width=160, anchor="e",
                        font=ctk.CTkFont("Consolas", 12, "bold"),
                        text_color="#0078d4",
                    ).pack(side=tk.LEFT)
                    ctk.CTkLabel(
                        row, text=item[2], anchor="w",
                        font=ctk.CTkFont("Segoe UI", 12),
                    ).pack(side=tk.LEFT, padx=(12, 0))
                elif kind == "step":
                    ctk.CTkLabel(
                        scroll, text=item[1], wraplength=460,
                        justify="left", anchor="w",
                        text_color="#ccc",
                        font=ctk.CTkFont("Segoe UI", 12),
                    ).pack(anchor="w", padx=(16, 0), pady=(2, 2))
                elif kind == "sep":
                    ctk.CTkFrame(
                        scroll, height=1, fg_color="#333",
                    ).pack(fill=tk.X, pady=6)

        # ── Define pages ──
        pages["Quick Start"] = [
            ("heading", "Getting Started"),
            ("step", "1.  Open a video — click the dark area or drag a file onto the window."),
            ("step", "2.  Play/pause with Space, scrub by clicking the timeline."),
            ("step", "3.  Press Q to mark the start of a clip (Mark In)."),
            ("step", "4.  Press E to mark the end (Mark Out)."),
            ("step", "5.  Click ✂ Export to save the clipped section."),
            ("sep",),
            ("heading", "Tips"),
            ("text", "• Hover any button or control to see what it does."),
            ("text", "• The status bar at the bottom always shows key shortcuts."),
            ("text", "• Press ? at any time to reopen this guide."),
            ("text", "• You can drag & drop a video file directly onto the window." if HAS_DND else ""),
        ]

        pages["Shortcuts"] = [
            ("heading", "Playback"),
            ("shortcut", "Space", "Play / Pause"),
            ("shortcut", "A / D  or  ← / →", "Seek ±5 seconds"),
            ("shortcut", "Shift + A/D  or  ← / →", "Seek ±1 second"),
            ("shortcut", "Alt + A / D", "Seek ±100 ms (frame-level)"),
            ("sep",),
            ("heading", "Clip Markers"),
            ("shortcut", "Q  or  [", "Set Mark In"),
            ("shortcut", "E  or  ]", "Set Mark Out"),
            ("shortcut", "C", "Clear all markers"),
            ("sep",),
            ("heading", "Timeline"),
            ("shortcut", "Scroll Wheel", "Zoom in / out"),
            ("shortcut", "Click track", "Scrub playhead"),
            ("shortcut", "Drag handles", "Adjust clip boundaries"),
            ("text", "Click above or below the track bar to scrub without accidentally grabbing a handle."),
            ("sep",),
            ("heading", "Other"),
            ("shortcut", "?", "Toggle this Help guide"),
        ]

        pages["Multi-Clip"] = [
            ("heading", "Multi-Clip Mode"),
            ("text", "Enable the Multi Clips checkbox to mark several clips on the same video and export them all at once."),
            ("sep",),
            ("heading", "Workflow"),
            ("step", "1.  Check Multi Clips in the control bar."),
            ("step", "2.  Press Q to start the first clip."),
            ("step", "3.  Press E to confirm that clip's end — it locks in."),
            ("step", "4.  Repeat Q → E for each additional clip."),
            ("step", "5.  Click ✂ Export — a dialog lets you name each clip."),
            ("sep",),
            ("heading", "Notes"),
            ("text", "• Clips appear as coloured regions on the timeline."),
            ("text", "• Drag a clip's handles to adjust after placing it."),
            ("text", "• Press C to clear all clips at once."),
            ("text", "• Loop Clip loops within whichever clip the playhead is in."),
        ]

        pages["Timeline"] = [
            ("heading", "Timeline Controls"),
            ("text", "The timeline shows your video from left to right. The bright bar is the track; the thin vertical line is the playhead."),
            ("sep",),
            ("heading", "Scrubbing"),
            ("text", "Click anywhere on (or above/below) the track to move the playhead. Dashed lines above and below the track hint at these safe-scrub zones — no handles will grab there."),
            ("sep",),
            ("heading", "Zooming"),
            ("text", "Use the scroll wheel over the timeline to zoom in for precise trimming. A minimap appears at the top showing your zoomed view within the full video."),
            ("sep",),
            ("heading", "Handles"),
            ("text", "After setting Mark In/Out, two thin handles appear on the track. Drag them horizontally to adjust the clip boundaries. Handles only activate when the mouse is directly on them, so move slightly above or below to scrub freely."),
        ]

        pages["Discord"] = [
            ("heading", "Discord Auto-Upload"),
            ("text", "ClipCut can upload your exported clips directly to a Discord channel via a bot."),
            ("sep",),
            ("heading", "Setup"),
            ("step", "1.  Go to discord.com/developers and create a New Application."),
            ("step", "2.  Go to the Bot tab and click Reset Token, then Copy."),
            ("step", "3.  Under OAuth2 → URL Generator, check 'bot' and 'Send Messages' + 'Attach Files'."),
            ("step", "4.  Open the generated URL to invite the bot to your server."),
            ("step", "5.  Back in ClipCut, check Upload to Discord, paste the token, and click Connect."),
            ("step", "6.  Choose your Server and Channel from the dropdowns."),
            ("sep",),
            ("heading", "Nitro Tier"),
            ("text", "Set your Nitro tier so the app can warn you when a file exceeds your upload limit (10 MB free, up to 500 MB for server boosts)."),
            ("sep",),
            ("heading", "Manual Upload"),
            ("text", "Click 📤 Upload Now to pick any file from disk and send it to the selected channel — useful for previously exported clips."),
        ]

        # ── Build tab buttons ──
        for name in pages:
            btn = ctk.CTkButton(
                tab_bar, text=name, width=90, height=28, corner_radius=6,
                fg_color="#333", hover_color="#444",
                font=ctk.CTkFont("Segoe UI", 12),
                command=lambda n=name: _switch(n),
            )
            btn.pack(side=tk.LEFT, padx=(0, 6))
            tab_buttons.append(btn)

        # ── Footer ──
        ctk.CTkButton(
            win, text="Close", command=win.destroy,
            width=100, height=30, corner_radius=8,
        ).pack(pady=(8, 12))

        # Show first tab
        _switch("Quick Start")

    # ------------------------------------------------------------------
    # Drag-and-drop
    # ------------------------------------------------------------------

    def _setup_dnd(self):
        """Register drag-and-drop using tkdnd Tcl package bundled with tkinterdnd2."""
        try:
            import tkinterdnd2
            import platform
            system = platform.system()
            if system == "Windows":
                machine = os.environ.get('PROCESSOR_ARCHITECTURE', platform.machine())
            else:
                machine = platform.machine()
            plat_map = {
                ("Windows", "AMD64"): "win-x64",
                ("Windows", "x86"): "win-x86",
                ("Windows", "ARM64"): "win-arm64",
                ("Linux", "x86_64"): "linux-x64",
                ("Linux", "aarch64"): "linux-arm64",
                ("Darwin", "x86_64"): "osx-x64",
                ("Darwin", "arm64"): "osx-arm64",
            }
            tkdnd_dir = os.path.join(
                os.path.dirname(tkinterdnd2.__file__),
                "tkdnd", plat_map.get((system, machine), "")
            )
            self.root.tk.call("lappend", "auto_path", tkdnd_dir)
            self.root.tk.call("package", "require", "tkdnd")
            self.root.tk.call("tkdnd::drop_target", "register", self.root._w, "DND_Files")

            # Use a Tcl-level proc to handle the drop — avoids %# substitution
            # errors that occur with Python-level bind on <<Drop:DND_Files>>.
            tcl_cmd_name = "_py_dnd_drop"
            self.root.tk.createcommand(tcl_cmd_name, self._on_drop_tcl)
            self.root.tk.call(
                "bind", self.root._w, "<<Drop:DND_Files>>",
                tcl_cmd_name + " %D"
            )
        except Exception:
            pass

    def _on_drop_tcl(self, data):
        """Called from Tcl with the dropped data (%D substitution)."""
        if self._encoding:
            return
        path = data.strip() if data else ""
        if not path:
            return
        # tkdnd wraps paths with spaces in braces: {C:/path with spaces/file.mp4}
        if path.startswith("{") and path.endswith("}"):
            path = path[1:-1]
        # Handle multiple files — take the first
        if path.startswith("\""):
            path = path.split("\"")[1]
        video_exts = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".ts", ".m4v", ".mpg", ".mpeg"}
        if os.path.splitext(path)[1].lower() not in video_exts:
            messagebox.showwarning("Unsupported file", "Please drop a video file.")
            return
        self._load_video(path)

    def _in_entry(self):
        w = self.root.focus_get()
        return isinstance(w, (tk.Entry, ctk.CTkEntry))

    def _key_toggle_play(self, _ev):
        if not self._in_entry():
            self._toggle_play()
            return "break"

    # ------------------------------------------------------------------
    # Open file
    # ------------------------------------------------------------------

    def _on_video_area_click(self, _ev=None):
        """Open file dialog when clicking the video preview area (only when no video is loaded)."""
        if not self.video_path and not self._encoding:
            self._open_file()
        else:
            self.video_frame.focus_set()

    def _open_file(self):
        if self._encoding:
            return
        path = filedialog.askopenfilename(
            title="Select Video File",
            filetypes=[
                ("Video files", "*.mp4 *.mkv *.avi *.mov *.wmv *.flv *.webm *.ts *.m4v *.mpg *.mpeg"),
                ("All files", "*.*"),
            ],
        )
        if not path:
            return
        self._load_video(path)

    def _load_video(self, path):
        self.lbl_status.configure(text="Reading video metadata...")
        self.root.update_idletasks()

        try:
            info = self.processor.probe_video(path)
        except Exception as e:
            messagebox.showerror("Error", f"Failed to read video:\n{e}")
            self.lbl_status.configure(text="Ready")
            return

        self.video_path = path
        self.video_info = info
        dur = info["duration"]

        self.lbl_file.configure(text=os.path.basename(path), text_color="#e0e0e0")
        self.lbl_status.configure(
            text=f"{info['width']}×{info['height']}  │  {info['codec'].upper()}  │  "
                 f"{info['fps']} fps  │  {_fmt_time(dur)}  │  "
                 f"{self.processor._human_size(info['file_size_bytes'])}")

        # timeline
        self.timeline.set_duration(dur)
        self._sync_clip()

        # load video
        if self._mpv_ok:
            self._mpv_load(path)
        else:
            self._thumb_seek(0)

        self.lbl_status.configure(text="Q / E = mark in/out  │  drag handles to adjust")

    # ------------------------------------------------------------------
    # mpv playback
    # ------------------------------------------------------------------

    def _mpv_load(self, path):
        self.lbl_preview.place_forget()
        self._mpv.pause = True
        self._mpv.play(path)
        self.btn_play.configure(text="▶  Play")

    def _toggle_play(self):
        if not self._mpv_ok or not self.video_path:
            return
        self._mpv.pause = not self._mpv.pause

    def _seek_to(self, sec):
        if not self.video_path:
            return
        sec = max(0, sec)
        if self._mpv_ok:
            self._mpv.seek(sec, "absolute", "exact")
            if not self.timeline._dragging:
                self.timeline.set_position(sec)
            self._update_time(sec)
        else:
            self._thumb_seek(sec)
            if not self.timeline._dragging:
                self.timeline.set_position(sec)
            self._update_time(sec)

    def _seek_rel(self, delta):
        if not self.video_path:
            return
        cur = self._cur_sec()
        self._seek_to(cur + delta)

    def _cur_sec(self):
        if self.timeline._dragging:
            return self.timeline.position
        if self._mpv_ok and self._mpv:
            tp = self._mpv.time_pos
            return tp if tp is not None else 0
        return self.timeline.position

    def _on_volume(self, val):
        if self._mpv_ok and self._mpv:
            self._mpv.volume = int(float(val))

    def _update_time(self, sec):
        dur = self.video_info["duration"] if self.video_info else 0
        self.lbl_time.configure(text=f"{_fmt_time(sec)} / {_fmt_time(dur)}")

    def _close_video(self):
        """Stop mpv, clear video state, and reset the UI."""
        if self._mpv_ok:
            self._mpv.command("stop")
        self.video_path = None
        self.video_info = None
        self.timeline.set_duration(0)
        self.timeline.clear_clip()
        self._sync_clip()
        self.lbl_file.configure(text="No file loaded", text_color="#888")
        self.lbl_time.configure(text="0:00.00 / 0:00.00")
        self.lbl_status.configure(text="Video ended \u2014 open a file to start again")
        self.btn_play.configure(text="\u25b6  Play")
        self.lbl_preview.config(image="", text="Click to open video or drag directly here")
        self.lbl_preview.place(relx=0.5, rely=0.5, anchor="center")
        self._thumb_ref = None

    # ------------------------------------------------------------------
    # Scrub frame cache (keyframe thumbnails in RAM)
    # ------------------------------------------------------------------
    # Fallback thumbnails (no mpv)
    # ------------------------------------------------------------------

    def _thumb_seek(self, sec):
        if not self.video_path or not HAS_PIL:
            return

        def _work():
            p = self.processor.generate_thumbnail(self.video_path, sec)
            if p:
                self.root.after(0, lambda: self._show_thumb(p))

        threading.Thread(target=_work, daemon=True).start()

    def _show_thumb(self, path):
        try:
            img = Image.open(path)
            pw = max(self.video_frame.winfo_width(), 320)
            ph = max(self.video_frame.winfo_height(), 180)
            img.thumbnail((pw, ph), Image.LANCZOS)
            photo = ImageTk.PhotoImage(img)
            self.lbl_preview.config(image=photo, text="")
            self.lbl_preview.place(relx=0.5, rely=0.5, anchor="center")
            self._thumb_ref = photo
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Timeline / range interaction
    # ------------------------------------------------------------------

    def _tl_seek(self, sec):
        """Seek mpv to the given position — precise seeking handled natively."""
        if self._mpv_ok and self.video_path:
            self._mpv.seek(sec, "absolute", "exact")
        self._update_time(sec)

    def _on_quality_changed(self, *_):
        self._update_size_estimate()
        self._update_discord_warning()
        self.root.focus_set()

    def _tl_drag_start(self):
        """Pause playback while the user is scrubbing the timeline."""
        if self._mpv_ok and not self._mpv.pause:
            self._was_playing_before_drag = True
            self._mpv.pause = True
        else:
            self._was_playing_before_drag = False

    def _tl_drag_end(self):
        """Seek mpv to final position, resume if was playing."""
        if self._mpv_ok:
            sec = self.timeline.position
            self._mpv.seek(sec, "absolute", "exact")
        if self._was_playing_before_drag and self._mpv_ok:
            self._mpv.pause = False
            self._was_playing_before_drag = False

    def _tl_range_changed(self, start, end):
        self._update_clip_label()
        self._update_discord_warning()

    def _sync_clip(self):
        self._update_clip_label()
        self._update_discord_warning()

    def _on_multi_clip_toggle(self):
        """Toggle multi-clip mode on the timeline."""
        enabled = self._multi_clip_var.get()
        tl = self.timeline
        if enabled:
            # Preserve existing single clip as the first multi-clip entry
            if tl.clip_start >= 0 and tl.clip_end >= 0:
                tl.clips.append((tl.clip_start, tl.clip_end))
            tl.clip_start = -1.0
            tl.clip_end = -1.0
            tl.pending_max = -1.0
            tl.multi_clip = True
            tl._invalidate()
            self._sync_clip()
            n = len(tl.clips)
            if n:
                self.lbl_status.configure(
                    text=f"Multi-clip mode ON — {n} clip kept, press Q/E to add more (max 60s each)")
            else:
                self.lbl_status.configure(
                    text="Multi-clip mode ON — press Q/E to add clips (max 60s each)")
        else:
            tl.multi_clip = False
            tl.clips.clear()
            tl.clip_start = -1.0
            tl.clip_end = -1.0
            tl.pending_max = -1.0
            tl._active_clip_idx = None
            tl._invalidate()
            self._sync_clip()
            self.lbl_status.configure(text="Multi-clip mode OFF — single clip mode")

    def _mark_in(self):
        if not self.video_path:
            return
        sec = self._cur_sec()
        dur = self.video_info["duration"]
        sec = max(0.0, min(sec, dur - RangeTimeline.MIN_CLIP))

        if self.timeline.multi_clip:
            # In multi-clip mode, cap the ghost zone at 60s from mark-in
            max_end = min(sec + 60.0, dur)
            self.timeline.clip_start = sec
            self.timeline.clip_end = -1.0
            self.timeline.pending_max = max_end
            self.timeline._invalidate()
            self._sync_clip()
            self.lbl_status.configure(
                text=f"Mark In at {_fmt_time(sec)} — press E to set Mark Out (max 60s)")
        else:
            # Set clip_start, clear clip_end, show ghost zone
            self.timeline.clip_start = sec
            self.timeline.clip_end = -1.0
            self.timeline.pending_max = dur
            self.timeline._invalidate()
            self._sync_clip()
            self.lbl_status.configure(text=f"Mark In at {_fmt_time(sec)} — press E to set Mark Out")

    def _mark_out(self):
        if not self.video_path:
            return
        if not self.timeline.is_pending:
            self.lbl_status.configure(text="Press Q first to set Mark In")
            return
        sec = self._cur_sec()
        start = self.timeline.clip_start
        dur = self.video_info["duration"]
        min_end = start + RangeTimeline.MIN_CLIP

        sec = max(min_end, min(sec, dur))

        if self.timeline.multi_clip:
            # Enforce 60s max per clip
            max_end = start + 60.0
            sec = min(sec, max_end)
            # Add to clips list
            self.timeline.clips.append((start, sec))
            # Reset pending state for next clip
            self.timeline.clip_start = -1.0
            self.timeline.clip_end = -1.0
            self.timeline.pending_max = -1.0
            self.timeline._invalidate()
            self._sync_clip()
            d = sec - start
            n = len(self.timeline.clips)
            self.lbl_status.configure(
                text=f"Clip {n} added: {_fmt_time(start)} → {_fmt_time(sec)} ({d:.1f}s) — press Q for next clip")
        else:
            self.timeline.clip_end = sec
            self.timeline.pending_max = -1.0   # clear ghost zone
            self.timeline._invalidate()
            self._sync_clip()
            d = sec - start
            self.lbl_status.configure(text=f"Clip set: {_fmt_time(start)} → {_fmt_time(sec)} ({d:.1f}s)")

    def _clear_markers(self):
        if not self.video_path:
            return
        self.timeline.clear_clip()
        self._sync_clip()
        self.lbl_status.configure(text="Markers cleared")

    def _update_clip_label(self):
        tl = self.timeline
        if tl.is_pending:
            if tl.multi_clip:
                n = len(tl.clips)
                prefix = f"[{n} clip{'s' if n != 1 else ''}] " if n else ""
                self.lbl_clip.configure(
                    text=f"{prefix}IN {_fmt_time(tl.clip_start)} — press E to mark out",
                    text_color="#FFC107")
            else:
                self.lbl_clip.configure(
                    text=f"IN {_fmt_time(tl.clip_start)} — press E to mark out",
                    text_color="#FFC107")
            self.lbl_estimate.configure(text="")
            self._tip_clip.text = ""
            self._tip_estimate.text = ""
            return
        if tl.multi_clip:
            if not tl.clips:
                self.lbl_clip.configure(text="No clips set — press Q to start", text_color="#888")
                self.lbl_estimate.configure(text="")
                self._tip_clip.text = ""
                self._tip_estimate.text = ""
                return
            n = len(tl.clips)
            total_d = sum(ce - cs for cs, ce in tl.clips)
            self.lbl_clip.configure(
                text=f"{n} clip{'s' if n != 1 else ''}  ({total_d:.1f}s total)",
                text_color="#4CAF50")
            self._tip_clip.text = "\n".join(
                f"  Clip {i+1}: {_fmt_time(cs)} → {_fmt_time(ce)} ({ce-cs:.1f}s)"
                for i, (cs, ce) in enumerate(tl.clips))
            self._update_size_estimate()
            return
        if not tl.has_clip:
            self.lbl_clip.configure(text="No clip set", text_color="#888")
            self.lbl_estimate.configure(text="")
            self._tip_clip.text = ""
            self._tip_estimate.text = ""
            return
        d = tl.clip_end - tl.clip_start
        if d < RangeTimeline.MIN_CLIP:
            color = "#ff4444"
            tip = f"Clip is too short — minimum is {RangeTimeline.MIN_CLIP:.0f} seconds."
        elif d <= 60:
            color = "#4CAF50"  # green — fast export
            tip = "Green: Short clip (≤ 1 min) — fast export, small file size."
        elif d <= 180:
            color = "#FFC107"  # yellow — moderate export
            tip = "Yellow: Medium clip (1–3 min) — moderate export time and file size."
        else:
            color = "#F44336"  # red — long export
            tip = "Red: Long clip (> 3 min) — longer export time and larger file size."
        self.lbl_clip.configure(
            text=f"{_fmt_time(tl.clip_start)} → {_fmt_time(tl.clip_end)}  ({d:.1f}s)",
            text_color=color)
        self._tip_clip.text = tip
        self._update_size_estimate()

    def _update_size_estimate(self):
        """Estimate output file size using a bits-per-pixel model for CRF/CQP encoding."""
        if not self.video_info:
            self.lbl_estimate.configure(text="")
            return
        tl = self.timeline
        if not tl.has_clip:
            self.lbl_estimate.configure(text="")
            return

        # Compute total clip duration
        if tl.multi_clip:
            clip_dur = sum(ce - cs for cs, ce in tl.clips)
        else:
            clip_dur = tl.clip_end - tl.clip_start
        if clip_dur <= 0:
            return

        w = self.video_info["width"]
        h = self.video_info["height"]
        fps = self.video_info["fps"]
        pixels_per_sec = w * h * fps

        preset = self.combo_q.get()
        quality, audio_br_str = VideoProcessor.QUALITY_PRESETS.get(preset, (26, "128k"))

        # Bits-per-pixel model: bpp decreases exponentially with quality value.
        # Tuned against real NVENC/software encodes at 1080p and 4K.
        if self.has_hevc:
            bpp = 0.22 * (2.0 ** (-quality / 7.0))
        else:
            bpp = 0.33 * (2.0 ** (-quality / 7.0))

        video_bps = pixels_per_sec * bpp / 8  # bytes per second

        # Cap: re-encode can't realistically exceed source bitrate
        src_bps = self.video_info["file_size_bytes"] / max(self.video_info["duration"], 1)
        video_bps = min(video_bps, src_bps)

        audio_bps = int(audio_br_str.rstrip("k")) * 1000 / 8
        est_bytes = (video_bps + audio_bps) * clip_dur
        est_str = self.processor._human_size(int(est_bytes))
        self.lbl_estimate.configure(text=f"≈ {est_str}")
        self._tip_estimate.text = (
            f"Estimated output file size based on resolution ({w}×{h}), "
            f"frame rate ({fps} fps), and selected quality ({preset}).\n"
            "Actual size may vary depending on video complexity."
        )

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def _ask_multi_clip_names(self, clips, base):
        """Show a dialog letting the user name each clip. Returns list of filenames or None if cancelled."""
        dialog = ctk.CTkToplevel(self.root)
        dialog.title("Name Your Clips")
        dialog.resizable(False, False)
        dialog.attributes("-topmost", True)
        dialog.grab_set()

        width = 480
        row_h = 40
        height = 80 + len(clips) * row_h + 60
        dialog.geometry(f"{width}x{height}")
        dialog.update_idletasks()
        px = self.root.winfo_x() + (self.root.winfo_width() - width) // 2
        py = self.root.winfo_y() + (self.root.winfo_height() - height) // 2
        dialog.geometry(f"+{px}+{py}")

        ctk.CTkLabel(
            dialog, text="Name each clip:",
            font=ctk.CTkFont("Segoe UI", 14, "bold"),
        ).pack(pady=(12, 8))

        entries = []
        for i, (cs, ce) in enumerate(clips):
            row = ctk.CTkFrame(dialog, fg_color="transparent")
            row.pack(fill=tk.X, padx=16, pady=2)
            ctk.CTkLabel(
                row, text=f"Clip {i+1}  ({_fmt_time(cs)} → {_fmt_time(ce)}):",
                font=ctk.CTkFont("Segoe UI", 11),
                width=200, anchor="w",
            ).pack(side=tk.LEFT)
            entry = ctk.CTkEntry(row, font=ctk.CTkFont("Segoe UI", 11), width=240)
            entry.insert(0, f"{base}_clip{i+1}_{cs:.0f}-{ce:.0f}")
            entry.pack(side=tk.LEFT, padx=(4, 0))
            entries.append(entry)

        result = [None]  # mutable container for closure

        def _ok():
            names = [e.get().strip() for e in entries]
            if all(names):
                result[0] = names
                dialog.destroy()

        def _cancel():
            dialog.destroy()

        btn_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        btn_frame.pack(pady=(12, 12))
        ctk.CTkButton(btn_frame, text="Export", command=_ok,
                       width=100, fg_color="#0078d4", hover_color="#1a8ae8",
                       font=ctk.CTkFont("Segoe UI", 12, "bold")).pack(side=tk.LEFT, padx=8)
        ctk.CTkButton(btn_frame, text="Cancel", command=_cancel,
                       width=80, fg_color="#444", hover_color="#555",
                       font=ctk.CTkFont("Segoe UI", 12)).pack(side=tk.LEFT, padx=8)

        dialog.wait_window()
        return result[0]

    def _start_cut(self):
        if self._encoding:
            return
        if not self.video_path:
            messagebox.showwarning("No video", "Open a video file first.")
            return

        tl = self.timeline

        # --- multi-clip mode ---
        if tl.multi_clip:
            if not tl.clips:
                messagebox.showwarning("No clips",
                                       "Add clips with Q/E first.")
                return
            # Validate all clips
            dur = self.video_info["duration"]
            for i, (cs, ce) in enumerate(tl.clips):
                cd = ce - cs
                if cd < RangeTimeline.MIN_CLIP:
                    messagebox.showwarning("Invalid clip",
                                           f"Clip {i+1} is too short ({cd:.1f}s).")
                    return
                if cs < 0 or ce > dur:
                    messagebox.showwarning("Out of range",
                                           f"Clip {i+1} exceeds video duration.")
                    return

            # Pause playback
            if self._mpv_ok and not self._mpv.pause:
                self._mpv.pause = True

            # Ask for output directory
            last_dir = self._config.get("last_save_dir", "")
            out_dir = filedialog.askdirectory(
                title="Select Output Folder for Clips",
                initialdir=last_dir if last_dir and os.path.isdir(last_dir) else None,
            )
            if not out_dir:
                return

            self._config["last_save_dir"] = out_dir
            _save_config(self._config)

            # Show naming dialog
            base = os.path.splitext(os.path.basename(self.video_path))[0]
            names = self._ask_multi_clip_names(tl.clips, base)
            if names is None:
                return

            clip_jobs = []
            for i, (cs, ce) in enumerate(tl.clips):
                fname = names[i] if names[i].lower().endswith(".mp4") else names[i] + ".mp4"
                clip_jobs.append((cs, ce, os.path.join(out_dir, fname)))

            self._multi_clip_jobs = clip_jobs
            self._multi_clip_idx = 0
            self._multi_clip_outputs = []
            self._set_encoding(True)
            self._run_next_multi_clip()
            return

        # --- single-clip mode ---
        if not tl.has_clip:
            messagebox.showwarning("No clip",
                                   "Set Mark In (Q) and Mark Out (E) first.")
            return

        start = tl.clip_start
        end = tl.clip_end
        clip_dur = end - start

        if clip_dur < RangeTimeline.MIN_CLIP:
            messagebox.showwarning("Invalid range",
                                   f"Clip must be at least {RangeTimeline.MIN_CLIP:.0f} seconds.\nCurrent: {clip_dur:.1f}s")
            return
        if start < 0 or end > self.video_info["duration"]:
            messagebox.showwarning("Out of range", "Clip exceeds video duration.")
            return

        # pause playback
        if self._mpv_ok and not self._mpv.pause:
            self._mpv.pause = True

        base = os.path.splitext(os.path.basename(self.video_path))[0]
        default_name = f"{base}_clip_{start:.0f}-{end:.0f}.mp4"
        last_dir = self._config.get("last_save_dir", "")
        output_path = filedialog.asksaveasfilename(
            title="Export Clip As",
            initialdir=last_dir if last_dir and os.path.isdir(last_dir) else None,
            initialfile=default_name, defaultextension=".mp4",
            filetypes=[("MP4 video", "*.mp4"), ("All files", "*.*")],
        )
        if not output_path:
            return

        self._config["last_save_dir"] = os.path.dirname(output_path)
        _save_config(self._config)

        self._last_output_path = output_path
        self._set_encoding(True)
        self.processor.cut_and_encode(
            input_path=self.video_path, start_sec=start, end_sec=end,
            quality_preset=self.combo_q.get(), output_path=output_path,
            progress_callback=lambda p, s: self.root.after(0, self._prog, p, s),
            done_callback=lambda ok, m: self.root.after(0, self._done, ok, m),
            use_hevc=self.has_hevc,
        )


    def _prog(self, pct, status):
        import time as _time
        self.progress.set(pct / 100)
        eta_str = ""
        if pct > 2:
            elapsed = _time.time() - self._encode_start_time
            remaining = elapsed / pct * (100 - pct)
            if remaining >= 60:
                eta_str = f"  (~{remaining / 60:.0f}m left)"
            else:
                eta_str = f"  (~{remaining:.0f}s left)"
        self.lbl_status.configure(text=f"{status}{eta_str}")

    def _done(self, success, msg):
        self._set_encoding(False)
        self.progress.set(0)
        if success:
            self.lbl_status.configure(text="Done!")
            output_path = getattr(self, "_last_output_path", None)
            folder = os.path.dirname(output_path) if output_path else None
            # Auto-upload to Discord if enabled and connected
            if (self._discord_auto_var.get()
                    and output_path and os.path.isfile(output_path)):
                channel_id = self._get_selected_channel_id()
                if channel_id and self._discord_connected:
                    tier = self._tier_var.get()
                    ok, file_size, limit = check_file_size(output_path, tier)
                    if ok:
                        self._show_success_dialog(
                            "Success",
                            f"{msg}\n\nUploading to Discord...",
                            folder)
                        self._do_discord_upload(output_path)
                        return
                    else:
                        limit_mb = limit / (1024 * 1024)
                        size_mb = file_size / (1024 * 1024)
                        self._show_success_dialog(
                            "Saved \u2014 Discord Upload Skipped",
                            f"{msg}\n\n"
                            f"File is {size_mb:.1f} MB \u2014 exceeds your Discord "
                            f"limit of {limit_mb:.0f} MB.\n"
                            "Saved locally only. Try lower quality or shorter clip.",
                            folder)
                        return
                else:
                    self._show_success_dialog(
                        "Success",
                        f"{msg}\n\n(Discord upload skipped \u2014 "
                        "bot not connected or no channel selected)",
                        folder)
                    return
            self._show_success_dialog("Success", msg, folder)
        else:
            self.lbl_status.configure(text="Failed")
            messagebox.showerror("Export Failed", msg)

    def _run_next_multi_clip(self):
        """Encode the next clip in the multi-clip batch."""
        idx = self._multi_clip_idx
        jobs = self._multi_clip_jobs
        if idx >= len(jobs):
            # All done
            self._set_encoding(False)
            self.progress.set(0)
            n = len(self._multi_clip_outputs)
            self.lbl_status.configure(text=f"Done — {n} clips exported!")
            folder = os.path.dirname(self._multi_clip_outputs[0]) if self._multi_clip_outputs else None
            self._show_success_dialog(
                "Multi-Clip Complete",
                f"Successfully exported {n} clip(s):\n\n" +
                "\n".join(os.path.basename(p) for p in self._multi_clip_outputs),
                folder)
            return
        start, end, out_path = jobs[idx]
        total = len(jobs)
        self.processor.cut_and_encode(
            input_path=self.video_path, start_sec=start, end_sec=end,
            quality_preset=self.combo_q.get(), output_path=out_path,
            progress_callback=lambda p, s: self.root.after(
                0, self._multi_prog, p, s, idx + 1, total),
            done_callback=lambda ok, m: self.root.after(
                0, self._multi_clip_done, ok, m, out_path),
            use_hevc=self.has_hevc,
        )

    def _multi_prog(self, pct, status, clip_num, total):
        import time as _time
        # Scale progress: each clip gets an equal share of 0-100%
        overall = ((clip_num - 1) / total + pct / 100.0 / total) * 100
        self.progress.set(overall / 100)
        eta_str = ""
        if overall > 2:
            elapsed = _time.time() - self._encode_start_time
            remaining = elapsed / overall * (100 - overall)
            if remaining >= 60:
                eta_str = f"  (~{remaining / 60:.0f}m left)"
            else:
                eta_str = f"  (~{remaining:.0f}s left)"
        self.lbl_status.configure(text=f"Clip {clip_num}/{total}: {status}{eta_str}")

    def _multi_clip_done(self, success, msg, out_path):
        if not success:
            self._set_encoding(False)
            self.progress.set(0)
            idx = self._multi_clip_idx
            self.lbl_status.configure(text=f"Clip {idx+1} failed")
            messagebox.showerror("Export Failed", f"Clip {idx+1} failed:\n{msg}")
            return
        self._multi_clip_outputs.append(out_path)
        self._multi_clip_idx += 1
        self._run_next_multi_clip()

    def _show_success_dialog(self, title, message, folder_path=None):
        """Custom success dialog with OK and optional Open Folder button."""
        dlg = ctk.CTkToplevel(self.root)
        dlg.title(title)
        dlg.resizable(False, False)
        dlg.grab_set()

        body = ctk.CTkFrame(dlg, fg_color="transparent")
        body.pack(padx=20, pady=(18, 10), fill=tk.BOTH)

        ctk.CTkLabel(
            body, text=message, wraplength=360, justify=tk.LEFT,
            font=ctk.CTkFont("Segoe UI", 13),
        ).pack(anchor=tk.W)

        btn_row = ctk.CTkFrame(dlg, fg_color="transparent")
        btn_row.pack(padx=20, pady=(4, 16), fill=tk.X)

        ctk.CTkButton(
            btn_row, text="OK", width=90, command=dlg.destroy,
        ).pack(side=tk.RIGHT, padx=(6, 0))

        if folder_path and os.path.isdir(folder_path):
            def _open():
                if sys.platform == "win32":
                    os.startfile(folder_path)
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", folder_path])
                else:
                    subprocess.Popen(["xdg-open", folder_path])
            ctk.CTkButton(
                btn_row, text="Open Folder", width=110, command=_open,
            ).pack(side=tk.RIGHT, padx=(6, 0))

        dlg.update_idletasks()
        w, h = dlg.winfo_width(), dlg.winfo_height()
        x = self.root.winfo_x() + (self.root.winfo_width() - w) // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - h) // 2
        dlg.geometry(f"+{x}+{y}")
        dlg.focus_force()
        dlg.wait_window()

    def _set_encoding(self, active):
        self._encoding = active
        for w in (self.btn_cut,):
            w.configure(state="disabled" if active else "normal")
        if active:
            import time as _time
            self._encode_start_time = _time.time()
            self.progress.pack(fill=tk.X, pady=(0, 6), before=self._status_bar)
            self._progress_visible = True
            self.progress.set(0)
            self.lbl_status.configure(text="Exporting...")
        else:
            if self._progress_visible:
                self.progress.pack_forget()
                self._progress_visible = False

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def cleanup(self):
        self._closing = True
        if self._mpv:
            # Unobserve all properties first to prevent callbacks during shutdown
            for prop, handler in getattr(self, "_mpv_observers", []):
                try:
                    self._mpv.unobserve_property(prop, handler)
                except Exception:
                    pass
            try:
                self._mpv.pause = True
            except Exception:
                pass
            try:
                self._mpv.command("quit")
            except Exception:
                pass
            # Terminate in a thread to avoid blocking/deadlocking the mainloop
            mpv_ref = self._mpv
            self._mpv = None
            self._mpv_ok = False

            def _bg_terminate():
                try:
                    mpv_ref.terminate()
                except Exception:
                    pass

            t = threading.Thread(target=_bg_terminate, daemon=True)
            t.start()
            t.join(timeout=2)
        self.processor.cleanup()
        self.processor.cleanup()
