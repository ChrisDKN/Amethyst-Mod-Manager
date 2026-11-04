from Utils.games import install_rules
from .jobs import JobError
from .routing import _prepare, _revision, _snapshot


def get_install_rules(game):
    try:
        return _snapshot(install_rules.load_entries(game))
    except (OSError, ValueError) as exc:
        raise JobError("install_rules_unavailable", "Could not read install rules.",
                       {"reason": str(exc)}) from exc


def set_install_rules(game, revision, rows, *, log_fn=None, progress_fn=None):
    from Utils.operations import acquire_game_operation

    with acquire_game_operation(game, "games.install_rules.set",
                                owner="headless:games.install_rules.set"):
        try:
            current = install_rules.load_entries(game)
            if _revision(current) != revision:
                raise JobError("stale_install_rules", "Install rules changed; get them again.",
                               {"install_rules": _snapshot(current)})
            proposed = _prepare(current, rows, rules_api=install_rules,
                                rule_type=install_rules.InstallRule, kind="install")
            if proposed == current:
                return {"changed": False, "install_rules": _snapshot(current)}
            if progress_fn:
                progress_fn(0, 1, "Saving install rules")
            latest = install_rules.load_entries(game)
            if _revision(latest) != revision:
                raise JobError("stale_install_rules", "Install rules changed; get them again.",
                               {"install_rules": _snapshot(latest)})
            install_rules.save_entries(game, proposed)
        except (OSError, ValueError) as exc:
            raise JobError("install_rules_save_failed", "Could not save install rules.",
                           {"reason": str(exc)}) from exc
        if progress_fn:
            progress_fn(1, 1, "Install rules saved")
        if log_fn:
            log_fn("Saved install rules for future plain-archive installs and reinstalls.")
        return {"changed": True, "install_rules": _snapshot(proposed)}
