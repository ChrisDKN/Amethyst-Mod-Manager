from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Iterable

from Utils.bethesda.laa import PEFormatError, write_large_address_aware
from .hashes import canonical_hash, hash_bytes, verify_file
from .models import Archive, GameFilePreparation, InstallRequest
from .paths import WabbajackError, safe_name, within


def plan_game_file(archive: Archive, candidates: Iterable[Path], stop=None) -> GameFilePreparation | None:
    source_name = str(archive.state.get("GameFile", archive.name)).replace("\\", "/")
    if not source_name.casefold().endswith(".exe"):
        return None
    try:
        source_hash = canonical_hash(archive.state["Hash"])
    except (KeyError, WabbajackError):
        return None
    if source_hash == archive.key:
        return None
    for source in dict.fromkeys(Path(path) for path in candidates):
        try:
            if not verify_file(source, source_hash, archive.size, stop):
                continue
            with tempfile.TemporaryDirectory(prefix="amethyst-wj-") as temporary:
                target = Path(temporary) / "prepared.exe"
                write_large_address_aware(source, target, stop)
                if verify_file(target, archive.key, archive.size, stop):
                    return GameFilePreparation(source, source_hash, "pe-laa")
        except InterruptedError:
            raise
        except (OSError, PEFormatError):
            continue
    return None


def materialize_game_file(request: InstallRequest, archive: Archive,
                          preparation: GameFilePreparation, stop=None) -> Path:
    if preparation.kind != "pe-laa":
        raise WabbajackError(f"Unsupported game-file preparation: {preparation.kind}")
    if not verify_file(preparation.source, preparation.source_hash, archive.size, stop):
        raise WabbajackError(f"Original game file changed after preflight: {archive.name}")
    name = hash_bytes(archive.key).hex() + "-" + safe_name(archive.name)
    target = within(request.directory, "work/game-files/" + name)
    if verify_file(target, archive.key, archive.size, stop):
        target.chmod(preparation.source.stat().st_mode & 0o777)
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
    return target
