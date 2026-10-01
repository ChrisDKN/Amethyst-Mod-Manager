"""Focused regression tests for physical deployment transfer failures.

Run from the source tree with::

    PYTHONPATH=src python3 -m Utils.deployment._selftest -v
"""

from __future__ import annotations

import errno
import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from Utils.deployment import (
    CustomRule,
    LinkMode,
    deploy_core,
    deploy_custom_rules,
    deploy_filemap,
    deploy_root_folder,
    move_to_core,
    restore_data_core,
    restore_custom_rules,
    restore_root_folder,
)
from Utils.deployment.game_root import (
    deploy_filemap_to_root,
    restore_filemap_from_root,
)
from Utils.deployment.shared import _do_link_ex
from Utils.deployment.pipeline import run_deploy_pipeline
from Utils.filegraph.deploy import begin, current, finish
from Utils.filegraph.models import DeployEntry, DeploymentPlan


def _entry(source_root: Path, filename: str = "failed.txt") -> DeployEntry:
    encoded = os.fsencode(filename)
    return DeployEntry(
        candidate_id=1,
        mod_name="Test Mod",
        mod_key="test mod",
        provider_kind="mod",
        target="data",
        destination_key=encoded,
        destination_display=filename,
        source_rel=encoded,
        source_display=filename,
        source_fingerprint=b"fingerprint",
        legacy_root=False,
        legacy_rel=filename,
        source_root=source_root,
    )


class _ProfileSession:
    def __init__(self, plan: DeploymentPlan):
        self.plan = plan
        self.phases: list[str] = []
        self.commits: list[str] = []

    def ensure_reconciled(self, **_kwargs) -> None:
        return None

    def snapshot(self):
        return types.SimpleNamespace(generation=self.plan.generation)

    def begin_deployment(self, _generation: int, _mode: str):
        return "test-transaction", self.plan

    def update_deployment_phase(self, transaction_id: str, phase: str) -> None:
        assert transaction_id == "test-transaction"
        self.phases.append(phase)

    def commit_deployment(self, transaction_id: str) -> None:
        self.commits.append(transaction_id)


class _Library:
    def __init__(self, session: _ProfileSession):
        self.session = session

    def ensure_ready(self, _profile_dir: Path) -> None:
        return None

    def open_profile(self, _profile_dir: Path) -> _ProfileSession:
        return self.session


class _PipelineGame:
    name = "Transfer Failure Test"
    restore_before_deploy = False
    root_folder_deploy_enabled = True
    wine_dll_overrides: dict[str, str] = {}
    mod_folder_strip_prefixes: set[str] = set()

    def __init__(self, root: Path, plan: DeploymentPlan):
        self.root = root
        self.game = root / "game"
        self.profiles = root / "profiles-root"
        self.profile = self.profiles / "profiles" / "default"
        self.staging = self.profiles / "mods"
        self.data = self.game / "Data"
        self.filemap = self.profiles / "filemap.txt"
        self.root_folder = root / "missing-root-folder"
        self.plan = plan
        self.saved_profile = None
        self._active_profile_dir = self.profile
        for directory in (self.game, self.profile, self.staging, self.data):
            directory.mkdir(parents=True, exist_ok=True)
        (self.game / "Game.exe").write_text("launcher", encoding="utf-8")
        self.filemap.write_text("", encoding="utf-8")

    def get_game_path(self) -> Path:
        return self.game

    def get_profile_root(self) -> Path:
        return self.profiles

    def get_last_deployed_profile(self):
        return None

    def set_active_profile_dir(self, path: Path) -> None:
        self._active_profile_dir = Path(path)

    def load_paths(self) -> None:
        return None

    def get_effective_root_folder_path(self) -> Path:
        return self.root_folder

    def get_effective_mod_staging_path(self) -> Path:
        return self.staging

    def get_effective_filemap_path(self) -> Path:
        return self.filemap

    def get_mod_data_path(self) -> Path:
        return self.data

    def get_prefix_path(self):
        return None

    def get_deploy_mode(self) -> LinkMode:
        return LinkMode.HARDLINK

    def begin_deferred_runtime_snapshot(self) -> None:
        return None

    def end_deferred_runtime_snapshot(self):
        return False, []

    def deploy(self, **kwargs) -> None:
        deploy_filemap(
            self.filemap,
            self.data,
            self.staging,
            mode=kwargs["mode"],
            log_fn=kwargs["log_fn"],
        )

    def save_last_deployed_profile(
        self, profile: str, *, deploy_mode: str,
    ) -> None:
        self.saved_profile = profile, deploy_mode

    def post_deploy(self, **_kwargs) -> None:
        return None


class DeploymentTransferFailureTests(unittest.TestCase):
    def _run_pipeline(self, transfer_error: OSError | None):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        mod_root = root / "profiles-root" / "mods" / "Test Mod"
        mod_root.mkdir(parents=True)
        (mod_root / "failed.txt").write_text("payload", encoding="utf-8")
        plan = DeploymentPlan(1, 1, (_entry(mod_root),))
        session = _ProfileSession(plan)
        game = _PipelineGame(root, plan)
        logs: list[str] = []
        finish_results: list[bool] = []

        from Utils.filegraph import deploy as filegraph_deploy
        real_finish = filegraph_deploy.finish

        def recording_finish(*, success: bool) -> None:
            finish_results.append(success)
            real_finish(success=success)

        if transfer_error is None:
            transfer = None
        else:
            transfer = patch(
                "Utils.deployment.standard._do_link_ex",
                return_value=(None, transfer_error),
            )

        mod_files = types.ModuleType("Utils.mods.files")
        mod_files.excluded_raw_by_mod = lambda _profile: {}
        filegraph_service = types.ModuleType("Utils.filegraph.service")
        filegraph_service.FileGraphService = types.SimpleNamespace(
            open_library=lambda *_args, **_kwargs: _Library(session))
        transfer_context = transfer or _NullContext()
        with (
            transfer_context,
            patch.dict("sys.modules", {
                "Utils.mods.files": mod_files,
                "Utils.filegraph.service": filegraph_service,
            }),
            patch("Utils.filegraph.deploy.finish", side_effect=recording_finish),
            patch("Utils.profiles.groups.materialize_if_group"),
            patch("Utils.deployment.pipeline.refresh_filegraph_after_restore"),
            patch("Utils.flatpak.sandbox.ensure_symlink_target_access"),
            patch("Utils.flatpak.sandbox.ensure_launcher_handoff_access"),
            patch("Utils.deployment.pipeline._log_deploy_context"),
            patch("Utils.deployment.pipeline.load_per_mod_strip_prefixes",
                  return_value={}),
            patch("Utils.deployment.pipeline.deploy_root_flagged_mods",
                  return_value=0),
            patch("Utils.launchers.handoff.refresh_launch_handoff_script"),
        ):
            result = run_deploy_pipeline(
                game, "default", log_fn=logs.append, do_backup=False)

        return result, game, session, finish_results, logs

    def test_successful_transfer_commits_filegraph(self) -> None:
        result, game, session, finish_results, logs = self._run_pipeline(None)

        self.assertTrue(result)
        self.assertEqual((game.data / "failed.txt").read_text(), "payload")
        self.assertEqual(finish_results, [True])
        self.assertEqual(session.commits, ["test-transaction"])
        self.assertTrue(any("Deploy finished OK" in line for line in logs))

    def test_standard_transfer_errors_fail_without_success_commit(self) -> None:
        for error_number in (errno.EACCES, errno.EIO, errno.EROFS):
            with self.subTest(errno=error_number):
                result, game, session, finish_results, logs = self._run_pipeline(
                    OSError(error_number, os.strerror(error_number)))

                self.assertFalse(result)
                self.assertFalse((game.data / "failed.txt").exists())
                self.assertEqual(finish_results, [False])
                self.assertEqual(session.commits, [])
                self.assertTrue(any("Deploy FAILED" in line for line in logs))

    def test_enospc_remains_fatal(self) -> None:
        result, game, session, finish_results, _logs = self._run_pipeline(
            OSError(errno.ENOSPC, os.strerror(errno.ENOSPC)))

        self.assertFalse(result)
        self.assertFalse((game.data / "failed.txt").exists())
        self.assertEqual(finish_results, [False])
        self.assertEqual(session.commits, [])

    def test_hardlink_capability_failure_can_fall_back_successfully(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            destination = root / "destination"
            source.write_text("payload", encoding="utf-8")
            with patch("Utils.deployment.shared.os.link",
                       side_effect=OSError(errno.EXDEV, "cross-device link")):
                actual, error = _do_link_ex(
                    str(source), str(destination), LinkMode.HARDLINK)

            self.assertIsNone(error)
            self.assertEqual(actual, LinkMode.SYMLINK)
            self.assertTrue(destination.is_symlink())
            self.assertEqual(destination.read_text(), "payload")

    def test_core_transfer_error_is_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            deploy_dir = root / "Data"
            core_dir = root / "Data_Core"
            deploy_dir.mkdir()
            core_dir.mkdir()
            (core_dir / "vanilla.txt").write_text("vanilla", encoding="utf-8")
            with patch(
                "Utils.deployment.standard._do_link_ex",
                return_value=(None, OSError(errno.EROFS, os.strerror(errno.EROFS))),
            ):
                with self.assertRaises(OSError) as raised:
                    deploy_core(deploy_dir, set(), core_dir=core_dir)

            self.assertEqual(raised.exception.errno, errno.EROFS)
            self.assertFalse((deploy_dir / "vanilla.txt").exists())

    def test_partial_standard_deploy_remains_restoreable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = root / "profile"
            mod_root = profile / "mods" / "Test Mod"
            deploy_dir = root / "Data"
            mod_root.mkdir(parents=True)
            deploy_dir.mkdir()
            (mod_root / "placed.txt").write_text("placed", encoding="utf-8")
            (mod_root / "failed.txt").write_text("failed", encoding="utf-8")
            (deploy_dir / "vanilla.txt").write_text("vanilla", encoding="utf-8")
            move_to_core(deploy_dir)
            plan = DeploymentPlan(
                1, 1,
                (_entry(mod_root, "placed.txt"), _entry(mod_root)),
            )
            session = _ProfileSession(plan)
            begin(session, 1, "hardlink")
            self.addCleanup(
                lambda: finish(success=False) if current() is not None else None)

            real_transfer = _do_link_ex

            def fail_second(src: str, dst: str, mode: LinkMode):
                if dst.endswith("/failed.txt"):
                    return None, OSError(errno.EIO, os.strerror(errno.EIO))
                return real_transfer(src, dst, mode)

            with (
                patch("Utils.deployment.shared._deploy_workers", return_value=1),
                patch("Utils.deployment.standard._do_link_ex",
                      side_effect=fail_second),
            ):
                with self.assertRaises(OSError):
                    deploy_filemap(
                        profile / "filemap.txt",
                        deploy_dir,
                        profile / "mods",
                    )

            self.assertEqual((deploy_dir / "placed.txt").read_text(), "placed")
            self.assertFalse((deploy_dir / "failed.txt").exists())
            self.assertEqual(session.commits, [])

            restored = restore_data_core(deploy_dir)
            self.assertEqual(restored, 1)
            self.assertEqual((deploy_dir / "vanilla.txt").read_text(), "vanilla")
            self.assertFalse((deploy_dir / "placed.txt").exists())

    def test_game_root_transfer_error_is_fatal_and_keeps_recovery_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            game_root = root / "game"
            profile = root / "profile"
            mod_root = profile / "mods" / "Test Mod"
            game_root.mkdir()
            mod_root.mkdir(parents=True)
            (mod_root / "failed.txt").write_text("payload", encoding="utf-8")
            (game_root / "failed.txt").write_text("vanilla", encoding="utf-8")
            plan = DeploymentPlan(1, 1, (_entry(mod_root),))
            session = _ProfileSession(plan)
            begin(session, 1, "hardlink")
            self.addCleanup(
                lambda: finish(success=False) if current() is not None else None)

            with patch(
                "Utils.deployment.game_root._do_link",
                return_value=OSError(errno.EIO, os.strerror(errno.EIO)),
            ):
                with self.assertRaises(OSError) as raised:
                    deploy_filemap_to_root(
                        profile / "filemap.txt",
                        game_root,
                        profile / "mods",
                        log_fn=lambda _message: None,
                        write_snapshot=False,
                    )

            self.assertEqual(raised.exception.errno, errno.EIO)
            self.assertFalse((game_root / "failed.txt").exists())
            self.assertEqual(
                (profile / "filemap_deployed.txt").read_text(), "failed.txt")
            self.assertEqual(
                (profile / "filemap_backup" / "failed.txt").read_text(),
                "vanilla",
            )

            restored = restore_filemap_from_root(
                profile / "filemap.txt", game_root, move_runtime_files=False)
            self.assertEqual(restored, 0)
            self.assertEqual(
                (game_root / "failed.txt").read_text(), "vanilla")

    def test_root_folder_transfer_error_is_fatal_and_restoreable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            root_folder = root / "profile" / "Root_Folder"
            game_root = root / "game"
            root_folder.mkdir(parents=True)
            game_root.mkdir()
            (root_folder / "failed.txt").write_text("mod", encoding="utf-8")
            (game_root / "failed.txt").write_text("vanilla", encoding="utf-8")

            with patch(
                "Utils.deployment.root._do_link_ex",
                return_value=(None, OSError(errno.EACCES, os.strerror(errno.EACCES))),
            ):
                with self.assertRaises(OSError):
                    deploy_root_folder(root_folder, game_root)

            self.assertFalse((game_root / "failed.txt").exists())
            self.assertEqual(
                (root_folder.parent / "root_folder_deployed.txt").read_text(),
                "failed.txt",
            )
            restore_root_folder(root_folder, game_root)
            self.assertEqual(
                (game_root / "failed.txt").read_text(), "vanilla")

    def test_custom_rule_transfer_error_is_fatal_and_restoreable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = root / "profile"
            mod_root = profile / "mods" / "Test Mod"
            game_root = root / "game"
            mod_root.mkdir(parents=True)
            game_root.mkdir()
            (mod_root / "failed.txt").write_text("mod", encoding="utf-8")
            (game_root / "failed.txt").write_text("vanilla", encoding="utf-8")
            session = _ProfileSession(
                DeploymentPlan(1, 1, (_entry(mod_root),)))
            begin(session, 1, "hardlink")
            self.addCleanup(
                lambda: finish(success=False) if current() is not None else None)
            rules = [CustomRule(dest="", filenames=["failed.txt"])]

            with patch(
                "Utils.deployment.custom_rules._do_link",
                return_value=OSError(errno.EIO, os.strerror(errno.EIO)),
            ):
                with self.assertRaises(OSError):
                    deploy_custom_rules(
                        profile / "filemap.txt",
                        game_root,
                        profile / "mods",
                        rules,
                    )

            self.assertFalse((game_root / "failed.txt").exists())
            self.assertEqual(
                (profile / "custom_rules_deployed.txt").read_text(),
                str(game_root / "failed.txt"),
            )
            restore_custom_rules(
                profile / "filemap.txt", game_root, rules)
            self.assertEqual(
                (game_root / "failed.txt").read_text(), "vanilla")

    def test_incremental_transfer_error_is_fatal(self) -> None:
        from Utils.deployment.incremental import IncrementalPlan, apply_incremental

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            deploy_dir = root / "Data"
            core_dir = root / "Data_Core"
            overwrite_dir = root / "overwrite"
            mod_root = root / "mods" / "Test Mod"
            for directory in (deploy_dir, core_dir, overwrite_dir, mod_root):
                directory.mkdir(parents=True)
            entries = {
                name: _entry(mod_root, name)
                for name in ("same-a.txt", "same-b.txt", "failed.txt")
            }
            (mod_root / "failed.txt").write_text("mod", encoding="utf-8")
            old_entries = {
                name: entry for name, entry in entries.items()
                if name != "failed.txt"
            }
            plan = IncrementalPlan(
                game=object(),
                deploy_dir_str=str(deploy_dir),
                core_dir=core_dir,
                state_dir=root,
                mode=LinkMode.HARDLINK,
                old_entries=old_entries,
                deploy_stats={},
                new_entries=entries,
            )
            tasks = [
                (str(mod_root / name), str(deploy_dir / name), name,
                 False, False, None)
                for name in entries
            ]

            with patch(
                "Utils.deployment.incremental._do_link_ex",
                return_value=(None, OSError(errno.EROFS, os.strerror(errno.EROFS))),
            ):
                with self.assertRaises(OSError) as raised:
                    apply_incremental(
                        plan,
                        tasks,
                        {name: (name, "Test Mod") for name in entries},
                        deploy_dir=deploy_dir,
                        core_dir=core_dir,
                        overwrite_dir=overwrite_dir,
                        mode=LinkMode.HARDLINK,
                        state_dir=root,
                    )

            self.assertEqual(raised.exception.errno, errno.EROFS)
            self.assertFalse((deploy_dir / "failed.txt").exists())
            self.assertFalse(plan.ran_incremental)


class _NullContext:
    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        return None


if __name__ == "__main__":
    unittest.main()
