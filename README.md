# Reel

A simple video and music player for GNOME (GTK 4 + libadwaita + GStreamer).

- Plays anything GStreamer can decode; uses VA-API hardware decoding (H.264, H.265, VP9, AV1) when available
- Controls fade away while a video plays; click to pause, double-click for fullscreen, scroll for volume
- Audio tracks, embedded and external subtitles (auto-loads a matching `.srt` next to the video), speed 0.5–2×
- Remembers where you stopped in long videos
- Previous/next walk through the other videos in the same folder; open several files to play them in a row
- Network streams (http, HLS, rtsp)

## Install

    ./install.sh            # launcher + app-grid entry
    ./install.sh --default  # also make Reel the default player for videos and music

Run `reel [file|url] [--sub-file=FILE] [--title=TITLE]`.

## Keys

Space play/pause · ←/→ 10 s (Shift 3 s, Ctrl 1 min) · ↑/↓ volume · M mute · F fullscreen ·
S subtitles · A audio track · [ ] speed · N/P next/previous · 0–9 jump · Ctrl+O open · Ctrl+L open stream
