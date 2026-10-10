from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from urllib.request import Request

from Utils.bethesda.xedit import applications_dir
from Utils.ca_bundle import download_file
from Utils.environment.xdg import host_env
from Utils.wizards.archives import extract_to_dir

DOWNLOAD_URL = "https://votv.dev/patcher_assets/download/YeetPatch-latest-linux.tar"

_TERMINAL = r'''
if command -v konsole >/dev/null 2>&1; then
    exec konsole --nofork -e "$@"
elif command -v gnome-terminal >/dev/null 2>&1; then
    exec gnome-terminal --wait -- "$@"
elif command -v xfce4-terminal >/dev/null 2>&1; then
    exec xfce4-terminal --disable-server -x "$@"
elif command -v xterm >/dev/null 2>&1; then
    exec xterm -e "$@"
else
    echo 'No supported terminal found. Install Konsole, GNOME Terminal, Xfce Terminal or xterm.' >&2
    exit 127
fi
'''

_RUN = r'''
cd -- "$(dirname -- "$1")" || exit 1
bash "$1" update "$2"
result=$?
printf '%s\n' "$result" > "$3"
printf '\nYeetPatch exited with code %s. Press Enter to close.\n' "$result"
read -r _
exit "$result"
'''


def find_game_exe(game) -> Path:
    root = game.get_game_path()
    if root is None:
        raise FileNotFoundError("Game path is not configured.")
    root = Path(root).absolute()
    folders = [root / "WindowsNoEditor", root]
    if root.name.casefold() == "votv":
        folders.insert(0, root.parent)
    for folder in folders:
        exe = folder / "VotV.exe"
        if exe.is_file():
            return exe
    raise FileNotFoundError(f"VotV.exe not found in: {', '.join(map(str, folders))}.")


def run_update(game, log_fn, status_fn) -> int:
    exe = find_game_exe(game)
    dest = applications_dir(game, "YeetPatch").absolute()
    dest.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="run-", dir=dest) as temporary:
        work = Path(temporary)
        archive = work / "YeetPatch-latest-linux.tar"
        status_fn("download")
        log_fn(f"YeetPatch: downloading {DOWNLOAD_URL}")
        download_file(Request(DOWNLOAD_URL, headers={"User-Agent": "Amethyst-Mod-Manager"}),
                      archive)
        extract_to_dir(archive, work / "tool", log_fn=log_fn)
        script = work / "tool" / "YeetPatch.sh"
        if not script.is_file():
            raise FileNotFoundError("YeetPatch.sh was not found in the downloaded archive.")
        result_file = work / "exit-code"
        cmd = ["bash", "-c", _TERMINAL, "yeetpatch-terminal",
               "bash", "-c", _RUN, "yeetpatch-update",
               str(script), str(exe), str(result_file)]
        if Path("/.flatpak-info").is_file():
            cmd = ["flatpak-spawn", "--host", *cmd]
        status_fn("run")
        log_fn(f"YeetPatch: running {script} update {exe}")
        result = subprocess.run(cmd, env=host_env(), capture_output=True, text=True)
        if not result_file.is_file():
            detail = (result.stderr or result.stdout).strip()
            raise RuntimeError(detail or "The terminal closed before YeetPatch finished.")
        return int(result_file.read_text().strip())
