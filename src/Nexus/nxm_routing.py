from __future__ import annotations

import configparser
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from Utils.atomic_write import atomic_writer
from Utils.config_paths import get_config_dir
from Utils.environment.xdg import host_env


AMETHYST_HANDLER = "amethyst"


def load_routes() -> dict[str, str]:
    try:
        data = json.loads((get_config_dir() / "nxm_routes.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict) or any(
            not isinstance(k, str) or not isinstance(v, str)
            for k, v in data.items()):
        raise ValueError("Invalid NXM routing settings.")
    return data


def save_route(domain: str, desktop: str | None) -> None:
    domain = domain.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", domain):
        raise ValueError("Enter a Nexus game domain, such as helldivers2.")
    routes = load_routes()
    if desktop == AMETHYST_HANDLER:
        routes[domain] = AMETHYST_HANDLER
    elif desktop:
        handler = read_handler(Path(desktop))
        routes[domain] = str(handler.path)
    else:
        routes.pop(domain, None)
    with atomic_writer(get_config_dir() / "nxm_routes.json") as stream:
        json.dump(routes, stream, indent=2)
        stream.write("\n")


@dataclass(frozen=True)
class ExternalHandler:
    name: str
    path: Path


def read_handler(path: Path, *, require_nxm: bool = False) -> ExternalHandler:
    path = path.expanduser().absolute()
    visible_path = path
    if Path("/.flatpak-info").exists() and str(path).startswith("/usr/"):
        visible_path = Path("/run/host") / path.relative_to("/")
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.read_string(visible_path.read_text(encoding="utf-8"))
    entry = parser["Desktop Entry"]
    name, command = entry.get("Name", path.stem), entry.get("Exec", "")
    if entry.get("Type") != "Application" or entry.get("Hidden", "false").lower() == "true":
        raise ValueError("This launcher is not an available application.")
    if "amethyst" in f"{path.name} {name} {command}".lower():
        raise ValueError("Choose another mod manager, not Amethyst.")
    if not re.search(r"(?<!%)%[uU]", command):
        raise ValueError("This launcher does not accept links. Choose the manager's NXM launcher.")
    if re.search(r"\b(?:xdg-open|gio\s+open)\b", command):
        raise ValueError("Choose a launcher that opens the manager directly.")
    if require_nxm and "x-scheme-handler/nxm" not in entry.get("MimeType", "").split(";"):
        raise ValueError("Not an NXM handler.")
    if str(path).startswith("/run/host/usr/"):
        path = Path("/") / path.relative_to("/run/host")
    return ExternalHandler(name, path)


def discover_handlers() -> list[ExternalHandler]:
    env = host_env()
    flatpak = Path("/.flatpak-info").exists()
    if flatpak:
        roots = [Path.home() / ".local/share", Path("/run/host/usr/local/share"),
                 Path("/run/host/usr/share")]
    else:
        roots = [Path(env.get("XDG_DATA_HOME") or Path.home() / ".local/share")]
        roots.extend(Path(p) for p in env.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share").split(":") if p)
    roots.extend([Path.home() / ".local/share/flatpak/exports/share",
                  Path("/var/lib/flatpak/exports/share")])
    found, seen = [], set()
    for root in dict.fromkeys(roots):
        applications = root / "applications"
        for path in sorted(applications.rglob("*.desktop")):
            desktop_id = str(path.relative_to(applications)).replace("/", "-")
            if desktop_id in seen:
                continue
            seen.add(desktop_id)
            try:
                found.append(read_handler(path, require_nxm=True))
            except (OSError, ValueError, KeyError, configparser.Error):
                continue
    return sorted(found, key=lambda h: (h.name.casefold(), str(h.path)))


def launch_command(desktop: str) -> tuple[list[str], dict[str, str], ExternalHandler]:
    handler = read_handler(Path(desktop))
    flatpak = Path("/.flatpak-info").exists()
    env = host_env()
    # Resolve host tools using the cleaned PATH, outside the AppImage runtime.
    tool = shutil.which("flatpak-spawn" if flatpak else "gio", path=env.get("PATH"))
    if not tool:
        raise ValueError("The host's gio launcher is unavailable.")
    command = ([tool, "--host", "--directory=/", "gio"] if flatpak else [tool])
    return [*command, "launch", str(handler.path)], env, handler
