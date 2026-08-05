import os
import subprocess
import time
import threading
from datetime import datetime

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


if __name__ == "__main__":
    CartApp().run()
