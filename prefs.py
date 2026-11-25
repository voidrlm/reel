"""Reel's Preferences window: General (playback, window, audio, data) and Subtitles (showing + style)."""
import os
import shutil

from gi.repository import Adw, Gdk, Gtk, Pango

import ai

DEFAULTS = {
    # playback
    "resume": False, "autoplay_next": False, "seek_step": 10, "hw_decode": True,
    # window
    "hide_after": 2.5, "fit_window": True, "theme": "dark",
    # audio
    "audio_lang": "",
    # subtitles
    "subs_auto": True, "sub_lang": "", "sub_delay": 0.0,
    "sub_font": "Sans Semi-Bold", "sub_scale": 1.0, "sub_color": "#ffffff",
    "sub_style": "outline", "sub_box_opacity": 0.55, "sub_position": 0.05,
}
SUB_STYLE_KEYS = ("sub_font", "sub_scale", "sub_color", "sub_style", "sub_box_opacity", "sub_position")
# Settings without a control any more: always their default, never read from the saved config.
FIXED = ("resume", "seek_step", "hw_decode", "hide_after", "fit_window", "theme", "audio_lang",
         "subs_auto", "sub_lang", "sub_delay")

LANGUAGES = [("", "From the file"), ("en", "English"), ("hi", "Hindi"), ("ml", "Malayalam"), ("ta", "Tamil"),
             ("te", "Telugu"), ("kn", "Kannada"), ("bn", "Bengali"), ("mr", "Marathi"), ("ur", "Urdu"),
             ("es", "Spanish"), ("fr", "French"), ("de", "German"), ("it", "Italian"), ("pt", "Portuguese"),
             ("ru", "Russian"), ("ar", "Arabic"), ("tr", "Turkish"), ("ja", "Japanese"), ("ko", "Korean"),
             ("zh", "Chinese")]
SEEK_STEPS = [5, 10, 15, 30]
HIDE_AFTER = [1.5, 2.5, 4.0, 6.0]
THEMES = [("dark", "Dark"), ("system", "Follow system"), ("light", "Light")]
SUB_STYLES = [("outline", "Outline"), ("shadow", "Drop shadow"), ("box", "Dark box")]


def dir_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def human(n):
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("bytes", "KB") else f"{n:.1f} {unit}"
        n /= 1024


def _combo(title, labels, selected, subtitle=None):
    row = Adw.ComboRow(title=title, model=Gtk.StringList.new(labels))
    if subtitle:
        row.set_subtitle(subtitle)
    row.set_selected(max(0, selected))
    return row


def _scale_row(title, lo, hi, step, value, fmt, subtitle=None):
    row = Adw.ActionRow(title=title)
    if subtitle:
        row.set_subtitle(subtitle)
    scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, lo, hi, step)
    scale.set_value(value)
    scale.set_hexpand(True)
    scale.set_size_request(180, -1)
    scale.set_valign(Gtk.Align.CENTER)
    scale.set_draw_value(True)
    scale.set_value_pos(Gtk.PositionType.LEFT)
    scale.set_format_value_func(lambda _s, v: fmt(v))
    row.add_suffix(scale)
    return row, scale


class Preferences(Adw.PreferencesDialog):
    def __init__(self, win):
        super().__init__(title="Preferences", search_enabled=False)
        self.win = win
        self.cfg = win.cfg
        self.set_content_width(560)
        self.add(self._general_page())
        self.add(self._subtitles_page())

    # ---- helpers --------------------------------------------------------------------------

    def _set(self, key, value):
        self.cfg.data[key] = value
        self.cfg.save()
        self.win.on_setting_changed(key)

    def _bind_switch(self, row, key):
        row.set_active(bool(self.cfg.data[key]))
        row.connect("notify::active", lambda r, _p: self._set(key, r.get_active()))
        return row

    def _bind_combo(self, row, key, values):
        row.connect("notify::selected", lambda r, _p: self._set(key, values[r.get_selected()]))
        return row

    # ---- General ------------------------------------------------------------------------------

    def _general_page(self):
        d = self.cfg.data
        page = Adw.PreferencesPage(title="General", icon_name="preferences-system-symbolic")

        playback = Adw.PreferencesGroup(title="Playback")
        playback.add(self._bind_switch(Adw.SwitchRow(title="Play the next video automatically",
                                                     subtitle="Continues with the next file in the same folder"),
                                       "autoplay_next"))
        page.add(playback)

        data = Adw.PreferencesGroup(title="Storage")
        self.cache_row = Adw.ActionRow(title="Saved AI subtitles")
        btn = Gtk.Button(label="Delete", valign=Gtk.Align.CENTER)
        btn.connect("clicked", self._clear_ai_cache)
        self.cache_row.add_suffix(btn)
        data.add(self.cache_row)
        self.models_row = Adw.ActionRow(title="AI models",
                                        subtitle="")
        btn = Gtk.Button(label="Delete", valign=Gtk.Align.CENTER)
        btn.connect("clicked", self._delete_models)
        self.models_row.add_suffix(btn)
        data.add(self.models_row)
        self._refresh_storage()
        page.add(data)
        return page

    def _refresh_storage(self):
        cache = sum(os.path.getsize(os.path.join(ai.CACHE_DIR, f)) for f in os.listdir(ai.CACHE_DIR)
                    if f.endswith(".json")) if os.path.isdir(ai.CACHE_DIR) else 0
        self.cache_row.set_subtitle(human(cache) if cache else "Nothing saved")
        models = dir_size(os.path.join(ai.DATA_DIR, "models"))
        engine = "AI engine installed" if ai.whisper_cli() else "AI engine not installed"
        self.models_row.set_subtitle(f"{human(models)} · {engine} · this PC uses “{ai.pick_model()}”"
                                     if models else f"None downloaded · {engine}")

    def _clear_ai_cache(self, _btn):
        if os.path.isdir(ai.CACHE_DIR):
            for f in os.listdir(ai.CACHE_DIR):
                if f.endswith(".json"):
                    try:
                        os.remove(os.path.join(ai.CACHE_DIR, f))
                    except OSError:
                        pass
        self._refresh_storage()
        self.add_toast(Adw.Toast(title="Saved AI subtitles deleted"))

    def _delete_models(self, _btn):
        if self.win.ai_job or self.win.ai_installer:
            self.add_toast(Adw.Toast(title="Stop generating AI subtitles first"))
            return
        shutil.rmtree(os.path.join(ai.DATA_DIR, "models"), ignore_errors=True)
        try:
            os.remove(ai.PROFILE)
        except OSError:
            pass
        self._refresh_storage()
        self.add_toast(Adw.Toast(title="AI models deleted. They download again when needed"))

    # ---- Subtitles --------------------------------------------------------------------------

    def _subtitles_page(self):
        d = self.cfg.data
        page = Adw.PreferencesPage(title="Subtitles", icon_name="media-view-subtitles-symbolic",
                                   name="subtitles")

        # live preview on a video-like background
        preview_group = Adw.PreferencesGroup()
        frame = Gtk.Overlay()
        frame.add_css_class("reel-preview")
        frame.add_css_class("card")
        frame.set_overflow(Gtk.Overflow.HIDDEN)
        frame.set_size_request(-1, 150)
        frame.set_child(Gtk.Box(hexpand=True, vexpand=True))
        self.preview = Gtk.Label(label="This is how your subtitles will look", halign=Gtk.Align.CENTER,
                                 valign=Gtk.Align.END, wrap=True, justify=Gtk.Justification.CENTER)
        self.preview.add_css_class("reel-aisub")
        frame.add_overlay(self.preview)
        preview_group.add(frame)
        page.add(preview_group)


        style = Adw.PreferencesGroup(title="Style")
        font_row = Adw.ActionRow(title="Font")
        font_btn = Gtk.FontDialogButton(dialog=Gtk.FontDialog(title="Subtitle Font"), level=Gtk.FontLevel.FACE,
                                        valign=Gtk.Align.CENTER, use_font=True)
        font_btn.set_font_desc(Pango.FontDescription.from_string(d["sub_font"]))
        font_btn.connect("notify::font-desc", self._on_font)
        font_row.add_suffix(font_btn)
        style.add(font_row)

        row, self.size_scale = _scale_row("Size", 50, 250, 5, d["sub_scale"] * 100, lambda v: f"{v:.0f}%")
        self.size_scale.connect("value-changed", lambda s: self._set("sub_scale", round(s.get_value() / 100, 2)))
        style.add(row)

        color_row = Adw.ActionRow(title="Text color")
        self.color_btn = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog(title="Subtitle Color", with_alpha=False),
                                               valign=Gtk.Align.CENTER)
        rgba = Gdk.RGBA()
        rgba.parse(d["sub_color"])
        self.color_btn.set_rgba(rgba)
        self.color_btn.connect("notify::rgba", self._on_color)
        color_row.add_suffix(self.color_btn)
        for hex_ in ("#ffffff", "#ffe14d", "#7fe0ff"):  # quick picks
            swatch = Gtk.Button(valign=Gtk.Align.CENTER, tooltip_text=hex_)
            swatch.add_css_class("circular")
            swatch.add_css_class("reel-swatch")
            swatch.set_child(Gtk.Label(label="●"))
            swatch.get_child().set_markup(f"<span foreground='{hex_}' size='x-large'>●</span>")
            swatch.connect("clicked", lambda _b, h=hex_: self._pick_color(h))
            color_row.add_suffix(swatch)
        style.add(color_row)

        keys = [k for k, _ in SUB_STYLES]
        self.style_row = self._bind_combo(_combo("Background", [n for _, n in SUB_STYLES],
                                                 keys.index(d["sub_style"]) if d["sub_style"] in keys else 0),
                                          "sub_style", keys)
        style.add(self.style_row)

        self.box_row, self.box_scale = _scale_row("Box darkness", 10, 100, 5, d["sub_box_opacity"] * 100,
                                                  lambda v: f"{v:.0f}%")
        self.box_scale.connect("value-changed",
                               lambda s: self._set("sub_box_opacity", round(s.get_value() / 100, 2)))
        self.box_row.set_sensitive(d["sub_style"] == "box")
        self.style_row.connect("notify::selected",
                               lambda r, _p: self.box_row.set_sensitive(keys[r.get_selected()] == "box"))
        style.add(self.box_row)

        row, self.pos_scale = _scale_row("Distance from bottom", 0, 30, 1, d["sub_position"] * 100,
                                         lambda v: f"{v:.0f}%")
        self.pos_scale.connect("value-changed", lambda s: self._set("sub_position", round(s.get_value() / 100, 3)))
        style.add(row)
        page.add(style)

        reset_group = Adw.PreferencesGroup()
        reset = Gtk.Button(label="Reset Subtitle Style", halign=Gtk.Align.CENTER)
        reset.add_css_class("pill")
        reset.connect("clicked", lambda *_: self._reset_style(font_btn))
        reset_group.add(reset)
        page.add(reset_group)
        self.update_preview()
        return page

    def _on_font(self, btn, _p):
        desc = btn.get_font_desc()
        if desc is None:
            return
        desc = desc.copy()
        desc.unset_fields(Pango.FontMask.SIZE)
        self._set("sub_font", desc.to_string())

    def _on_color(self, btn, _p):
        c = btn.get_rgba()
        self._set("sub_color", "#{:02x}{:02x}{:02x}".format(round(c.red * 255), round(c.green * 255),
                                                             round(c.blue * 255)))

    def _pick_color(self, hex_):
        rgba = Gdk.RGBA()
        rgba.parse(hex_)
        self.color_btn.set_rgba(rgba)  # fires notify::rgba → saved

    def _reset_style(self, font_btn):
        for key in SUB_STYLE_KEYS:
            self.cfg.data[key] = DEFAULTS[key]
        font_btn.set_font_desc(Pango.FontDescription.from_string(DEFAULTS["sub_font"]))
        self._pick_color(DEFAULTS["sub_color"])
        self.size_scale.set_value(DEFAULTS["sub_scale"] * 100)
        self.box_scale.set_value(DEFAULTS["sub_box_opacity"] * 100)
        self.pos_scale.set_value(DEFAULTS["sub_position"] * 100)
        self.style_row.set_selected([k for k, _ in SUB_STYLES].index(DEFAULTS["sub_style"]))
        self.cfg.save()
        self.win.on_setting_changed("sub_style")

    def update_preview(self):
        self.preview.set_attributes(self.win.sub_attrs(round(24 * self.cfg.data["sub_scale"])))
        self.preview.set_margin_bottom(int(8 + 150 * self.cfg.data["sub_position"]))
        self.preview.set_margin_start(12)
        self.preview.set_margin_end(12)


def apply_theme(theme):
    scheme = {"dark": Adw.ColorScheme.FORCE_DARK, "light": Adw.ColorScheme.FORCE_LIGHT}.get(
        theme, Adw.ColorScheme.DEFAULT)
    Adw.StyleManager.get_default().set_color_scheme(scheme)


_hw_ranks = {}


def set_hardware_decoding(enabled):
    """Hardware decoders win by rank; dropping it to NONE makes playbin pick software decoders."""
    from gi.repository import Gst
    registry = Gst.Registry.get()
    for plugin in ("va", "vaapi", "nvcodec", "v4l2codecs", "qsv", "msdk", "amfcodec"):
        for feature in registry.get_feature_list_by_plugin(plugin):
            if not isinstance(feature, Gst.ElementFactory):
                continue
            if "Decoder" not in (feature.get_metadata("klass") or ""):
                continue
            name = feature.get_name()
            _hw_ranks.setdefault(name, feature.get_rank())
            feature.set_rank(_hw_ranks[name] if enabled else Gst.Rank.NONE)
