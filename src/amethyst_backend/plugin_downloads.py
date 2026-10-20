from __future__ import annotations

from .jobs import JobCancelled, JobError


def runner(game, plugin_id, provider_id, source, control, revision=None):
    def run(log, progress):
        from contextlib import nullcontext
        from amethyst_plugins.downloads import resolve, transfer
        from Utils.operations import OperationCancelled, acquire_game_operation
        try:
            with (acquire_game_operation(game, "manager_plugins.download") if revision is not None else nullcontext()):
                progress(0, 1, "Resolving plugin download")
                plan = resolve(game, plugin_id, provider_id, source, log, control)
                progress(1, 1, "Resolving plugin download")
                if revision is None:
                    return plan.public()
                if revision != plan.revision:
                    raise JobError("stale_plugin_download", "The provider artifact changed; resolve and review the source again.",
                                   {"plan": plan.public()})
                return transfer(game, plan, log, progress, control)
        except OperationCancelled as exc:
            raise JobCancelled("Plugin download cancelled.") from exc
        except ValueError as exc:
            raise JobError("plugin_download_failed", str(exc)) from exc
    return run
