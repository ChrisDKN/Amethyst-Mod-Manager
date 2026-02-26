"""
portal_filechooser.py
XDG Desktop Portal file/folder chooser for Flatpak and modern Linux desktops.

Uses org.freedesktop.portal.FileChooser. Falls back to zenity when the portal
is unavailable (e.g. headless, older systems).
"""

from __future__ import annotations

import os
import subprocess
import threading
import traceback
import uuid
from pathlib import Path
from typing import Callable

from Utils.app_log import app_log

_DEBUG = 1


def _debug_log(msg: str) -> None:
    """Log to app log panel when PORTAL_DEBUG is set."""
    if _DEBUG:
        app_log(f"[portal] {msg}")

_PORTAL_BUS = "org.freedesktop.portal.Desktop"
_PORTAL_PATH = "/org/freedesktop/portal/desktop"
_FILE_CHOOSER_IFACE = "org.freedesktop.portal.FileChooser"
_REQUEST_IFACE = "org.freedesktop.portal.Request"


def _uri_to_path(uri: str) -> Path | None:
    """Convert file:// URI to Path. Returns None if not a file URI."""
    if not uri.startswith("file://"):
        return None
    path_str = uri[7:]  # strip "file://"
    # URI may be percent-encoded
    if "%" in path_str:
        import urllib.parse
        path_str = urllib.parse.unquote(path_str)
    return Path(path_str)


def _run_portal_folder_impl(title: str, parent_window: str) -> Path | None:
    """
    Run the portal folder picker. Must be called from a thread that can run
    a GLib main loop (not the main Tkinter thread).
    Returns the selected folder or None.
    """
    try:
        from gi.repository import Gio, GLib
    except ImportError as e:
        _debug_log(f"ImportError: {e}")
        return None

    result_holder: list[Path | None] = []
    # Use thread-default context so D-Bus signals are delivered to our loop
    context = GLib.MainContext.new()
    context.push_thread_default()
    try:
        loop = GLib.MainLoop.new(context)
    except Exception:
        context.pop_thread_default()
        raise

    def on_response(
        _connection: Gio.DBusConnection,
        _sender_name: str,
        _object_path: str,
        _interface_name: str,
        _signal_name: str,
        parameters: GLib.Variant,
        _user_data: object,
    ) -> None:
        response = parameters.get_child_value(0).get_uint32()
        results = parameters.get_child_value(1)
        _debug_log(f"Response: code={response}")
        if response == 0:
            uris = results.lookup_value("uris", None)
            if uris is not None and uris.n_children() > 0:
                uri = uris.get_child_value(0).get_string()
                if uri:
                    result_holder.append(_uri_to_path(uri))
        if not result_holder:
            result_holder.append(None)
        loop.quit()

    try:
        _debug_log("Connecting to session bus...")
        conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        portal = Gio.DBusProxy.new_sync(
            conn,
            Gio.DBusProxyFlags.NONE,
            None,
            _PORTAL_BUS,
            _PORTAL_PATH,
            _FILE_CHOOSER_IFACE,
            None,
        )

        token = f"amethyst_{uuid.uuid4().hex[:16]}"
        options: dict[str, GLib.Variant] = {
            "directory": GLib.Variant("b", True),
            "handle_token": GLib.Variant("s", token),
        }

        handle = portal.call_sync(
            "OpenFile",
            GLib.Variant("(ssa{sv})", (parent_window, title, options)),
            Gio.DBusCallFlags.NONE,
            -1,
            None,
        )
        handle_path = handle.get_child_value(0).get_string()
        if not handle_path:
            _debug_log("No handle path returned")
            return None

        _debug_log(f"Subscribing to Response on {handle_path}")
        # Subscribe to Response signal before the portal processes (avoid race)
        conn.signal_subscribe(
            _PORTAL_BUS,
            _REQUEST_IFACE,
            "Response",
            handle_path,
            None,
            Gio.DBusSignalFlags.NONE,
            on_response,
            None,
        )
        _debug_log("Running main loop, waiting for user...")
        loop.run()
    except Exception as e:
        _debug_log(f"Exception: {e}")
        for line in traceback.format_exc().splitlines():
            _debug_log(f"  {line}")
        return None
    finally:
        context.pop_thread_default()

    return result_holder[0] if result_holder else None


def _run_portal_file_impl(title: str, parent_window: str, filters: list[tuple[str, list[str]]]) -> Path | None:
    """
    Run the portal file picker. Must be called from a thread that can run
    a GLib main loop. Returns the selected file or None.
    filters: [(label, ["*.zip", "*.7z", ...]), ...]
    """
    try:
        from gi.repository import Gio, GLib
    except ImportError as e:
        _debug_log(f"ImportError: {e}")
        return None

    result_holder: list[Path | None] = []
    context = GLib.MainContext.new()
    context.push_thread_default()
    try:
        loop = GLib.MainLoop.new(context)
    except Exception:
        context.pop_thread_default()
        raise

    def on_response(
        _connection: Gio.DBusConnection,
        _sender_name: str,
        _object_path: str,
        _interface_name: str,
        _signal_name: str,
        parameters: GLib.Variant,
        _user_data: object,
    ) -> None:
        response = parameters.get_child_value(0).get_uint32()
        results = parameters.get_child_value(1)
        if response == 0:
            uris = results.lookup_value("uris", None)
            if uris is not None and uris.n_children() > 0:
                uri = uris.get_child_value(0).get_string()
                if uri:
                    result_holder.append(_uri_to_path(uri))
        if not result_holder:
            result_holder.append(None)
        loop.quit()

    try:
        conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        portal = Gio.DBusProxy.new_sync(
            conn,
            Gio.DBusProxyFlags.NONE,
            None,
            _PORTAL_BUS,
            _PORTAL_PATH,
            _FILE_CHOOSER_IFACE,
            None,
        )

        # filters: a(sa(us)) - list of (name, [(0, "*.zip"), (0, "*.7z"), ...])
        filter_array = []
        for label, patterns in filters:
            filter_array.append((label, [(0, p) for p in patterns]))

        token = f"amethyst_{uuid.uuid4().hex[:16]}"
        options: dict[str, GLib.Variant] = {
            "handle_token": GLib.Variant("s", token),
            "filters": GLib.Variant("a(sa(us))", filter_array),
        }

        handle = portal.call_sync(
            "OpenFile",
            GLib.Variant("(ssa{sv})", (parent_window, title, options)),
            Gio.DBusCallFlags.NONE,
            -1,
            None,
        )
        handle_path = handle.get_child_value(0).get_string()
        if not handle_path:
            return None

        conn.signal_subscribe(
            _PORTAL_BUS,
            _REQUEST_IFACE,
            "Response",
            handle_path,
            None,
            Gio.DBusSignalFlags.NONE,
            on_response,
            None,
        )
        loop.run()
    except Exception as e:
        _debug_log(f"Exception: {e}")
        for line in traceback.format_exc().splitlines():
            _debug_log(f"  {line}")
        return None
    finally:
        context.pop_thread_default()

    return result_holder[0] if result_holder else None


def pick_folder(title: str, callback: Callable[[Path | None], None]) -> None:
    """
    Open a native folder picker via XDG portal (or zenity fallback).
    Runs in a background thread; callback is invoked on the calling thread
    with the selected Path or None.
    """
    def _worker() -> None:
        chosen: Path | None = None
        try:
            chosen = _run_portal_folder_impl(title, "")
        except Exception:
            pass
        if chosen is None:
            try:
                result = subprocess.run(
                    ["zenity", "--file-selection", "--directory", f"--title={title}"],
                    capture_output=True, text=True,
                )
                if result.returncode == 0:
                    p = Path(result.stdout.strip())
                    if p.is_dir():
                        chosen = p
            except FileNotFoundError:
                pass
        callback(chosen)

    threading.Thread(target=_worker, daemon=True).start()


_MOD_ARCHIVE_FILTERS = [
    ("Mod Archives (*.zip, *.7z, *.tar.gz, *.tar)", ["*.zip", "*.7z", "*.tar.gz", "*.tar"]),
    ("All files", ["*"]),
]


def _run_file_picker_worker(title: str, filters: list[tuple[str, list[str]]], cb: Callable[[Path | None], None]) -> None:
    """Worker for file picker; runs in background thread."""
    chosen: Path | None = None
    try:
        chosen = _run_portal_file_impl(title, "", filters)
    except Exception:
        pass
    if chosen is None:
        try:
            result = subprocess.run(
                [
                    "zenity", "--file-selection",
                    f"--title={title}",
                    "--file-filter=Mod Archives (*.zip, *.7z, *.tar.gz, *.tar) | *.zip *.7z *.tar.gz *.tar",
                    "--file-filter=All files | *",
                ],
                capture_output=True, text=True,
            )
            if result.returncode == 0:
                p = Path(result.stdout.strip())
                if p.is_file():
                    chosen = p
        except FileNotFoundError:
            pass
    cb(chosen)


def pick_file(title: str, callback: Callable[[Path | None], None]) -> None:
    """
    Open a native file picker via XDG portal (or zenity fallback).
    Runs in a background thread; callback is invoked with the selected Path or None.
    Caller should schedule callback on main thread if doing Tkinter operations, e.g.:
        pick_file(title, lambda p: self.after(0, lambda: self._on_file_picked(p)))
    """
    filters = _MOD_ARCHIVE_FILTERS
    threading.Thread(
        target=_run_file_picker_worker,
        args=(title, filters, callback),
        daemon=True,
    ).start()
