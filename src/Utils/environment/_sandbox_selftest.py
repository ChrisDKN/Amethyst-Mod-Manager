"""Regression tests for :mod:`Utils.environment.sandbox`.

Guards the Flatpak reachability hint against a false positive on Fedora Atomic /
Bazzite, where ``/mnt`` is a symlink to ``/var/mnt``: the ``--filesystem=/mnt``
grant already exposes ``/var/mnt`` and ``Path.resolve()`` rewrites any
``/mnt/...`` path to its ``/var/mnt/...`` target, so a resolved ``/var/mnt/...``
staging or game path must NOT be flagged as unreachable. Genuinely ungranted
trees (``/opt``, ``/data``, ...) must still surface the grant hint.

The check drives ``flatpak_blocked_path_hint`` directly with ``in_flatpak``
forced on and paths that do not exist on the test host (so the granted-root
logic is exercised rather than the early "path exists" return). No runtime
dependencies beyond the module under test.

Run with::

    PYTHONPATH=src python3 -m Utils.environment._sandbox_selftest
"""

from __future__ import annotations

from contextlib import contextmanager

from Utils.environment import sandbox


@contextmanager
def _pretend_flatpak():
    """Force the in-sandbox branch regardless of the real host."""
    original = sandbox.in_flatpak
    sandbox.in_flatpak = lambda: True
    try:
        yield
    finally:
        sandbox.in_flatpak = original


# Deep, non-existent paths so p.exists() is False and the granted-root logic
# (not the early "reachable" return) is what actually decides.
_GRANTED_PATHS = (
    "/var/mnt/D/ModManagers/Amethyst/staging/SkyrimSE",
    "/mnt/D/ModManagers/Amethyst/staging/SkyrimSE",
    "/run/media/deck/SD/SteamLibrary/game",
    "/media/user/Drive/SteamLibrary/game",
)
_UNGRANTED_PATHS = (
    "/opt/SteamLibrary/game",
    "/data/SteamLibrary/game",
    "/srv/games/game",
)


def test_var_mnt_is_treated_as_granted() -> None:
    with _pretend_flatpak():
        hint = sandbox.flatpak_blocked_path_hint(
            "/var/mnt/D/ModManagers/Amethyst/staging/SkyrimSE")
    assert hint is None, (
        "/var/mnt path (granted via the /mnt symlink) must not be flagged as "
        f"unreachable, got: {hint!r}")
    print("✓ /var/mnt paths are recognised as granted (no false warning)")


def test_granted_roots_never_warn() -> None:
    with _pretend_flatpak():
        for path in _GRANTED_PATHS:
            assert sandbox.flatpak_blocked_path_hint(path) is None, (
                f"granted root path unexpectedly flagged: {path}")
    print("✓ all granted-root trees stay silent")


def test_ungranted_paths_still_warn() -> None:
    with _pretend_flatpak():
        for path in _UNGRANTED_PATHS:
            hint = sandbox.flatpak_blocked_path_hint(path)
            assert hint is not None and "flatpak override" in hint, (
                f"genuinely ungranted path should still warn: {path} -> {hint!r}")
    print("✓ genuinely ungranted paths still surface the grant hint")


def test_var_mnt_in_granted_roots_constant() -> None:
    # The constant and the manifest grant must stay in step; a bare membership
    # check catches an accidental revert of the tuple.
    assert "/var/mnt" in sandbox._GRANTED_ROOTS
    print("✓ /var/mnt is present in _GRANTED_ROOTS")


def main() -> None:
    test_var_mnt_in_granted_roots_constant()
    test_var_mnt_is_treated_as_granted()
    test_granted_roots_never_warn()
    test_ungranted_paths_still_warn()
    print("All sandbox self-tests passed.")


if __name__ == "__main__":
    main()
