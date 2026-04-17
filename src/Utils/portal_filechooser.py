"""
portal_filechooser.py
XDG Desktop Portal file/folder chooser for Flatpak and modern Linux desktops.

Uses org.freedesktop.portal.FileChooser. Falls back to zenity when the portal
is unavailable (e.g. headless, older systems).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import traceback
import uuid
from pathlib import Path
from typing import Callable

from Utils.app_log import app_log

_DEBUG = os.environ.get("PORTAL_DEBUG", "1") not in ("", "0", "false", "False")

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


def _jeepney_portal_call(
    method: str,
    title: str,
    parent_window: str,
    options_extra: "list[tuple[str, tuple[str, object]]]",
) -> "tuple[int, dict] | None":
    """
    Send a FileChooser method call (OpenFile / SaveFile) via jeepney and wait
    for the Response signal. Returns (response_code, results_dict) on success,
    or None if the portal is unavailable/failed.
    """
    try:
        from jeepney import DBusAddress, MatchRule, new_method_call
        from jeepney.io.blocking import open_dbus_connection
        from jeepney.low_level import HeaderFields
    except ImportError as e:
        _debug_log(f"jeepney unavailable: {e}")
        return None

    try:
        conn = open_dbus_connection("SESSION")
    except Exception as e:
        _debug_log(f"D-Bus session connection failed: {e}")
        return None

    try:
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

        options: list[tuple[str, tuple[str, object]]] = [("handle_token", ("s", token))]
        options.extend(options_extra)

        # Register a broad match on the whole Request object-path namespace
        # BEFORE calling the portal method. This avoids a race where the
        # Response arrives before we'd otherwise subscribe (important on a
        # mismatched handle path, since we can't predict it).
        rule = MatchRule(
            type="signal",
            interface=_REQUEST_IFACE,
            member="Response",
            path_namespace="/org/freedesktop/portal/desktop/request",
        )
        bus_addr = DBusAddress(
            "/org/freedesktop/DBus",
            bus_name="org.freedesktop.DBus",
            interface="org.freedesktop.DBus",
        )
        add_match_msg = new_method_call(bus_addr, "AddMatch", "s", (rule.serialise(),))
        conn.send_and_get_reply(add_match_msg)

        portal_addr = DBusAddress(_PORTAL_PATH, bus_name=_PORTAL_BUS, interface=_FILE_CHOOSER_IFACE)
        call_msg = new_method_call(
            portal_addr, method, "ssa{sv}", (parent_window, title, options)
        )

        with conn.filter(rule) as matches:
            handle_reply = conn.send_and_get_reply(call_msg)
            if handle_reply.header.message_type.name == "error":
                _debug_log(f"{method} call failed: {handle_reply.body}")
                return None

            handle_path = handle_reply.body[0] if handle_reply.body else ""
            if not handle_path:
                _debug_log(f"No handle path returned from {method}")
                return None
            if handle_path != predicted_handle:
                _debug_log(f"Handle mismatch: predicted={predicted_handle!r} actual={handle_path!r}")

            # Drain signals until we see one whose object path matches our handle.
            # (The namespace rule may pick up Response signals for other concurrent
            # portal requests — extremely rare, but we filter defensively.)
            while True:
                response_msg = conn.recv_until_filtered(matches)
                msg_path = response_msg.header.fields.get(HeaderFields.path)
                if msg_path == handle_path:
                    break
                _debug_log(f"Ignoring Response for unrelated path {msg_path!r}")

        response_code, results = response_msg.body
        _debug_log(f"{method} Response: code={response_code}")
        return response_code, results

    except Exception as e:
        _debug_log(f"jeepney {method} exception: {e}")
        for line in traceback.format_exc().splitlines():
            _debug_log(f"  {line}")
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _extract_uris(results: dict, multiple: bool) -> "list[Path] | Path | None":
    """Pull the uris out of a portal Response results dict."""
    uris = results.get("uris")
    if uris is None:
        return None
    uri_list = uris[1] if isinstance(uris, tuple) else uris
    if not uri_list:
        return None
    if multiple:
        paths = [p for uri in uri_list if (p := _uri_to_path(uri)) is not None]
        return paths if paths else None
    return _uri_to_path(uri_list[0])


def _run_portal_impl_jeepney(
    title: str,
    parent_window: str,
    *,
    directory: bool = False,
    multiple: bool = False,
    filters: "list[tuple[str, list[str]]] | None" = None,
) -> "list[Path] | Path | object | None":
    """XDG portal file/folder picker using jeepney (pure-Python D-Bus)."""
    options: list[tuple[str, tuple[str, object]]] = []
    if directory:
        options.append(("directory", ("b", True)))
    if multiple:
        options.append(("multiple", ("b", True)))
    if filters:
        filter_array = [(label, [(0, p) for p in pats]) for label, pats in filters]
        options.append(("filters", ("a(sa(us))", filter_array)))

    reply = _jeepney_portal_call("OpenFile", title, parent_window, options)
    if reply is None:
        return None
    response_code, results = reply
    if response_code == 0:
        extracted = _extract_uris(results, multiple)
        if extracted is not None:
            return extracted
    return _CANCELLED


def _run_portal_impl_gi(
    title: str,
    parent_window: str,
    *,
    directory: bool = False,
    multiple: bool = False,
    filters: "list[tuple[str, list[str]]] | None" = None,
) -> "list[Path] | Path | object | None":
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
        try:
            loop = GLib.MainLoop.new(context, False)
        except TypeError:
            loop = GLib.MainLoop(context)
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
                if multiple:
                    paths = [
                        p for i in range(uris.n_children())
                        if (p := _uri_to_path(uris.get_child_value(i).get_string())) is not None
                    ]
                    if paths:
                        result_holder.append(paths)
                else:
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
        if multiple:
            options["multiple"] = GLib.Variant("b", True)
        if filters:
            filter_array = [(label, [(0, p) for p in pats]) for label, pats in filters]
            options["filters"] = GLib.Variant("a(sa(us))", filter_array)

        sender = conn.get_unique_name().lstrip(":").replace(".", "_")
        predicted_handle = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
        sub_id = conn.signal_subscribe(
            _PORTAL_BUS, _REQUEST_IFACE, "Response", predicted_handle,
            None, Gio.DBusSignalFlags.NONE, on_response, None,
        )
        try:
            handle = portal.call_sync(
                "OpenFile",
                GLib.Variant("(ssa{sv})", (parent_window, title, options)),
                Gio.DBusCallFlags.NONE, -1, None,
            )
            handle_path = handle.get_child_value(0).get_string()
            if not handle_path:
                return None
            if handle_path != predicted_handle:
                _debug_log(f"Handle mismatch: re-subscribing on {handle_path}")
                conn.signal_unsubscribe(sub_id)
                sub_id = conn.signal_subscribe(
                    _PORTAL_BUS, _REQUEST_IFACE, "Response", handle_path,
                    None, Gio.DBusSignalFlags.NONE, on_response, None,
                )
            loop.run()
        finally:
            conn.signal_unsubscribe(sub_id)
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
    multiple: bool = False,
    filters: "list[tuple[str, list[str]]] | None" = None,
) -> "list[Path] | Path | object | None":
    """Try jeepney first (pure-Python, works in AppImage), fall back to gi."""
    result = _run_portal_impl_jeepney(title, parent_window, directory=directory, multiple=multiple, filters=filters)
    if result is None:
        result = _run_portal_impl_gi(title, parent_window, directory=directory, multiple=multiple, filters=filters)
    return result


def _run_portal_folder_impl(title: str, parent_window: str) -> "Path | object | None":
    return _run_portal_impl(title, parent_window, directory=True)  # type: ignore[return-value]


def _run_portal_file_impl(title: str, parent_window: str, filters: "list[tuple[str, list[str]]]") -> "Path | object | None":
    return _run_portal_impl(title, parent_window, filters=filters)  # type: ignore[return-value]


def _run_portal_file_impl_multi(title: str, parent_window: str, filters: "list[tuple[str, list[str]]]") -> "list[Path] | object | None":
    return _run_portal_impl(title, parent_window, multiple=True, filters=filters)  # type: ignore[return-value]


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
        if shutil.which(cmd[0]) is None:
            _debug_log(f"zenity not found at: {cmd[0]}")
            continue
        try:
            result = subprocess.run(cmd + args, capture_output=True, text=True)
        except FileNotFoundError:
            _debug_log(f"zenity not found at: {cmd[0]}")
            continue
        # flatpak-spawn returns 127 when the host binary is missing. Treat that
        # as "not found" rather than a zenity result.
        if cmd[0] == "flatpak-spawn" and result.returncode == 127:
            _debug_log("flatpak-spawn: zenity not installed on host")
            continue
        return result
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
    # Exit code 1 with empty stderr = user pressed Cancel. Exit 1 with stderr
    # output (or any other code) usually means zenity failed to start (e.g.
    # D-Bus failure on bare X11/DWM) — fall through so the next picker is tried.
    if result.returncode == 1 and not result.stderr.strip():
        return _CANCELLED
    _debug_log(f"zenity exited with code {result.returncode}: {result.stderr.strip()!r} — falling through to next picker")
    return None


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
    if result.returncode == 1 and not result.stderr.strip():
        return _CANCELLED
    _debug_log(f"zenity exited with code {result.returncode}: {result.stderr.strip()!r} — falling through to next picker")
    return None


def _kdialog_folder(title: str) -> Path | object | None:
    """Folder picker via kdialog (KDE). Returns None if kdialog is unavailable."""
    if shutil.which("kdialog") is None:
        return None
    try:
        result = subprocess.run(
            ["kdialog", "--getexistingdirectory", str(Path.home()), "--title", title],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            p = Path(result.stdout.strip())
            if p.is_dir():
                return p
        if result.returncode == 1 and not result.stderr.strip():
            return _CANCELLED
        _debug_log(f"kdialog exited with code {result.returncode}: {result.stderr.strip()!r} — falling through")
        return None
    except FileNotFoundError:
        pass
    return None


def _kdialog_file(title: str) -> Path | object | None:
    """File picker via kdialog (KDE). Returns None if kdialog is unavailable."""
    if shutil.which("kdialog") is None:
        return None
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
        if result.returncode == 1 and not result.stderr.strip():
            return _CANCELLED
        _debug_log(f"kdialog exited with code {result.returncode}: {result.stderr.strip()!r} — falling through")
        return None
    except FileNotFoundError:
        pass
    return None


def _tkinter_dispatch(fn: "Callable[[], object]", kind: str, empty):
    """Dispatch a tkinter.filedialog call on the main thread. Returns `empty`
    if no main-thread dispatcher is registered (unsafe to call Tk otherwise).
    """
    dispatcher = _main_thread_dispatcher
    if dispatcher is None:
        _debug_log("tkinter picker unavailable: no main-thread dispatcher registered")
        return empty

    result_holder: list = [empty]
    done = threading.Event()

    def _run() -> None:
        try:
            result_holder[0] = fn()
        except Exception as e:
            _debug_log(f"tkinter {kind} picker failed: {e}")
        finally:
            done.set()

    dispatcher(_run)
    done.wait()
    return result_holder[0]


def _tkinter_folder(title: str) -> Path | None:
    """Last-resort folder picker using tkinter.filedialog."""
    import tkinter.filedialog as fd

    def _fn() -> Path | None:
        chosen = fd.askdirectory(title=title)
        if chosen:
            p = Path(chosen)
            if p.is_dir():
                return p
        return None

    return _tkinter_dispatch(_fn, "folder", None)


def _tkinter_file(title: str) -> Path | None:
    """Last-resort file picker using tkinter.filedialog."""
    import tkinter.filedialog as fd

    def _fn() -> Path | None:
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
                return p
        return None

    return _tkinter_dispatch(_fn, "file", None)


def _run_waterfall(
    steps: "list[tuple[str, Callable[[], object]]]",
    result_type: type,
    empty,
    label: str,
):
    """Run a portal→zenity→kdialog→tkinter waterfall.

    steps: list of (name, fn) pairs. Each fn returns either a result of
    `result_type`, the _CANCELLED sentinel, or None (= unavailable, try next).
    The last step is always the tkinter fallback which never returns _CANCELLED.

    Returns the chosen result (typed), or `empty` if the user cancelled or
    every step was unavailable.
    """
    for name, fn in steps:
        _debug_log(f"Trying {name}...")
        try:
            result = fn()
        except Exception as e:
            _debug_log(f"{name} raised unexpected exception: {e}")
            continue
        if result is _CANCELLED:
            _debug_log(f"{name}: user cancelled")
            return empty
        if isinstance(result, result_type):
            _debug_log(f"{name}: {label} selected: {result}")
            return result
        _debug_log(f"{name}: unavailable, falling through")
    return empty


def pick_folder(title: str, callback: Callable[[Path | None], None]) -> None:
    """
    Open a native folder picker via XDG portal (or zenity fallback).
    Runs in a background thread; callback is invoked on the worker thread
    with the selected Path or None.
    """
    def _worker() -> None:
        chosen = _run_waterfall(
            [
                ("XDG portal (jeepney/gi)", lambda: _run_portal_folder_impl(title, "")),
                ("zenity", lambda: _zenity_folder(title)),
                ("kdialog", lambda: _kdialog_folder(title)),
                ("tkinter", lambda: _tkinter_folder(title)),
            ],
            Path, None, "Folder",
        )
        callback(chosen)

    threading.Thread(target=_worker, daemon=True).start()


_MOD_ARCHIVE_FILTERS = [
    ("Mod Archives (*.zip, *.7z, *.rar, *.tar.gz, *.tar)", ["*.zip", "*.7z", "*.rar", "*.tar.gz", "*.tar"]),
    ("All files", ["*"]),
]


def _run_file_picker_worker(title: str, filters: list[tuple[str, list[str]]], cb: Callable[[Path | None], None]) -> None:
    """Worker for file picker; runs in background thread."""
    chosen = _run_waterfall(
        [
            ("XDG portal (jeepney/gi)", lambda: _run_portal_file_impl(title, "", filters)),
            ("zenity", lambda: _zenity_file(title)),
            ("kdialog", lambda: _kdialog_file(title)),
            ("tkinter", lambda: _tkinter_file(title)),
        ],
        Path, None, "File",
    )
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


def _zenity_files(title: str) -> "list[Path] | object | None":
    """Multi-file picker via zenity. Returns list of Paths, _CANCELLED, or None."""
    result = _run_zenity([
        "--file-selection",
        "--multiple",
        "--separator=\n",
        f"--title={title}",
        "--file-filter=Mod Archives (*.zip, *.7z, *.rar, *.tar.gz, *.tar) | *.zip *.7z *.rar *.tar.gz *.tar",
        "--file-filter=All files | *",
    ])
    if result is None:
        return None
    if result.returncode == 0:
        paths = [Path(s) for s in result.stdout.strip().splitlines() if s]
        paths = [p for p in paths if p.is_file()]
        if paths:
            return paths
    if result.returncode == 1 and not result.stderr.strip():
        return _CANCELLED
    _debug_log(f"zenity exited with code {result.returncode}: {result.stderr.strip()!r} — falling through to next picker")
    return None


def _kdialog_files(title: str) -> "list[Path] | object | None":
    """Multi-file picker via kdialog. Returns list of Paths, _CANCELLED, or None."""
    if shutil.which("kdialog") is None:
        return None
    try:
        result = subprocess.run(
            [
                "kdialog", "--getopenfilenames", str(Path.home()),
                "*.zip *.7z *.rar *.tar.gz *.tar|Mod Archives (*.zip, *.7z, *.rar, *.tar.gz, *.tar)",
                "--title", title,
            ],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            # kdialog --getopenfilenames separates paths by newlines
            paths = [Path(s) for s in result.stdout.strip().splitlines() if s]
            paths = [p for p in paths if p.is_file()]
            if paths:
                return paths
        if result.returncode == 1 and not result.stderr.strip():
            return _CANCELLED
        _debug_log(f"kdialog exited with code {result.returncode}: {result.stderr.strip()!r} — falling through")
        return None
    except FileNotFoundError:
        pass
    return None


def _tkinter_files(title: str) -> "list[Path]":
    """Multi-file picker using tkinter.filedialog.askopenfilenames."""
    import tkinter.filedialog as fd

    def _fn() -> list[Path]:
        chosen = fd.askopenfilenames(
            title=title,
            filetypes=[
                ("Mod Archives", "*.zip *.7z *.rar *.tar.gz *.tar"),
                ("All files", "*"),
            ],
        )
        if chosen:
            return [Path(s) for s in chosen if Path(s).is_file()]
        return []

    return _tkinter_dispatch(_fn, "multi-file", [])


def _run_file_picker_worker_multi(title: str, filters: list[tuple[str, list[str]]], cb: "Callable[[list[Path]], None]") -> None:
    """Worker for multi-file picker; runs in background thread."""
    # tkinter fallback returns [] (never None), which the waterfall treats
    # as "unavailable" — so wrap it to keep the empty-list semantics intact.
    def _tkinter_step() -> "list[Path] | None":
        result = _tkinter_files(title)
        return result if result else None

    chosen = _run_waterfall(
        [
            ("XDG portal (jeepney/gi) multi-file", lambda: _run_portal_file_impl_multi(title, "", filters)),
            ("zenity multi-file", lambda: _zenity_files(title)),
            ("kdialog multi-file", lambda: _kdialog_files(title)),
            ("tkinter multi-file", _tkinter_step),
        ],
        list, [], "Files",
    )
    cb(chosen)


def pick_files(title: str, callback: "Callable[[list[Path]], None]") -> None:
    """
    Open a native multi-file picker via XDG portal (or zenity/kdialog/tkinter fallback).
    Runs in a background thread; callback is invoked with a list of selected Paths
    (empty list if the user cancelled or nothing was selected).
    Caller should schedule callback on main thread if doing Tkinter operations, e.g.:
        pick_files(title, lambda ps: self.after(0, lambda: self._on_files_picked(ps)))
    """
    filters = _MOD_ARCHIVE_FILTERS
    threading.Thread(
        target=_run_file_picker_worker_multi,
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


# ---------------------------------------------------------------------------
# Save-file picker
# ---------------------------------------------------------------------------

def _run_portal_save_impl_jeepney(
    title: str,
    parent_window: str,
    *,
    current_name: str = "",
    filters: "list[tuple[str, list[str]]] | None" = None,
) -> "Path | object | None":
    """XDG portal SaveFile picker using jeepney."""
    options: list[tuple[str, tuple[str, object]]] = []
    if current_name:
        options.append(("current_name", ("s", current_name)))
    if filters:
        filter_array = [(label, [(0, p) for p in pats]) for label, pats in filters]
        options.append(("filters", ("a(sa(us))", filter_array)))

    reply = _jeepney_portal_call("SaveFile", title, parent_window, options)
    if reply is None:
        return None
    response_code, results = reply
    if response_code == 0:
        extracted = _extract_uris(results, multiple=False)
        if isinstance(extracted, Path):
            return extracted
    return _CANCELLED


def _zenity_save(title: str, current_name: str) -> "Path | object | None":
    result = _run_zenity([
        "--file-selection",
        "--save",
        "--confirm-overwrite",
        f"--title={title}",
        f"--filename={current_name}",
        "--file-filter=JSON files (*.json) | *.json",
        "--file-filter=All files | *",
    ])
    if result is None:
        return None
    if result.returncode == 0:
        raw = result.stdout.strip()
        if raw:
            p = Path(raw)
            if p.parent.is_dir():
                return p
            _debug_log(f"zenity save returned path with missing parent dir: {p}")
    if result.returncode == 1 and not result.stderr.strip():
        return _CANCELLED
    _debug_log(f"zenity save exited with code {result.returncode}: {result.stderr.strip()!r}")
    return None


def _tkinter_save(title: str, current_name: str, filters: list) -> "Path | None":
    """Last-resort save-file picker using tkinter.filedialog."""
    import tkinter.filedialog as fd

    def _fn() -> Path | None:
        chosen = fd.asksaveasfilename(
            title=title,
            initialfile=current_name,
            defaultextension=".json",
            filetypes=filters or [("JSON files", "*.json"), ("All files", "*.*")],
        )
        if chosen:
            return Path(chosen)
        return None

    return _tkinter_dispatch(_fn, "save", None)


def _run_save_worker(
    title: str,
    current_name: str,
    filters: "list[tuple[str, list[str]]]",
    cb: "Callable[[Path | None], None]",
) -> None:
    tk_filters = [(label, " ".join(pats)) for label, pats in filters]
    chosen = _run_waterfall(
        [
            ("XDG portal SaveFile (jeepney)", lambda: _run_portal_save_impl_jeepney(title, "", current_name=current_name, filters=filters)),
            ("zenity save", lambda: _zenity_save(title, current_name)),
            ("tkinter save", lambda: _tkinter_save(title, current_name, tk_filters)),
        ],
        Path, None, "Save path",
    )
    cb(chosen)


def pick_save_file(
    title: str,
    callback: "Callable[[Path | None], None]",
    *,
    current_name: str = "manifest.json",
    filters: "list[tuple[str, list[str]]] | None" = None,
) -> None:
    """
    Open a native save-file dialog via XDG portal (or zenity/tkinter fallback).
    Runs in a background thread; callback is invoked with the selected Path or None.
    """
    if filters is None:
        filters = [("JSON files (*.json)", ["*.json"]), ("All files", ["*"])]
    threading.Thread(
        target=_run_save_worker,
        args=(title, current_name, filters, callback),
        daemon=True,
    ).start()
