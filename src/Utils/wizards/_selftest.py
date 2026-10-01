"""Focused self-tests for shared wizard helpers.

    PYTHONPATH=src python3 -m Utils.wizards._selftest
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

from Utils.deployment import RestoreIncompleteError, restore_filemap_from_root
from Utils.wizards.archives import install_archive_payload


class _FakeGame:
    def __init__(self, game_path: Path, restore_error: Exception | None = None):
        self._game_path = game_path
        self._restore_error = restore_error
        self.restore_calls = 0

    def get_game_path(self) -> Path:
        return self._game_path

    def restore(self, log_fn=None) -> None:
        self.restore_calls += 1
        if self._restore_error is not None:
            raise self._restore_error


class _NoOpRestoreGame(_FakeGame):
    def restore(self, log_fn=None) -> None:
        self.restore_calls += 1
        restored = restore_filemap_from_root(
            self._game_path.parent / "state" / "filemap.txt",
            self._game_path,
            log_fn=log_fn,
        )
        assert restored == 0


def _run_game_install(*, restore_first: bool, restore_error: Exception | None = None):
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    game_path = root / "game"
    game_path.mkdir()
    archive = root / "payload.zip"
    archive.write_bytes(b"archive fixture")
    game = _FakeGame(game_path, restore_error)
    extracted: list[Path] = []

    def _extract(_archive: Path, dest: Path, log_fn=None) -> list[Path]:
        payload = dest / "payload.txt"
        payload.write_text("installed", encoding="utf-8")
        extracted.append(payload)
        return [payload]

    return tmp, game, archive, extracted, _extract


def test_game_install_restores_before_extracting() -> None:
    tmp, game, archive, extracted, extract = _run_game_install(
        restore_first=True)
    with tmp, patch("Utils.wizards.archives.extract_archive", extract):
        install_archive_payload(
            game, archive, "game", mod_fallback_name="Fixture",
            restore_first=True, delete_archive=False)
        assert game.restore_calls == 1
        assert len(extracted) == 1
        assert extracted[0].read_text(encoding="utf-8") == "installed"


def test_game_install_can_skip_restore() -> None:
    tmp, game, archive, extracted, extract = _run_game_install(
        restore_first=False)
    with tmp, patch("Utils.wizards.archives.extract_archive", extract):
        install_archive_payload(
            game, archive, "game", mod_fallback_name="Fixture",
            restore_first=False, delete_archive=False)
        assert game.restore_calls == 0
        assert len(extracted) == 1


def test_game_install_allows_nothing_to_restore() -> None:
    tmp, _game, archive, extracted, extract = _run_game_install(
        restore_first=True)
    game = _NoOpRestoreGame(_game.get_game_path())
    with tmp, patch("Utils.wizards.archives.extract_archive", extract):
        install_archive_payload(
            game, archive, "game", mod_fallback_name="Fixture",
            restore_first=True, delete_archive=False)
        assert game.restore_calls == 1
        assert len(extracted) == 1


def test_game_install_aborts_when_restore_fails() -> None:
    error = RestoreIncompleteError("managed recovery state remains")
    tmp, game, archive, extracted, extract = _run_game_install(
        restore_first=True, restore_error=error)
    with tmp, patch("Utils.wizards.archives.extract_archive", extract):
        caught = None
        try:
            install_archive_payload(
                game, archive, "game", mod_fallback_name="Fixture",
                restore_first=True, delete_archive=False)
        except RestoreIncompleteError as exc:
            caught = exc
        assert caught is error, (
            "restore failure was suppressed; "
            f"extract calls={len(extracted)}, "
            f"payload exists={(game.get_game_path() / 'payload.txt').exists()}"
        )
        assert game.restore_calls == 1
        assert extracted == []
        assert not (game.get_game_path() / "payload.txt").exists()


if __name__ == "__main__":
    test_game_install_restores_before_extracting()
    print("✓ game install restores before extracting")
    test_game_install_can_skip_restore()
    print("✓ game install can skip restore")
    test_game_install_allows_nothing_to_restore()
    print("✓ nothing-to-restore no-op allows extraction")
    test_game_install_aborts_when_restore_fails()
    print("✓ failed restore aborts game install")
