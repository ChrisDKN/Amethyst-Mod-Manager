from __future__ import annotations

import configparser
import hashlib
import json
from pathlib import Path

from .jobs import JobCancelled, JobError
from .mod_versions import _binding, _context, _selected_target
from .nexus import _call
from .quick_update import _selection


ACTIONS = ("endorse", "abstain", "track", "untrack")


def _error(exc):
    try:
        def fail():
            raise exc
        _call(fail)
    except JobError as error:
        return {"name": error.name, "message": str(error), "details": error.details}
    except Exception as error:
        return {"name": "nexus_action_failed", "message": "The Nexus mod action could not be completed.",
                "details": {"exception": type(error).__name__}}


def _identity(row):
    from Nexus.nexus_meta import normalise_game_domain

    if not isinstance(row, dict):
        raise JobError("nexus_response_invalid", "Nexus returned an invalid account state.")
    domain, mod_id = row.get("domain_name"), row.get("mod_id")
    if (not isinstance(domain, str) or not domain or isinstance(mod_id, bool)
            or not isinstance(mod_id, int) or mod_id <= 0):
        raise JobError("nexus_response_invalid", "Nexus returned an invalid mod identity.")
    return normalise_game_domain(domain), mod_id


def _remote(api):
    endorsements, tracked = api.get_endorsements(), api.get_tracked_mods()
    if not isinstance(endorsements, list) or not isinstance(tracked, list):
        raise JobError("nexus_response_invalid", "Nexus account state must contain mod lists.")
    states = {}
    for row in endorsements:
        key = _identity(row)
        status = row.get("status")
        if not isinstance(status, str) or status.lower() not in {"endorsed", "abstained", "undecided"}:
            raise JobError("nexus_response_invalid", "Nexus returned an unknown endorsement state.")
        value = status.lower()
        if key in states and states[key] != value:
            raise JobError("nexus_response_invalid", "Nexus returned conflicting endorsement states.")
        states[key] = value
    return states, {_identity(row) for row in tracked}


def _state(remote, domain, mod_id):
    endorsements, tracked = remote
    return {"endorsement": endorsements.get((domain, mod_id), "undecided"),
            "tracked": (domain, mod_id) in tracked}


def _plan(api, game, profile, mods, log, progress):
    from Utils.mods.metadata import meta_file_identity

    selected, selection_revision = _selection(profile, mods)
    user = api.validate(bypass_cache=True)
    if isinstance(user.user_id, bool) or not isinstance(user.user_id, int) or user.user_id <= 0:
        raise JobError("nexus_response_invalid", "Nexus returned an invalid account identity.")
    remote = _remote(api)
    targets, skipped, failed, by_identity = [], [], [], {}
    for index, mod in enumerate(selected):
        progress(index, len(selected), f"Reading Nexus actions: {mod}")
        try:
            context = _context(game, profile, mod)
            _selected_target(game, context)
            binding = _binding(context)
            owner = {**binding, "revision": context["properties"]["revision"],
                     "path": context["properties"]["path"],
                     "metadata_token": meta_file_identity(context["properties"]["path"]),
                     "cached_endorsed": context["nexus_meta"].endorsed}
            key = (binding["domain"], binding["mod_id"])
            if key not in by_identity:
                target = {**_state(remote, *key), "domain": key[0], "mod_id": key[1],
                          "version": context["nexus_meta"].version, "mods": [], "owners": []}
                by_identity[key] = target
                targets.append(target)
            target = by_identity[key]
            target["mods"].append(mod)
            if not any(owner["path"] == context["properties"]["path"] for owner in target["owners"]):
                target["owners"].append(owner)
        except JobError as exc:
            if exc.name == "mod_version_source_unavailable":
                skipped.append({"mod": mod, "reason": "no_nexus_identity"})
            else:
                failed.append({"mod": mod, "error": _error(exc)})
        except Exception as exc:
            failed.append({"mod": mod, "error": _error(exc)})
    payload = {"profile": profile.name, "user_id": user.user_id, "source": "nexus", "requested_mods": mods,
               "mods": selected, "targets": targets, "skipped": skipped, "failed": failed, "actions": list(ACTIONS)}
    revision = hashlib.sha256(json.dumps({**payload, "selection_revision": selection_revision},
                                        sort_keys=True, ensure_ascii=True).encode()).hexdigest()
    progress(len(selected), len(selected), "Nexus actions ready")
    return {**payload, "revision": revision,
            "counts": {"selected": len(selected), "targets": len(targets), "skipped": len(skipped), "failed": len(failed)}}


def get_runner(session, game, profile, mods):
    def run(log, progress):
        from Utils.operations import acquire_game_operation

        with acquire_game_operation(game, "mods.nexus.get"):
            return _call(lambda: {"nexus_actions": _plan(session.require_api(), game, profile, mods, log, progress)})
    return run


def _matches(state, action):
    if action in {"endorse", "abstain"}:
        return (state["endorsement"] == "endorsed") == (action == "endorse")
    return state["tracked"] == (action == "track")


def _owner_context(game, profile, owner):
    context = _context(game, profile, owner["requested_mod"], owner["revision"])
    _selected_target(game, context)
    return context


def _metadata(path):
    parser = configparser.ConfigParser(allow_no_value=True, strict=False, interpolation=None)
    parser.read_string(path.read_text(encoding="utf-8"))
    return {section: dict(parser.items(section, raw=True)) for section in parser.sections()}


def _save_endorsement(game, profile, owner, endorsed):
    from Nexus.nexus_meta import read_meta, set_meta_keys
    from Utils.atomic_write import write_atomic
    from Utils.mods.metadata import meta_file_identity, meta_file_lock

    context = _owner_context(game, profile, owner)
    path = Path(owner["path"])
    with meta_file_lock(path):
        if (path.is_symlink() or path.parent.is_symlink() or not path.is_file()
                or list(meta_file_identity(path)) != list(owner["metadata_token"])):
            raise JobError("stale_mod_properties", "The mod changed before its endorsement flag could be saved.")
        previous = path.read_bytes()
        expected = _metadata(path)
        expected["General"]["endorsed"] = "true" if endorsed else "false"
        try:
            set_meta_keys(path, {"endorsed": "true" if endorsed else "false"})
            if _metadata(path) != expected or read_meta(path).endorsed != endorsed:
                raise OSError("The endorsement flag could not be verified without changing other metadata.")
        except Exception as exc:
            try:
                if path.is_symlink() or path.parent.is_symlink():
                    raise OSError("The metadata path changed during recovery.")
                if path.read_bytes() != previous:
                    write_atomic(path, previous)
                if path.read_bytes() != previous:
                    raise OSError("Could not verify restored metadata.")
            except Exception as recovery:
                raise JobError("nexus_action_metadata_rollback_failed", "Nexus state changed, but metadata recovery failed.",
                               {"state_persisted": True, "path": str(path), "reason": str(recovery)}) from exc
            raise JobError("nexus_action_metadata_failed", "Nexus state was verified, but its local flag could not be saved.",
                           {"metadata_restored": True, "path": str(path), "reason": str(exc)}) from exc
    return {**_binding(context), "endorsed": endorsed}


def action_runner(session, game, profile, mods, revision, action, cancel):
    def run(log, progress):
        from Utils.deployment.locking import coordinated_game_mutation

        with coordinated_game_mutation(game, "mods.nexus.action"):
            api = session.require_api()
            plan = _call(lambda: _plan(api, game, profile, mods, log, progress))
            if plan["revision"] != revision:
                raise JobError("stale_nexus_actions", "Nexus account state or installed targets changed; read them again.",
                               {"nexus_actions": plan})
            completed, failed = [], list(plan["failed"])
            remaining = [{"domain": row["domain"], "mod_id": row["mod_id"]} for row in plan["targets"]]
            remote_may_have_changed = False

            def summary():
                return {"profile": profile.name, "user_id": plan["user_id"], "action": action, "revision": revision,
                        "completed": completed, "skipped": plan["skipped"], "failed": failed,
                        "remaining": list(remaining), "remote_may_have_changed": remote_may_have_changed,
                        "counts": {"completed": len(completed), "skipped": len(plan["skipped"]),
                                   "failed": len(failed), "remaining": len(remaining)}}

            def checkpoint():
                if cancel.is_set():
                    raise JobCancelled("Nexus mod action cancelled.", {"nexus_actions": summary()})

            checkpoint()
            for index, row in enumerate(plan["targets"]):
                checkpoint()
                identity = {"domain": row["domain"], "mod_id": row["mod_id"]}
                progress(index, len(plan["targets"]), f"Nexus {action}: {', '.join(row['mods'])}")
                phase, requested, verified, cache_results = "prepare", False, False, []
                try:
                    for owner in row["owners"]:
                        _owner_context(game, profile, owner)
                    unchanged = _matches(row, action)
                    if not unchanged:
                        phase, requested, remote_may_have_changed = "request", True, True
                        if action in {"endorse", "abstain"}:
                            _call(lambda: getattr(api, action + "_mod")(row["domain"], row["mod_id"], row["version"]))
                        else:
                            _call(lambda: getattr(api, action + "_mod")(row["domain"], row["mod_id"]))
                    phase = "verify"
                    observed = _call(lambda: _state(_remote(api), row["domain"], row["mod_id"]))
                    if not _matches(observed, action):
                        raise JobError("nexus_action_unverified", "Nexus did not confirm the requested account state.",
                                       {"observed": observed})
                    verified = True
                    phase = "metadata"
                    if action in {"endorse", "abstain"}:
                        for owner in row["owners"]:
                            cache_results.append(_save_endorsement(game, profile, owner, action == "endorse"))
                    completed.append({**identity, "mods": row["mods"], **observed,
                                      "unchanged": unchanged, "metadata": cache_results})
                    remaining.remove(identity)
                except Exception as exc:
                    error = _error(exc)
                    failed.append({**identity, "mods": row["mods"], "phase": phase, "request_attempted": requested,
                                   "remote_verified": verified, "metadata": cache_results, "error": error})
                    remaining.remove(identity)
                    if error["name"] in {"nexus_not_authenticated", "nexus_auth_expired", "nexus_auth_unavailable",
                                         "nexus_response_invalid",
                                         "nexus_rate_limited", "nexus_action_metadata_rollback_failed"}:
                        break
            checkpoint()
            result = summary()
            progress(len(plan["targets"]) - len(remaining), len(plan["targets"]), "Nexus actions finished")
            if failed:
                raise JobError("nexus_actions_incomplete", "Some Nexus mod actions could not be completed.",
                               {"nexus_actions": result, "state_persisted": remote_may_have_changed
                                or any(item.get("metadata") for item in completed + failed)
                                or any(item["error"]["details"].get("state_persisted") for item in failed)})
            return {"nexus_actions": result}
    return run
