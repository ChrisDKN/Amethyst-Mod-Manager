"""Regression tests for :mod:`Utils.collection_install`.

Guards the deferred-FOMOD install path: ``run_collection_install`` builds the
``fomod_expected_installed_files`` / ``fomod_expected_active_files`` sets and
must thread them through to ``_process_deferred``, whose body references them.
When they are not passed, those names are undefined inside ``_process_deferred``
and the deferred install loop raises ``NameError`` -- which its surrounding
``except`` swallows, so every deferred FOMOD mod in a collection silently fails
to install.

The check is static (AST) so it needs none of the module's runtime
dependencies: it verifies both halves of the contract at once -- the parameter
exists *and* the caller supplies it.

Run with::

    PYTHONPATH=src python3 -m Utils._collection_install_selftest
"""

from __future__ import annotations

import ast
from pathlib import Path

_MODULE = Path(__file__).with_name("collection_install.py")
_THREADED_ARGS = ("fomod_expected_installed_files", "fomod_expected_active_files")


def _func(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function {name!r} not found in {_MODULE.name}")


def _call_arg_names(call: ast.Call) -> set[str]:
    names = {a.id for a in call.args if isinstance(a, ast.Name)}
    names |= {
        kw.value.id for kw in call.keywords if isinstance(kw.value, ast.Name)
    }
    return names


def test_deferred_expected_files_are_threaded() -> None:
    tree = ast.parse(_MODULE.read_text(encoding="utf-8"), filename=str(_MODULE))

    # 1. _process_deferred must accept the expected-files sets as parameters,
    #    otherwise its body references them as undefined names (NameError).
    deferred = _func(tree, "_process_deferred")
    params = {
        a.arg
        for a in deferred.args.posonlyargs + deferred.args.args + deferred.args.kwonlyargs
    }
    for name in _THREADED_ARGS:
        assert name in params, (
            f"_process_deferred() is missing parameter {name!r}: the deferred "
            "FOMOD install path would raise NameError at runtime")

    # 2. run_collection_install must actually pass them at the call site,
    #    otherwise _process_deferred never receives the real sets.
    caller = _func(tree, "run_collection_install")
    calls = [
        node
        for node in ast.walk(caller)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_process_deferred"
    ]
    assert calls, "no _process_deferred(...) call found in run_collection_install()"
    for call in calls:
        supplied = _call_arg_names(call)
        missing = [name for name in _THREADED_ARGS if name not in supplied]
        assert not missing, (
            "run_collection_install() calls _process_deferred() without "
            f"passing {missing}")
    print("✓ deferred FOMOD install threads the expected-files sets through")


def main() -> None:
    test_deferred_expected_files_are_threaded()
    print("All collection-install self-tests passed.")


if __name__ == "__main__":
    main()
