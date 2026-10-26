"""Forven launcher window for Linux desktops (GTK 3).

launcher.sh opens this with the system Python, which ships PyGObject on
GNOME desktops such as Ubuntu; it falls back to its terminal menu when GTK is
missing. Every button runs a launcher.sh subcommand, so the window holds no
service logic of its own. Closing it leaves Forven running.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "launcher.sh"
ICON = ROOT / "packaging" / "linux" / "forven.svg"
LOG_DIR = ROOT / ".tmp" / "logs"
STATUS_SECONDS = 5

CSS = b"""
window { background-color: #0a0a0a; color: #dcdcdc; }
.title { font-family: monospace; font-size: 20px; font-weight: bold; color: #d4a72c; }
.muted { color: #8a8a8a; }
.healthy { color: #66cdaa; }
.warn { color: #d4a72c; }
.bad { color: #fa8072; }
button { background-image: none; background-color: #161616; color: #dcdcdc;
         border: 1px solid #414141; border-radius: 4px; padding: 6px 14px; }
button:hover { background-color: #222222; }
button:disabled { color: #555555; }
button.primary { color: #d4a72c; border-color: #d4a72c; }
textview, textview text { background-color: #111111; color: #c0c0c0; font-family: monospace; }
"""

WARNING = (
    "This interrupts Forven workers and may briefly interrupt trade monitoring. "
    "Open exchange positions are NOT closed. In-flight research recovers on startup."
)


class LauncherWindow(Gtk.Window):
    def __init__(self) -> None:
        super().__init__(title="Forven Launcher")
        self.set_default_size(720, 520)
        self.set_border_width(20)
        if ICON.exists():
            self.set_icon_from_file(str(ICON))
        self.busy = False
        self.action_buttons: list[Gtk.Button] = []

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.add(box)

        title = Gtk.Label(label="FORVEN  /  LAUNCHER", xalign=0)
        title.get_style_context().add_class("title")
        box.pack_start(title, False, False, 0)
        box.pack_start(
            self._muted(
                "Start, reload and restart Forven. Closing this window leaves Forven running."
            ),
            False,
            False,
            0,
        )

        main_row = Gtk.Box(spacing=8)
        for text, args, warn in (
            ("Start Forven", ["start"], None),
            ("Restart Forven", ["restart"], "Restart Forven?"),
            ("Stop Forven", ["stop"], "Stop Forven?"),
        ):
            main_row.pack_start(self._action(text, args, warn), False, False, 0)
        open_button = Gtk.Button(label="Open Forven")
        open_button.get_style_context().add_class("primary")
        open_button.connect("clicked", lambda _b: self.run_action(["open"]))
        main_row.pack_end(open_button, False, False, 0)
        box.pack_start(main_row, False, False, 0)

        grid = Gtk.Grid(column_spacing=16, row_spacing=10)
        grid.attach(self._muted("SERVICES"), 0, 0, 1, 1)
        self.checked = self._muted("Checking current status...")
        grid.attach(self.checked, 1, 0, 2, 1)
        self.state: dict[str, Gtk.Label] = {}
        for row, (key, name, text, args, warn) in enumerate(
            (
                (
                    "Backend",
                    "Backend + API workers",
                    "Reload backend",
                    ["restart-backend"],
                    "Reload the backend?",
                ),
                (
                    "Dashboard",
                    "Dashboard",
                    "Restart dashboard",
                    ["restart-frontend"],
                    None,
                ),
                ("Supervisor", "Supervisor", None, None, None),
            ),
            start=1,
        ):
            grid.attach(Gtk.Label(label=name, xalign=0), 0, row, 1, 1)
            self.state[key] = Gtk.Label(label="Checking...", xalign=0)
            self.state[key].set_size_request(220, -1)
            grid.attach(self.state[key], 1, row, 1, 1)
            if text:
                grid.attach(self._action(text, args, warn), 2, row, 1, 1)
        box.pack_start(grid, False, False, 0)

        self.message = Gtk.Label(
            label="Opening this window does not start or stop trading.", xalign=0
        )
        self.message.set_line_wrap(True)
        self.message.get_style_context().add_class("warn")
        box.pack_start(self.message, False, False, 0)

        log_row = Gtk.Box(spacing=8)
        log_row.pack_start(self._muted("ACTIVITY"), False, False, 0)
        logs_button = Gtk.Button(label="Open logs folder")
        logs_button.connect("clicked", self.open_logs)
        log_row.pack_end(logs_button, False, False, 0)
        box.pack_start(log_row, False, False, 0)

        self.output = Gtk.TextView(
            editable=False, cursor_visible=False, wrap_mode=Gtk.WrapMode.WORD_CHAR
        )
        scroll = Gtk.ScrolledWindow()
        scroll.add(self.output)
        box.pack_start(scroll, True, True, 0)
        box.pack_start(self._muted(str(ROOT)), False, False, 0)

        self.refresh_status()
        GLib.timeout_add_seconds(STATUS_SECONDS, self.refresh_status)

    def _muted(self, text: str) -> Gtk.Label:
        label = Gtk.Label(label=text, xalign=0)
        label.get_style_context().add_class("muted")
        return label

    def _action(self, text: str, args: list[str], warn: str | None) -> Gtk.Button:
        button = Gtk.Button(label=text)
        button.connect("clicked", lambda _b: self.confirm_then_run(args, warn))
        self.action_buttons.append(button)
        return button

    # --- Actions --------------------------------------------------------------

    def confirm_then_run(self, args: list[str], warn: str | None) -> None:
        if warn:
            dialog = Gtk.MessageDialog(
                transient_for=self,
                modal=True,
                message_type=Gtk.MessageType.WARNING,
                buttons=Gtk.ButtonsType.YES_NO,
                text=warn,
            )
            dialog.format_secondary_text(WARNING)
            dialog.set_default_response(Gtk.ResponseType.NO)
            answer = dialog.run()
            dialog.destroy()
            if answer != Gtk.ResponseType.YES:
                return
        self.run_action(["-y", *args])

    def run_action(self, args: list[str]) -> None:
        if args == ["open"]:
            port = os.environ.get("FRONTEND_PORT", "5173")
            webbrowser.open(f"http://localhost:{port}")
            return
        if self.busy:
            return
        self.set_busy(True, "Working... the activity box shows progress.")
        self.append(f"\n$ launcher.sh {' '.join(a for a in args if a != '-y')}\n")

        def work() -> None:
            proc = subprocess.Popen(
                [str(LAUNCHER), *args],
                cwd=ROOT,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                GLib.idle_add(self.append, line)
            code = proc.wait()
            GLib.idle_add(self.finish, code)

        threading.Thread(target=work, daemon=True).start()

    def finish(self, code: int) -> bool:
        if code == 0:
            self.set_busy(False, "Done.")
        else:
            self.set_busy(
                False, "That did not finish cleanly; see the activity box and the logs."
            )
            self.message.get_style_context().add_class("bad")
        self.refresh_status()
        return False

    def set_busy(self, busy: bool, text: str) -> None:
        self.busy = busy
        for button in self.action_buttons:
            button.set_sensitive(not busy)
        self.message.get_style_context().remove_class("bad")
        self.message.set_text(text)

    def append(self, text: str) -> bool:
        buffer = self.output.get_buffer()
        buffer.insert(buffer.get_end_iter(), text)
        self.output.scroll_to_mark(buffer.get_insert(), 0.0, False, 0.0, 1.0)
        return False

    def open_logs(self, _button: Gtk.Button) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(
            ["xdg-open", str(LOG_DIR)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    # --- Status ---------------------------------------------------------------

    def refresh_status(self) -> bool:
        def work() -> None:
            try:
                out = subprocess.run(
                    [str(LAUNCHER), "status"],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    timeout=20,
                ).stdout
            except (OSError, subprocess.SubprocessError):
                out = ""
            GLib.idle_add(self.show_status, out)

        threading.Thread(target=work, daemon=True).start()
        return True

    def show_status(self, out: str) -> bool:
        values: dict[str, str] = {}
        for line in out.splitlines():
            key, sep, value = line.strip().partition(": ")
            if sep:
                values[key.split(" ")[0]] = value.strip()
        for key, label in self.state.items():
            value = values.get(key, "unknown")
            if key == "Supervisor" and value not in ("not running", "unknown"):
                value = f"running (PID {value})"
            label.set_text(value)
            context = label.get_style_context()
            for cls in ("healthy", "warn", "bad"):
                context.remove_class(cls)
            if value == "healthy" or value.startswith("running"):
                context.add_class("healthy")
            elif value in ("stopped", "not running"):
                context.add_class("muted")
            else:
                context.add_class("warn")
        self.checked.set_text(
            "Status checked " + GLib.DateTime.new_now_local().format("%H:%M:%S")
        )
        return False


def main() -> int:
    # Matches forven.desktop, so the dock groups this window under its icon.
    GLib.set_prgname("forven")
    GLib.set_application_name("Forven")
    settings = Gtk.Settings.get_default()
    if settings is not None:
        settings.set_property("gtk-application-prefer-dark-theme", True)
    provider = Gtk.CssProvider()
    provider.load_from_data(CSS)
    Gtk.StyleContext.add_provider_for_screen(
        Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )
    if "--smoke-test" in sys.argv:
        window = LauncherWindow()
        window.destroy()
        print("Launcher window constructed successfully.")
        return 0
    window = LauncherWindow()
    window.connect("destroy", Gtk.main_quit)
    window.show_all()
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
