from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


# Run the same scan on the host for Flatpak, without requiring host Python.
_SCRIPT = r'''
own_namespace=$1
own_pid=$2
mode=$3
sig=$4
shift 4

identify() {
    local d=$1 stat args arg script base namespace line
    local -a fields argv namespace_pids
    [[ -O "$d" ]] || return 1
    IFS= read -r stat < "$d/stat" 2>/dev/null || return 1
    read -r -a fields <<< "${stat##*) }"
    [[ ${fields[0]} != Z && ${fields[0]} != X ]] || return 1
    started=${fields[19]}
    [[ $started =~ ^[0-9]+$ ]] || return 1
    mapfile -d '' -t argv < "$d/cmdline" 2>/dev/null || return 1
    [[ ${argv[0]##*/} =~ ^python([0-9]+(\.[0-9]+)*)?$ ]] || return 1
    args=1
    while (( args < ${#argv[@]} )); do
        arg=${argv[args]}
        case "$arg" in
            -u|-B|-E|-s|-S|-I|-O|-OO|-P) ((args+=1));;
            -W|-X) ((args+=2));;
            --) ((args+=1)); break;;
            -*) return 1;;
            *) break;;
        esac
    done
    script=${argv[args]}
    [[ ${script##*/} == run_qt.py ]] || return 1
    if [[ $script == /* ]]; then
        base="$d/root${script%/*}"
    elif [[ $script == */* ]]; then
        base="$d/cwd/${script%/*}"
    else
        base="$d/cwd"
    fi
    [[ -f "$base/gui_qt/app.py" && -f "$base/Nexus/nxm_handler.py" ]] || return 1
    namespace=$(readlink "$d/ns/pid") || return 1
    if [[ $namespace == "$own_namespace" ]]; then
        while IFS= read -r line; do
            if [[ $line == NSpid:* ]]; then
                read -r -a namespace_pids <<< "$line"
                [[ ${namespace_pids[-1]} == "$own_pid" ]] && return 1
                break
            fi
        done < "$d/status"
    fi
    return 0
}

if [[ $mode == scan ]]; then
    [[ -d /proc/self ]] || exit 1
    for d in /proc/[0-9]*; do
        if identify "$d"; then
            printf '%s:%s\n' "${d##*/}" "$started"
        fi
    done
else
    failed=0
    for identity do
        pid=${identity%%:*}
        expected=${identity#*:}
        [[ $pid =~ ^[0-9]+$ && $expected =~ ^[0-9]+$ && $pid -gt 1 ]] || exit 1
        if identify "/proc/$pid" && [[ $started == "$expected" ]]; then
            if ! kill -"$sig" "$pid" 2>/dev/null; then
                if identify "/proc/$pid" && [[ $started == "$expected" ]]; then
                    printf 'Could not terminate Amethyst process %s\n' "$pid" >&2
                    failed=1
                fi
            fi
        fi
    done
    exit "$failed"
fi
'''


@dataclass(frozen=True)
class Instance:
    pid: int
    started: int


def _run(mode: str, instances=(), sig: int = 0) -> str:
    from Utils.environment.xdg import host_env

    command = ["bash", "-c", _SCRIPT, "amethyst-instance-check",
               os.readlink("/proc/self/ns/pid"), str(os.getpid()), mode, str(sig),
               *(f"{item.pid}:{item.started}" for item in instances)]
    if Path("/.flatpak-info").is_file():
        command = ["flatpak-spawn", "--host", *command]
    try:
        result = subprocess.run(command, capture_output=True, text=True,
                                timeout=5, env=host_env())
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"Could not check Amethyst processes: {exc}") from exc
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "Amethyst process check failed")
    return result.stdout


def running_instances() -> list[Instance]:
    return sorted((Instance(*(int(part) for part in line.split(":")))
                   for line in _run("scan").splitlines()), key=lambda item: item.pid)


def terminate_instances(instances: list[Instance]) -> None:
    remaining = set(instances) & set(running_instances())
    for sig, grace in ((15, 3.0), (9, 2.0)):
        if not remaining:
            return
        _run("signal", remaining, sig)
        deadline = time.monotonic() + grace
        while remaining and time.monotonic() < deadline:
            time.sleep(0.1)
            remaining &= set(running_instances())
    if remaining:
        raise RuntimeError("Amethyst processes are still running: "
                           + ", ".join(str(item.pid) for item in remaining))
