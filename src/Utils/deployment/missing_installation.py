from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from tempfile import mkdtemp

from Utils.atomic_write import write_atomic, write_atomic_text


RECOVERY_DIR = ".amethyst-deployment-recovery"
current_recovery: ContextVar[MissingInstallation | None] = ContextVar(
    "missing_installation", default=None)


def archive_if_missing(path: Path | None, *artifacts: Path) -> bool:
    recovery = current_recovery.get()
    if recovery is None or path is None or not recovery.contains(path):
        return False
    recovery.archive(*artifacts)
    return True


def path_is_missing(path: Path | None) -> bool:
    if path is None or not path.is_absolute():
        return False
    try:
        path.stat()
    except FileNotFoundError:
        return True
    return False


class MissingInstallation:
    def __init__(self, game, game_root: Path, log_fn):
        from Utils.deployment.shared import RestoreIncompleteError

        self.roots = [game_root]
        context = game.get_deployment_context()
        targets = context.get("instance_write_targets", ())
        mod_data = dict(targets).get("mod data")
        self.mod_data_path = Path(mod_data) if mod_data else None
        profile = context.get("profile_state") or {}
        settings = profile.get("profile_settings") or {}
        configured = context.get("configured_game_path") or settings.get("game_path")
        outer = Path(configured) if configured else game.get_global_game_path()
        if (outer is not None and outer != game_root
                and game_root.is_relative_to(outer) and path_is_missing(outer)):
            self.roots.append(outer)
        prefix = game.get_prefix_path()
        if path_is_missing(prefix):
            self.roots.append(prefix)
        for label, raw in targets or game.get_instance_write_targets():
            if label == "separator deployment" and raw:
                destination = Path(raw)
                if path_is_missing(destination):
                    self.roots.append(destination)
        for root in self.roots:
            if not path_is_missing(root):
                raise RestoreIncompleteError(f"{root} is available again. Run Restore again.")
        self.resolved_roots = [root.resolve() for root in self.roots]
        self.state_path = game._deploy_state_file
        state = self.state_path.read_bytes()
        if not json.loads(state).get("deploy_active"):
            raise RestoreIncompleteError("The deployment changed. Run Restore again.")
        self.staging = game.get_profile_root().resolve(strict=True)
        recovery = self.staging / RECOVERY_DIR
        recovery.mkdir(exist_ok=True)
        if recovery.is_symlink():
            raise RestoreIncompleteError(f"Symlinked recovery directory: {recovery}")
        self.archive_dir = Path(mkdtemp(prefix="missing-installation-", dir=recovery))
        write_atomic(self.archive_dir / "deploy_state.json", state)
        log_fn(f"Missing installation recovery files: {self.archive_dir}")

    @contextmanager
    def activate(self):
        token = current_recovery.set(self)
        try:
            yield
        finally:
            current_recovery.reset(token)

    def contains(self, path: Path) -> bool:
        if not path.is_absolute():
            return False
        if any(path.is_relative_to(root) for root in self.roots):
            return True
        resolved = path.parent.resolve() / path.name
        return any(resolved.is_relative_to(root) for root in self.resolved_roots)

    def archive(self, *paths: Path):
        for path in paths:
            try:
                path.lstat()
            except FileNotFoundError:
                continue
            target = self._archive_path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            path.rename(target)

    def retire_journal(self, path: Path):
        write_atomic(self._archive_path(path), path.read_bytes())
        # An empty journal lets a retry resume restoring the remaining backups.
        write_atomic_text(path, "")

    def _archive_path(self, path: Path) -> Path:
        relative = (path.parent.resolve() / path.name).relative_to(self.staging)
        return self.archive_dir / "staging" / relative

    def finish(self):
        state = json.loads(self.state_path.read_bytes())
        state["deploy_active"] = False
        for key in ("recovery_context", "last_deployed", "last_deploy_mode"):
            state.pop(key, None)
        write_atomic_text(self.state_path, json.dumps(state, indent=2))
