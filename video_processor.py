"""
Video processing backend — all ffprobe/ffmpeg operations.
"""

import io
import json
import os
import re
import subprocess
import tempfile
import threading


class VideoProcessor:
    """Wraps ffmpeg/ffprobe calls for probing, thumbnail generation, cutting, and encoding."""

    # Quality presets: (quality_value, audio_bitrate)
    # quality_value meaning depends on encoder:
    #   - Software (libx265/libx264): used as CRF value
    #   - NVENC: mapped to -cq value (lower = better)
    #   - QSV: mapped to -global_quality value
    #   - AMF: mapped to -qp_i / -qp_p value
    QUALITY_PRESETS = {
        "Low (smallest file)":    (32, "96k"),
        "Medium (balanced)":      (26, "128k"),
        "High (best quality)":    (20, "192k"),
    }

    # Encoder preference order (fastest first)
    _HW_ENCODERS_HEVC = ["hevc_nvenc", "hevc_qsv", "hevc_amf"]
    _HW_ENCODERS_H264 = ["h264_nvenc", "h264_qsv", "h264_amf"]

    def __init__(self, ffmpeg_path: str, ffprobe_path: str):
        self.ffmpeg = ffmpeg_path
        self.ffprobe = ffprobe_path
        self._thumb_dir = tempfile.mkdtemp(prefix="vidcutter_thumbs_")
        self._thumb_cache: dict[str, str] = {}
        self._encoder_cache: dict[str, str | None] = {}

    # ------------------------------------------------------------------
    # Encoder detection
    # ------------------------------------------------------------------

    def _get_available_encoders(self) -> set[str]:
        """Return set of encoder names available in this ffmpeg build."""
        if not hasattr(self, "_available_encoders"):
            try:
                r = subprocess.run(
                    [self.ffmpeg, "-hide_banner", "-encoders"],
                    capture_output=True, text=True, timeout=10,
                    creationflags=self._no_window_flag(),
                )
                self._available_encoders = set()
                for line in r.stdout.splitlines():
                    parts = line.strip().split()
                    if len(parts) >= 2 and parts[0].startswith("V"):
                        self._available_encoders.add(parts[1])
            except Exception:
                self._available_encoders = set()
        return self._available_encoders

    def _pick_best_encoder(self, use_hevc: bool) -> tuple[str, str]:
        """
        Return (encoder_name, encoder_type) where encoder_type is
        'nvenc', 'qsv', 'amf', or 'sw' (software).
        Tries hardware encoders first, falls back to software.
        """
        available = self._get_available_encoders()
        hw_list = self._HW_ENCODERS_HEVC if use_hevc else self._HW_ENCODERS_H264

        for enc in hw_list:
            if enc in available:
                kind = enc.rsplit("_", 1)[-1]  # nvenc, qsv, amf
                return enc, kind

        # Software fallback
        sw = "libx265" if use_hevc else "libx264"
        if sw in available:
            return sw, "sw"
        return "libx264", "sw"

    def _build_encode_args(
        self, encoder: str, kind: str, quality: int, use_hevc: bool,
    ) -> list[str]:
        """Build encoder-specific quality arguments."""
        args = ["-c:v", encoder]

        if kind == "nvenc":
            # NVENC: use -cq (constant quality) mode, -preset p4 is fast+good
            args += ["-rc", "constqp", "-qp", str(quality), "-preset", "p4"]
        elif kind == "qsv":
            args += ["-global_quality", str(quality), "-preset", "fast"]
        elif kind == "amf":
            args += ["-rc", "cqp", "-qp_i", str(quality), "-qp_p", str(quality),
                     "-quality", "speed"]
        else:
            # Software fallback — use ultrafast preset to keep it quick
            args += ["-crf", str(quality), "-preset", "ultrafast"]

        if use_hevc:
            args += ["-tag:v", "hvc1"]

        return args

    # ------------------------------------------------------------------
    # Probe
    # ------------------------------------------------------------------

    def probe_video(self, path: str) -> dict:
        """
        Return metadata dict:
            duration, width, height, codec, fps, file_size_bytes
        """
        cmd = [
            self.ffprobe,
            "-v", "error",
            "-show_entries", "format=duration,size",
            "-show_entries", "stream=width,height,codec_name,r_frame_rate",
            "-select_streams", "v:0",
            "-of", "json",
            path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            raise RuntimeError(f"ffprobe failed: {result.stderr.strip()}")

        data = json.loads(result.stdout)

        fmt = data.get("format", {})
        streams = data.get("streams", [{}])
        vs = streams[0] if streams else {}

        # Parse frame rate fraction like "30000/1001"
        fps_str = vs.get("r_frame_rate", "30/1")
        try:
            num, den = fps_str.split("/")
            fps = round(float(num) / float(den), 2)
        except Exception:
            fps = 30.0

        return {
            "duration": float(fmt.get("duration", 0)),
            "width": int(vs.get("width", 0)),
            "height": int(vs.get("height", 0)),
            "codec": vs.get("codec_name", "unknown"),
            "fps": fps,
            "file_size_bytes": int(fmt.get("size", 0)),
        }

    # ------------------------------------------------------------------
    # Thumbnail
    # ------------------------------------------------------------------

    def generate_thumbnail(self, path: str, timestamp_sec: float, width: int = 640) -> str:
        """
        Generate a JPEG thumbnail at *timestamp_sec* and return the file path.
        Uses keyframe-seek (-ss before -i) for speed on large files.
        Results are cached per (path, rounded timestamp).
        """
        key = f"{path}|{timestamp_sec:.1f}"
        if key in self._thumb_cache and os.path.exists(self._thumb_cache[key]):
            return self._thumb_cache[key]

        out_path = os.path.join(
            self._thumb_dir, f"thumb_{abs(hash(key)) % 10**10}.jpg"
        )

        ts = self._format_time(timestamp_sec)
        cmd = [
            self.ffmpeg,
            "-ss", ts,
            "-i", path,
            "-vf", f"scale={width}:-1",
            "-frames:v", "1",
            "-q:v", "2",
            "-y",
            out_path,
        ]
        subprocess.run(
            cmd, capture_output=True, timeout=15,
            creationflags=self._no_window_flag(),
        )

        if os.path.exists(out_path):
            self._thumb_cache[key] = out_path
            return out_path
        return ""

    # ------------------------------------------------------------------
    # Cut & Encode
    # ------------------------------------------------------------------

    def cut_and_encode(
        self,
        input_path: str,
        start_sec: float,
        end_sec: float,
        quality_preset: str,
        output_path: str,
        progress_callback=None,
        done_callback=None,
        use_hevc: bool = True,
    ):
        """
        Single-pass: seek into the source file and encode the clip directly.
        Uses hardware GPU encoding (NVENC/QSV/AMF) when available for ~10s
        processing time on a 20-second clip.

        *progress_callback(percent: float, status: str)* — called from bg thread.
        *done_callback(success: bool, message: str)* — called when finished.
        """
        t = threading.Thread(
            target=self._cut_and_encode_worker,
            args=(input_path, start_sec, end_sec, quality_preset,
                  output_path, progress_callback, done_callback, use_hevc),
            daemon=True,
        )
        t.start()
        return t

    def _cut_and_encode_worker(
        self, input_path, start_sec, end_sec, quality_preset,
        output_path, progress_callback, done_callback, use_hevc,
    ):
        try:
            duration = end_sec - start_sec

            # Pick the fastest available encoder
            encoder, kind = self._pick_best_encoder(use_hevc)
            kind_label = {
                "nvenc": "NVIDIA NVENC",
                "qsv": "Intel QuickSync",
                "amf": "AMD AMF",
                "sw": "Software",
            }.get(kind, kind)

            if progress_callback:
                progress_callback(0, f"Encoding with {kind_label} ({encoder})...")

            quality, audio_br = self.QUALITY_PRESETS.get(
                quality_preset, (26, "128k")
            )

            # Build command: single-pass seek → encode
            # -ss before -i for fast keyframe seek on large files
            cmd = [
                self.ffmpeg,
                "-ss", self._format_time(start_sec),
                "-i", input_path,
                "-t", str(duration),
            ]

            # Add encoder-specific video args
            cmd += self._build_encode_args(encoder, kind, quality, use_hevc)

            # Audio encoding
            cmd += ["-c:a", "aac", "-b:a", audio_br]

            # Progress tracking + output
            cmd += [
                "-fflags", "+genpts",
                "-progress", "pipe:1",
                "-y",
                output_path,
            ]

            # Drain stderr in a separate thread to prevent pipe deadlock.
            # Without this, ffmpeg blocks once the stderr buffer fills (~4-64KB),
            # which stalls stdout too, freezing our progress reader.
            stderr_lines: list[str] = []

            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                creationflags=self._no_window_flag(),
            )

            def _drain_stderr():
                for line in proc.stderr:
                    stderr_lines.append(line)

            stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
            stderr_thread.start()

            # Parse progress from stdout
            total_us = duration * 1_000_000
            for line in proc.stdout:
                line = line.strip()
                if line.startswith("out_time_us="):
                    try:
                        current_us = int(line.split("=")[1])
                        if total_us > 0:
                            pct = min(current_us / total_us * 100, 99)
                            if progress_callback:
                                progress_callback(pct, f"Encoding ({kind_label})...")
                    except ValueError:
                        pass

            proc.wait(timeout=300)
            stderr_thread.join(timeout=5)

            if proc.returncode != 0:
                err_text = "".join(stderr_lines[-20:])
                raise RuntimeError(f"Encoding failed: {err_text[-500:]}")

            if progress_callback:
                progress_callback(100, "Done!")

            # Report result
            out_size = os.path.getsize(output_path) if os.path.exists(output_path) else 0
            msg = (
                f"Saved to: {output_path}\n"
                f"Output size: {self._human_size(out_size)}\n"
                f"Encoder: {kind_label}"
            )

            if done_callback:
                done_callback(True, msg)

        except Exception as e:
            if progress_callback:
                progress_callback(0, f"Error: {e}")
            if done_callback:
                done_callback(False, str(e))

    # ------------------------------------------------------------------
    # Keyframe thumbnail extraction (for scrub cache)
    # ------------------------------------------------------------------

    def extract_keyframes(self, path, width=512, cancel_event=None,
                          on_frame=None):
        """Extract keyframe-only thumbnails for scrub preview.

        Runs FFmpeg with -skip_frame nokey so only I-frames are decoded —
        extremely fast even on 20 GB files.  Each frame is returned as
        (timestamp_seconds, jpeg_bytes) via the *on_frame* callback.

        Call from a background thread.  Set *cancel_event* to abort early.
        """
        from PIL import Image as _PILImage

        cmd = [
            self.ffmpeg,
            "-skip_frame", "nokey",
            "-flags2", "+export_mvs",
            "-i", path,
            "-vf", f"scale={width}:-2,showinfo",
            "-vsync", "vfr",
            "-f", "image2pipe", "-c:v", "bmp",
            "pipe:1",
        ]

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=self._no_window_flag(),
        )

        # Drain stderr in a thread; parse pts_time from showinfo lines
        timestamps: list[float] = []

        def _drain():
            for raw in proc.stderr:
                line = raw.decode("utf-8", errors="replace")
                m = re.search(r"pts_time:([\d.]+)", line)
                if m:
                    timestamps.append(float(m.group(1)))

        stderr_t = threading.Thread(target=_drain, daemon=True)
        stderr_t.start()

        frame_idx = 0
        try:
            while True:
                if cancel_event and cancel_event.is_set():
                    proc.kill()
                    break

                # BMP file header is 14 bytes; bytes 2-5 = file size (LE u32)
                header = proc.stdout.read(14)
                if len(header) < 14:
                    break
                if header[:2] != b"BM":
                    break

                file_size = int.from_bytes(header[2:6], "little")
                rest = proc.stdout.read(file_size - 14)
                if len(rest) < file_size - 14:
                    break

                # Convert BMP → compact JPEG bytes kept in RAM
                img = _PILImage.open(io.BytesIO(header + rest))
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=88)
                jpeg = buf.getvalue()

                # Wait for the matching timestamp from stderr
                for _ in range(200):          # up to 1 s
                    if frame_idx < len(timestamps):
                        break
                    if cancel_event and cancel_event.is_set():
                        break
                    import time; time.sleep(0.005)

                ts = (timestamps[frame_idx]
                      if frame_idx < len(timestamps)
                      else frame_idx * 2.0)

                if on_frame:
                    on_frame(ts, jpeg)

                frame_idx += 1
        finally:
            proc.kill()
            proc.wait()
            stderr_t.join(timeout=2)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _format_time(seconds: float) -> str:
        """Convert seconds to HH:MM:SS.mmm string."""
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = seconds % 60
        return f"{h:02d}:{m:02d}:{s:06.3f}"

    @staticmethod
    def _human_size(nbytes: int) -> str:
        for unit in ("B", "KB", "MB", "GB"):
            if nbytes < 1024:
                return f"{nbytes:.1f} {unit}"
            nbytes /= 1024
        return f"{nbytes:.1f} TB"

    @staticmethod
    def _no_window_flag() -> int:
        """On Windows, prevent ffmpeg from spawning a console window."""
        import platform
        if platform.system() == "Windows":
            return 0x08000000  # CREATE_NO_WINDOW
        return 0

    def cleanup(self):
        """Remove temp thumbnail directory."""
        import shutil
        try:
            shutil.rmtree(self._thumb_dir, ignore_errors=True)
        except Exception:
            pass
