"""Focused self-tests for Nexus API retries and multi-file browser installs.

    PYTHONPATH=src python3 -m Nexus._selftest
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import requests

from Nexus import nexus_api
from Nexus.nexus_api import NexusAPI, NexusAPIError


def _response(status: int, body=None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.ok = 200 <= status < 300
    resp.headers = {}
    resp.json.return_value = body if body is not None else {}
    resp.text = ""
    resp.reason = ""
    return resp


def _api(responses) -> tuple[NexusAPI, MagicMock]:
    api = NexusAPI("test-key")
    session = MagicMock()
    session.get.side_effect = responses
    session.post.side_effect = responses
    api._session = session
    return api, session


def check_get_retries_connection_reset() -> None:
    reset = requests.ConnectionError(
        ConnectionResetError(104, "Connection reset by peer"))
    api, session = _api([reset, _response(200, [{"URI": "https://cdn"}])])
    with patch.object(nexus_api.time, "sleep") as sleep:
        links = api.get_download_links("skyrimspecialedition", 59818, 1)
    assert [link.URI for link in links] == ["https://cdn"]
    assert session.get.call_count == 2
    sleep.assert_called_once()


def check_get_retries_timeout() -> None:
    api, session = _api([requests.Timeout(), _response(200, {"ok": 1})])
    with patch.object(nexus_api.time, "sleep"):
        assert api._get("/x") == {"ok": 1}
    assert session.get.call_count == 2


def check_get_gives_up_after_retries() -> None:
    api, session = _api([requests.ConnectionError("reset")] * 3)
    with patch.object(nexus_api.time, "sleep"):
        try:
            api._get("/x")
        except NexusAPIError as exc:
            assert "Connection failed" in str(exc), exc
        else:
            raise AssertionError("expected NexusAPIError")
    assert session.get.call_count == 3


def check_get_does_not_retry_4xx() -> None:
    for status in (401, 403, 404):
        api, session = _api([_response(status), _response(200)])
        with patch.object(nexus_api.time, "sleep"):
            try:
                api._get("/x")
            except NexusAPIError as exc:
                assert exc.status_code == status
            else:
                raise AssertionError("expected NexusAPIError")
        assert session.get.call_count == 1, status


def check_graphql_retries_queries_not_mutations() -> None:
    api, session = _api([requests.ConnectionError("reset"), _response(200)])
    with patch.object(nexus_api.time, "sleep"):
        assert api._post_graphql("query { x }").status_code == 200
    assert session.post.call_count == 2

    api, session = _api([requests.ConnectionError("reset"), _response(200)])
    with patch.object(nexus_api.time, "sleep"):
        try:
            api._post_graphql("\n  mutation M { x }")
        except NexusAPIError:
            pass
        else:
            raise AssertionError("expected NexusAPIError")
    assert session.post.call_count == 1


def _browser_stub():
    return SimpleNamespace(
        _install_target=lambda: "target",
        _log=lambda message: None,
        _install_fn=MagicMock(),
        _report_selected_failures=MagicMock(),
        tr=lambda text: text,
    )


def _group(labels):
    return {"name": "Crash Logger", "target": "target",
            "remaining": len(labels), "results": [None] * len(labels),
            "labels": list(labels), "errors": {}}


def check_partial_group_reports_failed_file() -> None:
    from gui_qt.nexus.browser import NexusBrowserView
    view = _browser_stub()
    group = _group(["CrashLogger", "Skyrim PDBs"])
    complete = NexusBrowserView._complete_selected_file
    complete(view, group, 1, "/cache/pdbs.7z", {"modid": 1})
    complete(view, group, 0, None, None, "Connection failed: reset")
    view._install_fn.assert_called_once_with(
        ["/cache/pdbs.7z"], {"/cache/pdbs.7z": {"modid": 1}})
    view._report_selected_failures.assert_called_once_with(group, installed=1)
    assert group["errors"] == {0: "Connection failed: reset"}


def check_failed_group_reports_without_install() -> None:
    from gui_qt.nexus.browser import NexusBrowserView
    view = _browser_stub()
    group = _group(["A", "B"])
    complete = NexusBrowserView._complete_selected_file
    complete(view, group, 0, None, None, "error a")
    complete(view, group, 1, None, None, "error b")
    view._install_fn.assert_not_called()
    view._report_selected_failures.assert_called_once_with(group, installed=0)


def check_cancelled_file_is_not_reported() -> None:
    from gui_qt.nexus.browser import NexusBrowserView
    view = _browser_stub()
    group = _group(["A", "B"])
    complete = NexusBrowserView._complete_selected_file
    complete(view, group, 0, "/cache/a.7z", None)
    complete(view, group, 1, None, None)
    view._install_fn.assert_called_once()
    view._report_selected_failures.assert_not_called()


def check_failure_message_names_file() -> None:
    from gui_qt.nexus.browser import NexusBrowserView
    view = _browser_stub()
    group = _group(["CrashLogger", "Skyrim PDBs"])
    group["errors"] = {0: "Connection failed: reset"}
    with patch("gui_qt.dialogs.confirm.ConfirmOverlay.show_message") as show:
        NexusBrowserView._report_selected_failures(view, group, installed=1)
    _host, _title, body = show.call_args.args
    assert "1 of 2" in body and "Crash Logger" in body, body
    assert show.call_args.kwargs["list_items"] == [
        "CrashLogger: Connection failed: reset"]


def main() -> None:
    checks = [
        check_get_retries_connection_reset,
        check_get_retries_timeout,
        check_get_gives_up_after_retries,
        check_get_does_not_retry_4xx,
        check_graphql_retries_queries_not_mutations,
        check_partial_group_reports_failed_file,
        check_failed_group_reports_without_install,
        check_cancelled_file_is_not_reported,
        check_failure_message_names_file,
    ]
    for check in checks:
        check()
        print(f"ok  {check.__name__}")
    print(f"Nexus selftests passed ({len(checks)})")


if __name__ == "__main__":
    main()
