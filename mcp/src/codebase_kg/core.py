"""The MCP surface every transport serves: the tool catalog, argument checks, one tool call. Stdlib only.

`catalog.json` holds the `tools/list` result and the server instructions,
generated from the fastmcp registrations in `server.py` by
`mcp/scripts/gen_catalog.py`; a test fails when the two differ. This module
never imports fastmcp, so the always-on server stays small.

`validate` checks arguments against each tool's `inputSchema` the way
pydantic's lax mode does in fastmcp: an integral float or a numeric string is
an integer, `"true"`/`"off"`/`1`/`0` are booleans, an unknown argument is
refused, and a missing one takes its default. The write tools' list items keep
only their required keys checked, because `edits` validates the rest and
answers with a refusal.

`Core.call_tool` resolves the session's graph, sends the call to a pool worker
(or runs it here when `max_workers` is 0), and returns the `CallToolResult`.
Writes to one graph run one at a time.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any, cast

from . import resolve, shim, worker
from .pool import CallError, Pool, Refused, WorkerError

CATALOG: dict[str, Any] = json.loads((Path(__file__).with_name("catalog.json")).read_text(encoding="utf-8"))
TOOLS: dict[str, dict[str, Any]] = {t["name"]: t for t in CATALOG["tools"]}
INSTRUCTIONS: str = CATALOG["instructions"]
SERVER_INFO = {"name": "codebase-kg", "version": shim.package_version()}

CLASSIC_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS = -32700, -32600, -32601, -32602
EMPTY_LISTS = {"prompts/list": "prompts", "resources/list": "resources",
               "resources/templates/list": "resourceTemplates"}

_INT = re.compile(r"[+-]?[0-9]+(?:_[0-9]+)*(?:\.0*)?")
_TRUE = frozenset({"1", "on", "t", "true", "y", "yes"})
_FALSE = frozenset({"0", "off", "f", "false", "n", "no"})
_TYPES: dict[str, type] = {"string": str, "object": dict, "array": list}


def dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def _join(where: str, key: str) -> str:
    return f"{where}.{key}" if where else key


def _integer(value: Any, where: str) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and _INT.fullmatch(value.strip()):
        return int(value.strip().partition(".")[0].replace("_", ""))
    raise ValueError(f"{where}: input should be a valid integer, got {value!r}")


def _boolean(value: Any, where: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.lower() in _TRUE | _FALSE:
        return value.lower() in _TRUE
    raise ValueError(f"{where}: input should be a valid boolean, got {value!r}")


def _check(schema: dict[str, Any], value: Any, where: str, items_shallow: bool) -> Any:
    if "anyOf" in schema:
        errors = []
        for option in schema["anyOf"]:
            try:
                return _check(option, value, where, items_shallow)
            except ValueError as exc:
                errors.append(str(exc))
        raise ValueError(errors[0])
    kind = schema.get("type")
    if kind == "null":
        if value is not None:
            raise ValueError(f"{where}: input should be null")
    elif kind == "integer":
        value = _integer(value, where)
    elif kind == "boolean":
        value = _boolean(value, where)
    elif kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{where}: input should be a valid number")
    elif kind in _TYPES and not isinstance(value, _TYPES[kind]):
        raise ValueError(f"{where}: input should be a valid {kind}")
    if "minimum" in schema and value < schema["minimum"]:
        raise ValueError(f"{where}: input should be greater than or equal to {schema['minimum']}")
    if "maximum" in schema and value > schema["maximum"]:
        raise ValueError(f"{where}: input should be less than or equal to {schema['maximum']}")
    if kind == "array" and "items" in schema:
        items = cast("list[Any]", value)
        return [_check(schema["items"], v, f"{where}.{i}", items_shallow) for i, v in enumerate(items)]
    if kind == "object" and "properties" in schema:
        fields = cast("dict[str, Any]", value)
        return _object(schema, fields, where, items_shallow)
    return value


def _object(schema: dict[str, Any], value: dict[str, Any], where: str, shallow: bool) -> dict[str, Any]:
    props: dict[str, Any] = schema.get("properties", {})
    missing = [k for k in schema.get("required", []) if k not in value]
    if missing:
        raise ValueError(f"{_join(where, missing[0])}: missing required argument")
    if schema.get("additionalProperties") is False:
        extra = [k for k in value if k not in props]
        if extra:
            raise ValueError(f"{_join(where, extra[0])}: unexpected argument")
    if shallow and where:
        return value
    out: dict[str, Any] = {}
    for key, sub in props.items():
        if key in value:
            out[key] = _check(sub, value[key], _join(where, key), shallow)
        elif "default" in sub:
            out[key] = sub["default"]
    for key in value:
        out.setdefault(key, value[key])
    return out


def validate(name: str, args: Any) -> dict[str, Any]:
    """The arguments as the tool receives them, or ValueError naming the first bad one."""
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise ValueError("arguments: input should be an object")
    return _object(TOOLS[name]["inputSchema"], args, "", shallow=name in worker.WRITE_TOOLS)


def text_result(data: dict[str, Any], is_error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": dumps(data)}], "structuredContent": data, "isError": is_error}


def error_result(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": True}


class Core:
    """Thread-safe: each transport thread calls `call_tool` for its own session."""

    def __init__(self, pool: Pool | None) -> None:
        self.pool = pool
        self._guard = threading.Lock()
        self._writers: dict[str, threading.Lock] = {}

    def _writer(self, graph: Path) -> threading.Lock:
        key = str(graph).casefold()
        with self._guard:
            return self._writers.setdefault(key, threading.Lock())

    def _run(self, name: str, args: dict[str, Any], graph: Path) -> dict[str, Any]:
        if self.pool is not None:
            return self.pool.call(name, args, str(graph))
        request = dumps({"id": 0, "tool": name, "args": args, "graph": str(graph)}).encode("utf-8")
        reply = json.loads(worker.answer(request))
        if reply.get("ok"):
            return reply.get("result") or {}
        if isinstance(reply.get("refused"), dict):
            raise Refused(reply["refused"])
        raise CallError(str(reply.get("error")))

    def call_tool(self, conn: resolve.Connection, name: Any, raw_args: Any) -> dict[str, Any]:
        if not isinstance(name, str) or name not in TOOLS:
            return error_result(f"Unknown tool: {name!r}")
        try:
            args = validate(name, raw_args)
        except ValueError as exc:
            return error_result(f"1 validation error for call[{name}]\n{exc}")
        writes = name in worker.WRITE_TOOLS
        try:
            graph = resolve.graph_file(conn)
        except FileNotFoundError as exc:
            if writes:
                return text_result(worker.refusal(str(exc)), is_error=True)
            return error_result(f"Error calling tool {name!r}: {exc}")
        try:
            if writes:
                with self._writer(graph):
                    result = self._run(name, args, graph)
            else:
                result = self._run(name, args, graph)
        except Refused as exc:
            return text_result(exc.body, is_error=True)
        except (CallError, WorkerError) as exc:
            return error_result(f"Error calling tool {name!r}: {exc}")
        return text_result(result)


def classic(core: Core, conn: resolve.Connection, msg: dict[str, Any]) -> dict[str, Any] | None:
    """The response to one message of a classic (initialize-first) MCP session, or None for none."""
    method = msg.get("method")
    if not isinstance(method, str) or "id" not in msg:
        return None
    raw = msg.get("params")
    params: dict[str, Any] = raw if isinstance(raw, dict) else {}
    result: dict[str, Any]
    if method == "initialize":
        asked = params.get("protocolVersion")
        result = {
            "protocolVersion": asked if asked in CLASSIC_VERSIONS else CLASSIC_VERSIONS[-1],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO, "instructions": INSTRUCTIONS,
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": CATALOG["tools"]}
    elif method in EMPTY_LISTS:
        result = {EMPTY_LISTS[method]: []}
    elif method == "tools/call":
        result = core.call_tool(conn, params.get("name"), params.get("arguments"))
    else:
        return {"jsonrpc": "2.0", "id": msg["id"],
                "error": {"code": METHOD_NOT_FOUND, "message": "Method not found"}}
    return {"jsonrpc": "2.0", "id": msg["id"], "result": result}
