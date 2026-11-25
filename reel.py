"""Reel: a simple video and music player (GTK 4 + libadwaita + GStreamer)."""
import bisect
import json
import os
import re
import sys
import threading
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gst, Gtk, Pango  # noqa: E402

import ai  # noqa: E402
import prefs  # noqa: E402

try:
    gi.require_version("GstPbutils", "1.0")
    from gi.repository import GstPbutils  # noqa: E402
except (ValueError, ImportError):
    GstPbutils = None
try:
    gi.require_version("GstTag", "1.0")
    from gi.repository import GstTag  # noqa: E402
except (ValueError, ImportError):
    GstTag = None

APP_ID = os.environ.get("REEL_APP_ID", "io.github.voidrlm.Reel")
CONFIG_DIR = os.environ.get("REEL_CONFIG_DIR", os.path.join(GLib.get_user_config_dir(), "reel"))
VERSION = "1.0"

VIDEO_EXT = {".mp4", ".m4v", ".mkv", ".webm", ".avi", ".mov", ".wmv", ".flv", ".mpg", ".mpeg",
             ".ts", ".m2ts", ".mts", ".3gp", ".ogv", ".vob", ".divx", ".asf", ".rm", ".rmvb"}
AUDIO_EXT = {".mp3", ".flac", ".ogg", ".oga", ".opus", ".m4a", ".aac", ".wav", ".wma", ".ape",
             ".aiff", ".aif", ".mka", ".alac", ".ac3", ".dts"}
MEDIA_EXT = VIDEO_EXT | AUDIO_EXT
SUB_EXT = {".srt", ".ass", ".ssa", ".vtt", ".sub", ".smi"}
SPEEDS = ["0.5", "0.75", "1.0", "1.25", "1.5", "1.75", "2.0"]
PLAY_FLAG_TEXT = 1 << 2
AI_TRACK = 1000  # subtitle menu value for the AI subtitles
TAP_GAP_MS = 280  # max gap between clicks of a double/triple click
HOLD_MS = 400  # press this long on the left/right of the video to rewind/fast-forward

CSS = b"""
.reel-stage { background: black; }
.reel-stage statuspage { background: black; color: white; }
headerbar.reel-osd {
  background: linear-gradient(to bottom, alpha(black, 0.75), alpha(black, 0));
  color: white; box-shadow: none;
}
.reel-controls { padding: 18px 12px 10px 12px; }
.reel-controls.reel-osd {
  background: linear-gradient(to top, alpha(black, 0.8), alpha(black, 0));
  color: white;
}
.reel-time { font-feature-settings: "tnum"; font-size: 0.92em; min-width: 3.6em; }
.reel-aisub { border-radius: 8px; padding: 2px 12px; }
.reel-preview { background: linear-gradient(135deg, #3a4a5c, #1d2630 60%, #2b2220); }
.reel-swatch { min-width: 26px; min-height: 26px; padding: 0; }
.reel-flash {
  background: alpha(black, 0.6); color: white; border-radius: 14px;
  padding: 10px 20px; font-weight: bold; font-size: 1.25em;
}
"""


def fmt_time(sec):
    sec = max(0, int(sec))
    h, rest = divmod(sec, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def natural_key(name):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def ext_of(uri_or_path):
    if uri_or_path.startswith("file://"):
        path = GLib.filename_from_uri(uri_or_path)[0]
    elif "://" in uri_or_path:
        path = GLib.uri_unescape_string(uri_or_path.split("?")[0].split("#")[0], None) or uri_or_path
    else:
        path = uri_or_path
    return os.path.splitext(path)[1].lower()


def display_name(uri):
    if uri.startswith("file://"):
        return os.path.basename(GLib.filename_from_uri(uri)[0])
    name = GLib.uri_unescape_string(uri.split("?")[0].rstrip("/").rsplit("/", 1)[-1], None)
    return name or uri


class Config:
    def __init__(self):
        self.path = os.path.join(CONFIG_DIR, "config.json")
        self.data = {"volume": 80, "muted": False, "positions": {}, **prefs.DEFAULTS}
        try:
            with open(self.path) as f:
                self.data.update(json.load(f))
        except (OSError, ValueError):
            pass

    def save(self):
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.data, f)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def remember(self, uri, pos, dur):
        positions = self.data["positions"]
        if dur > 120 and 15 < pos < dur - 25:
            positions[uri] = [round(pos, 1), time.time()]
            if len(positions) > 300:
                for old in sorted(positions, key=lambda k: positions[k][1])[:50]:
                    del positions[old]
        else:
            positions.pop(uri, None)

    def resume_point(self, uri):
        entry = self.data["positions"].get(uri)
        return entry[0] if entry else 0


class Player:
    """playbin with a GTK paintable sink."""

    def __init__(self, on_message):
        self.pipe = Gst.ElementFactory.make("playbin", "reel")
        sink = Gst.ElementFactory.make("gtk4paintablesink", "videosink")
        if self.pipe is None or sink is None:
            raise RuntimeError("GStreamer's GTK 4 sink is missing. Install gstreamer1-plugin-gtk4.")
        self.paintable = sink.props.paintable
        video_sink = sink
        if self.paintable.props.gl_context is not None:
            glbin = Gst.ElementFactory.make("glsinkbin", "glsink")
            if glbin is not None:
                glbin.props.sink = sink
                video_sink = glbin
        self.pipe.props.video_sink = video_sink
        self.pipe.props.subtitle_font_desc = "Sans Semi-Bold 20"
        tempo = Gst.ElementFactory.make("scaletempo", "tempo")
        if tempo is not None:  # keeps voices at normal pitch when the speed changes
            self.pipe.props.audio_filter = tempo
        self.rate = 1.0
        bus = self.pipe.get_bus()
        bus.add_signal_watch()
        bus.connect("message", on_message)

    def set_state(self, state):
        return self.pipe.set_state(state)

    def position(self):
        ok, pos = self.pipe.query_position(Gst.Format.TIME)
        return pos / Gst.SECOND if ok and pos >= 0 else None

    def duration(self):
        ok, dur = self.pipe.query_duration(Gst.Format.TIME)
        return dur / Gst.SECOND if ok and dur > 0 else None

    def seek(self, seconds, accurate=True, before=False):
        flags = Gst.SeekFlags.FLUSH
        if accurate:
            flags |= Gst.SeekFlags.ACCURATE
        else:  # jump to a keyframe: fast, and "before" keeps rewinding from snapping forward again
            flags |= Gst.SeekFlags.KEY_UNIT | (Gst.SeekFlags.SNAP_BEFORE if before else Gst.SeekFlags.SNAP_NEAREST)
        return self.pipe.seek(self.rate, Gst.Format.TIME, flags, Gst.SeekType.SET,
                              int(max(0, seconds) * Gst.SECOND), Gst.SeekType.NONE, -1)

    def set_rate(self, rate):
        pos = self.position()
        self.rate = rate
        if pos is not None:
            self.seek(pos)

    def set_volume(self, percent, muted):
        self.pipe.props.volume = (max(0, min(100, percent)) / 100) ** 3  # perceptual curve
        self.pipe.props.mute = muted

    def subtitles_on(self):
        return bool(self.pipe.props.flags & PLAY_FLAG_TEXT)

    def set_subtitle(self, index):
        flags = self.pipe.props.flags
        if index < 0:
            self.pipe.props.flags = flags & ~PLAY_FLAG_TEXT
        else:
            self.pipe.props.flags = flags | PLAY_FLAG_TEXT
            self.pipe.props.current_text = index

    def track_name(self, kind, index):
        tags = self.pipe.emit(f"get-{kind}-tags", index)
        parts = []
        if tags is not None:
            ok, lang = tags.get_string(Gst.TAG_LANGUAGE_NAME)
            if not ok:
                ok, code = tags.get_string(Gst.TAG_LANGUAGE_CODE)
                lang = None
                if ok and code and code not in ("und", "unknown"):
                    lang = (GstTag.tag_get_language_name(code) if GstTag else None) or code
            if lang:
                parts.append(lang)
            ok, title = tags.get_string(Gst.TAG_TITLE)
            if ok and title and title != lang:
                parts.append(title)
        return " · ".join(parts) if parts else f"Track {index + 1}"


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Reel")
        self.set_default_size(960, 540)
        self.set_size_request(360, 240)
        self.cfg = app.cfg
        self.player = Player(self._on_bus)
        self.player.pipe.connect("audio-changed", lambda *_: GLib.idle_add(self._rebuild_tracks))
        self.player.pipe.connect("text-changed", lambda *_: GLib.idle_add(self._rebuild_tracks))

        self.queue, self.index, self.autoadvance = [], -1, False
        self.uri = None
        self.title_override = None
        self.want_play = False
        self.playing = False
        self.ended = False
        self.has_video = False
        self.prerolled = False
        self.pending_seek = None
        self.pending_subtitle = None
        self.buffering = False
        self.fitted_for = None
        self.ext_sub_name = None
        self._hide_id = self._flash_id = self._click_id = self._seek_id = 0
        self._hold_id = self._hold_tick_id = 0
        self._holding = None
        self._press_zone = None
        self._tap_count = self._tap_total = 0
        self._last_release = 0.0
        self._hold_started = self._rewind_pos = 0.0
        self._hold_rate = 1.0
        self._hold_was_playing = False
        self.ai_job = None
        self.ai_installer = None
        self.ai_chunks = {}
        self.ai_cues, self.ai_starts = [], []
        self.ai_show = False
        self.ai_label = None  # e.g. "AI · English"
        self.ai_translate = False
        self.ai_status = None
        self._sub_px = 0
        self._inhibit = 0
        self._last_user_seek = 0.0
        self._last_motion = None
        self._pointer_in_bars = False
        self._scroll_acc = 0.0

        self._build_ui()
        self._install_actions()
        self._prefs = None
        self._overlays = []  # textoverlay elements that draw embedded / .srt subtitles
        self._sub_css = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), self._sub_css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
        self.player.pipe.connect("deep-element-added", self._on_element_added)
        self.apply_sub_style()
        self.player.set_volume(self.cfg.data["volume"], self.cfg.data["muted"])
        self._sync_volume_ui()
        self._set_mode(False)
        GLib.timeout_add(250, self._tick)
        GLib.timeout_add(100, self._ai_tick)
        self.connect("close-request", self._on_close)
        self.connect("notify::fullscreened", self._on_fullscreen_changed)

    # ---- UI -------------------------------------------------------------------------

    def _build_ui(self):
        self.toasts = Adw.ToastOverlay()
        self.view = Adw.ToolbarView()
        self.toasts.set_child(self.view)
        self.set_content(self.toasts)

        # header
        self.header = Adw.HeaderBar()
        self.wtitle = Adw.WindowTitle(title="Reel")
        self.header.set_title_widget(self.wtitle)
        open_btn = Gtk.Button(icon_name="document-open-symbolic", tooltip_text="Open File (Ctrl+O)",
                              action_name="win.open", focusable=False)
        self.header.pack_start(open_btn)
        menu = Gio.Menu()
        menu.append("Open File…", "win.open")
        menu.append("Open Network Stream…", "win.open-url")
        more = Gio.Menu()
        more.append("Preferences", "win.preferences")
        more.append("Keyboard Shortcuts", "win.shortcuts")
        more.append("About Reel", "win.about")
        menu.append_section(None, more)
        self.menu_btn = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu,
                                       tooltip_text="Main Menu", focusable=False)
        self.header.pack_end(self.menu_btn)
        self.view.add_top_bar(self.header)

        # empty state
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        empty = Adw.StatusPage(icon_name=APP_ID, title="Reel",
                               description="Open a video or song, or drop it here")
        empty_btn = Gtk.Button(label="Open File…", action_name="win.open", halign=Gtk.Align.CENTER)
        empty_btn.add_css_class("pill")
        empty_btn.add_css_class("suggested-action")
        empty.set_child(empty_btn)
        self.stack.add_named(empty, "empty")

        # player stage
        stage = Gtk.Overlay()
        stage.add_css_class("reel-stage")
        self.picture = Gtk.Picture(paintable=self.player.paintable, content_fit=Gtk.ContentFit.CONTAIN,
                                   hexpand=True, vexpand=True)
        video = self.picture
        if hasattr(Gtk, "GraphicsOffload"):  # lets the compositor scan out video frames directly
            video = Gtk.GraphicsOffload(child=self.picture)
            if hasattr(video, "set_black_background"):
                video.set_black_background(True)
        self.media_stack = Gtk.Stack()
        self.media_stack.add_named(video, "video")
        self.audio_page = Adw.StatusPage(icon_name="audio-x-generic-symbolic")
        self.media_stack.add_named(self.audio_page, "audio")
        stage.set_child(self.media_stack)
        self.flash = Gtk.Label(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER, visible=False,
                               can_target=False)
        self.flash.add_css_class("reel-flash")
        stage.add_overlay(self.flash)
        self.ai_sub = Gtk.Label(halign=Gtk.Align.CENTER, valign=Gtk.Align.END, visible=False, can_target=False,
                                wrap=True, justify=Gtk.Justification.CENTER, max_width_chars=48)
        self.ai_sub.add_css_class("reel-aisub")
        stage.add_overlay(self.ai_sub)
        self.spinner = Adw.Spinner(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER, visible=False,
                                   width_request=48, height_request=48, can_target=False)
        stage.add_overlay(self.spinner)
        self.stage = stage
        self.stack.add_named(stage, "player")
        self.view.set_content(self.stack)

        # press/release on the video drives tap zones and hold-to-seek (see _on_press)
        press = Gtk.GestureDrag(button=Gdk.BUTTON_PRIMARY)
        press.connect("drag-begin", self._on_press)
        press.connect("drag-end", self._on_release)
        press.connect("cancel", self._on_release)
        stage.add_controller(press)
        scroll = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.connect("scroll", self._on_scroll)
        stage.add_controller(scroll)

        # bottom controls
        bar = Gtk.Box(spacing=6)
        bar.add_css_class("reel-controls")

        def button(icon, tip, cb):
            b = Gtk.Button(icon_name=icon, tooltip_text=tip, focusable=False, valign=Gtk.Align.CENTER)
            b.add_css_class("flat")
            b.connect("clicked", cb)
            return b

        self.prev_btn = button("media-skip-backward-symbolic", "Previous (P)", lambda *_: self.step_queue(-1))
        self.play_btn = button("media-playback-start-symbolic", "Play/Pause (Space)", lambda *_: self.toggle())
        self.play_btn.add_css_class("circular")
        self.next_btn = button("media-skip-forward-symbolic", "Next (N)", lambda *_: self.step_queue(1))
        self.pos_label = Gtk.Label(label="0:00", xalign=1)
        self.pos_label.add_css_class("reel-time")
        self.seekbar = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1, 1)
        self.seekbar.set_draw_value(False)
        self.seekbar.set_hexpand(True)
        self.seekbar.set_focusable(False)
        self.seekbar.set_valign(Gtk.Align.CENTER)
        self.seekbar.connect("change-value", self._on_seekbar)
        self.dur_label = Gtk.Label(label="0:00", xalign=0)
        self.dur_label.add_css_class("reel-time")
        self.mute_btn = button("audio-volume-high-symbolic", "Mute (M)", lambda *_: self.toggle_mute())
        self.volbar = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.volbar.set_draw_value(False)
        self.volbar.set_size_request(90, -1)
        self.volbar.set_focusable(False)
        self.volbar.set_valign(Gtk.Align.CENTER)
        self._vol_handler = self.volbar.connect("value-changed", self._on_volbar)
        self.tracks_btn = Gtk.MenuButton(icon_name="media-view-subtitles-symbolic", focusable=False,
                                         tooltip_text="Audio, Subtitles & Speed", valign=Gtk.Align.CENTER)
        self.tracks_btn.add_css_class("flat")
        self.full_btn = button("view-fullscreen-symbolic", "Fullscreen (F)", lambda *_: self.toggle_fullscreen())
        for w in (self.prev_btn, self.play_btn, self.next_btn, self.pos_label, self.seekbar, self.dur_label,
                  self.mute_btn, self.volbar, self.tracks_btn, self.full_btn):
            bar.append(w)
        self.controls = bar
        self.view.add_bottom_bar(bar)

        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        motion.connect("leave", lambda *_: setattr(self, "_pointer_in_bars", False))
        self.add_controller(motion)

        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)

        drop = Gtk.DropTarget(actions=Gdk.DragAction.COPY)
        drop.set_gtypes([Gdk.FileList])
        drop.connect("drop", self._on_drop)
        self.add_controller(drop)

    def _install_actions(self):
        def add(name, cb):
            a = Gio.SimpleAction(name=name)
            a.connect("activate", lambda *_: cb())
            self.add_action(a)

        add("open", self.choose_files)
        add("open-url", self.choose_url)
        add("load-subtitle", self.choose_subtitle)
        add("shortcuts", self.show_shortcuts)
        add("about", self.show_about)
        add("ai-generate", self.choose_ai)
        add("preferences", self.show_preferences)
        add("sub-style", lambda: self.show_preferences("subtitles"))
        add("ai-stop", self.stop_ai)

        self.audio_action = Gio.SimpleAction.new_stateful("audio-track", GLib.VariantType("i"), GLib.Variant("i", 0))
        self.audio_action.connect("change-state", self._on_audio_action)
        self.add_action(self.audio_action)
        self.sub_action = Gio.SimpleAction.new_stateful("subtitle", GLib.VariantType("i"), GLib.Variant("i", -1))
        self.sub_action.connect("change-state", self._on_sub_action)
        self.add_action(self.sub_action)
        self.speed_action = Gio.SimpleAction.new_stateful("speed", GLib.VariantType("s"), GLib.Variant("s", "1.0"))
        self.speed_action.connect("change-state", lambda a, v: self.set_speed(float(v.get_string())))
        self.add_action(self.speed_action)
        self._rebuild_tracks()

    def _set_mode(self, player):
        self.stack.set_visible_child_name("player" if player else "empty")
        self.view.set_extend_content_to_top_edge(player)
        self.view.set_extend_content_to_bottom_edge(player)
        for w in (self.header, self.controls):
            if player:
                w.add_css_class("reel-osd")
            else:
                w.remove_css_class("reel-osd")
        self.view.set_reveal_bottom_bars(player)
        self.view.set_reveal_top_bars(True)
        if not player:
            self.wtitle.set_title("Reel")
            self.wtitle.set_subtitle("")

    # ---- auto-hiding controls ----------------------------------------------------

    def _show_chrome(self, show):
        if self.stack.get_visible_child_name() != "player":
            return
        self.view.set_reveal_top_bars(show)
        self.view.set_reveal_bottom_bars(show)
        self.stage.set_cursor(None if show else Gdk.Cursor.new_from_name("none"))

    def _poke(self):
        self._show_chrome(True)
        if self._hide_id:
            GLib.source_remove(self._hide_id)
        self._hide_id = GLib.timeout_add(int(self.cfg.data['hide_after'] * 1000), self._auto_hide)

    def _auto_hide(self):
        self._hide_id = 0
        menus_open = self.tracks_btn.get_active() or self.menu_btn.get_active()
        if self.playing and self.has_video and not self._pointer_in_bars and not menus_open:
            self._show_chrome(False)
        elif self.playing:
            self._hide_id = GLib.timeout_add(int(self.cfg.data['hide_after'] * 1000), self._auto_hide)
        return GLib.SOURCE_REMOVE

    def _on_motion(self, _ctl, x, y):
        if self._last_motion and abs(self._last_motion[0] - x) < 1 and abs(self._last_motion[1] - y) < 1:
            return
        self._last_motion = (x, y)
        top = self.view.get_top_bar_height()
        bottom = self.view.get_bottom_bar_height()
        self._pointer_in_bars = y < top or y > self.get_height() - bottom
        self._poke()

    # ---- opening ----------------------------------------------------------------------

    def open_files(self, files, subtitle=None, title=None):
        try:
            self._open_files(files, subtitle, title)
        except Exception as e:  # never leave the user on a blank window without a reason
            import traceback
            traceback.print_exc()
            self.toast(f"Couldn't open this: {e}")

    def _open_files(self, files, subtitle, title):
        uris = [f.get_uri() for f in files]
        subs = [u for u in uris if ext_of(u) in SUB_EXT]
        media = [u for u in uris if ext_of(u) not in SUB_EXT]
        if not media:
            if subs and self.uri:
                self.load_subtitle(subs[0])
            return
        if subtitle is None and subs:
            subtitle = subs[0]
        if len(media) == 1 and media[0].startswith("file://"):
            path = GLib.filename_from_uri(media[0])[0]
            folder = os.path.dirname(path)
            try:
                names = sorted((n for n in os.listdir(folder) if os.path.splitext(n)[1].lower() in MEDIA_EXT),
                               key=natural_key)
            except OSError:
                names = []
            siblings = [Gio.File.new_for_path(os.path.join(folder, n)).get_uri() for n in names]
            if media[0] not in siblings:
                siblings = [media[0]]
            self.queue, self.autoadvance = siblings, bool(self.cfg.data['autoplay_next'])
            index = siblings.index(media[0])
        else:
            self.queue, self.autoadvance = media, len(media) > 1
            index = 0
        self.play_index(index, subtitle=subtitle, title=title)

    def play_index(self, index, subtitle=None, title=None):
        if not 0 <= index < len(self.queue):
            return
        self.index = index
        self.load(self.queue[index], subtitle=subtitle, title=title)

    def step_queue(self, delta):
        if 0 <= self.index + delta < len(self.queue):
            self.play_index(self.index + delta)

    def _find_subtitle(self, uri):
        if not uri.startswith("file://"):
            return None
        path = GLib.filename_from_uri(uri)[0]
        folder, base = os.path.split(path)
        stem = os.path.splitext(base)[0].lower()
        candidates = []
        for sub_folder in (folder, os.path.join(folder, "Subs"), os.path.join(folder, "subs")):
            try:
                names = os.listdir(sub_folder)
            except OSError:
                continue
            for n in names:
                if os.path.splitext(n)[1].lower() in SUB_EXT:
                    candidates.append(os.path.join(sub_folder, n))
        if not candidates:
            return None
        same = [c for c in candidates if os.path.basename(c).lower().startswith(stem)]
        english = [c for c in same + candidates if re.search(r"(^|[._ -])(en|eng|english)([._ -]|$)",
                                                              os.path.basename(c).lower())]
        videos_here = [n for n in os.listdir(folder) if os.path.splitext(n)[1].lower() in VIDEO_EXT]
        pick = (same and (english[0] if english and english[0] in same else same[0])) or \
               (len(videos_here) == 1 and (english[0] if english else candidates[0])) or None
        return Gio.File.new_for_path(pick).get_uri() if pick else None

    def load(self, uri, subtitle=None, title=None, resume=None):
        self._save_position()
        p = self.player
        p.set_state(Gst.State.NULL)
        if uri != self.uri:
            self._reset_ai()
        self.uri = uri
        self.title_override = title
        sub = subtitle or self._find_subtitle(uri)
        if sub and "://" not in sub:
            sub = Gio.File.new_for_path(sub).get_uri()
        p.pipe.props.uri = uri
        p.pipe.props.suburi = sub
        self.ext_sub_name = display_name(sub) if sub else None
        self.pending_subtitle = ("chosen" if subtitle else "found") if sub else None
        p.pipe.props.flags = p.pipe.props.flags | PLAY_FLAG_TEXT
        p.rate = 1.0
        self.speed_action.set_state(GLib.Variant("s", "1.0"))
        self.prerolled = False
        self.ended = False
        self.has_video = False
        start = (self.cfg.resume_point(uri) if self.cfg.data['resume'] else 0) if resume is None else resume
        self.pending_seek = (start, resume is None) if start else None
        self.want_play = True
        self.seekbar.set_range(0, 1)
        self.seekbar.set_value(0)
        self.pos_label.set_label("0:00")
        self.dur_label.set_label("0:00")
        self.wtitle.set_title(title or display_name(uri))
        self._update_subtitle_line()
        self.prev_btn.set_visible(len(self.queue) > 1)
        self.next_btn.set_visible(len(self.queue) > 1)
        self.media_stack.set_visible_child_name("video")
        self._set_mode(True)
        self.spinner.set_visible(True)
        if p.set_state(Gst.State.PAUSED) == Gst.StateChangeReturn.FAILURE:
            self.spinner.set_visible(False)
        self._poke()

    def load_subtitle(self, sub_uri):
        if not self.uri:
            return
        pos = self.player.position() or 0
        was_playing = self.want_play
        self.load(self.uri, subtitle=sub_uri, title=self.title_override, resume=pos)
        self.want_play = was_playing
        self.toast(f"Subtitles: {display_name(sub_uri)}")

    # ---- playback control -----------------------------------------------------------

    def play(self):
        if not self.uri:
            self.choose_files()
            return
        if self.ended:
            self.ended = False
            self.player.seek(0)
        self.want_play = True
        if not self.buffering:
            self.player.set_state(Gst.State.PLAYING)

    def pause(self):
        self.want_play = False
        self.player.set_state(Gst.State.PAUSED)
        self._save_position()

    def toggle(self):
        if self.want_play and not self.ended:
            self.pause()
            self._flash("Paused")
        else:
            self.play()

    def seek_by(self, delta):
        pos = self.player.position()
        dur = self.player.duration()
        if pos is None:
            return
        target = max(0, pos + delta)
        if dur:
            target = min(target, max(0, dur - 1))
        self.ended = False
        self.player.seek(target)
        self.seekbar.set_value(target)
        self._last_user_seek = time.monotonic()
        self._flash(f"{'+' if delta > 0 else '−'}{abs(int(delta))}s · {fmt_time(target)}")

    def _on_seekbar(self, _scale, _scroll, value):
        dur = self.player.duration() or 0
        value = max(0, min(value, dur))
        self._last_user_seek = time.monotonic()
        self.pos_label.set_label(fmt_time(value))
        self.ended = False
        # quick keyframe seeks while dragging, then one exact seek when the slider settles
        self.player.seek(value, accurate=False)
        if self._seek_id:
            GLib.source_remove(self._seek_id)
        self._seek_id = GLib.timeout_add(200, self._settle_seek, value)
        return False

    def _settle_seek(self, value):
        self._seek_id = 0
        self.player.seek(value)
        return GLib.SOURCE_REMOVE

    def set_speed(self, rate):
        rate = max(0.25, min(4.0, rate))
        self.player.set_rate(rate)
        key = f"{rate:.2f}".rstrip("0")
        key = key + "0" if key.endswith(".") else key
        self.speed_action.set_state(GLib.Variant("s", key))
        self._flash(f"Speed {key}×")

    def change_speed(self, direction):
        values = [float(s) for s in SPEEDS]
        cur = self.player.rate
        if direction > 0:
            nxt = next((v for v in values if v > cur + 1e-6), values[-1])
        else:
            nxt = next((v for v in reversed(values) if v < cur - 1e-6), values[0])
        self.set_speed(nxt)

    def set_volume(self, percent):
        self.cfg.data["volume"] = max(0, min(100, round(percent)))
        self.cfg.data["muted"] = False
        self.player.set_volume(self.cfg.data["volume"], False)
        self._sync_volume_ui()
        self._flash(f"Volume {self.cfg.data['volume']}%")

    def toggle_mute(self):
        self.cfg.data["muted"] = not self.cfg.data["muted"]
        self.player.set_volume(self.cfg.data["volume"], self.cfg.data["muted"])
        self._sync_volume_ui()
        self._flash("Muted" if self.cfg.data["muted"] else f"Volume {self.cfg.data['volume']}%")

    def _on_volbar(self, scale):
        self.cfg.data["volume"] = round(scale.get_value())
        self.cfg.data["muted"] = False
        self.player.set_volume(self.cfg.data["volume"], False)
        self._sync_volume_ui()

    def _sync_volume_ui(self):
        vol, muted = self.cfg.data["volume"], self.cfg.data["muted"]
        if muted or vol == 0:
            icon = "audio-volume-muted-symbolic"
        elif vol < 34:
            icon = "audio-volume-low-symbolic"
        elif vol < 67:
            icon = "audio-volume-medium-symbolic"
        else:
            icon = "audio-volume-high-symbolic"
        self.mute_btn.set_icon_name(icon)
        self.volbar.handler_block(self._vol_handler)
        self.volbar.set_value(vol)
        self.volbar.handler_unblock(self._vol_handler)

    def toggle_fullscreen(self):
        if self.is_fullscreen():
            self.unfullscreen()
        else:
            self.fullscreen()

    def _on_fullscreen_changed(self, *_):
        self.full_btn.set_icon_name("view-restore-symbolic" if self.is_fullscreen() else "view-fullscreen-symbolic")

    def cycle_subtitle(self):
        choices = [-1] + list(range(self.player.pipe.props.n_text)) + ([AI_TRACK] if self.ai_label else [])
        if len(choices) == 1:
            self._flash("No subtitles")
            return
        cur = self.sub_action.get_state().get_int32()
        nxt = choices[(choices.index(cur) + 1) % len(choices)] if cur in choices else choices[0]
        self.sub_action.change_state(GLib.Variant("i", nxt))
        name = self.ai_label if nxt == AI_TRACK else self._sub_name(nxt) if nxt >= 0 else None
        self._flash(f"Subtitles: {name}" if name else "Subtitles off")

    def cycle_audio(self):
        n = self.player.pipe.props.n_audio
        if n < 2:
            self._flash("Only one audio track")
            return
        nxt = (self.player.pipe.props.current_audio + 1) % n
        self.audio_action.change_state(GLib.Variant("i", nxt))
        self._flash(f"Audio: {self.player.track_name('audio', nxt)}")

    def _on_audio_action(self, action, value):
        self.player.pipe.props.current_audio = value.get_int32()
        action.set_state(value)

    def _on_sub_action(self, action, value):
        index = value.get_int32()
        self.ai_show = index == AI_TRACK
        self.player.set_subtitle(-1 if self.ai_show else index)
        action.set_state(value)

    def _sub_name(self, i):
        n = self.player.pipe.props.n_text
        if self.ext_sub_name and i == n - 1:
            return self.ext_sub_name
        return self.player.track_name("text", i)

    def _rebuild_tracks(self):
        pipe = self.player.pipe
        menu = Gio.Menu()
        n_audio = pipe.props.n_audio
        if n_audio > 1:
            audio = Gio.Menu()
            for i in range(n_audio):
                audio.append(self.player.track_name("audio", i), f"win.audio-track({i})")
            menu.append_section("Audio", audio)
            self.audio_action.set_state(GLib.Variant("i", max(0, pipe.props.current_audio)))
        subs = Gio.Menu()
        subs.append("Off", "win.subtitle(-1)")
        for i in range(pipe.props.n_text):
            subs.append(self._sub_name(i), f"win.subtitle({i})")
        if self.ai_label:
            subs.append(self.ai_label, f"win.subtitle({AI_TRACK})")
        subs.append("Load from File…", "win.load-subtitle")
        subs.append("Style & Delay…", "win.sub-style")
        if self.ai_job or self.ai_installer:
            subs.append("Stop Generating", "win.ai-stop")
        else:
            subs.append("Generate with AI…", "win.ai-generate")
        menu.append_section("Subtitles", subs)
        if self.ai_show:
            cur = AI_TRACK
        else:
            cur = pipe.props.current_text if self.player.subtitles_on() and pipe.props.n_text else -1
        self.sub_action.set_state(GLib.Variant("i", cur))
        speed = Gio.Menu()
        for s in SPEEDS:
            speed.append("Normal" if s == "1.0" else f"{s}×", f"win.speed('{s}')")
        menu.append_submenu("Speed", speed)
        self.tracks_btn.set_menu_model(menu)
        return GLib.SOURCE_REMOVE

    # ---- GStreamer bus ----------------------------------------------------------------

    def _on_bus(self, _bus, msg):
        t = msg.type
        pipe = self.player.pipe
        if t == Gst.MessageType.STATE_CHANGED and msg.src == pipe:
            _old, new, _pending = msg.parse_state_changed()
            self.playing = new == Gst.State.PLAYING
            self.play_btn.set_icon_name("media-playback-pause-symbolic" if self.playing
                                        else "media-playback-start-symbolic")
            self._update_inhibit()
            if self.playing:
                self._poke()
            else:
                self._show_chrome(True)
        elif t == Gst.MessageType.ASYNC_DONE:
            if not self.prerolled:
                self.prerolled = True
                self._on_preroll()
            self.spinner.set_visible(self.buffering)
        elif t == Gst.MessageType.EOS:
            self._on_eos()
        elif t == Gst.MessageType.ERROR:
            err, debug = msg.parse_error()
            print(f"reel: error: {err.message}\n{debug}", file=sys.stderr)
            self.player.set_state(Gst.State.NULL)
            self.spinner.set_visible(False)
            self.want_play = False
            missing = getattr(self, "_missing", None)
            self._missing = None
            self.toast(f"Can't play this: {missing or err.message}")
        elif t == Gst.MessageType.ELEMENT and GstPbutils and GstPbutils.is_missing_plugin_message(msg):
            self._missing = "missing " + GstPbutils.missing_plugin_message_get_description(msg)
        elif t == Gst.MessageType.BUFFERING:
            percent = msg.parse_buffering()
            if percent < 100 and not self.buffering:
                self.buffering = True
                self.spinner.set_visible(True)
                if self.want_play:
                    self.player.set_state(Gst.State.PAUSED)
            elif percent >= 100 and self.buffering:
                self.buffering = False
                self.spinner.set_visible(False)
                if self.want_play:
                    self.player.set_state(Gst.State.PLAYING)
        elif t == Gst.MessageType.TAG and not self.title_override:
            tags = msg.parse_tag()
            ok, title = tags.get_string(Gst.TAG_TITLE)
            ok_a, artist = tags.get_string(Gst.TAG_ARTIST)
            if not self.has_video and self.prerolled and ok and title:
                self.audio_page.set_title(title)
                if ok_a:
                    self.audio_page.set_description(artist)

    def _on_preroll(self):
        pipe = self.player.pipe
        self.has_video = pipe.props.n_video > 0
        if not self.has_video:
            self.audio_page.set_title(self.title_override or display_name(self.uri))
            self.audio_page.set_description("")
            self.media_stack.set_visible_child_name("audio")
        self._apply_track_preferences()
        self.pending_subtitle = None
        self._restore_ai()
        self._rebuild_tracks()
        if self.pending_seek:
            start, announce = self.pending_seek
            self.pending_seek = None
            dur = self.player.duration()
            if not dur or start < dur - 5:
                self.player.seek(start)
                if announce:
                    toast = Adw.Toast(title=f"Resumed from {fmt_time(start)}", button_label="Start Over",
                                      timeout=6)
                    toast.connect("button-clicked", lambda *_: self.player.seek(0))
                    self.toasts.add_toast(toast)
        if self.want_play and not self.buffering:
            self.player.set_state(Gst.State.PLAYING)
        self._update_inhibit()

    def _on_eos(self):
        if self.uri:
            self.cfg.data["positions"].pop(self.uri, None)
        if self.autoadvance and self.index + 1 < len(self.queue):
            self.play_index(self.index + 1)
            return
        self.ended = True
        self.want_play = False
        self.player.set_state(Gst.State.PAUSED)
        self.play_btn.set_icon_name("media-playlist-repeat-symbolic")
        self._show_chrome(True)

    def _update_inhibit(self):
        app = self.get_application()
        want = self.playing and self.has_video
        if want and not self._inhibit:
            self._inhibit = app.inhibit(self, Gtk.ApplicationInhibitFlags.IDLE | Gtk.ApplicationInhibitFlags.SUSPEND,
                                        "Playing a video")
        elif not want and self._inhibit:
            app.uninhibit(self._inhibit)
            self._inhibit = 0

    def _tick(self):
        if self.uri and self.prerolled:
            dur = self.player.duration()
            pos = self.player.position()
            if dur:
                self.seekbar.set_range(0, dur)
                self.dur_label.set_label(fmt_time(dur))
            if pos is not None and time.monotonic() - self._last_user_seek > 0.6:
                self.seekbar.set_value(pos)
                self.pos_label.set_label(fmt_time(pos))
            self._fit_window()
        return GLib.SOURCE_CONTINUE

    def _fit_window(self):
        """Size the window to the video once per file, so it opens without black borders."""
        if self.fitted_for == self.uri or not self.has_video or not self.cfg.data["fit_window"]:
            return
        p = self.player.paintable
        w, h = p.get_intrinsic_width(), p.get_intrinsic_height()
        if w <= 0 or h <= 0:
            return
        self.fitted_for = self.uri
        if self.is_fullscreen() or self.is_maximized():
            return
        surface = self.get_surface()
        monitor = surface and self.get_display().get_monitor_at_surface(surface)
        if not monitor:
            return
        geo = monitor.get_geometry()
        scale = min(geo.width * 0.75 / w, geo.height * 0.75 / h)
        scale = min(scale, max(1.0, 640 / w))  # small videos are enlarged up to 640 px wide
        self.set_default_size(round(w * scale), round(h * scale))

    def _save_position(self):
        if self.uri and self.prerolled and not self.ended and self.cfg.data["resume"]:
            pos, dur = self.player.position(), self.player.duration()
            if pos is not None and dur:
                self.cfg.remember(self.uri, pos, dur)
                self.cfg.save()

    # ---- input --------------------------------------------------------------------------

    def _on_key(self, _ctl, keyval, _code, state):
        if isinstance(self.get_focus(), Gtk.Text) or self.tracks_btn.get_active() or self.menu_btn.get_active():
            return False
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        k = Gdk.keyval_name(keyval) or ""
        if ctrl and k not in ("Left", "Right"):
            return False
        has_media = self.uri is not None
        step = self.cfg.data["seek_step"]
        actions = {
            "space": self.toggle, "k": self.toggle, "AudioPlay": self.toggle, "AudioPause": self.toggle,
            "f": self.toggle_fullscreen, "F11": self.toggle_fullscreen,
            "m": self.toggle_mute, "AudioMute": self.toggle_mute,
            "n": lambda: self.step_queue(1), "AudioNext": lambda: self.step_queue(1),
            "p": lambda: self.step_queue(-1), "AudioPrev": lambda: self.step_queue(-1),
            "s": self.cycle_subtitle, "v": self.cycle_subtitle, "a": self.cycle_audio, "b": self.cycle_audio,
            "bracketleft": lambda: self.change_speed(-1), "bracketright": lambda: self.change_speed(1),
            "equal": lambda: self.set_speed(1.0), "BackSpace": lambda: self.set_speed(1.0),
            "Up": lambda: self.set_volume(self.cfg.data["volume"] + 5),
            "Down": lambda: self.set_volume(self.cfg.data["volume"] - 5),
            "AudioRaiseVolume": lambda: self.set_volume(self.cfg.data["volume"] + 5),
            "AudioLowerVolume": lambda: self.set_volume(self.cfg.data["volume"] - 5),
            "Left": lambda: self.seek_by(-60 if ctrl else -3 if shift else -step),
            "Right": lambda: self.seek_by(60 if ctrl else 3 if shift else step),
            "j": lambda: self.seek_by(-step), "l": lambda: self.seek_by(step),
            "g": lambda: self.change_sub_delay(-0.1), "h": lambda: self.change_sub_delay(0.1),
            "Home": lambda: self.player.seek(0),
        }
        if k == "Escape" and self.is_fullscreen():
            self.unfullscreen()
            return True
        if k.isdigit() and has_media:  # 0-9 jump to 0%..90%
            dur = self.player.duration()
            if dur:
                self.ended = False
                self.player.seek(dur * int(k) / 10)
                self._last_user_seek = 0
            return True
        fn = actions.get(k)
        if fn is None or (not has_media and k not in ("space", "f", "F11")):
            return False
        fn()
        self._poke()
        return True

    # Mouse on the video, split into thirds:
    #   middle: click = play/pause, double-click = fullscreen
    #   right/left: double-click = ±10 s (each further quick click adds 10 s more),
    #               hold = fast-forward (2×, then 4×) / rewind, until released

    def _zone(self, x):
        width = self.stage.get_width() or 1
        return "left" if x < width / 3 else "right" if x > width * 2 / 3 else "center"

    def _on_press(self, _gesture, x, _y):
        self._poke()
        if not self.uri:
            return
        now = time.monotonic()
        zone = self._zone(x)
        if self._click_id:
            GLib.source_remove(self._click_id)
            self._click_id = 0
        if now - self._last_release < TAP_GAP_MS / 1000 and zone == self._press_zone:
            self._tap_count += 1
        else:
            self._tap_count = 1
        self._press_zone = zone
        if self._tap_count >= 2:
            if zone != "center":
                self._tap_seek(zone)
            elif self._tap_count == 2:
                self.toggle_fullscreen()
            return
        if zone != "center":
            self._hold_id = GLib.timeout_add(HOLD_MS, self._start_hold, zone)

    def _on_release(self, *_):
        if self._hold_id:
            GLib.source_remove(self._hold_id)
            self._hold_id = 0
        if self._holding:
            self._end_hold()
            self._tap_count = 0
            self._last_release = 0
            return
        self._last_release = time.monotonic()
        if self._tap_count == 1 and self._press_zone == "center" and self.uri:
            # wait briefly: a second click means fullscreen instead
            self._click_id = GLib.timeout_add(TAP_GAP_MS, self._single_click)

    def _single_click(self):
        self._click_id = 0
        self.toggle()
        return GLib.SOURCE_REMOVE

    def _tap_seek(self, zone):
        sign = 1 if zone == "right" else -1
        step = self.cfg.data["seek_step"]
        self._tap_total = step if self._tap_count == 2 else self._tap_total + step
        self.seek_by(step * sign)
        self._flash(f"{self._tap_total} s  ▶▶" if sign > 0 else f"◀◀  {self._tap_total} s", side=zone)

    def _start_hold(self, zone):
        self._hold_id = 0
        self._holding = zone
        self._hold_started = time.monotonic()
        self._hold_was_playing = self.want_play and not self.ended
        if zone == "right":
            self._hold_rate = self.player.rate
            self.ended = False
            self.player.set_rate(2.0)
            if not self._hold_was_playing:
                self.player.set_state(Gst.State.PLAYING)
            self._flash("2×  ▶▶", side=zone, ms=0)
            self._hold_tick_id = GLib.timeout_add(250, self._hold_tick)
        else:
            self.player.set_state(Gst.State.PAUSED)
            self._rewind_pos = self.player.position() or 0
            self._hold_tick()
            self._hold_tick_id = GLib.timeout_add(150, self._hold_tick)
        return GLib.SOURCE_REMOVE

    def _hold_tick(self):
        held = time.monotonic() - self._hold_started
        if self._holding == "right":
            if held > 2 and self.player.rate < 4:
                self.player.set_rate(4.0)
                self._flash("4×  ▶▶", side="right", ms=0)
            return GLib.SOURCE_CONTINUE
        step = 0.5 if held < 1.5 else 2 if held < 4 else 5  # about 3×, then 13×, then 33× backwards
        self._rewind_pos = max(0, self._rewind_pos - step)
        self.ended = False
        self.player.seek(self._rewind_pos, accurate=False, before=True)
        self._last_user_seek = time.monotonic()
        self.seekbar.set_value(self._rewind_pos)
        self.pos_label.set_label(fmt_time(self._rewind_pos))
        self._flash(f"◀◀  {fmt_time(self._rewind_pos)}", side="left", ms=0)
        return GLib.SOURCE_CONTINUE

    def _end_hold(self):
        zone, self._holding = self._holding, None
        if self._hold_tick_id:
            GLib.source_remove(self._hold_tick_id)
            self._hold_tick_id = 0
        if zone == "right":
            self.player.set_rate(self._hold_rate)
            if not self._hold_was_playing:
                self.player.set_state(Gst.State.PAUSED)
        else:
            self.player.seek(self._rewind_pos)
            self._last_user_seek = time.monotonic()
            if self._hold_was_playing:
                self.player.set_state(Gst.State.PLAYING)
        self._hide_flash()

    def _on_scroll(self, _ctl, _dx, dy):
        self._scroll_acc += dy
        if abs(self._scroll_acc) >= 1:
            steps = int(self._scroll_acc)
            self._scroll_acc -= steps
            self.set_volume(self.cfg.data["volume"] - 5 * steps)
        return True

    def _on_drop(self, _target, value, _x, _y):
        files = value.get_files()
        if files:
            self.open_files(files)
            return True
        return False

    # ---- dialogs ------------------------------------------------------------------------

    def choose_files(self):
        media = Gtk.FileFilter(name="Videos and Music")
        media.add_mime_type("video/*")
        media.add_mime_type("audio/*")
        for e in MEDIA_EXT:
            media.add_suffix(e[1:])
        everything = Gtk.FileFilter(name="All Files")
        everything.add_pattern("*")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(media)
        filters.append(everything)
        dialog = Gtk.FileDialog(title="Open", filters=filters, default_filter=media)
        if self.uri and self.uri.startswith("file://"):
            dialog.set_initial_folder(Gio.File.new_for_uri(self.uri).get_parent())

        def done(d, res):
            try:
                files = d.open_multiple_finish(res)
            except GLib.Error:
                return
            self.open_files([files.get_item(i) for i in range(files.get_n_items())])

        dialog.open_multiple(self, None, done)

    def choose_subtitle(self):
        subs = Gtk.FileFilter(name="Subtitles")
        for e in SUB_EXT:
            subs.add_suffix(e[1:])
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(subs)
        dialog = Gtk.FileDialog(title="Load Subtitles", filters=filters)
        if self.uri and self.uri.startswith("file://"):
            dialog.set_initial_folder(Gio.File.new_for_uri(self.uri).get_parent())

        def done(d, res):
            try:
                f = d.open_finish(res)
            except GLib.Error:
                return
            self.load_subtitle(f.get_uri())

        dialog.open(self, None, done)

    def choose_url(self):
        entry = Gtk.Entry(placeholder_text="https://…", activates_default=True, hexpand=True)
        dialog = Adw.AlertDialog(heading="Open Network Stream", body="Paste a link to a video, song or stream.")
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("open", "Play")
        dialog.set_response_appearance("open", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("open")
        dialog.set_close_response("cancel")

        def got_clip(clip, res):
            try:
                text = (clip.read_text_finish(res) or "").strip()
            except GLib.Error:
                return
            if re.match(r"^(https?|rtsp|rtmp|mms)://\S+$", text) and not entry.get_text():
                entry.set_text(text)
                entry.select_region(0, -1)

        self.get_clipboard().read_text_async(None, got_clip)

        def response(_d, rid):
            url = entry.get_text().strip()
            if rid == "open" and url:
                if "://" not in url:
                    url = "https://" + url
                self.queue, self.autoadvance = [url], False
                self.play_index(0)

        dialog.connect("response", response)
        dialog.present(self)

    def show_shortcuts(self):
        dialog = Adw.ShortcutsDialog()
        groups = [
            ("Playback", [("Play / Pause", "space"), ("Back / forward (skip length)", "Left Right"),
                          ("Back / forward 3 s", "<Shift>Left <Shift>Right"),
                          ("Back / forward 1 min", "<Ctrl>Left <Ctrl>Right"),
                          ("Jump to 0–90 %", "0…9"), ("Slower / faster", "bracketleft bracketright"),
                          ("Normal speed", "equal"), ("Previous / next file", "p n")]),
            ("Sound & Subtitles", [("Volume up / down", "Up Down"), ("Mute", "m"),
                                   ("Next subtitle track", "s"), ("Subtitle delay − / +", "g h"),
                                   ("Next audio track", "a")]),
            ("Window", [("Fullscreen", "f"), ("Leave fullscreen", "Escape"), ("Open file", "<Ctrl>o"),
                        ("Preferences", "<Ctrl>comma"),
                        ("Open network stream", "<Ctrl>l"), ("Quit", "<Ctrl>q")]),
        ]
        for title, items in groups:
            section = Adw.ShortcutsSection(title=title)
            for label, accel in items:
                if accel == "0…9":
                    section.add(Adw.ShortcutsItem(title=label, accelerator="0 9"))
                else:
                    section.add(Adw.ShortcutsItem(title=label, accelerator=accel))
            dialog.add(section)
        dialog.present(self)

    def show_about(self):
        about = Adw.AboutDialog(application_name="Reel", application_icon=APP_ID, version=VERSION,
                                developer_name="Arjun",
                                comments="A simple player for videos, music and streams.")
        about.present(self)

    # ---- settings ---------------------------------------------------------------------------

    def show_preferences(self, page=None):
        dialog = prefs.Preferences(self)
        if page:
            dialog.set_visible_page_name(page)
        self._prefs = dialog
        dialog.connect("closed", lambda *_: setattr(self, "_prefs", None))
        dialog.present(self)

    def on_setting_changed(self, key):
        if key.startswith("sub_"):
            self.apply_sub_style()
            if key == "sub_lang":
                self._apply_track_preferences(only="text")
        elif key == "subs_auto":
            self._apply_track_preferences(only="text")
        elif key == "audio_lang":
            self._apply_track_preferences(only="audio")
        elif key == "theme":
            prefs.apply_theme(self.cfg.data["theme"])
        elif key == "autoplay_next":
            if len(self.queue) > 1 and self.uri and self.uri.startswith("file://"):
                self.autoadvance = bool(self.cfg.data["autoplay_next"])
        elif key == "hw_decode":
            prefs.set_hardware_decoding(self.cfg.data["hw_decode"])
            if self.uri and self.prerolled:  # reopen so the new decoder is used right away
                pos = self.player.position() or 0
                was_playing = self.want_play
                self.load(self.uri, title=self.title_override, resume=pos)
                self.want_play = was_playing
        elif key == "fit_window" and self.cfg.data["fit_window"]:
            self.fitted_for = None

    def sub_attrs(self, px):
        desc = Pango.FontDescription.from_string(self.cfg.data["sub_font"] or "Sans Semi-Bold")
        desc.set_absolute_size(px * Pango.SCALE)
        attrs = Pango.AttrList()
        attrs.insert(Pango.attr_font_desc_new(desc))
        return attrs

    def apply_sub_style(self):
        """Apply the subtitle style to Reel's own subtitle layer (AI) and to GStreamer's text overlays."""
        d = self.cfg.data
        color = d["sub_color"] if re.match(r"^#[0-9a-fA-F]{6}$", d["sub_color"] or "") else "#ffffff"
        if d["sub_style"] == "box":
            look = f"background: alpha(black, {d['sub_box_opacity']:.2f}); text-shadow: none;"
        elif d["sub_style"] == "shadow":
            look = "background: none; text-shadow: 0 2px 4px alpha(black, 0.9), 0 0 2px black;"
        else:
            look = ("background: none; text-shadow: -1px -1px 0 black, 1px -1px 0 black, -1px 1px 0 black, "
                    "1px 1px 0 black, 0 0 4px black;")
        self._sub_css.load_from_string(f".reel-aisub {{ color: {color}; {look} }}")
        self._sub_px = 0
        self.player.pipe.props.text_offset = int(d["sub_delay"] * Gst.SECOND)
        self._overlays = [o for o in self._overlays if o.get_parent() is not None]
        for overlay in self._overlays:
            self._style_overlay(overlay)
        if self._prefs:
            self._prefs.update_preview()

    def _style_overlay(self, overlay):
        d = self.cfg.data
        desc = Pango.FontDescription.from_string(d["sub_font"] or "Sans Semi-Bold")
        desc.set_size(int(20 * d["sub_scale"] * Pango.SCALE))
        r, g, b = (int(d["sub_color"][i:i + 2], 16) for i in (1, 3, 5)) if len(d["sub_color"]) == 7 else (255,) * 3
        overlay.set_property("font-desc", desc.to_string())
        overlay.set_property("color", (0xFF << 24) | (r << 16) | (g << 8) | b)
        overlay.set_property("draw-outline", d["sub_style"] == "outline")
        overlay.set_property("draw-shadow", d["sub_style"] == "shadow")
        overlay.set_property("shaded-background", d["sub_style"] == "box")
        overlay.set_property("shading-value", int(40 + d["sub_box_opacity"] * 215))
        Gst.util_set_object_arg(overlay, "valignment", "bottom")
        height = self.player.paintable.get_intrinsic_height() or 720
        overlay.set_property("ypad", int(height * d["sub_position"]))

    def _on_element_added(self, _bin, _sub_bin, element):
        factory = element.get_factory()
        if factory and factory.get_name() == "textoverlay":
            self._overlays.append(element)
            GLib.idle_add(lambda: (self._style_overlay(element), False)[1])

    def change_sub_delay(self, delta):
        value = round(self.cfg.data["sub_delay"] + delta, 2)
        self.cfg.data["sub_delay"] = value
        self.apply_sub_style()
        self.cfg.save()
        if self._prefs:
            self._prefs.show_delay(value)
        self._flash(f"Subtitle delay {value:+.1f} s")

    def _track_lang(self, kind, index):
        tags = self.player.pipe.emit(f"get-{kind}-tags", index)
        if tags is None:
            return None
        ok, code = tags.get_string(Gst.TAG_LANGUAGE_CODE)
        if not ok or not code:
            return None
        code = code.lower()
        if GstTag and len(code) != 2:
            code = GstTag.tag_get_language_code_iso_639_1(code) or code
        return code

    def _apply_track_preferences(self, only=None):
        """Pick audio/subtitle tracks from the Preferences (language, show automatically)."""
        pipe = self.player.pipe
        d = self.cfg.data
        if only in (None, "audio") and d["audio_lang"] and pipe.props.n_audio > 1:
            for i in range(pipe.props.n_audio):
                if self._track_lang("audio", i) == d["audio_lang"]:
                    pipe.props.current_audio = i
                    break
        if only in (None, "text"):
            n = pipe.props.n_text
            chosen = self.pending_subtitle == "chosen"
            if self.ai_show:
                pass  # AI subtitles stay on
            elif n and (chosen or (self.pending_subtitle == "found" and d["subs_auto"])):
                self.player.set_subtitle(n - 1)  # the external file is added last
            elif n and d["subs_auto"]:
                pick = next((i for i in range(n) if d["sub_lang"] and self._track_lang("text", i) == d["sub_lang"]),
                            None)
                self.player.set_subtitle(pick if pick is not None else max(0, pipe.props.current_text))
            else:
                self.player.set_subtitle(-1)
        if only:
            self._rebuild_tracks()

    # ---- AI subtitles -----------------------------------------------------------------------

    def choose_ai(self):
        if not self.uri:
            return
        key = ai.pick_model()
        mb = ai.download_mb(key)
        need_engine = ai.whisper_cli() is None
        lines = ["Whisper listens to the audio and writes subtitles on this computer. Nothing is uploaded. "
                 "They appear while you watch, a minute at a time."]
        setup = []
        if need_engine:
            setup.append("set up the AI engine (a few minutes)")
        if mb:
            setup.append(f"download the “{key}” model picked for this PC ({mb} MB)")
        if setup:
            lines.append("First, Reel will " + " and ".join(setup) + ". This happens only once.")
        dialog = Adw.AlertDialog(heading="Generate Subtitles with AI", body="\n\n".join(lines))
        lang = Adw.ComboRow(title="Language", model=Gtk.StringList.new(["Same as the video", "English (translate)"]))
        lang.set_selected(1 if self.cfg.data.get("ai_translate") else 0)
        box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        box.add_css_class("boxed-list")
        box.append(lang)
        dialog.set_extra_child(box)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("go", "Generate")
        dialog.set_response_appearance("go", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("go")
        dialog.set_close_response("cancel")

        def response(_d, rid):
            if rid == "go":
                self.cfg.data["ai_translate"] = lang.get_selected() == 1
                self.cfg.save()
                self.start_ai(self.cfg.data["ai_translate"])

        dialog.connect("response", response)
        dialog.present(self)

    def start_ai(self, translate):
        if self.ai_job:
            self.ai_job.cancel()
            self.ai_job = None
        if ai.whisper_cli() is None:
            self.ai_installer = ai.Installer(self._on_ai_status, lambda ok, why: self._on_installed(ok, why, translate))
            self.ai_installer.start()
            self._on_ai_status("Setting up the AI engine", 0)
            self._rebuild_tracks()
            return
        if self.ai_label is None or self.ai_translate != translate:
            self.ai_chunks = ai.load_cached_cues(self.uri, translate)
            self._merge_ai_cues()
        self.ai_translate = translate
        self.ai_label = "AI · English" if translate else "AI subtitles"
        self.ai_show = True
        self.player.set_subtitle(-1)
        pipe = self.player.pipe
        self.ai_job = ai.Job(self.uri, translate, pipe.props.current_audio, self.player.position() or 0,
                             self._on_ai_status, self._on_ai_cues, self._on_ai_done, self._on_ai_error, self.toast)
        self.ai_job.start()
        self._rebuild_tracks()

    def _on_installed(self, ok, why, translate):
        self.ai_installer = None
        if ok and self.uri:
            self.start_ai(translate)
        else:
            self._on_ai_status(None, 0)
            self._rebuild_tracks()
            if not ok:
                self.toast(f"Couldn't set up the AI engine: {why or 'unknown error'}")
        return GLib.SOURCE_REMOVE

    def stop_ai(self):
        if self.ai_job:
            self.ai_job.cancel()
            self.ai_job = None
        if self.ai_installer:
            self.ai_installer.cancel()
            self.ai_installer = None
        self._on_ai_status(None, 0)
        self._rebuild_tracks()

    def _reset_ai(self):
        if self.ai_job:
            self.ai_job.cancel()
            self.ai_job = None
        self.ai_chunks = {}
        self.ai_cues, self.ai_starts = [], []
        self.ai_show = False
        self.ai_label = None
        self.ai_status = None
        self.ai_sub.set_visible(False)

    def _restore_ai(self):
        """Bring back AI subtitles made earlier for this video, and finish them if they were cut short."""
        if self.ai_label or self.ai_installer or not self.uri:
            return
        cached = ai.find_cached(self.uri)
        if not cached:
            return
        translate, complete = cached
        if not complete and ai.whisper_cli():
            self.start_ai(translate)
            return
        self.ai_translate = translate
        self.ai_chunks = ai.load_cached_cues(self.uri, translate)
        self._merge_ai_cues()
        self.ai_label = "AI · English" if translate else "AI subtitles"
        if self.cfg.data["subs_auto"] and (not self.player.subtitles_on() or not self.player.pipe.props.n_text):
            self.ai_show = True
            self.player.set_subtitle(-1)

    def _on_ai_status(self, text, fraction):
        self.ai_status = f"{text} · {int(fraction * 100)}%" if text else None
        self._update_subtitle_line()
        return GLib.SOURCE_REMOVE

    def _on_ai_cues(self, index, cues):
        self.ai_chunks[index] = cues
        self._merge_ai_cues()

    def _merge_ai_cues(self):
        self.ai_cues = sorted(c for chunk in self.ai_chunks.values() for c in chunk)
        self.ai_starts = [c[0] for c in self.ai_cues]

    def _on_ai_done(self):
        self.ai_job = None
        self._on_ai_status(None, 0)
        self._rebuild_tracks()
        self.toast("AI subtitles are ready")

    def _on_ai_error(self, message):
        self.ai_job = None
        self._on_ai_status(None, 0)
        self._rebuild_tracks()
        self.toast(f"AI subtitles stopped: {message}")

    def _update_subtitle_line(self):
        parts = []
        if len(self.queue) > 1 and self.uri:
            parts.append(f"{self.index + 1} of {len(self.queue)}")
        if self.ai_status:
            parts.append(self.ai_status)
        self.wtitle.set_subtitle("  ·  ".join(parts))

    def _ai_tick(self):
        pos = self.player.position() if self.uri else None
        if self.ai_job and pos is not None:
            self.ai_job.focus = pos
        text = ""
        if self.ai_show and self.ai_cues and pos is not None:
            pos -= self.cfg.data["sub_delay"]
            i = bisect.bisect_right(self.ai_starts, pos) - 1
            if i >= 0 and pos <= self.ai_cues[i][1]:
                text = self.ai_cues[i][2]
        if text != self.ai_sub.get_label():
            self.ai_sub.set_label(text)
        self.ai_sub.set_visible(bool(text))
        if text:
            height = self.stage.get_height()
            px = int(min(120, max(12, height * 0.045 * self.cfg.data["sub_scale"])))
            if px != self._sub_px:
                self._sub_px = px
                self.ai_sub.set_attributes(self.sub_attrs(px))
            margin = max(8, int(height * self.cfg.data["sub_position"]))
            if self.view.get_reveal_bottom_bars():
                margin = max(margin, self.view.get_bottom_bar_height() + 6)
            if self.ai_sub.get_margin_bottom() != margin:
                self.ai_sub.set_margin_bottom(margin)
        return GLib.SOURCE_CONTINUE

    # ---- misc ---------------------------------------------------------------------------

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(text), timeout=4))

    def _flash(self, text, side=None, ms=900):
        """Show a short message on the video; side="left"/"right" puts it on that side, ms=0 keeps it up."""
        self.flash.set_label(text)
        self.flash.set_halign({"left": Gtk.Align.START, "right": Gtk.Align.END}.get(side, Gtk.Align.CENTER))
        self.flash.set_margin_start(48 if side == "left" else 0)
        self.flash.set_margin_end(48 if side == "right" else 0)
        self.flash.set_visible(True)
        if self._flash_id:
            GLib.source_remove(self._flash_id)
            self._flash_id = 0
        if ms:
            self._flash_id = GLib.timeout_add(ms, self._flash_timeout)

    def _flash_timeout(self):
        self._flash_id = 0
        self.flash.set_visible(False)
        return GLib.SOURCE_REMOVE

    def _hide_flash(self):
        if self._flash_id:
            GLib.source_remove(self._flash_id)
        self._flash_id = 0
        self.flash.set_visible(False)
        return GLib.SOURCE_REMOVE

    def _on_close(self, *_):
        """Closing the window ends Reel completely, even if something below hangs."""
        self._save_position()
        self.cfg.save()
        if self.ai_job:
            self.ai_job.cancel()
        if self.ai_installer:
            self.ai_installer.cancel()
        self.set_visible(False)
        threading.Timer(3, os._exit, (0,)).start()  # last resort if GStreamer teardown blocks
        self.player.set_state(Gst.State.NULL)
        self.get_application().quit()
        return False


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE | Gio.ApplicationFlags.HANDLES_OPEN)
        self.cfg = Config()
        self.window = None
        self.add_main_option("sub-file", 0, GLib.OptionFlags.NONE, GLib.OptionArg.STRING,
                             "Subtitle file to use", "FILE")
        self.add_main_option("title", 0, GLib.OptionFlags.NONE, GLib.OptionArg.STRING,
                             "Title to show for the media", "TITLE")

    def do_startup(self):
        Adw.Application.do_startup(self)
        prefs.apply_theme(self.cfg.data["theme"])
        prefs.set_hardware_decoding(self.cfg.data["hw_decode"])
        css = Gtk.CssProvider()
        css.load_from_data(CSS, -1)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        quit_action = Gio.SimpleAction(name="quit")
        quit_action.connect("activate", lambda *_: self.window.close() if self.window else self.quit())
        self.add_action(quit_action)
        self.set_accels_for_action("app.quit", ["<Ctrl>q"])
        self.set_accels_for_action("win.open", ["<Ctrl>o"])
        self.set_accels_for_action("win.open-url", ["<Ctrl>l"])
        self.set_accels_for_action("win.shortcuts", ["<Ctrl>question"])
        self.set_accels_for_action("win.preferences", ["<Ctrl>comma"])

    def _window(self):
        if self.window is None:
            try:
                self.window = Window(self)
            except RuntimeError as e:
                dialog = Adw.AlertDialog(heading="Reel can't start", body=str(e))
                dialog.add_response("ok", "OK")
                win = Adw.ApplicationWindow(application=self)
                dialog.connect("response", lambda *_: self.quit())
                win.present()
                dialog.present(win)
                return None
        return self.window

    def do_activate(self):
        win = self._window()
        if win:
            win.present()

    def do_open(self, files, _n, _hint):
        win = self._window()
        if win:
            win.present()
            win.open_files(files)

    def do_command_line(self, cmdline):
        opts = cmdline.get_options_dict().end().unpack()
        args = cmdline.get_arguments()[1:]
        files = [cmdline.create_file_for_arg(a) for a in args]
        win = self._window()
        if not win:
            return 1
        win.present()
        if files:
            sub = opts.get("sub-file")
            if sub:
                sub = cmdline.create_file_for_arg(sub).get_uri()
            win.open_files(files, subtitle=sub, title=opts.get("title"))
        return 0


def main():
    Gst.init(None)
    status = App().run(sys.argv)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(status)  # don't wait on GStreamer or AI worker threads
