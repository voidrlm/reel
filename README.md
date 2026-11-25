# Reel

A simple video and music player for GNOME (GTK 4 + libadwaita + GStreamer).

- Plays anything GStreamer can decode; uses VA-API hardware decoding (H.264, H.265, VP9, AV1) when available
- Controls fade away while a video plays; scroll for volume
- Mouse on the video: click the middle to pause/play (double-click: fullscreen); double-click the right/left third
  to skip ±10 s (each further quick click adds 10 s); hold the right side to fast-forward (2×, then 4×) and the
  left side to rewind, until you let go
- Audio tracks, embedded and external subtitles (auto-loads a matching `.srt` next to the video), speed 0.5–2×
- Remembers where you stopped in long videos
- **Generate subtitles with AI** (Subtitles menu): Whisper runs on this computer, nothing is uploaded.
  Reel picks the model from the PC's hardware (tiny / base / small / medium by cores, memory and
  AVX2/AVX-512/NEON), steps down by itself if the PC can't keep ahead of playback, transcribes the minutes
  around the playback position first, can translate to English, and caches results per video.
  The first use builds whisper.cpp for the CPU (`install-ai.sh`, installs build tools via pkexec if needed)
  and downloads the model.
- Previous/next walk through the other videos in the same folder; open several files to play them in a row
- Network streams (http, HLS, rtsp)

## Preferences (Ctrl+,)

- **General**: resume where you left off, play the next video in the folder automatically, skip length,
  hardware decoding, hide-controls delay, fit window to video, appearance (dark / system / light),
  preferred audio language, clear watch history / saved AI subtitles / AI models
- **Subtitles**: show automatically, preferred language, delay (also G / H keys), font, size, colour,
  outline / shadow / dark box, box darkness, distance from the bottom, with a live preview.
  The style applies to embedded, .srt and AI subtitles alike.

Only one AI subtitle job runs at a time, across all Reel windows: starting one stops any other, and
Whisper is killed together with Reel.

## Install

    ./install.sh            # launcher + app-grid entry
    ./install.sh --default  # also make Reel the default player for videos and music

Run `reel [file|url] [--sub-file=FILE] [--title=TITLE]`.

## Keys

Space play/pause · ←/→ 10 s (Shift 3 s, Ctrl 1 min) · ↑/↓ volume · M mute · F fullscreen ·
S subtitles (incl. AI) · A audio track · [ ] speed · N/P next/previous · 0–9 jump · Ctrl+O open · Ctrl+L open stream
