import os
import subprocess
import time
import threading
from datetime import datetime
import struct
import queue
import glob

from textual.app import App
from textual.widgets import Header, Footer, ListView, ListItem, Static
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive

MEDIA_DIR = "/srv/playout/ready"
SLATE_FILE = "/srv/playout/slate.jpg"


# ---------- BIG ASCII NUMBERS ----------
BIG_NUMBERS = {
    "0": [" ███ ", "█   █", "█   █", "█   █", " ███ "],
    "1": ["  █  ", " ██  ", "  █  ", "  █  ", " ███ "],
    "2": [" ███ ", "█   █", "   █ ", "  █  ", "█████"],
    "3": ["████ ", "    █", " ███ ", "    █", "████ "],
    "4": ["█  █ ", "█  █ ", "█████", "   █ ", "   █ "],
    "5": ["█████", "█    ", "████ ", "    █", "████ "],
    "6": [" ███ ", "█    ", "████ ", "█   █", " ███ "],
    "7": ["█████", "   █ ", "  █  ", " █   ", " █   "],
    "8": [" ███ ", "█   █", " ███ ", "█   █", " ███ "],
    "9": [" ███ ", "█   █", " ████", "    █", " ███ "],
    ":": ["     ", "  █  ", "     ", "  █  ", "     "],
    " ": ["     ", "     ", "     ", "     ", "     "],
}


def render_big(text):
    lines = ["", "", "", "", ""]
    for c in text:
        pat = BIG_NUMBERS.get(c, BIG_NUMBERS[" "])
        for i in range(5):
            lines[i] += pat[i] + "  "
    return "\n".join(lines)


# ---------- PLAYER ----------
class Player:
    def __init__(self):
        self.process = None

    def idle(self, filepath):
        if self.process:
            self.process.kill()

        self.process = subprocess.Popen(
            [
                "ffmpeg",
                "-loop", "1",
                "-re",
                "-i", filepath,
                "-vf", "scale=1920:1080",
                "-r", "30000/1001",
                "-pix_fmt", "uyvy422",
                "-f", "decklink",
                "-format_code", "Hp29",
                "DeckLink SDI 4K",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def play(self, filepath, on_complete=None):
        if self.process:
            self.process.kill()

        self.process = subprocess.Popen(
            [
                "ffmpeg",
                "-i", filepath,
                "-vf", "scale=1920:1080,fps=30000/1001",
                "-pix_fmt", "uyvy422",
                "-af", "adelay=75|75",
                "-f", "decklink",
                "-format_code", "Hp29",
                "DeckLink SDI 4K",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        if on_complete:
            def wait():
                self.process.wait()
                on_complete()
            threading.Thread(target=wait, daemon=True).start()


player = Player()


def get_duration(fp):
    try:
        r = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                fp,
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        return float(r.stdout.strip())
    except:
        return 0


# ---------- TOUCHSCREEN SUPPORT (console PenMount) ----------
# This implements a background thread that reads Linux input_event devices
# and locates a PenMount device by reading /sys/class/input/*/device/name.
# When a BTN_LEFT press is observed, it enqueues the calibrated terminal
# coordinates to be handled in the main Textual thread. No external libs used.

# Linux input event constants
_EV_KEY = 0x01
_EV_ABS = 0x03
_BTN_LEFT = 0x110  # 272
_ABS_X = 0x00
_ABS_Y = 0x01

# calibrated touchscreen extents (from user)
TOUCH_MIN_X = 44
TOUCH_MAX_X = 969
TOUCH_MIN_Y = 51
TOUCH_MAX_Y = 972

# input_event struct: struct timeval (tv_sec long, tv_usec long), unsigned short type, unsigned short code, int value
_INPUT_EVENT_FORMAT = 'llHHi'
_INPUT_EVENT_SIZE = struct.calcsize(_INPUT_EVENT_FORMAT)

_TOUCH_QUEUE = queue.Queue()

# single-shot detection: if device is not present we will not enable touch support
_thread_started = False


def _find_penmount_event():
    """Locate an event device whose name file under /sys/class/input contains 'PenMount'.
    Returns the /dev/input/eventX path or None.
    """
    try:
        paths = glob.glob('/sys/class/input/*/device/name')
    except Exception:
        return None

    for name_path in paths:
        try:
            with open(name_path, 'r', encoding='utf-8', errors='ignore') as f:
                name = f.read().strip()
        except Exception:
            continue

        if not name:
            continue

        if 'PenMount' in name:
            # name_path: /sys/class/input/event2/device/name
            event_dir = os.path.dirname(os.path.dirname(name_path))
            event_basename = os.path.basename(event_dir)
            dev_path = os.path.join('/dev/input', event_basename)
            if os.path.exists(dev_path):
                return dev_path
    return None


def _touchscreen_thread_main(device_path):
    """Thread that reads events from the given device_path and pushes calibrated
    terminal coordinates on BTN_LEFT presses to _TOUCH_QUEUE. If the device cannot
    be opened, the thread exits and touch support is disabled.
    """
    abs_x = None
    abs_y = None

    try:
        fd = open(device_path, 'rb')
    except Exception as e:
        try:
            print(f"[touch] failed to open {device_path}: {e}", flush=True)
        except Exception:
            pass
        # disable further touch attempts
        return

    try:
        while True:
            data = fd.read(_INPUT_EVENT_SIZE)
            if not data or len(data) < _INPUT_EVENT_SIZE:
                # device disconnected or short read
                try:
                    fd.close()
                except Exception:
                    pass
                try:
                    print(f"[touch] device {device_path} disconnected", flush=True)
                except Exception:
                    pass
                break

            try:
                tv_sec, tv_usec, etype, code, value = struct.unpack(_INPUT_EVENT_FORMAT, data)
            except Exception:
                continue

            if etype == _EV_ABS:
                if code == _ABS_X:
                    abs_x = int(value)
                elif code == _ABS_Y:
                    abs_y = int(value)
            elif etype == _EV_KEY and code == _BTN_LEFT:
                # value 1 == press, 0 == release
                if value == 1:
                    if abs_x is None or abs_y is None:
                        # ignore if position unknown
                        continue

                    # clamp raw values
                    rx = max(TOUCH_MIN_X, min(TOUCH_MAX_X, abs_x))
                    ry = max(TOUCH_MIN_Y, min(TOUCH_MAX_Y, abs_y))

                    # normalize to 0..1
                    try:
                        nx = (rx - TOUCH_MIN_X) / float(TOUCH_MAX_X - TOUCH_MIN_X)
                        ny = (ry - TOUCH_MIN_Y) / float(TOUCH_MAX_Y - TOUCH_MIN_Y)
                    except Exception:
                        continue

                    # flip both axes (180° rotation)
                    nx = 1.0 - min(max(nx, 0.0), 1.0)
                    ny = 1.0 - min(max(ny, 0.0), 1.0)

                    # map to terminal size
                    try:
                        ts = os.get_terminal_size()
                        cols = max(1, ts.columns)
                        rows = max(1, ts.lines)
                    except OSError:
                        cols, rows = 80, 24

                    tx = int(nx * (cols - 1))
                    ty = int(ny * (rows - 1))

                    try:
                        _TOUCH_QUEUE.put_nowait((tx, ty))
                    except Exception:
                        pass
    finally:
        try:
            fd.close()
        except Exception:
            pass


# ---------- APP ----------
class CartApp(App):

    CSS = """
    #files_box { border: round cyan; padding: 1; }
    #queue_box { border: round yellow; padding: 1; }
    #preview_box { border: round magenta; padding: 1; }
    #controls_box { border: round blue; padding: 1; }
    #status_bar { border: round green; padding: 1; }

    #controls_box > .control { width: 100%; }
    """

    queue = reactive([])
    current_duration = reactive(0.0)
    start_time = reactive(0.0)

    def compose(self):
        self.file_list = ListView()
        self.queue_list = ListView()
        self.preview_panel = Static("Waiting...")

        self.now_playing = Static("Idle")
        # replaced big timer/clock with a single status bar
        self.status_bar = Static("")

        # controls
        self.controls_buttons = []

        yield Header()

        yield Horizontal(
            Vertical(Static("[bold]FILES[/bold]"), self.file_list, id="files_box"),
            Vertical(Static("[bold]QUEUE[/bold]"), self.queue_list, id="queue_box"),
            Vertical(Static("[bold]PREVIEW[/bold]"), self.preview_panel, id="preview_box"),
            # put the buttons in a box so it's better to use the touch screen
            Vertical(Static("[bold]CONTROLS[/bold]"), id="controls_box"),
        )

        yield self.status_bar

        yield Footer()

    def on_mount(self):
        player.idle(SLATE_FILE)
        self.set_interval(30, self.load_files)
        # status updates once per second
        self.set_interval(1, self.update_status_bar)
        self.set_interval(0.5, self.check_remote_trigger)

        # Start touchscreen thread (only once). This thread will run on console
        # systems where a PenMount device is present. It will push touch events
        # into _TOUCH_QUEUE which we poll from the main thread. If no PenMount
        # device is present, we will not enable touch support.
        global _thread_started
        if not _thread_started:
            dev = _find_penmount_event()
            if dev is None:
                try:
                    print("[touch] PenMount device not found; console touch disabled", flush=True)
                except Exception:
                    pass
            else:
                t = threading.Thread(target=_touchscreen_thread_main, args=(dev,), daemon=True)
                t.start()
                _thread_started = True

        # Poll the touch queue at a short interval so handling runs on the main thread
        self.set_interval(0.05, self._process_touch_queue)

        # build controls (top-down) inside controls_box
        labels = ["▲ UP", "▼ DOWN", "PLAY", "ENTER", "BREAK", "CLEAR", "SLATE"]
        box = self.query_one("#controls_box")
        for lbl in labels:
            w = Static(lbl, classes="control")
            box.mount(w)
            self.controls_buttons.append(w)

        # track file selection for ENTER/UP/DOWN
        self.selected_index = 0
        self.load_files()

    def check_remote_trigger(self):

        if os.path.exists("/tmp/playnow"):

            os.remove("/tmp/playnow")

            self.trigger_play()


    def load_files(self):
        # preserve selected index across reloads
        try:
            prev = self.selected_index
        except Exception:
            prev = 0

        files = [f for f in sorted(os.listdir(MEDIA_DIR)) if f.endswith(".mov")]
        self.file_list.clear()
        for i, f in enumerate(files):
            item = ListItem(Static(f))
            item.filename = f
            item._file_index = i
            self.file_list.append(item)

        if files:
            # clamp
            self.selected_index = max(0, min(prev, len(files) - 1))
        else:
            self.selected_index = 0
        self._refresh_file_selection()

    def _refresh_file_selection(self):
        # visually mark the selected file
        items = [c for c in self.file_list.children]
        for i, item in enumerate(items):
            try:
                widget = item.children[0]
                if i == self.selected_index:
                    widget.update(f"[reverse]{item.filename}[/reverse]")
                else:
                    widget.update(item.filename)
            except Exception:
                pass

    def on_list_view_selected(self, event):
        # keep selected_index in sync if user selects via keyboard/mouse
        try:
            # event.item may have ._file_index
            idx = getattr(event.item, "_file_index", None)
            if idx is not None:
                self.selected_index = idx
                self._refresh_file_selection()
        except Exception:
            pass

        # ENTER behavior previously queued when selecting; keep same behavior
        self.queue.append(event.item.filename)
        self.update_queue()
        if len(self.queue) == 1:
            self.arm_next()

    def update_queue(self):
        self.queue_list.clear()
        for f in self.queue:
            if f == "BREAK":
                self.queue_list.append(ListItem(Static("[red]--- BREAK ---[/red]")))
            else:
                self.queue_list.append(ListItem(Static(f)))

    def arm_next(self):
        if not self.queue:
            self.preview_panel.update("Slate Active")
            return

        if self.queue[0] == "BREAK":
            self.preview_panel.update("BREAK")
            self.now_playing.update("⏸ BREAK - Waiting")
            return

        f = self.queue[0]
        self.preview_panel.update(f"[cyan]{f}[/cyan]")
        self.now_playing.update(f"[yellow]Ready: {f}[/yellow]")

    # 🔥 5 SECOND HOLD
    def play_next_auto(self):
        if not self.queue or self.queue[0] == "BREAK":

            def delayed_idle():
                time.sleep(5)
                player.idle(SLATE_FILE)

            threading.Thread(target=delayed_idle, daemon=True).start()
            self.arm_next()
            return

        self.trigger_play()

    def trigger_play(self):
        if not self.queue:
            return

        if self.queue[0] == "BREAK":
            self.queue.pop(0)
            self.update_queue()
            self.arm_next()
            return

        f = self.queue.pop(0)
        fp = os.path.join(MEDIA_DIR, f)

        self.now_playing.update(f"Now Playing: {f}")
        self.start_time = time.time()
        self.current_duration = get_duration(fp)

        player.play(fp, self.play_next_auto)
        self.update_queue()

    def key_p(self):
        self.trigger_play()

    def key_1(self):
        self.trigger_play()

    def key_b(self):
        self.queue.append("BREAK")
        self.update_queue()

    def key_s(self):
        player.idle(SLATE_FILE)

    def key_c(self):
        self.queue.clear()
        self.update_queue()
        player.idle(SLATE_FILE)

    # New control actions
    def control_up(self):
        items = [c for c in self.file_list.children]
        if not items:
            return
        self.selected_index = max(0, self.selected_index - 1)
        self._refresh_file_selection()

    def control_down(self):
        items = [c for c in self.file_list.children]
        if not items:
            return
        self.selected_index = min(len(items) - 1, self.selected_index + 1)
        self._refresh_file_selection()

    def control_enter(self):
        items = [c for c in self.file_list.children]
        if not items:
            return
        f = items[self.selected_index].filename
        self.queue.append(f)
        self.update_queue()
        if len(self.queue) == 1:
            self.arm_next()

    def control_play(self):
        self.trigger_play()

    def control_break(self):
        self.queue.append("BREAK")
        self.update_queue()

    def control_clear(self):
        self.queue.clear()
        self.update_queue()
        player.idle(SLATE_FILE)

    def control_slate(self):
        player.idle(SLATE_FILE)

    def update_status_bar(self):
        # NOW, REMAIN, CLOCK on one line
        now_text = "Idle"
        try:
            np = self.now_playing.renderable
            # if now_playing contains a simple string we can use text
            now_text = str(np)
        except Exception:
            try:
                now_text = self.now_playing._text
            except Exception:
                now_text = "Idle"

        if self.current_duration <= 0:
            remain = "00:00"
        else:
            remaining = max(0, self.current_duration - (time.time() - self.start_time))
            remain = f"{int(remaining//60):02}:{int(remaining%60):02}"

        clock = datetime.now().strftime("%H:%M:%S")

        self.status_bar.update(f"NOW: {now_text}    REMAIN: {remain}    CLOCK: {clock}")

    def _process_touch_queue(self):
        """Called in the main thread via set_interval. Pops touch events and
        maps them to UI actions by calling existing handlers directly.
        
        Touch events are enqueued as terminal coordinates (col, row) by the
        background thread, so we map those coordinates directly to columns
        and rows of the current terminal size.
        """
        while not _TOUCH_QUEUE.empty():
            try:
                x, y = _TOUCH_QUEUE.get_nowait()
            except queue.Empty:
                break

            # x,y are terminal coordinates (0..cols-1, 0..rows-1)
            try:
                ts_cols, ts_rows = os.get_terminal_size()
                cols = ts_cols
                rows = ts_rows
            except OSError:
                cols, rows = 80, 24

            # clamp coordinates just in case
            try:
                cx = max(0, min(cols - 1, int(x)))
                cy = max(0, min(rows - 1, int(y)))
            except Exception:
                continue

            # Determine column (now four columns for FILES / QUEUE / PREVIEW / CONTROLS horizontally)
            col_index = int((cx / float(cols)) * 4)
            if col_index < 0:
                col_index = 0
            if col_index > 3:
                col_index = 3

            # Determine whether touch is in the top area (lists/previews) or status area.
            # We'll consider the top ~60% of the terminal as the content area and the bottom as status.
            is_status = (cy / float(rows)) > 0.6

            # Map y to an index within list items if appropriate
            if not is_status and col_index == 0:
                # FILES list touched
                items = [c for c in self.file_list.children]
                if not items:
                    continue
                idx = int((cy / float(rows)) * len(items))
                if idx < 0:
                    idx = 0
                if idx >= len(items):
                    idx = len(items) - 1
                item = items[idx]
                # mimic what on_list_view_selected does
                try:
                    self.queue.append(item.filename)
                    self.update_queue()
                    if len(self.queue) == 1:
                        self.arm_next()
                except Exception as e:
                    print(f"[touch] error handling file touch: {e}", flush=True)

            elif not is_status and col_index == 2:
                # PREVIEW touched -> trigger play
                try:
                    self.trigger_play()
                except Exception as e:
                    print(f"[touch] error handling preview touch: {e}", flush=True)

            elif not is_status and col_index == 3:
                # CONTROLS touched -> map to buttons
                controls = self.controls_buttons
                if not controls:
                    continue
                idx = int((cy / float(rows)) * len(controls))
                if idx < 0:
                    idx = 0
                if idx >= len(controls):
                    idx = len(controls) - 1
                try:
                    label = controls[idx].renderable if hasattr(controls[idx], 'renderable') else None
                except Exception:
                    label = None
                try:
                    text = str(controls[idx]._text) if hasattr(controls[idx], '_text') else str(label)
                except Exception:
                    text = ""
                # dispatch
                if text.startswith("▲") or text.strip().endswith("UP"):
                    self.control_up()
                elif text.startswith("▼") or text.strip().endswith("DOWN"):
                    self.control_down()
                elif text.strip() == "PLAY":
                    self.control_play()
                elif text.strip() == "ENTER":
                    self.control_enter()
                elif text.strip() == "BREAK":
                    self.control_break()
                elif text.strip() == "CLEAR":
                    self.control_clear()
                elif text.strip() == "SLATE":
                    self.control_slate()

            else:
                # For queue column or status area, treat as a play command
                try:
                    self.trigger_play()
                except Exception as e:
                    print(f"[touch] error handling generic touch: {e}", flush=True)

    # Keep main entrypoint unchanged


if __name__ == "__main__":
    CartApp().run()
