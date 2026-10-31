from dataclasses import asdict
from pathlib import Path

from .jobs import JobError


def identify_runner(game, profile, identities, confirm):
    def run(log, progress):
        from Thunderstore.thunderstore_identify import identify_all, stamp_identified
        from Utils.mods.copy import resolve_target_staging
        from Utils.operations import acquire_game_operation

        with acquire_game_operation(game, "hexium.mods.identify"):
            staging = Path(resolve_target_staging(game, profile))
            results = identify_all(staging, game.hexium_community, source="hexium", save=False)
            by_name = {result.mod_name: result for result in results}
            for name, namespace in identities.items():
                result = by_name.get(name)
                if result is None or namespace not in result.candidates:
                    raise JobError("hexium_identity_invalid", "Choose an author from the current identification candidates.")
                result.namespace, result.matched = namespace, True
            if confirm:
                for index, result in enumerate(results):
                    progress(index, len(results), f"Identifying {result.mod_name}")
                    if result.matched:
                        stamp_identified(staging / result.mod_name, result)
            progress(1, 1, "Identification complete")
            return {"source": "hexium", "saved": confirm, "results": [asdict(r) for r in results]}
    return run


def share_runner(game, code):
    def run(log, progress):
        from Thunderstore.r2_profile import download_share_profile, import_manifest
        from Utils.config_paths import get_download_cache_dir_for_game
        progress(0, 1, "Downloading Hexium profile")
        profile = download_share_profile(code, get_download_cache_dir_for_game(game.name), source="hexium")
        try:
            manifest = import_manifest(profile, getattr(game, "thunderstore_community", ""),
                                       hexium_community=game.hexium_community)
        except ValueError as exc:
            raise JobError("hexium_community_mismatch", str(exc)) from exc
        progress(1, 1, "Profile ready for import")
        return {"source": "hexium", "path": profile.archive_path,
                "manifest": manifest}
    return run
