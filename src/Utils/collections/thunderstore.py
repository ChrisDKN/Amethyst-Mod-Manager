from __future__ import annotations

from pathlib import Path


def download_entries(entries, game, control=None, callbacks=None, *, log_fn=None):
    from Thunderstore.ror2mm_handler import Ror2mmLink
    from Thunderstore.thunderstore_download import download_package
    from Utils.config_paths import get_download_cache_dir_for_game

    log = callbacks.on_log if callbacks is not None else (log_fn or (lambda _m: None))
    downloaded = 0
    failures = []
    total = len(entries)
    dest = get_download_cache_dir_for_game(getattr(game, "name", "") or "")
    for index, entry in enumerate(entries, 1):
        if control is not None and control.stop.is_set():
            break
        full_name = entry.get("full_name") or entry.get("name") or "?"
        if callbacks is not None:
            callbacks.on_status(f"Thunderstore {index}/{total}: {full_name}")
            callbacks.on_progress(float(index - 1) / max(total, 1))
        try:
            link = Ror2mmLink(
                namespace=entry["namespace"], name=entry["package"],
                version=entry["version"],
                community=str(entry.get("community") or ""))
        except (ValueError, KeyError) as exc:
            log(f"[thunderstore] skipping {full_name} - bad package id ({exc})")
            failures.append((full_name, "bad package id"))
            continue
        try:
            result = download_package(
                link, dest_dir=dest,
                expected_size=int(entry.get("file_size") or 0),
                cancel=(control.stop if control is not None else None))
        except Exception as exc:
            log(f"[thunderstore] download failed for {full_name} - {exc}")
            failures.append((full_name, str(exc) or type(exc).__name__))
            continue
        if getattr(result, "cancelled", False):
            break
        if result.success and result.file_path:
            downloaded += 1
        else:
            log(f"[thunderstore] download failed for {full_name} - {result.error}")
            failures.append((full_name, result.error or "download failed"))
    return downloaded, failures


def stamp_meta(game, link, info, installed_name, staging_root, *, log_fn=None) -> bool:
    from Thunderstore.thunderstore_meta import build_meta_from_link, read_meta, write_meta

    log = log_fn or (lambda _message: None)
    try:
        staging = Path(staging_root)
        if not staging.is_dir():
            return False
        meta = build_meta_from_link(link, info)
        if not meta.community:
            try:
                from Thunderstore.thunderstore_download import resolve_communities
                communities = resolve_communities(link)
                meta.community = communities[0] if communities else ""
            except Exception:
                pass
        target = staging / str(installed_name or "")
        if not installed_name or not target.is_dir():
            log(f"[thunderstore] installed folder for {link.full_name} not "
                "found - meta.ini not stamped.")
            return False
        meta_path = target / "meta.ini"
        existing = read_meta(meta_path)
        if existing.package_id and existing.package_id != meta.package_id:
            log(f"[thunderstore] '{target.name}' already carries metadata "
                f"for {existing.package_id} - not overwriting.")
            return False
        write_meta(meta_path, meta)
        log(f"[thunderstore] stamped {meta.full_name} → {target.name}/meta.ini")
        return True
    except Exception as exc:
        log(f"[thunderstore] could not stamp meta.ini: {exc}")
        return False


def install_entries(entries, game, profile_dir, control=None, callbacks=None,
                    ownership=None, *, log_fn=None):
    from Thunderstore.ror2mm_handler import Ror2mmLink
    from Thunderstore.thunderstore_download import download_package, fetch_version_info
    from Utils.config_paths import get_download_cache_dir_for_game
    from Utils.mods.copy import resolve_target_staging
    from Utils.mods.install import install_collection_archive

    log = callbacks.on_log if callbacks is not None else (log_fn or (lambda _m: None))
    order = []
    failures = []
    installed = 0
    total = len(entries)
    dest = get_download_cache_dir_for_game(getattr(game, "name", "") or "")
    try:
        staging_root = Path(resolve_target_staging(game, Path(profile_dir)))
    except Exception as exc:
        log(f"[thunderstore] no staging folder for the import: {exc}")
        return [], 0, [(entry.get("full_name", "?"), str(exc))
                       for entry in entries]

    for index, entry in enumerate(entries, 1):
        if control is not None and control.stop.is_set():
            break
        full_name = entry.get("full_name") or entry.get("name") or "?"
        if callbacks is not None:
            callbacks.on_status(f"Thunderstore {index}/{total}: {full_name}")
            callbacks.on_progress(float(index - 1) / max(total, 1))
        try:
            link = Ror2mmLink(
                namespace=entry["namespace"], name=entry["package"],
                version=entry["version"],
                community=str(entry.get("community") or ""))
        except (ValueError, KeyError) as exc:
            log(f"[thunderstore] skipping {full_name} - bad package id ({exc})")
            failures.append((full_name, "bad package id"))
            continue

        result = download_package(
            link, dest_dir=dest,
            expected_size=int(entry.get("file_size") or 0),
            cancel=(control.stop if control is not None else None))
        if getattr(result, "cancelled", False):
            break
        if not (result.success and result.file_path):
            log(f"[thunderstore] download failed for {full_name} - {result.error}")
            failures.append((full_name, result.error or "download failed"))
            continue

        try:
            info = fetch_version_info(link)
        except Exception:
            info = None
        if not isinstance(info, dict):
            info = {
                "full_version_name": full_name,
                "download_url": link.download_url,
                "size": int(entry.get("file_size") or 0),
            }
        folder = None
        try:
            folder = install_collection_archive(
                str(result.file_path), game, Path(profile_dir), log_fn=log,
                preferred_name=(entry.get("logical") or entry.get("name") or ""),
                prebuilt_meta=None, skip_index_update=False,
                cancel=(control.stop if control is not None else None))
        except Exception as exc:
            log(f"[thunderstore] install failed for {full_name} - {exc}")
        if not folder:
            failures.append((full_name, "install failed"))
            continue

        stamp_meta(game, link, info, folder, staging_root, log_fn=log)
        from Nexus.nexus_meta import set_meta_keys
        set_meta_keys(staging_root / folder / "meta.ini", {
            "rootFolder": "true" if entry.get("root_folder") else "false",
            "collectionOptional": (
                "true" if entry.get("optional") else "false"),
            "collectionPhase": str(int(entry.get("phase") or 0)),
        })
        if ownership is not None:
            ownership.track(folder)
        order.append((int(entry.get("array_index") or 0), folder))
        installed += 1

    if failures:
        log(f"[thunderstore] {len(failures)} package(s) did NOT install:")
        for name, reason in failures:
            log(f"    • {name} - {reason}")
    return order, installed, failures
