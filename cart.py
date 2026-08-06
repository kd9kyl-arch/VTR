import os
import subprocess
import time
import threading
from datetime import datetime
import struct
import queue

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
# and looks for a device named "PenMount". When a BTN_LEFT press is
# observed, it enqueues the current ABS_X/ABS_Y to be handled in the
# main Textual thread. We avoid generating synthetic mouse events; instead
# the main thread maps the terminal coordinates to on-screen controls and
# calls the existing handlers directly.

# Linux input event constants
_EV_KEY = 0x01
_EV_ABS = 0x03
_BTN_LEFT = 0x110  # 272
_ABS_X = 0x00
_ABS_Y = 0x01

# input_event struct: struct timeval (tv_sec long, tv_usec long), unsigned short type, unsigned short code, int value
_INPUT_EVENT_FORMAT = 'llHHi'
_INPUT_EVENT_SIZE = struct.calcsize(_INPUT_EVENT_FORMAT)

_TOUCH_QUEUE = queue.Queue()


def _find_penmount_event():
    """Parse /proc/bus/input/devices to locate an event device whose Name contains PenMount.
    Returns the /dev/input/eventX path or None."""
    try:
        with open('/proc/bus/input/devices', 'r', encoding='utf-8', errors='ignore') as f:
            data = f.read()
    except Exception:
        return None

    # blocks separated by blank lines
    blocks = data.split('\n\n')
    for block in blocks:
        lines = block.splitlines()
        name = None
        handlers = None
        for line in lines:
            line = line.strip()
            if line.startswith('N:') and 'Name=' in line:
                # N: Name="PenMount ..."
                try:
                    name = line.split('Name=')[1].strip().strip('"')
                except Exception:
                    name = line
            if line.startswith('H:') and 'Handlers=' in line:
                try:
                    handlers = line.split('Handlers=')[1].strip()
                except Exception:
                    handlers = line
        if name and 'PenMount' in name and handlers:
            # find token like event2
            for tok in handlers.split():
                if tok.startswith('event'):
                    path = os.path.join('/dev/input', tok)
                    if os.path.exists(path):
                        return path
    return None


def _touchscreen_thread_main():
    """Thread that searches for PenMount device and reads events, pushing presses to _TOUCH_QUEUE."""
    abs_x = None
    abs_y = None
    device_path = None
    fd = None

    while True:
        try:
            if device_path is None:
                device_path = _find_penmount_event()
                if device_path is None:
                    # no device yet; sleep and retry
                    time.sleep(2)
                    continue

            if fd is None:
                try:
                    fd = open(device_path, 'rb')
                except Exception as e:
                    print(f"[touch] failed to open {device_path}: {e}", flush=True)
                    device_path = None
                    time.sleep(2)
                    continue

            data = fd.read(_INPUT_EVENT_SIZE)
            if not data or len(data) < _INPUT_EVENT_SIZE:
                # EOF or short read, device might have been disconnected
                fd.close()
                fd = None
                device_path = None
                abs_x = None
                abs_y = None
                time.sleep(1)
                continue

            tv_sec, tv_usec, etype, code, value = struct.unpack(_INPUT_EVENT_FORMAT, data)

            if etype == _EV_ABS:
                if code == _ABS_X:
                    abs_x = int(value)
                elif code == _ABS_Y:
                    abs_y = int(value)
            elif etype == _EV_KEY and code == _BTN_LEFT:
                # value 1 == press, 0 == release
                if value == 1:
                    # enqueue the latest known coordinates
                    if abs_x is None or abs_y is None:
                        # ignore if position unknown
                        continue
                    # clamp and normalize to 0-1023
                    x = max(0, min(1023, int(abs_x)))
                    y = max(0, min(1023, int(abs_y)))
                    try:
                        _TOUCH_QUEUE.put_nowait((x, y))
                    except Exception:
                        pass
        except Exception as e:
            # Log and reset to attempt recovery
            try:
                print(f"[touch] error: {e}", flush=True)
            except Exception:
                pass
            try:
                if fd:
                    fd.close()
            except Exception:
                pass
            fd = None
            device_path = None
            abs_x = None
            abs_y = None
            time.sleep(1)


# start the thread as a daemon so it won't block program exit
_thread_started = False


# ---------- APP ----------
class CartApp(App):

    CSS = """
    #files_box { border: round cyan; padding: 1; }
    #queue_box { border: round yellow; padding: 1; }
    #preview_box { border: round magenta; padding: 1; }
    #status_box { border: round green; padding: 1; }

    #timer { content-align: center middle; height: 7; }
    #clock { content-align: center middle; height: 10; }
    """

    queue = reactive([])
    current_duration = reactive(0.0)
    start_time = reactive(0.0)

    def compose(self):
        self.file_list = ListView()
        self.queue_list = ListView()
        self.preview_panel = Static("Waiting...")

        self.now_playing = Static("Idle")
        self.timer_display = Static("", id="timer")
        self.clock_display = Static("", id="clock")

        self.colon_on = True

        yield Header()

        yield Horizontal(
            Vertical(Static("[bold]FILES[/bold]"), self.file_list, id="files_box"),
            Vertical(Static("[bold]QUEUE[/bold]"), self.queue_list, id="queue_box"),
            Vertical(Static("[bold]PREVIEW[/bold]"), self.preview_panel, id="preview_box"),
        )

        yield Vertical(
            Static("[bold]STATUS[/bold]"),
            self.now_playing,
            self.timer_display,
            self.clock_display,
            id="status_box"
        )

        yield Footer()

    def on_mount(self):
        player.idle(SLATE_FILE)
        self.set_interval(30, self.load_files)
        self.set_interval(1, self.update_timer)
        self.set_interval(1, self.update_clock)
        self.set_interval(0.5, self.check_remote_trigger)

        # Start touchscreen thread (only once). This thread will run on console
        # systems where a PenMount device is present. It will push touch events
        # into _TOUCH_QUEUE which we poll from the main thread.
        global _thread_started
        if not _thread_started:
            t = threading.Thread(target=_touchscreen_thread_main, daemon=True)
            t.start()
            _thread_started = True

        # Poll the touch queue at a short interval so handling runs on the main thread
        self.set_interval(0.05, self._process_touch_queue)

    def check_remote_trigger(self):

        if os.path.exists("/tmp/playnow"):

            os.remove("/tmp/playnow")

            self.trigger_play()


    def load_files(self):
        self.file_list.clear()
        for f in sorted(os.listdir(MEDIA_DIR)):
            if f.endswith(".mov"):
                item = ListItem(Static(f))
                item.filename = f
                self.file_list.append(item)

    def on_list_view_selected(self, event):
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

    def update_timer(self):
        if self.current_duration <= 0:
            return

        remaining = max(0, self.current_duration - (time.time() - self.start_time))
        t = f"{int(remaining//60):02}:{int(remaining%60):02}"
        self.timer_display.update(render_big(t))

    def update_clock(self):
        now = datetime.now().astimezone()
        t = now.strftime("%H:%M:%S") if self.colon_on else now.strftime("%H %M %S")
        self.colon_on = not self.colon_on
        self.clock_display.update(f"[bold cyan]{render_big(t)}[/bold cyan]")

    def _process_touch_queue(self):
        """Called in the main thread via set_interval. Pops touch events and
        maps them to UI actions by calling existing handlers directly.
        """
        while not _TOUCH_QUEUE.empty():
            try:
                x, y = _TOUCH_QUEUE.get_nowait()
            except queue.Empty:
                break

            # Map touchscreen 0-1023 to terminal coordinates
            try:
                ts_cols, ts_rows = os.get_terminal_size()
                cols = ts_cols
                rows = ts_rows
            except OSError:
                # fallback
                cols, rows = 80, 24

            # Determine column (three columns for FILES / QUEUE / PREVIEW horizontally)
            col_index = int((x / 1023.0) * 3)
            if col_index < 0:
                col_index = 0
            if col_index > 2:
                col_index = 2

            # Determine whether touch is in the top area (lists/previews) or status area.
            # We'll consider the top ~60% of the terminal as the content area and the bottom as status.
            is_status = (y / 1023.0) > 0.6

            # Map y to an index within list items if appropriate
            if not is_status and col_index == 0:
                # FILES list touched
                items = [c for c in self.file_list.children]
                if not items:
                    continue
                idx = int((y / 1023.0) * len(items))
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

            else:
                # For queue column or status area, treat as a play command
                try:
                    self.trigger_play()
                except Exception as e:
                    print(f"[touch] error handling generic touch: {e}", flush=True)

    # Keep main entrypoint unchanged


if __name__ == "__main__":
    CartApp().run()
