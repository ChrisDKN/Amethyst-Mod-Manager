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

# Main-thread dispatcher registered by the GUI on startup.
# Signature: (fn: Callable[[], None]) -> None
# Should schedule fn() to run on the Tkinter main thread.
_main_thread_dispatcher: "Callable[[Callable[[], None]], None] | None" = None


def set_main_thread_dispatcher(fn: "Callable[[Callable[[], None]], None]") -> None:
    """Register a callback that schedules a function on the Tkinter main thread.
    Call this once after the main App window is created, e.g.:
        set_main_thread_dispatcher(app.call_threadsafe)
    """
    global _main_thread_dispatcher
    _main_thread_dispatcher = fn


def _debug_log(msg: str) -> None:
    """Log to app log panel when PORTAL_DEBUG is set."""
    if _DEBUG:
        app_log(f"[portal] {msg}")

_PORTAL_BUS = "org.freedesktop.portal.Desktop"
_PORTAL_PATH = "/org/freedesktop/portal/desktop"
_FILE_CHOOSER_IFACE = "org.freedesktop.portal.FileChooser"
_REQUEST_IFACE = "org.freedesktop.portal.Request"

# Sentinel returned by portal impls to mean "portal worked, user cancelled".
# Distinct from None which means "portal unavailable/failed → try zenity".
_CANCELLED = object()


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


def _run_portal_impl_jeepney(
    title: str,
    parent_window: str,
    *,
    directory: bool = False,
    filters: "list[tuple[str, list[str]]] | None" = None,
) -> "Path | object | None":
    """
    XDG portal file/folder picker using jeepney (pure-Python D-Bus).
    No gi/GLib dependency — works inside AppImage and on any system with
    a session bus.  Runs blocking on the calling thread (no event loop needed).

    Returns the selected Path, _CANCELLED if the user dismissed the dialog,
    or None if the portal is unavailable/failed (caller should try zenity).
    """
    try:
        from jeepney import DBusAddress, MatchRule, new_method_call, MessageType
        from jeepney.io.blocking import open_dbus_connection
    except ImportError as e:
        _debug_log(f"jeepney unavailable: {e}")
        return None

    try:
        conn = open_dbus_connection("SESSION")
    except Exception as e:
        _debug_log(f"D-Bus session connection failed: {e}")
        return None

    try:
        # Check portal version property to confirm FileChooser backend exists
        props_addr = DBusAddress(
            _PORTAL_PATH,
            bus_name=_PORTAL_BUS,
            interface="org.freedesktop.DBus.Properties",
        )
        ver_msg = new_method_call(props_addr, "Get", "ss", (_FILE_CHOOSER_IFACE, "version"))
        ver_reply = conn.send_and_get_reply(ver_msg)
        if ver_reply.header.message_type.name == "error":
            _debug_log("FileChooser interface not available on this portal (no backend)")
            return None

        token = f"amethyst_{uuid.uuid4().hex[:16]}"
        sender = conn.unique_name.lstrip(":").replace(".", "_")
        predicted_handle = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"

        # Build options dict as a{sv}
        options: list[tuple[str, tuple[str, object]]] = [
            ("handle_token", ("s", token)),
        ]
        if directory:
            options.append(("directory", ("b", True)))
        if filters:
            # a(sa(us)) — list of (name, [(0, "*.zip"), ...])
            filter_array = [(label, [(0, p) for p in pats]) for label, pats in filters]
            options.append(("filters", ("a(sa(us))", filter_array)))

        # Subscribe to Response signal on the predicted handle BEFORE calling OpenFile
        # to avoid a race condition where the signal arrives before we start listening.
        # Match rule for the bus daemon — no sender filter since the portal
        # sends Response signals from its unique name, not the well-known name.
        rule = MatchRule(
            type="signal",
            interface=_REQUEST_IFACE,
            member="Response",
            path=predicted_handle,
        )
        bus_addr = DBusAddress("/org/freedesktop/DBus", bus_name="org.freedesktop.DBus", interface="org.freedesktop.DBus")
        add_match_msg = new_method_call(bus_addr, "AddMatch", "s", (rule.serialise(),))
        conn.send_and_get_reply(add_match_msg)

        portal_addr = DBusAddress(_PORTAL_PATH, bus_name=_PORTAL_BUS, interface=_FILE_CHOOSER_IFACE)
        open_msg = new_method_call(
            portal_addr, "OpenFile", "ssa{sv}", (parent_window, title, options)
        )

        with conn.filter(rule) as matches:
            handle_reply = conn.send_and_get_reply(open_msg)
            if handle_reply.header.message_type.name == "error":
                _debug_log(f"OpenFile call failed: {handle_reply.body}")
                return None

            handle_path = handle_reply.body[0] if handle_reply.body else ""
            _debug_log(f"predicted_handle={predicted_handle!r}")
            _debug_log(f"actual handle_path={handle_path!r}")
            if not handle_path:
                _debug_log("No handle path returned from OpenFile")
                return None

            if handle_path != predicted_handle:
                # Re-subscribe on the actual path (shouldn't happen with handle_token)
                _debug_log(f"Handle mismatch: re-subscribing on {handle_path}")
                rule2 = MatchRule(
                    type="signal",
                    interface=_REQUEST_IFACE,
                    member="Response",
                    path=handle_path,
                )
                add_match2 = new_method_call(bus_addr, "AddMatch", "s", (rule2.serialise(),))
                conn.send_and_get_reply(add_match2)
                with conn.filter(rule2) as matches2:
                    _debug_log("Waiting for portal Response signal...")
                    response_msg = conn.recv_until_filtered(matches2)
            else:
                _debug_log("Waiting for portal Response signal...")
                response_msg = conn.recv_until_filtered(matches)

        # Parse response: (u response_code, a{sv} results)
        response_code, results = response_msg.body
        _debug_log(f"Response: code={response_code}, results type={type(results).__name__}, results={results!r}")
        if response_code == 0:
            uris = results.get("uris")
            _debug_log(f"uris entry={uris!r}")
            if uris is not None:
                # jeepney deserialises a{sv} values as (type_str, value) tuples
                uri_list = uris[1] if isinstance(uris, tuple) else uris
                if uri_list:
                    uri = uri_list[0]
                    _debug_log(f"uri={uri!r}")
                    p = _uri_to_path(uri)
                    if p is not None:
                        return p
        return _CANCELLED

    except Exception as e:
        _debug_log(f"jeepney portal exception: {e}")
        for line in traceback.format_exc().splitlines():
            _debug_log(f"  {line}")
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _run_portal_impl_gi(
    title: str,
    parent_window: str,
    *,
    directory: bool = False,
    filters: "list[tuple[str, list[str]]] | None" = None,
) -> "Path | object | None":
    """
    XDG portal file/folder picker using gi (GLib/Gio). Requires python-gobject.
    Falls back to jeepney implementation is preferred; this is kept for
    systems that have gi but not jeepney.
    """
    try:
        from gi.repository import Gio, GLib
    except ImportError as e:
        _debug_log(f"gi unavailable: {e}")
        return None

    result_holder: list = []
    context = GLib.MainContext.new()
    context.push_thread_default()
    try:
        loop = GLib.MainLoop.new(context)
    except Exception:
        context.pop_thread_default()
        raise

    def on_response(_conn, _sender, _path, _iface, _sig, parameters, _data):
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
            result_holder.append(_CANCELLED)
        loop.quit()

    try:
        conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        portal = Gio.DBusProxy.new_sync(
            conn, Gio.DBusProxyFlags.NONE, None,
            _PORTAL_BUS, _PORTAL_PATH, _FILE_CHOOSER_IFACE, None,
        )
        if portal.get_cached_property("version") is None:
            _debug_log("FileChooser interface not available on this portal (no backend)")
            return None

        token = f"amethyst_{uuid.uuid4().hex[:16]}"
        options: dict = {"handle_token": GLib.Variant("s", token)}
        if directory:
            options["directory"] = GLib.Variant("b", True)
        if filters:
            filter_array = [(label, [(0, p) for p in pats]) for label, pats in filters]
            options["filters"] = GLib.Variant("a(sa(us))", filter_array)

        sender = conn.get_unique_name().lstrip(":").replace(".", "_")
        predicted_handle = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
        sub_id = conn.signal_subscribe(
            _PORTAL_BUS, _REQUEST_IFACE, "Response", predicted_handle,
            None, Gio.DBusSignalFlags.NONE, on_response, None,
        )
        handle = portal.call_sync(
            "OpenFile",
            GLib.Variant("(ssa{sv})", (parent_window, title, options)),
            Gio.DBusCallFlags.NONE, -1, None,
        )
        handle_path = handle.get_child_value(0).get_string()
        if not handle_path:
            conn.signal_unsubscribe(sub_id)
            return None
        if handle_path != predicted_handle:
            _debug_log(f"Handle mismatch: re-subscribing on {handle_path}")
            conn.signal_unsubscribe(sub_id)
            conn.signal_subscribe(
                _PORTAL_BUS, _REQUEST_IFACE, "Response", handle_path,
                None, Gio.DBusSignalFlags.NONE, on_response, None,
            )
        loop.run()
    except Exception as e:
        _debug_log(f"gi portal exception: {e}")
        for line in traceback.format_exc().splitlines():
            _debug_log(f"  {line}")
        return None
    finally:
        context.pop_thread_default()

    return result_holder[0] if result_holder else None


def _run_portal_impl(
    title: str,
    parent_window: str,
    *,
    directory: bool = False,
    filters: "list[tuple[str, list[str]]] | None" = None,
) -> "Path | object | None":
    """Try jeepney first (pure-Python, works in AppImage), fall back to gi."""
    result = _run_portal_impl_jeepney(title, parent_window, directory=directory, filters=filters)
    if result is None:
        result = _run_portal_impl_gi(title, parent_window, directory=directory, filters=filters)
    return result


def _run_portal_folder_impl(title: str, parent_window: str) -> "Path | object | None":
    return _run_portal_impl(title, parent_window, directory=True)


def _run_portal_file_impl(title: str, parent_window: str, filters: "list[tuple[str, list[str]]]") -> "Path | object | None":
    return _run_portal_impl(title, parent_window, filters=filters)


def _is_flatpak() -> bool:
    return os.path.exists("/.flatpak-info")


def _zenity_candidates() -> list[list[str]]:
    """Return zenity invocation candidates to try in order."""
    if _is_flatpak():
        # Inside flatpak: try flatpak-spawn --host first (needs org.freedesktop.Flatpak
        # talk-name), then fall back to zenity directly in case it's in the runtime.
        return [["flatpak-spawn", "--host", "zenity"], ["zenity"]]
    return [["zenity"]]


def _run_zenity(args: list[str]) -> subprocess.CompletedProcess[str] | None:
    """Try each zenity candidate with the given args. Returns first successful run or None."""
    for cmd in _zenity_candidates():
        try:
            result = subprocess.run(cmd + args, capture_output=True, text=True)
            return result
        except FileNotFoundError:
            _debug_log(f"zenity not found at: {cmd[0]}")
            continue
    _debug_log("zenity unavailable — install zenity (e.g. sudo pacman -S zenity) for a better file picker")
    return None


def _zenity_folder(title: str) -> Path | object | None:
    result = _run_zenity(["--file-selection", "--directory", f"--title={title}"])
    if result is None:
        return None  # zenity not found
    if result.returncode == 0:
        p = Path(result.stdout.strip())
        if p.is_dir():
            return p
    return _CANCELLED  # zenity ran but user cancelled (or bad path)


def _zenity_file(title: str) -> Path | object | None:
    result = _run_zenity([
        "--file-selection",
        f"--title={title}",
        "--file-filter=Mod Archives (*.zip, *.7z, *.rar, *.tar.gz, *.tar) | *.zip *.7z *.rar *.tar.gz *.tar",
        "--file-filter=All files | *",
    ])
    if result is None:
        return None  # zenity not found
    if result.returncode == 0:
        p = Path(result.stdout.strip())
        if p.is_file():
            return p
    return _CANCELLED  # zenity ran but user cancelled (or bad path)


def _kdialog_folder(title: str) -> Path | object | None:
    """Folder picker via kdialog (KDE). Returns None if kdialog is unavailable."""
    try:
        result = subprocess.run(
            ["kdialog", "--getexistingdirectory", str(Path.home()), "--title", title],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            p = Path(result.stdout.strip())
            if p.is_dir():
                return p
        return _CANCELLED  # kdialog ran but user cancelled (or bad path)
    except FileNotFoundError:
        pass
    return None


_MOD_ARCHIVE_MIMETYPES = "application/zip application/x-7z-compressed application/x-tar"


def _kdialog_file(title: str) -> Path | object | None:
    """File picker via kdialog (KDE). Returns None if kdialog is unavailable."""
    try:
        result = subprocess.run(
            [
                "kdialog", "--getopenfilename", str(Path.home()),
                "*.zip *.7z *.rar *.tar.gz *.tar|Mod Archives (*.zip, *.7z, *.rar, *.tar.gz, *.tar)",
                "--title", title,
            ],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            p = Path(result.stdout.strip())
            if p.is_file():
                return p
        return _CANCELLED  # kdialog ran but user cancelled (or bad path)
    except FileNotFoundError:
        pass
    return None


def _tkinter_folder(title: str) -> Path | None:
    """Last-resort folder picker using tkinter.filedialog.
    Must be dispatched to the main thread if a dispatcher is registered,
    because Tkinter is not thread-safe.
    """
    import threading
    import tkinter.filedialog as fd

    result_holder: list[Path | None] = [None]
    done = threading.Event()

    def _run() -> None:
        try:
            chosen = fd.askdirectory(title=title)
            if chosen:
                p = Path(chosen)
                if p.is_dir():
                    result_holder[0] = p
        except Exception as e:
            _debug_log(f"tkinter folder picker failed: {e}")
        finally:
            done.set()

    dispatcher = _main_thread_dispatcher
    if dispatcher is not None:
        dispatcher(_run)
        done.wait()
    else:
        # No dispatcher — call directly (only safe if already on main thread)
        _run()
    return result_holder[0]


def _tkinter_file(title: str) -> Path | None:
    """Last-resort file picker using tkinter.filedialog.
    Must be dispatched to the main thread if a dispatcher is registered,
    because Tkinter is not thread-safe.
    """
    import threading
    import tkinter.filedialog as fd

    result_holder: list[Path | None] = [None]
    done = threading.Event()

    def _run() -> None:
        try:
            chosen = fd.askopenfilename(
                title=title,
                filetypes=[
                    ("Mod Archives", "*.zip *.7z *.rar *.tar.gz *.tar"),
                    ("All files", "*"),
                ],
            )
            if chosen:
                p = Path(chosen)
                if p.is_file():
                    result_holder[0] = p
        except Exception as e:
            _debug_log(f"tkinter file picker failed: {e}")
        finally:
            done.set()

    dispatcher = _main_thread_dispatcher
    if dispatcher is not None:
        dispatcher(_run)
        done.wait()
    else:
        # No dispatcher — call directly (only safe if already on main thread)
        _run()
    return result_holder[0]


def pick_folder(title: str, callback: Callable[[Path | None], None]) -> None:
    """
    Open a native folder picker via XDG portal (or zenity fallback).
    Runs in a background thread; callback is invoked on the calling thread
    with the selected Path or None.
    """
    def _worker() -> None:
        result = None
        try:
            _debug_log("Trying XDG portal (jeepney/gi)...")
            result = _run_portal_folder_impl(title, "")
        except Exception as e:
            _debug_log(f"Portal raised unexpected exception: {e}")
        if result is _CANCELLED:
            _debug_log("Portal: user cancelled")
            callback(None)
            return
        chosen: Path | None = result if isinstance(result, Path) else None
        if chosen is None:
            _debug_log("Portal unavailable, trying zenity...")
            zenity_result = _zenity_folder(title)
            if zenity_result is _CANCELLED:
                _debug_log("zenity: user cancelled")
                callback(None)
                return
            chosen = zenity_result if isinstance(zenity_result, Path) else None
        if chosen is None:
            _debug_log("zenity unavailable, trying kdialog...")
            kdialog_result = _kdialog_folder(title)
            if kdialog_result is _CANCELLED:
                _debug_log("kdialog: user cancelled")
                callback(None)
                return
            chosen = kdialog_result if isinstance(kdialog_result, Path) else None
        if chosen is None:
            _debug_log("kdialog unavailable, falling back to tkinter picker")
            chosen = _tkinter_folder(title)
        if chosen:
            _debug_log(f"Folder selected: {chosen}")
        callback(chosen)

    threading.Thread(target=_worker, daemon=True).start()


_MOD_ARCHIVE_FILTERS = [
    ("Mod Archives (*.zip, *.7z, *.rar, *.tar.gz, *.tar)", ["*.zip", "*.7z", "*.rar", "*.tar.gz", "*.tar"]),
    ("All files", ["*"]),
]


def _run_file_picker_worker(title: str, filters: list[tuple[str, list[str]]], cb: Callable[[Path | None], None]) -> None:
    """Worker for file picker; runs in background thread."""
    result = None
    try:
        _debug_log("Trying XDG portal (jeepney/gi)...")
        result = _run_portal_file_impl(title, "", filters)
    except Exception as e:
        _debug_log(f"Portal raised unexpected exception: {e}")
    if result is _CANCELLED:
        _debug_log("Portal: user cancelled")
        cb(None)
        return
    chosen: Path | None = result if isinstance(result, Path) else None
    if chosen is None:
        _debug_log("Portal unavailable, trying zenity...")
        zenity_result = _zenity_file(title)
        if zenity_result is _CANCELLED:
            _debug_log("zenity: user cancelled")
            cb(None)
            return
        chosen = zenity_result if isinstance(zenity_result, Path) else None
    if chosen is None:
        _debug_log("zenity unavailable, trying kdialog...")
        kdialog_result = _kdialog_file(title)
        if kdialog_result is _CANCELLED:
            _debug_log("kdialog: user cancelled")
            cb(None)
            return
        chosen = kdialog_result if isinstance(kdialog_result, Path) else None
    if chosen is None:
        _debug_log("kdialog unavailable, falling back to tkinter picker")
        chosen = _tkinter_file(title)
    if chosen:
        _debug_log(f"File selected: {chosen}")
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


_EXE_FILTERS = [
    ("Executables (*.exe)", ["*.exe"]),
    ("All files", ["*"]),
]


def pick_exe_file(title: str, callback: Callable[[Path | None], None]) -> None:
    """Open a native file picker filtered to .exe files via XDG portal.
    Runs in a background thread; callback is invoked with the selected Path or None.
    """
    threading.Thread(
        target=_run_file_picker_worker,
        args=(title, _EXE_FILTERS, callback),
        daemon=True,
    ).start()
