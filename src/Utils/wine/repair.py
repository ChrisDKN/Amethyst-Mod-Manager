from __future__ import annotations

import copy
from contextlib import contextmanager
from pathlib import Path


class PrefixRepairError(RuntimeError):
    def __init__(self, name, message, details=None):
        super().__init__(message)
        self.name = name
        self.details = details or {}


def _prefix_scoped(installer):
    from Utils.wine.registry import normalize_pfx

    def run(game, log):
        prefix = game.get_prefix_path()
        if prefix is None:
            log("No Wine prefix is configured.")
            return False
        prefix = normalize_pfx(Path(prefix)).resolve()
        selected = copy.copy(game)
        selected._prefix_path = prefix
        selected.get_prefix_path = lambda: prefix
        return installer(selected, log)
    return run


def health_installers():
    from Utils.wine import proton
    from Utils.wine.protontricks import WINETRICKS_VERB_DEPS, install_winetricks_verb

    def registry(game, log):
        from Utils.bethesda.registry import repair_game_registry_path
        path = game.get_game_path()
        return bool(path and repair_game_registry_path(game, Path(path), log))

    def dotnet(version):
        return lambda game, log: proton.install_dotnet(game, version, log_fn=log)

    def verb(token):
        return lambda game, log: install_winetricks_verb(
            game, token, log_fn=log, strict_prefix=True)

    installers = {
        "vcredist": lambda game, log: proton.install_vcredist(game, log_fn=log),
        "d3dcompiler_47": lambda game, log: proton.install_d3dcompiler_47(
            game, log_fn=log, strict_prefix=True),
        "lavfilters": lambda game, log: proton.repair_lavfilters(
            game, log_fn=log, strict_prefix=True),
        "game_registry": registry,
        **{f"dotnet{version}": dotnet(version) for version in proton.DOTNET_VERSIONS},
        **{token: verb(token) for token in WINETRICKS_VERB_DEPS},
    }
    return {token: _prefix_scoped(installer) for token, installer in installers.items()}


def dependency_installers():
    from Utils.wine import proton

    installers = health_installers()
    installers.pop("game_registry")
    installers["xact"] = _prefix_scoped(
        lambda game, log: proton.install_xact(game, log_fn=log, strict_prefix=True))
    return installers


def prefix_usage(prefix):
    from Utils.processes.game import matching_pids, prefix_markers

    path = Path(prefix)
    markers = set(prefix_markers(path)) | set(prefix_markers(path.resolve()))
    return matching_pids(markers)


@contextmanager
def prefix_repair_operation(game, *, stop=None, require_initialized=True,
                            operation="wine.prefix.repair"):
    from Utils.deployment.locking import coordinated_game_mutation
    from Utils.operations import acquire_resource_operation
    from Utils.wine.registry import normalize_pfx

    prefix = game.get_prefix_path()
    if prefix is None:
        raise PrefixRepairError("wine_prefix_unavailable", "No Wine prefix is configured.")
    prefix = normalize_pfx(Path(prefix))
    resolved = prefix.resolve()
    with coordinated_game_mutation(game, operation):
        with acquire_resource_operation(
                f"wine-prefix:{resolved}", operation, cancel=stop):
            if prefix.resolve() != resolved:
                raise PrefixRepairError("stale_prefix_health", "The prefix target changed while waiting.")
            if not prefix.is_dir() or (require_initialized and (
                    not (prefix / "drive_c").is_dir() or not all(
                        (prefix / name).is_file() for name in ("user.reg", "system.reg")))):
                raise PrefixRepairError("wine_prefix_unavailable", "The selected prefix is not initialized.")
            pids = prefix_usage(prefix)
            if pids is None:
                raise PrefixRepairError("wine_prefix_scan_unavailable", "Could not check whether the prefix is in use.")
            if pids:
                raise PrefixRepairError("wine_prefix_in_use", "Close the game and tools using this prefix first.",
                                        {"pids": sorted(pids)})
            yield resolved
