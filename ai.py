"""AI subtitles: transcribe a video's audio on this computer with whisper.cpp, one minute at a time.

Nothing is uploaded. The Whisper model is picked from the computer's hardware (cores, memory, SIMD) and
stepped down automatically if this PC turns out too slow to stay ahead of playback. Minutes near the
playback position are done first, so subtitles show up while watching; finished minutes are cached, so
reopening the video (or generating again) continues where it stopped.
"""
import hashlib
import json
import math
import os
import platform
import re
import shutil
import subprocess
import threading
import time
import urllib.request
import wave

import gi  # noqa: E402

gi.require_version("GstApp", "1.0")
from gi.repository import GLib, Gst, GstApp  # noqa: E402,F401  (GstApp gives appsink its pull methods)

DATA_DIR = os.environ.get("REEL_DATA_DIR", os.path.join(GLib.get_user_data_dir(), "reel"))
CACHE_DIR = os.environ.get("REEL_AI_CACHE", os.path.join(GLib.get_user_cache_dir(), "reel", "ai"))
INSTALLER = os.path.join(os.path.dirname(os.path.realpath(__file__)), "install-ai.sh")
PROFILE = os.path.join(DATA_DIR, "ai-profile.json")
_HF = "https://huggingface.co/"
# lightest first: (key, file, download size in MB)
MODELS = [
    ("tiny", "ggml-tiny-q5_1.bin", 31),
    ("base", "ggml-base-q5_1.bin", 57),
    ("small", "ggml-small-q5_1.bin", 181),
    ("medium", "ggml-medium-q5_0.bin", 514),
]
VAD_MODEL = ("ggml-silero-v5.1.2.bin", _HF + "ggml-org/whisper-vad/resolve/main/ggml-silero-v5.1.2.bin")
CHUNK = 60.0  # seconds of audio per whisper run
KEEP_UP = 1.3  # a model must transcribe at least this many times faster than real time
_LINE = re.compile(r"^\[(\d+):(\d+):(\d+(?:\.\d+)?)\s*-->\s*(\d+):(\d+):(\d+(?:\.\d+)?)\]\s*(.*)$")
_NOISE = re.compile(r"^\W*(blank_audio|silence|no speech|inaudible)?\W*$", re.I)


class Cancelled(Exception):
    pass


# ---- hardware → model ----------------------------------------------------------------------

def hardware():
    """What matters for whisper.cpp on the CPU: threads, memory, and vector instructions."""
    threads = os.cpu_count() or 2
    mem_gb = 4.0
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    mem_gb = int(line.split()[1]) / 1024 / 1024
                    break
    except OSError:
        pass
    flags = set()
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith(("flags", "Features")):
                    flags = set(line.split(":", 1)[1].split())
                    break
    except OSError:
        pass
    machine = platform.machine().lower()
    if "avx512f" in flags:
        simd = "avx512"
    elif "avx2" in flags:
        simd = "avx2"
    elif machine in ("aarch64", "arm64") or "asimd" in flags:
        simd = "neon"
    elif "avx" in flags:
        simd = "avx"
    else:
        simd = "none"
    return {"threads": threads, "mem_gb": round(mem_gb, 1), "simd": simd,
            "vnni": bool(flags & {"avx_vnni", "avx512_vnni"}), "machine": machine}


def _hw_signature(hw):
    return f"{hw['machine']}/{hw['threads']}/{round(hw['mem_gb'])}/{hw['simd']}"


def _hardware_choice(hw):
    t, mem, simd = hw["threads"], hw["mem_gb"], hw["simd"]
    fast_simd = simd in ("avx2", "avx512", "neon")
    if t < 4 or mem < 3 or not fast_simd:
        return "tiny"
    if t < 8 or mem < 6:
        return "base"
    if t >= 16 and mem >= 16 and (simd == "avx512" or hw["vnni"] or t >= 24):
        return "medium"
    return "small"


def _load_profile(hw):
    try:
        with open(PROFILE) as f:
            data = json.load(f)
        return data if data.get("hardware") == _hw_signature(hw) else {}
    except (OSError, ValueError):
        return {}


def _save_profile(hw, cap):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(PROFILE, "w") as f:
        json.dump({"hardware": _hw_signature(hw), "cap": cap}, f)


def pick_model():
    """Model key for this computer: hardware estimate, capped by what was measured to keep up."""
    hw = hardware()
    keys = [m[0] for m in MODELS]
    choice = _hardware_choice(hw)
    cap = _load_profile(hw).get("cap")
    if cap in keys and keys.index(cap) < keys.index(choice):
        choice = cap
    return choice


def model_info(key):
    for k, name, size in MODELS:
        if k == key:
            return name, size
    raise KeyError(key)


def model_file(name):
    return os.path.join(DATA_DIR, "models", name)


def download_mb(key):
    name, size = model_info(key)
    return 0 if os.path.exists(model_file(name)) else size


def threads_to_use():
    n = os.cpu_count() or 2
    return n - 2 if n > 4 else max(1, n - 1)  # leave room for video decoding and the desktop


def whisper_cli():
    for path in (os.path.join(DATA_DIR, "bin", "whisper-cli"), shutil.which("whisper-cli")):
        if path and os.access(path, os.X_OK):
            return path
    return None


# ---- cache ------------------------------------------------------------------------------

def _cache_path(uri, translate):
    key = hashlib.sha1(f"{uri}|{int(translate)}".encode()).hexdigest()
    return os.path.join(CACHE_DIR, key + ".json")


def find_cached(uri):
    """Earlier AI result for this video: (translate, complete), newest first, or None."""
    found = []
    for translate in (False, True):
        path = _cache_path(uri, translate)
        try:
            with open(path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        found.append((bool(data.get("complete")), os.path.getmtime(path), translate))
    if not found:
        return None
    complete, _mtime, translate = max(found)
    return translate, complete


def load_cached_cues(uri, translate):
    try:
        with open(_cache_path(uri, translate)) as f:
            data = json.load(f)
        return {int(k): [tuple(c) for c in v] for k, v in data.get("chunks", {}).items()}
    except (OSError, ValueError):
        return {}


# ---- the job ----------------------------------------------------------------------------

class Job:
    """Runs in a worker thread; every callback is delivered on the GTK main loop.

    on_status(text, fraction), on_cues(chunk_index, cues), on_done(), on_error(message), on_note(message)
    """

    def __init__(self, uri, translate, audio_index, start_at, on_status, on_cues, on_done, on_error, on_note):
        self.uri, self.translate = uri, translate
        self.audio_index = max(0, audio_index)
        self.focus = start_at or 0.0  # playback position, kept current by the window
        self._cb = (on_status, on_cues, on_done, on_error, on_note)
        self.cancelled = False
        self.proc = None
        self.chunks = {}
        self.n_chunks = 0
        self.duration = None
        self.model = pick_model()
        self.cache_path = _cache_path(uri, translate)
        self.tmp = os.path.join(CACHE_DIR, "tmp", f"{os.getpid()}-{id(self)}")
        self.dec = self.sink = None

    def start(self):
        threading.Thread(target=self._run, daemon=True, name="reel-ai").start()

    def cancel(self):
        self.cancelled = True
        proc = self.proc
        if proc and proc.poll() is None:
            proc.kill()

    def progress(self):
        return len(self.chunks) / self.n_chunks if self.n_chunks else 0.0

    # ---- helpers --------------------------------------------------------------------------

    def _emit(self, which, *args):
        GLib.idle_add(lambda: (self.cancelled or self._cb[which](*args), False)[1])

    def _status(self, text, fraction):
        self._emit(0, text, fraction)

    def _check(self):
        if self.cancelled:
            raise Cancelled()

    def _download(self, url, dest, label):
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        part = dest + ".part"
        req = urllib.request.Request(url, headers={"User-Agent": "Reel-media-player"})
        with urllib.request.urlopen(req, timeout=30) as r, open(part, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            got = 0
            while True:
                self._check()
                block = r.read(1 << 20)
                if not block:
                    break
                f.write(block)
                got += len(block)
                if total:
                    self._status(label, got / total)
        if total and got != total:
            raise RuntimeError("the AI model download was cut off; try again")
        os.replace(part, dest)

    def _ensure_model(self, key):
        name, _size = model_info(key)
        path = model_file(name)
        if not os.path.exists(path):
            self._download(_HF + "ggerganov/whisper.cpp/resolve/main/" + name, path, "Downloading AI model")
        return path

    def _load_cache(self):
        self.chunks = load_cached_cues(self.uri, self.translate)

    def _save_cache(self):
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = self.cache_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"uri": self.uri, "duration": self.duration, "model": self.model,
                       "complete": len(self.chunks) >= self.n_chunks,
                       "chunks": {str(k): v for k, v in self.chunks.items()}}, f)
        os.replace(tmp, self.cache_path)

    def _open_decoder(self):
        pipe = Gst.ElementFactory.make("playbin", "ai-decoder")
        pipe.props.uri = self.uri
        pipe.props.flags = 0x2  # audio only
        pipe.props.video_sink = Gst.ElementFactory.make("fakesink", None)
        bin_ = Gst.parse_bin_from_description(
            "audioconvert ! audioresample ! audio/x-raw,format=S16LE,rate=16000,channels=1 ! "
            "appsink name=sink sync=false max-buffers=400", True)
        pipe.props.audio_sink = bin_
        self.sink = bin_.get_by_name("sink")
        self.dec = pipe
        pipe.set_state(Gst.State.PAUSED)
        ret, _state, _pending = pipe.get_state(60 * Gst.SECOND)
        if ret == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("couldn't read the audio of this video")
        if pipe.props.n_audio == 0:
            raise RuntimeError("this video has no sound to transcribe")
        if self.audio_index < pipe.props.n_audio:
            pipe.props.current_audio = self.audio_index
        ok, dur = pipe.query_duration(Gst.Format.TIME)
        if not ok or dur <= 0:
            raise RuntimeError("AI subtitles need a video with a known length")
        self.duration = dur / Gst.SECOND

    def _extract(self, start, end):
        p = self.dec
        p.seek(1.0, Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE,
               Gst.SeekType.SET, int(start * Gst.SECOND), Gst.SeekType.SET, int(end * Gst.SECOND))
        p.get_state(30 * Gst.SECOND)
        p.set_state(Gst.State.PLAYING)
        data = bytearray()
        idle = 0
        while True:
            self._check()
            sample = self.sink.try_pull_sample(Gst.SECOND)
            if sample is None:
                if self.sink.is_eos():
                    break
                idle += 1
                if idle > 120:  # e.g. a stream that stopped sending data
                    break
                continue
            idle = 0
            buf = sample.get_buffer()
            ok, info = buf.map(Gst.MapFlags.READ)
            if ok:
                data += info.data
                buf.unmap(info)
        p.set_state(Gst.State.PAUSED)
        return bytes(data)

    def _next_chunk(self):
        first = min(int(max(0, self.focus) // CHUNK), self.n_chunks - 1)
        for i in list(range(first, self.n_chunks)) + list(range(0, first)):
            if i not in self.chunks:
                return i
        return None

    def _transcribe(self, index, cli, model, vad):
        """Returns (cues, seconds whisper took)."""
        start = index * CHUNK
        end = min(self.duration, start + CHUNK)
        pcm = self._extract(start, end)
        self._check()
        if len(pcm) < 16000:  # under half a second of sound
            return [], 0.0
        wav = os.path.join(self.tmp, "chunk.wav")
        with wave.open(wav, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(pcm)
        cmd = [cli, "-m", model, "-f", wav, "-l", "auto", "-t", str(threads_to_use()),
               "-np", "-mc", "0", "--suppress-nst"]
        if vad:
            cmd += ["--vad", "-vm", vad]
        if self.translate:
            cmd.append("-tr")
        errlog = os.path.join(self.tmp, "whisper.log")
        cues = []
        began = time.monotonic()
        with open(errlog, "w") as err:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err, stdin=subprocess.DEVNULL,
                                         text=True, errors="replace", preexec_fn=lambda: os.nice(10))
            for line in self.proc.stdout:
                m = _LINE.match(line.strip())
                if not m:
                    continue
                h1, m1, s1, h2, m2, s2, text = m.groups()
                text = text.strip()
                if not text or _NOISE.match(text):
                    continue
                a = start + int(h1) * 3600 + int(m1) * 60 + float(s1)
                b = start + int(h2) * 3600 + int(m2) * 60 + float(s2)
                cues.append((round(a, 3), round(max(b, a + 0.5), 3), text))
                self._emit(1, index, list(cues))
            rc = self.proc.wait()
        took = time.monotonic() - began
        self.proc = None
        self._check()
        if rc != 0:
            try:
                with open(errlog, errors="replace") as f:
                    tail = [ln.strip() for ln in f.read().splitlines() if ln.strip()][-3:]
            except OSError:
                tail = []
            raise RuntimeError("Whisper failed: " + (" / ".join(tail) or f"exit code {rc}"))
        return cues, took

    def _maybe_step_down(self, audio_seconds, took):
        """If this PC can't keep ahead of playback with the current model, use a lighter one from now on."""
        if took <= 0 or audio_seconds < CHUNK / 2:
            return False
        keys = [m[0] for m in MODELS]
        i = keys.index(self.model)
        if audio_seconds / took >= KEEP_UP or i == 0:
            return False
        self.model = keys[i - 1]
        _save_profile(hardware(), self.model)
        self._emit(4, "Switched to a lighter AI model so subtitles keep up")
        return True

    def _run(self):
        try:
            cli = whisper_cli()
            if not cli:
                raise RuntimeError("the Whisper engine isn't installed")
            model = self._ensure_model(self.model)
            vad = model_file(VAD_MODEL[0])
            if not os.path.exists(vad):
                try:
                    self._download(VAD_MODEL[1], vad, "Downloading AI model")
                except (OSError, ValueError, RuntimeError):
                    vad = None  # optional: it only filters out silence and music
            self._check()
            os.makedirs(self.tmp, exist_ok=True)
            self._load_cache()
            self._status("Reading audio", 0)
            self._open_decoder()
            self.n_chunks = max(1, math.ceil(self.duration / CHUNK))
            self.chunks = {k: v for k, v in self.chunks.items() if k < self.n_chunks}
            for index, cues in self.chunks.items():
                self._emit(1, index, cues)
            while True:
                self._check()
                self._status("AI subtitles", self.progress())
                index = self._next_chunk()
                if index is None:
                    break
                cues, took = self._transcribe(index, cli, model, vad)
                self.chunks[index] = cues
                self._emit(1, index, cues)
                self._save_cache()
                length = min(self.duration, (index + 1) * CHUNK) - index * CHUNK
                if self._maybe_step_down(length, took):
                    model = self._ensure_model(self.model)
            self._emit(2)
        except Cancelled:
            pass
        except Exception as e:  # noqa: BLE001 - shown to the user
            if not self.cancelled:
                self._emit(3, str(e))
        finally:
            if self.dec is not None:
                self.dec.set_state(Gst.State.NULL)
            shutil.rmtree(self.tmp, ignore_errors=True)


class Installer:
    """Runs install-ai.sh (installs build tools if needed, builds whisper.cpp for this CPU)."""

    def __init__(self, on_status, on_done):
        self.on_status, self.on_done = on_status, on_done
        self.proc = None

    def start(self):
        threading.Thread(target=self._run, daemon=True, name="reel-ai-install").start()

    def cancel(self):
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, 9)
            except OSError:
                pass

    def _run(self):
        tail = []
        try:
            self.proc = subprocess.Popen(["bash", INSTALLER], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         stdin=subprocess.DEVNULL, text=True, errors="replace",
                                         start_new_session=True)
            for line in self.proc.stdout:
                line = line.strip()
                if line.startswith("PROGRESS "):
                    GLib.idle_add(self.on_status, "Building the AI engine", int(line.split()[1]) / 100)
                elif line.startswith("STEP "):
                    GLib.idle_add(self.on_status, line[5:], 0)
                elif line:
                    tail = (tail + [line])[-3:]
            rc = self.proc.wait()
        except OSError as e:
            rc, tail = 1, [str(e)]
        ok = rc == 0 and whisper_cli() is not None
        GLib.idle_add(self.on_done, ok, " / ".join(tail))
