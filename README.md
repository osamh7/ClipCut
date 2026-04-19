# ClipCut

A fast, lightweight video clipper with built-in Discord upload. Trim clips from any video using an embedded mpv player, export with hardware-accelerated encoding, and optionally upload straight to a Discord channel.

## Features

- **Embedded video player** — mpv-based playback with audio and frame-accurate seeking
- **Visual range selection** — Drag handles on a timeline or use keyboard shortcuts to mark in/out points
- **Multi-clip export** — Define and export multiple clips from a single video in one batch
- **Hardware-accelerated encoding** — NVIDIA NVENC, Intel QSV, and AMD AMF with automatic fallback to software encoding
- **H.265/HEVC support** — Smaller files at the same quality when your hardware supports it
- **Quality presets** — Low (smallest), Medium (balanced), High (best quality)
- **Discord integration** — Connect a bot and upload clips directly to any server/channel
- **Drag-and-drop** — Drop a video file onto the window to open it
- **Auto FFmpeg setup** — Downloads FFmpeg automatically on first run if not already installed

## Requirements

- Python 3.10+
- [mpv](https://mpv.io/) / [mpv.net](https://github.com/mpvnet-player/mpv.net) (for video playback)
- FFmpeg (auto-downloaded if missing)

## Installation

```bash
pip install -r requirements.txt
```

### Dependencies

- `customtkinter` — Modern themed UI
- `python-mpv` — mpv player bindings
- `Pillow` — Image handling (fallback thumbnails)
- `requests` — Discord API and FFmpeg downloads
- `tkinterdnd2` — Drag-and-drop support

## Usage

```bash
python main.py
```

1. Open a video file (drag-and-drop or File → Open)
2. Set mark-in with **Q**, mark-out with **E**
3. Adjust quality preset and click **Export**
4. Optionally connect a Discord bot to upload clips directly

## Keyboard Shortcuts

| Key | Action |
|-----|--------|
| Q | Set mark in |
| E | Set mark out |
| C | Clear markers |
| Space | Play / Pause |
| ← / → | Seek backward / forward |
| Scroll | Zoom timeline |

## Building

Build a standalone `.exe` with PyInstaller:

```bash
pyinstaller video_cutter.spec --noconfirm
```

Output: `dist/ClipCut.exe`

## License

MIT
