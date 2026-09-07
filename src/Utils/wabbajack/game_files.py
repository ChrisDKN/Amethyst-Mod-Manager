from __future__ import annotations

import tempfile
import time
from pathlib import Path
from typing import Iterable

from Utils.bethesda.laa import PEFormatError, write_large_address_aware
from .diagnostics import emit
from .hashes import canonical_hash, hash_bytes, verify_file
from .models import Archive, GameFilePreparation, InstallRequest
from .paths import WabbajackError, safe_name, within


def plan_game_file(archive: Archive, candidates: Iterable[Path], stop=None,
                   log=None) -> GameFilePreparation | None:
    started = time.monotonic()
    candidates = list(dict.fromkeys(Path(path) for path in candidates))
    source_name = str(archive.state.get("GameFile", archive.name)).replace("\\", "/")
    emit(log, "game_file.preparation.probe", archive=archive.name,
         source_name=source_name, candidates=candidates,
         expected_size=archive.size, output_hash=archive.key)
    if not source_name.casefold().endswith(".exe"):
        emit(log, "game_file.preparation.skipped", archive=archive.name,
             reason="source is not a PE executable")
        return None
    try:
        source_hash = canonical_hash(archive.state["Hash"])
    except (KeyError, WabbajackError) as exc:
        emit(log, "game_file.preparation.skipped", archive=archive.name,
             reason="source hash is unavailable", exception=str(exc))
        return None
    if source_hash == archive.key:
        emit(log, "game_file.preparation.skipped", archive=archive.name,
             reason="source and output hashes are identical")
        return None
    for source in candidates:
        try:
            if not verify_file(source, source_hash, archive.size, stop):
                emit(log, "game_file.preparation.source_rejected", archive=archive.name,
                     source=source, expected_hash=source_hash)
                continue
            with tempfile.TemporaryDirectory(prefix="amethyst-wj-") as temporary:
                target = Path(temporary) / "prepared.exe"
                write_large_address_aware(source, target, stop)
                if verify_file(target, archive.key, archive.size, stop):
                    emit(log, "game_file.preparation.available", archive=archive.name,
                         source=source, source_hash=source_hash, kind="pe-laa",
                         elapsed_seconds=round(time.monotonic() - started, 3))
                    return GameFilePreparation(source, source_hash, "pe-laa")
        except InterruptedError:
            raise
        except (OSError, PEFormatError) as exc:
            emit(log, "game_file.preparation.source_failed", archive=archive.name,
                 source=source, exception_type=type(exc).__name__, exception=str(exc))
            continue
    emit(log, "game_file.preparation.unavailable", archive=archive.name,
         candidates=len(candidates), elapsed_seconds=round(time.monotonic() - started, 3))
    return None


def materialize_game_file(request: InstallRequest, archive: Archive,
                          preparation: GameFilePreparation, stop=None,
                          log=None) -> Path:
    started = time.monotonic()
    emit(log, "game_file.preparation.started", archive=archive.name,
         source=preparation.source, source_hash=preparation.source_hash,
         output_hash=archive.key, kind=preparation.kind)
    if preparation.kind != "pe-laa":
        raise WabbajackError(f"Unsupported game-file preparation: {preparation.kind}")
    if not verify_file(preparation.source, preparation.source_hash, archive.size, stop):
        raise WabbajackError(f"Original game file changed after preflight: {archive.name}")
    name = hash_bytes(archive.key).hex() + "-" + safe_name(archive.name)
    target = within(request.directory, "work/game-files/" + name)
    if verify_file(target, archive.key, archive.size, stop):
        target.chmod(preparation.source.stat().st_mode & 0o777)
        emit(log, "game_file.preparation.reused", archive=archive.name,
             target=target)
        return target
    try:
        write_large_address_aware(preparation.source, target, stop)
    except InterruptedError:
        raise
    except (OSError, PEFormatError) as exc:
        raise WabbajackError(f"Could not prepare {archive.name}: {exc}") from exc
    if not verify_file(target, archive.key, archive.size, stop):
        target.unlink(missing_ok=True)
        raise WabbajackError(f"Prepared game file failed verification: {archive.name}")
    emit(log, "game_file.preparation.completed", archive=archive.name,
         target=target, bytes=archive.size, hash=archive.key,
         elapsed_seconds=round(time.monotonic() - started, 3))
    return target
