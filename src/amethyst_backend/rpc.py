from __future__ import annotations

import json
import sys
import traceback
from typing import Any, TextIO

from .service import AmethystService, ServiceError

MAX_REQUEST_CHARACTERS = 8 * 1024 * 1024
MAX_JSON_DEPTH = 128
MAX_BATCH_REQUESTS = 256


def _decode(line: str) -> Any:
    depth = 0
    quoted = escaped = False
    for char in line:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise ValueError("JSON nesting limit exceeded")
        elif char in "]}":
            depth -= 1

    def invalid_constant(value: str) -> None:
        raise ValueError(f"Invalid JSON constant: {value}")

    return json.loads(line, parse_constant=invalid_constant)


def _error(request_id: Any, code: int, message: str, name: str,
           details: dict[str, Any] | None = None) -> dict[str, Any]:
    data: dict[str, Any] = {"name": name}
    if details:
        data["details"] = details
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message, "data": data},
    }


def _valid_id(request_id: Any) -> bool:
    return (request_id is None
            or isinstance(request_id, str)
            or (isinstance(request_id, int)
                and not isinstance(request_id, bool)))


def _handle_request(request: Any, service: AmethystService) -> Any:
    if not isinstance(request, dict):
        return _error(None, -32600, "Invalid Request", "invalid_request")

    request_id = request.get("id")
    if request.get("jsonrpc") != "2.0":
        return _error(request_id if _valid_id(request_id) else None,
                      -32600, "Invalid Request", "invalid_request")
    method = request.get("method")
    if not isinstance(method, str) or not method:
        return _error(request_id if _valid_id(request_id) else None,
                      -32600, "Invalid Request", "invalid_request")
    if "id" in request and not _valid_id(request_id):
        return _error(None, -32600, "Invalid Request", "invalid_request")

    notification = "id" not in request
    try:
        result = service.call(method, request.get("params"))
    except ServiceError as exc:
        if notification:
            return None
        return _error(request_id, exc.rpc_code, str(exc), exc.name, exc.details)
    except Exception as exc:
        try:
            print(
                f"[headless] {method} failed: {type(exc).__name__}: {exc}\n"
                + traceback.format_exc(),
                file=sys.stderr,
                flush=True,
            )
        except Exception:
            pass
        if notification:
            return None
        return _error(
            request_id,
            -32603,
            "Internal error",
            "internal_error",
            {"exception": type(exc).__name__},
        )

    if notification:
        return None
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def handle_message(message: Any, service: AmethystService) -> Any:
    if not isinstance(message, list):
        return _handle_request(message, service)
    if not message or len(message) > MAX_BATCH_REQUESTS:
        return _error(None, -32600, "Invalid Request", "invalid_request")
    responses = [
        response for request in message
        if (response := _handle_request(request, service)) is not None
    ]
    return responses or None


def run_stdio(service: AmethystService | None = None,
              input_stream: TextIO | None = None,
              output_stream: TextIO | None = None) -> int:
    service = service or AmethystService()
    input_stream = input_stream or sys.stdin
    output_stream = output_stream or sys.stdout

    try:
        while True:
            line = input_stream.readline(MAX_REQUEST_CHARACTERS + 1)
            if not line:
                break
            if not line.strip():
                continue
            try:
                if len(line) > MAX_REQUEST_CHARACTERS:
                    while line and not line.endswith("\n"):
                        line = input_stream.readline(MAX_REQUEST_CHARACTERS + 1)
                    raise ValueError("Request size limit exceeded")
                message = _decode(line)
            except (ValueError, UnicodeError, RecursionError):
                response = _error(None, -32700, "Parse error", "parse_error")
            else:
                response = handle_message(message, service)
            if response is None:
                continue
            try:
                output_stream.write(json.dumps(
                    response, ensure_ascii=True, separators=(",", ":")) + "\n")
                output_stream.flush()
            except (BrokenPipeError, OSError, ValueError):
                return 0
        return 0
    finally:
        close = getattr(service, "close", None)
        if callable(close):
            close()
