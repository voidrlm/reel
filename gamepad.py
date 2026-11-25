"""Gamepad input for Reel through libmanette (handles hot-plugging and the standard SDL button layouts).

Every control is reduced to a named "virtual button" (a, b, x, y, lb, rb, start, select, up/down/left/right,
lt, rt). D-pad and stick directions repeat while held; the triggers report press and release so the window
can run hold-to-fast-forward / rewind.
"""
import gi

from gi.repository import GLib

try:
    gi.require_version("Manette", "0.2")
    from gi.repository import Manette
except (ValueError, ImportError):
    Manette = None

# Linux input event codes (libmanette reports mapped gamepads in this standard layout)
BUTTONS = {
    304: "a",        # BTN_SOUTH: A / Cross
    305: "b",        # BTN_EAST: B / Circle
    307: "y",        # BTN_NORTH: Y / Triangle
    308: "x",        # BTN_WEST: X / Square
    310: "lb", 311: "rb", 312: "lt", 313: "rt",
    314: "select", 315: "start",
    544: "up", 545: "down", 546: "left", 547: "right",  # BTN_DPAD_*
}
REPEAT = {"up", "down", "left", "right"}
HOLD = {"lt", "rt"}
REPEAT_DELAY_MS, REPEAT_EVERY_MS = 400, 220
STICK_ON, STICK_OFF = 0.6, 0.35  # hysteresis so a resting stick doesn't flicker
TRIGGER_ON, TRIGGER_OFF = 0.5, 0.2


class Gamepads:
    def __init__(self, on_button, on_hold, on_connected):
        self.on_button, self.on_hold, self.on_connected = on_button, on_hold, on_connected
        self.available = Manette is not None
        self._repeat = {}  # virtual button -> GLib source id
        self._down = set()  # virtual buttons currently held, across all pads
        self._axis_state = {}  # (device id, axis) -> virtual button currently "pressed" by that axis
        self._devices = []
        if not self.available:
            return
        self.monitor = Manette.Monitor()
        self.monitor.connect("device-connected", lambda _m, dev: self._attach(dev, announce=True))
        it = self.monitor.iterate()
        while True:
            ok, dev = it.next()
            if not ok or dev is None:
                break
            self._attach(dev, announce=False)

    # ---- devices ----------------------------------------------------------------------------

    def _attach(self, dev, announce):
        self._devices.append(dev)
        dev.connect("button-press-event", self._on_button, True)
        dev.connect("button-release-event", self._on_button, False)
        dev.connect("hat-axis-event", self._on_hat)
        dev.connect("absolute-axis-event", self._on_axis)
        dev.connect("disconnected", self._on_disconnected)
        if announce:
            self.on_connected(dev.get_name() or "Gamepad")

    def _on_disconnected(self, dev):
        if dev in self._devices:
            self._devices.remove(dev)
        for key in [k for k in self._axis_state if k[0] == id(dev)]:
            self._release(self._axis_state.pop(key))

    # ---- raw events → virtual buttons -------------------------------------------------------

    def _on_button(self, _dev, event, pressed):
        ok, code = event.get_button()
        name = BUTTONS.get(code) if ok else None
        if name:
            (self._press if pressed else self._release)(name)

    def _on_hat(self, dev, event):
        ok, axis, value = event.get_hat()
        if not ok or axis not in (16, 17):  # ABS_HAT0X / ABS_HAT0Y
            return
        neg, pos = ("left", "right") if axis == 16 else ("up", "down")
        self._set_axis(dev, axis, neg if value < 0 else pos if value > 0 else None)

    def _on_axis(self, dev, event):
        ok, axis, value = event.get_absolute()
        if not ok:
            return
        if axis in (0, 1):  # left stick
            neg, pos = ("left", "right") if axis == 0 else ("up", "down")
            current = self._axis_state.get((id(dev), axis))
            threshold = STICK_OFF if current else STICK_ON
            name = neg if value <= -threshold else pos if value >= threshold else None
            self._set_axis(dev, axis, name)
        elif axis in (2, 5):  # analog triggers (ABS_Z / ABS_RZ), reported 0..1 or -1..1
            name = "lt" if axis == 2 else "rt"
            current = self._axis_state.get((id(dev), axis))
            self._set_axis(dev, axis, name if value >= (TRIGGER_OFF if current else TRIGGER_ON) else None)

    def _set_axis(self, dev, axis, name):
        key = (id(dev), axis)
        old = self._axis_state.get(key)
        if old == name:
            return
        if old:
            self._release(old)
        if name:
            self._axis_state[key] = name
            self._press(name)
        else:
            self._axis_state.pop(key, None)

    # ---- virtual buttons --------------------------------------------------------------------

    def _press(self, name):
        if name in self._down:
            return
        self._down.add(name)
        if name in HOLD:
            self.on_hold(name, True)
            return
        self.on_button(name)
        if name in REPEAT:
            self._repeat[name] = GLib.timeout_add(REPEAT_DELAY_MS, self._start_repeat, name)

    def _start_repeat(self, name):
        self._repeat[name] = GLib.timeout_add(REPEAT_EVERY_MS, self._repeat_tick, name)
        self.on_button(name)
        return GLib.SOURCE_REMOVE

    def _repeat_tick(self, name):
        if name not in self._down:
            self._repeat.pop(name, None)
            return GLib.SOURCE_REMOVE
        self.on_button(name)
        return GLib.SOURCE_CONTINUE

    def _release(self, name):
        if name not in self._down:
            return
        self._down.discard(name)
        source = self._repeat.pop(name, None)
        if source:
            GLib.source_remove(source)
        if name in HOLD:
            self.on_hold(name, False)
