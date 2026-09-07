from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import struct
import subprocess
import tempfile
import time
import wave
from dataclasses import dataclass
from pathlib import Path

from Utils.atomic_write import write_atomic_text
from .archive_io import extract_bethesda, read_member, records
from .games import nexus_domain, token
from .hashes import file_hash
from .paths import WabbajackError, source_path, within
from .diagnostics import emit, emit_exception

MOD_NAME = "Wabbajack Patched BSAs"
RECIPE = "fnv-vanilla-bsas-1"
PREFIX = f"root/mods/{MOD_NAME}/"
_ARCHIVE_LIMIT = 2 * 1024 ** 3 - 16 * 1024 ** 2
_SOURCES = (
    ("Fallout - Meshes.bsa", "jiqlT0njgBA="),
    ("Fallout - Misc.bsa", "EVhzdbXz0Bo="),
    ("Fallout - Textures.bsa", "BewSSpegvMc="),
    ("Fallout - Textures2.bsa", "QGPu8cxNgIY="),
    ("Fallout - Sound.bsa", "Rjyz0LLmiXA="),
    ("DeadMoney - Sounds.bsa", "hiYjCvwkUhs="),
    ("HonestHearts - Sounds.bsa", "HvpkqQYcq3o="),
    ("LonesomeRoad - Sounds.bsa", "Me926qGNh0c="),
    ("OldWorldBlues - Sounds.bsa", "lgV+M+EuPSw="),
)
_AUDIO_EXCLUSIONS = ("sound/songs/", "sound/fx/mus/", "sound/fx/emt/raintoggle/")


@dataclass(frozen=True)
class Requirement:
    reason: str
    folders: tuple[str, ...] = ()


@dataclass(frozen=True)
class Source:
    name: str
    path: Path
    digest: str
    expanded: int
    audio: bool


def requirement(package):
    if nexus_domain(package.game) != "newvegas":
        return None
    tools = {"vanillabsaspatcherexe", "fnvbsadecompressorexe", "fnvbsadecompressormpi"}
    folders = {d.path.split("/")[0] for d in package.directives
               if len(d.path.split("/")) == 2 and token(Path(d.path).name) in tools}
    if folders:
        return Requirement("The package includes a New Vegas vanilla BSA patcher", tuple(sorted(folders)))
    names = {token(package.name), *(token(p) for p in package.profiles)}
    if names & {"vivanewvegas", "vivanewvegasextended", "mojaveexpress"}:
        return Requirement("This list requires the New Vegas BSA decompression and audio fixes")
    return None


def _stop(stop):
    if stop is not None and stop.is_set():
        raise InterruptedError("BSA setup stopped")


def library_paths(package):
    needed = requirement(package)
    if not needed:
        return {}
    names = {"libvorbis.dll", "libvorbisfile.dll", "ogg.dll"}
    paths = {Path(d.path).name.casefold(): d.path for d in package.directives
             if len(d.path.split("/")) == 2 and d.path.split("/")[0] in needed.folders
             and Path(d.path).name.casefold() in names}
    if paths and paths.keys() != names:
        raise WabbajackError("The bundled BSA patcher's Vorbis libraries are incomplete; this list needs a corrected package including ogg.dll, libvorbis.dll and libvorbisfile.dll")
    return paths


def _audio_size(stream, row, stop):
    first, tail = bytearray(), bytearray()
    def collect(data):
        first.extend(data[:max(0, 4096 - len(first))])
        tail.extend(data)
        del tail[:-65536]
    read_member(stream, row, collect, stop)
    if first[:4] != b"OggS" or len(first) < 27:
        raise WabbajackError(f"Invalid vanilla Vorbis header: {row[0]}")
    start = 27 + first[26]
    packet = first[start:start + 16]
    end = tail.rfind(b"OggS")
    if (len(packet) != 16 or packet[:7] != b"\x01vorbis" or packet[11] not in (1, 2)
            or end < 0 or len(tail) - end < 27 or not tail[end + 5] & 4):
        raise WabbajackError(f"Invalid vanilla Vorbis stream: {row[0]}")
    frames = struct.unpack_from("<Q", tail, end + 6)[0]
    size = frames * packet[11] * 2 + 4096
    if not frames or size >= 1 << 30:
        raise WabbajackError(f"Vanilla audio exceeds the supported conversion size: {row[0]}")
    return size


def sources(request, stop=None, log=None):
    from .verification import verified_read
    roots = {p.resolve() for name, p in request.game_roots.items() if nexus_domain(name) == "newvegas"}
    if len(roots) != 1:
        raise WabbajackError("Configure one original New Vegas game directory for BSA setup")
    root = roots.pop()
    emit(log, "bsa.sources.started", game_root=root,
         expected_archives=len(_SOURCES))
    data = source_path(root, "Data")
    available = {p.name.casefold() for p in data.iterdir()}
    result = []
    for index, (name, expected) in enumerate(_SOURCES):
        _stop(stop)
        if index >= 5 and name.casefold() not in available:
            continue
        try:
            path = source_path(data, name)
        except (OSError, WabbajackError):
            raise WabbajackError(f"BSA setup requires the original Data/{name}; verify the game's files") from None
        digest, expanded, audio = verified_read(path, (RECIPE, expected),
            lambda: _inspect_source(path, expected, stop))
        result.append(Source(name, path, digest, expanded, audio))
        emit(log, "bsa.source.verified", archive=name, path=path,
             hash=digest, expanded_bytes=expanded, audio=audio)
    emit(log, "bsa.sources.completed", archives=len(result),
         expanded_bytes=sum(item.expanded for item in result),
         audio_archives=sum(item.audio for item in result))
    return result


def _inspect_source(path, expected, stop):
    digest = file_hash(path, stop)
    if digest != expected:
        raise WabbajackError(f"{path.name} differs from the supported English vanilla archive. Restore original game files and check the game language before BSA setup.")
    rows = records(path)
    expanded = sum(len(row[1]) + sum(segment[2] for segment in row[2]) for row in rows)
    audio = [row for row in rows if row[0].casefold().endswith(".ogg")
             and not row[0].casefold().startswith(_AUDIO_EXCLUSIONS)]
    with path.open("rb") as stream:
        for row in audio:
            expanded += max(0, _audio_size(stream, row, stop) - sum(s[2] for s in row[2]))
    return digest, expanded + len(rows) * 512, bool(audio)


def probe_audio(log=None):
    exe = shutil.which("ffmpeg")
    if not exe:
        raise WabbajackError("Install FFmpeg with Vorbis decoding and PCM WAV encoding for the required BSA audio fixes")
    for flag, codec in (("-decoders", "vorbis"), ("-encoders", "pcm_s16le")):
        result = subprocess.run([exe, "-hide_banner", flag], capture_output=True, text=True, timeout=15)
        emit(log, "bsa.ffmpeg.probe", executable=exe, flag=flag, codec=codec,
             exit_code=result.returncode, found=any(codec == word for word in result.stdout.split()),
             stderr=result.stderr[-1000:])
        if result.returncode or not any(codec == word for word in result.stdout.split()):
            raise WabbajackError(f"FFmpeg lacks the required {codec} audio codec")
    return exe


def _signature(group):
    data = [(s.name, s.digest) for s in group]
    return RECIPE + ":" + hashlib.sha256(json.dumps(data).encode()).hexdigest()


def groups(items):
    return [items[:2], *([item] for item in items[2:])]


def expected_outputs(items):
    return {PREFIX + item.name: _signature(group) for group in groups(items) for item in group}


def preflight_setup(request, check, stop=None, log=None):
    needed = requirement(request.package)
    if not needed:
        emit(log, "bsa.preflight.skipped")
        return 0, set()
    emit(log, "bsa.preflight.started", reason=needed.reason,
         folders=needed.folders)
    try:
        from .adapters import adapter_for
        library_paths(request.package)
        if not adapter_for(request.package, request.game, log=log).mo2:
            raise WabbajackError("Automatic New Vegas BSA setup requires a supported profile and shared-mods layout")
        if any(d.path.casefold().startswith(f"mods/{MOD_NAME}/".casefold()) for d in request.package.directives):
            raise WabbajackError(f"The authored list uses the reserved mod name {MOD_NAME}; rename it before installation")
        old, completed = {}, {}
        database = request.directory / "state.sqlite"
        if database.is_file():
            with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
                old = {p: (sig, digest) for p, sig, digest in db.execute("SELECT path,signature,authored_hash FROM outputs")}
                completed = {p: (sig, digest) for p, sig, digest in db.execute("SELECT path,signature,actual_hash FROM completed")}
        if (request.directory / "root" / "mods" / MOD_NAME).exists() and not any(p.startswith(PREFIX) for p in old):
            raise WabbajackError(f"Move or rename the existing unowned {MOD_NAME} mod before installing")
        items = sources(request, stop, log)
        if any(item.audio for item in items):
            probe_audio(log)
        reused, staged = set(), set()
        for key, sig in expected_outputs(items).items():
            row = old.get(key)
            path = within(request.directory, key)
            if row and row[0] == sig and path.is_file() and file_hash(path, stop) == row[1]:
                reused.add(key)
            row = completed.get(key)
            path = within(request.directory, "work/bsa-setup/output/" + key.removeprefix(PREFIX))
            if row and row[0] == sig and path.is_file() and file_hash(path, stop) == row[1]:
                staged.add(key)
        pending = [g for g in groups(items) if any(PREFIX + s.name not in reused | staged for s in g)]
        output = sum(s.expanded for g in pending for s in g)
        temporary = max((sum(s.expanded for s in g) for g in pending), default=0)
        publish = sum(s.expanded for s in items if PREFIX + s.name not in reused)
        check("pass", "BSA setup", f"{needed.reason}. Automatically rebuild {len(items)} archives and enable {MOD_NAME} in every selected profile; {len(reused | staged)} verified archives can be reused.")
        emit(log, "bsa.preflight.completed", archives=len(items),
             reusable=len(reused), staged=len(staged), pending_groups=len(pending),
             required_bytes=output + publish + temporary)
        return output + publish + temporary, reused
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        emit_exception(log, "bsa.preflight.failed", exc)
        check("error", "BSA setup", str(exc))
        return 0, set()


def _convert_audio(root, exe, stop, progress, log=None):
    paths = sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.casefold() == ".ogg"
                   and not p.relative_to(root).as_posix().casefold().startswith(_AUDIO_EXCLUSIONS))
    for index, path in enumerate(paths):
        _stop(stop)
        started = time.monotonic()
        emit(log, "bsa.audio.started", source=path,
             index=index + 1, total=len(paths), ffmpeg=exe)
        progress(index, len(paths), path.relative_to(root).as_posix())
        target = path.with_suffix(".wav")
        if target.exists():
            raise WabbajackError(f"BSA audio conversion would overwrite {target.relative_to(root)}")
        temporary = target.with_suffix(".wav.tmp")
        try:
            with tempfile.TemporaryFile() as errors, subprocess.Popen(
                    [exe, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                     "-i", str(path), "-map_metadata", "-1", "-vn", "-c:a", "pcm_s16le",
                     "-threads", "1", "-f", "wav", str(temporary)],
                    stdout=subprocess.DEVNULL, stderr=errors) as process:
                try:
                    deadline = time.monotonic() + 120
                    while True:
                        _stop(stop)
                        if time.monotonic() >= deadline:
                            raise WabbajackError(f"BSA audio conversion timed out: {path.relative_to(root)}")
                        try:
                            code = process.wait(timeout=0.1)
                            break
                        except subprocess.TimeoutExpired:
                            pass
                finally:
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
                errors.seek(0)
                stderr = errors.read().decode("utf-8", "replace")[-4000:]
            if code:
                raise WabbajackError(
                    f"BSA audio conversion failed: {path.relative_to(root)} "
                    f"(FFmpeg exit {code}): {stderr}")
            with wave.open(str(temporary), "rb") as audio:
                if audio.getsampwidth() != 2 or audio.getnchannels() not in (1, 2) or not audio.getnframes():
                    raise WabbajackError(f"Invalid converted BSA audio: {path.name}")
            temporary.replace(target)
            path.unlink()
            emit(log, "bsa.audio.completed", source=path, target=target,
                 bytes=target.stat().st_size,
                 elapsed_seconds=round(time.monotonic() - started, 3))
        finally:
            temporary.unlink(missing_ok=True)
    progress(len(paths), len(paths), "Audio fixes verified")


def _members(root):
    from Utils.bsa.writer import tes4_hash_file, tes4_hash_folder
    files = [p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()]
    files.sort(key=lambda p: (tes4_hash_folder(p.rpartition("/")[0].replace("/", "\\")),
                             tes4_hash_file(p.rpartition("/")[2])))
    return [{"Path": p, "Index": i, "FlipCompression": False} for i, p in enumerate(files)]


def _split_meshes(meshes, misc, stop):
    total = 0
    for item in _members(meshes):
        _stop(stop)
        path = within(meshes, item["Path"])
        size = path.stat().st_size + len(item["Path"].encode("cp1252")) + 64
        if total + size <= _ARCHIVE_LIMIT:
            total += size
            continue
        target = within(misc, item["Path"])
        if target.exists():
            raise WabbajackError(f"Conflicting vanilla mesh: {item['Path']}")
        target.parent.mkdir(parents=True, exist_ok=True)
        path.replace(target)


def run_setup(request, store, desired, stop, progress, log=None):
    needed = requirement(request.package)
    if not needed:
        emit(log, "bsa.setup.skipped")
        return []
    started = time.monotonic()
    emit(log, "bsa.setup.started", reason=needed.reason,
         output_mod=MOD_NAME)
    progress("Preparing vanilla BSAs", 0, 0, "Verifying original game archives")
    items = sources(request, stop, log)
    exe = probe_audio(log) if any(item.audio for item in items) else None
    base = within(store.work, "bsa-setup")
    output = within(base, "output")
    output.mkdir(parents=True, exist_ok=True)
    old = store.outputs()
    for group in groups(items):
        group_started = time.monotonic()
        sig = _signature(group)
        reusable = {}
        for item in group:
            key = PREFIX + item.name
            for candidate, digest in ((within(output, item.name), store.completed(key, sig)),
                    (store.target(key), old.get(key, {}).get("authored_hash") if old.get(key, {}).get("signature") == sig else None)):
                if digest and candidate.is_file() and file_hash(candidate, stop) == digest:
                    reusable[key] = {"source": str(candidate), "authored_hash": digest, "signature": sig}
                    break
        if len(reusable) == len(group):
            desired.update(reusable)
            progress("Preparing vanilla BSAs", 1, 1, "Reusing verified " + ", ".join(s.name for s in group))
            emit(log, "bsa.group.reused", archives=[item.name for item in group],
                 elapsed_seconds=round(time.monotonic() - group_started, 3))
            continue
        emit(log, "bsa.group.started", archives=[item.name for item in group],
             signature=sig)
        scratch = within(base, "extracted")
        if scratch.exists():
            shutil.rmtree(scratch)
        try:
            for item in group:
                extraction_started = time.monotonic()
                root = within(scratch, item.name)
                root.mkdir(parents=True, exist_ok=True)
                excluded = frozenset({"menus/s.txt"}) if item.name == "Fallout - Misc.bsa" else frozenset()
                emit(log, "bsa.archive.extract.started", archive=item.name,
                     source=item.path, target=root, excluded=sorted(excluded))
                extract_bethesda(item.path, root, stop,
                    lambda cur, total: progress("Decompressing vanilla BSAs", cur, total, item.name),
                    excluded_paths=excluded)
                if item.audio:
                    _convert_audio(root, exe, stop,
                        lambda cur, total, detail: progress(
                            "Fixing vanilla BSA audio", cur, total, detail), log)
                emit(log, "bsa.archive.extract.completed", archive=item.name,
                     elapsed_seconds=round(time.monotonic() - extraction_started, 3))
            if len(group) == 2:
                _split_meshes(within(scratch, group[0].name), within(scratch, group[1].name), stop)
            completed = {}
            for item in group:
                rebuild_started = time.monotonic()
                root = within(scratch, item.name)
                members = _members(root)
                with item.path.open("rb") as stream:
                    header = struct.unpack("<4s8I", stream.read(36))
                flags = header[3] & ~4
                file_flags = header[8] & 0x1ff
                if item.name == "Fallout - Misc.bsa":
                    file_flags |= 1
                if file_flags & 8:
                    file_flags |= 16
                    flags |= 16
                state = {"$type": "BSAState", "Version": 104, "ArchiveFlags": flags, "FileFlags": file_flags}
                emit(log, "bsa.archive.rebuild.started", archive=item.name,
                     members=len(members), state=state)
                from Utils.bsa.writer import write_bsa_reconstruction
                target = within(output, item.name)
                progress("Rebuilding vanilla BSAs", 0, len(members), item.name)
                write_bsa_reconstruction(target, root, state=state, file_states=members, cancel=stop,
                    progress=lambda cur, total: progress("Rebuilding vanilla BSAs", cur, total, item.name))
                if target.stat().st_size >= 2 * 1024 ** 3:
                    raise WabbajackError(f"{item.name} exceeds the New Vegas archive size limit")
                digest = file_hash(target, stop)
                key = PREFIX + item.name
                completed[key] = {"source": str(target), "authored_hash": digest, "signature": sig}
                emit(log, "bsa.archive.rebuild.completed", archive=item.name,
                     target=target, bytes=target.stat().st_size, hash=digest,
                     elapsed_seconds=round(time.monotonic() - rebuild_started, 3))
            for item in group:
                if file_hash(item.path, stop) != item.digest:
                    raise WabbajackError(f"Original game archive changed during BSA setup: {item.name}")
            for key, row in completed.items():
                store.record_completed(key, sig, row["authored_hash"])
            store.flush_completed()
            desired.update(completed)
            emit(log, "bsa.group.completed", archives=[item.name for item in group],
                 elapsed_seconds=round(time.monotonic() - group_started, 3))
        finally:
            if scratch.exists():
                shutil.rmtree(scratch)
    meta = within(output, "meta.ini")
    write_atomic_text(meta, "[General]\nrootFolder=false\n")
    desired[PREFIX + "meta.ini"] = {"source": str(meta), "authored_hash": file_hash(meta), "signature": RECIPE}
    for name, path in library_paths(request.package).items():
        row = desired["root/" + path]
        desired[PREFIX + "root/" + name] = {**row, "signature": "bsa-library:" + row["signature"]}
    store.set("pending_bsa_setup", {"recipe": RECIPE, "reason": needed.reason,
        "sources": {item.name: item.digest for item in items}, "mod": MOD_NAME})
    emit(log, "bsa.setup.completed", archives=len(items), output_mod=MOD_NAME,
         elapsed_seconds=round(time.monotonic() - started, 3))
    return [MOD_NAME]
