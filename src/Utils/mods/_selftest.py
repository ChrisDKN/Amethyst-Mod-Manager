"""Focused self-tests for the neutral mod installation pipeline.

    PYTHONPATH=src python3 -m Utils.mods._selftest
"""

from __future__ import annotations

import tempfile
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from Utils.mods.install import finish_install, install_collection_archive


class _Prepared:
    def __init__(self, root: Path, game):
        self.archive = root / "fixture.zip"
        self.archive.write_bytes(b"archive")
        self.game = game
        self.profile_dir = root / "profile"
        self.profile_dir.mkdir()
        self.mod_name = "Fixture Mod"
        self.extract_dir = root / "extracted"
        self.extract_dir.mkdir()
        (self.extract_dir / "payload.txt").write_text(
            "payload", encoding="utf-8")
        self.src_root = self.extract_dir
        self.fomod_config = None
        self.fomod_config_path = None
        self.prebuilt_meta = None
        self.on_need_prefix = None
        self.cleanup_called = False

    def is_multi_mod(self) -> bool:
        return False

    def is_fomod(self) -> bool:
        return False

    def is_bain(self) -> bool:
        return False

    def is_bundle(self) -> bool:
        return False

    def cleanup(self) -> None:
        self.cleanup_called = True


class _FakeGame:
    supports_bain = False
    plugin_extensions: list[str] = []

    def __init__(self, hook):
        self.additional_install_logic = [hook]


class _InstallCancelled(Exception):
    install_cancelled = True


def _stage_payload(_files, _src, dest: Path, _log, game=None) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "payload.txt").write_text("payload", encoding="utf-8")


def _install_patches(staging: Path, commits: list[str]) -> ExitStack:
    stack = ExitStack()
    stack.enter_context(patch(
        "Utils.mods.copy.resolve_target_staging", return_value=staging))
    stack.enter_context(patch(
        "Utils.mods.install.stage_file_list",
        return_value=[("payload.txt", "payload.txt", False)]))
    stack.enter_context(patch(
        "Utils.mods.install._copy_file_list", side_effect=_stage_payload))
    stack.enter_context(patch("Utils.mods.install._write_install_meta"))
    stack.enter_context(patch(
        "Utils.mods.install._update_indexes",
        side_effect=lambda *_a, **_k: commits.append("index") or True))
    stack.enter_context(patch(
        "Utils.mods.install._add_to_modlist",
        side_effect=lambda *_a, **_k: commits.append("modlist")))
    stack.enter_context(patch(
        "Utils.mods.install._add_plugins",
        side_effect=lambda *_a, **_k: commits.append("plugins")))
    stack.enter_context(patch(
        "Utils.mods.install._check_nexus_flags_after_install"))
    return stack


def test_finish_install_aborts_when_hook_fails() -> None:
    hook_calls: list[Path] = []
    error = RuntimeError("hook failed")

    def failing_hook(dest: Path, _name: str, _log) -> None:
        hook_calls.append(dest)
        raise error

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        game = _FakeGame(failing_hook)
        prepared = _Prepared(root, game)
        commits: list[str] = []
        caught = None
        result = None
        with _install_patches(root / "staging", commits):
            try:
                result = finish_install(prepared, None, log_fn=lambda _m: None)
            except RuntimeError as exc:
                caught = exc
        assert caught is error, (
            "hook failure was suppressed; "
            f"result={result!r}, commits={commits}"
        )
        assert len(hook_calls) == 1
        assert commits == []


def test_finish_install_continues_when_hook_succeeds() -> None:
    hook_calls: list[Path] = []

    def successful_hook(dest: Path, _name: str, _log) -> None:
        hook_calls.append(dest)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        game = _FakeGame(successful_hook)
        prepared = _Prepared(root, game)
        commits: list[str] = []
        with _install_patches(root / "staging", commits):
            result = finish_install(
                prepared, None, log_fn=lambda _m: None)
        assert result == "Fixture Mod"
        assert len(hook_calls) == 1
        assert commits == ["index", "modlist", "plugins"]


def test_finish_install_preserves_hook_cancellation() -> None:
    hook_calls: list[Path] = []

    def cancelling_hook(dest: Path, _name: str, _log) -> None:
        hook_calls.append(dest)
        raise _InstallCancelled("cancelled")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        staging = root / "staging"
        game = _FakeGame(cancelling_hook)
        prepared = _Prepared(root, game)
        commits: list[str] = []
        with _install_patches(staging, commits):
            result = finish_install(
                prepared, None, log_fn=lambda _m: None)
        assert result is None
        assert len(hook_calls) == 1
        assert commits == []
        assert not (staging / "Fixture Mod").exists()


def test_collection_install_aborts_when_hook_fails() -> None:
    hook_calls: list[Path] = []
    error = RuntimeError("collection hook failed")

    def failing_hook(dest: Path, _name: str, _log) -> None:
        hook_calls.append(dest)
        raise error

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        game = _FakeGame(failing_hook)
        prepared = _Prepared(root, game)
        commits: list[str] = []
        caught = None
        with _install_patches(root / "staging", commits), patch(
            "Utils.mods.install.prepare_archive", return_value=prepared,
        ):
            try:
                install_collection_archive(
                    str(prepared.archive), game, prepared.profile_dir,
                    log_fn=lambda _m: None, skip_index_update=False)
            except RuntimeError as exc:
                caught = exc
        assert caught is error
        assert len(hook_calls) == 1
        assert commits == []


if __name__ == "__main__":
    test_finish_install_aborts_when_hook_fails()
    print("✓ ordinary hook failure aborts before install commit")
    test_finish_install_continues_when_hook_succeeds()
    print("✓ successful hook continues through install commit")
    test_finish_install_preserves_hook_cancellation()
    print("✓ hook cancellation remains a clean cancellation")
    test_collection_install_aborts_when_hook_fails()
    print("✓ collection hook failure aborts before install commit")
