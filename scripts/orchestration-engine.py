#!/usr/bin/env python3
"""Configurable orchestration policy engine.

The existing shell scripts keep their legacy merge/gate mechanics. This engine
owns the configurable workflow policy introduced by schema_version: 2:
branch roles, states, transitions, evidence, approvals, artifacts, tags, and
adapter references. It intentionally does not call provider APIs or interpret
provider-specific payloads.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


SUPPORTED_SCHEMA_VERSIONS = {"1", "2"}
PASS_VALUES = {"pass", "passed", "green", "success", "succeeded", "true", "ok"}
IDENT_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


class EngineError(Exception):
    pass


def fail(message: str, code: int = 2) -> None:
    print(f"orchestration-engine: REFUSED: {message}", file=sys.stderr)
    raise SystemExit(code)


def strip_comment(line: str) -> str:
    quote = ""
    escaped = False
    for idx, ch in enumerate(line):
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if quote:
            if ch == quote:
                quote = ""
            continue
        if ch in {"'", '"'}:
            quote = ch
            continue
        if ch == "#":
            if idx == 0 or line[idx - 1].isspace():
                return line[:idx]
    return line


class ConfigSyntaxError(EngineError):
    """A config line the dependency-free YAML subset cannot represent.

    `hint` names the unsupported construct and the supported spelling so
    validate-config can tell the operator what to write instead.
    """

    def __init__(self, line: int, message: str, hint: str = "") -> None:
        self.line = line
        self.message = message
        self.hint = hint
        super().__init__(f"line {line}: {message}")


FLOW_SEQUENCE_FORMS = (
    "one line `key: [a, b]`, the next line `key:` then an indented `[a, b]`, "
    "or continuation lines indented deeper than the key"
)
HINT_UNTERMINATED_FLOW = (
    "flow sequence `[`...`]` must be closed; supported list forms are "
    + FLOW_SEQUENCE_FORMS
    + ", or a block list (`key:` then `  - a` lines)"
)
HINT_FLOW_MAPPING = (
    "flow mappings `{a: 1}` are not supported (only the empty map `{}` is); "
    "write a block mapping: `key:` then one indented `child: value` per line"
)
HINT_NESTED_FLOW = (
    "nested flow collections are not supported; quote an item that contains "
    "brackets or braces (`\"x [y]\"`) or use a block list of one-line flow lists"
)
HINT_FLOW_ITEM = (
    "flow sequence items are comma-separated scalars; remove the extra comma "
    "or quote the item, e.g. `key: [a, \"b, c\"]`"
)
HINT_FLOW_TRAILING = (
    "nothing may follow the closing `]` except a comment; quote the whole value "
    "if it is a string that starts with `[`, e.g. `run: \"[ -f x ] && y\"`"
)
HINT_FLOW_QUOTE = (
    "multi-line quoted scalars are not supported; keep each quoted flow item "
    "on one line, e.g. `key: [\"a, b\", c]`"
)
HINT_BLOCK_SCALAR = (
    "block scalars (`|` or `>`) are not supported; write the value on one line, "
    "quoted if it contains `: ` or ` #`"
)
HINT_MULTILINE_PLAIN = (
    "multi-line plain values are not supported; keep the value on one line, or "
    "leave the key's value empty and indent its child keys or list below it"
)
BLOCK_SCALAR_INDICATORS = {"|", ">", "|-", ">-", "|+", ">+"}


def _flow_scan(fragment: str, state: dict[str, Any], lineno: int) -> str:
    """Scan one physical fragment of a flow sequence, strictly.

    Returns the fragment with any trailing comment removed and updates `state`
    (depth, quote, item_start, closed, trailing). Quotes open only at the start
    of an item, as in YAML, so apostrophes inside plain items stay literal.
    """
    out: list[str] = []
    index = 0
    size = len(fragment)
    while index < size:
        ch = fragment[index]
        quote = state["quote"]
        if quote:
            out.append(ch)
            if quote == '"' and ch == "\\" and index + 1 < size:
                out.append(fragment[index + 1])
                index += 2
                continue
            if ch == quote:
                if quote == "'" and index + 1 < size and fragment[index + 1] == "'":
                    out.append("'")
                    index += 2
                    continue
                state["quote"] = ""
            index += 1
            continue
        if ch == "#" and (index == 0 or fragment[index - 1].isspace()):
            break
        if state["closed"]:
            if not ch.isspace():
                state["trailing"] = True
            out.append(ch)
            index += 1
            continue
        if ch.isspace():
            out.append(ch)
            index += 1
            continue
        if ch in {"'", '"'} and state["item_start"]:
            state["quote"] = ch
            state["item_start"] = False
            out.append(ch)
            index += 1
            continue
        if ch == "[":
            if state["depth"] == 0:
                state["depth"] = 1
                state["item_start"] = True
                out.append(ch)
                index += 1
                continue
            raise ConfigSyntaxError(
                lineno, "nested flow sequences are not supported", HINT_NESTED_FLOW
            )
        if ch in {"{", "}"}:
            raise ConfigSyntaxError(
                lineno,
                "flow mappings inside a flow sequence are not supported",
                HINT_NESTED_FLOW,
            )
        if ch == "]":
            state["depth"] = 0
            state["closed"] = True
            out.append(ch)
            index += 1
            continue
        if ch == ",":
            state["item_start"] = True
        else:
            state["item_start"] = False
        out.append(ch)
        index += 1
    if state["quote"]:
        raise ConfigSyntaxError(
            lineno, "quoted flow sequence item is not closed on its line", HINT_FLOW_QUOTE
        )
    return "".join(out).rstrip()


def _new_flow_state() -> dict[str, Any]:
    return {"depth": 0, "quote": "", "item_start": False, "closed": False, "trailing": False}


def _flow_depth_lenient(text: str) -> int:
    """Bracket depth of a one-line value, tolerant of anything a legacy string holds."""
    depth = 0
    quote = ""
    item_start = True
    index = 0
    while index < len(text):
        ch = text[index]
        if quote:
            if quote == '"' and ch == "\\":
                index += 2
                continue
            if ch == quote:
                quote = ""
            index += 1
            continue
        if ch == "#" and (index == 0 or text[index - 1].isspace()):
            break
        if ch in {"'", '"'} and item_start:
            quote = ch
        elif ch == "[":
            depth += 1
            item_start = True
        elif ch == "]":
            depth -= 1
        elif ch == ",":
            item_start = True
        elif not ch.isspace():
            item_start = False
        index += 1
    return depth


def parse_flow_sequence(value: str, lineno: int = 0) -> list[Any]:
    """Parse a complete one-line flow sequence `[a, "b, c", 'd',]` strictly."""
    state = _new_flow_state()
    clean = _flow_scan(value.strip(), state, lineno)
    if not state["closed"]:
        raise ConfigSyntaxError(
            lineno, "unterminated flow sequence '['", HINT_UNTERMINATED_FLOW
        )
    if state["trailing"]:
        raise ConfigSyntaxError(
            lineno, "unexpected content after flow sequence ']'", HINT_FLOW_TRAILING
        )
    inner = clean.strip()[1:-1]
    items: list[str] = []
    current: list[str] = []
    quote = ""
    item_start = True
    index = 0
    while index < len(inner):
        ch = inner[index]
        if quote:
            current.append(ch)
            if quote == '"' and ch == "\\" and index + 1 < len(inner):
                current.append(inner[index + 1])
                index += 2
                continue
            if ch == quote:
                if quote == "'" and index + 1 < len(inner) and inner[index + 1] == "'":
                    current.append("'")
                    index += 2
                    continue
                quote = ""
            index += 1
            continue
        if ch == ",":
            items.append("".join(current).strip())
            current = []
            item_start = True
            index += 1
            continue
        if ch in {"'", '"'} and item_start:
            quote = ch
        if not ch.isspace():
            item_start = False
        current.append(ch)
        index += 1
    last = "".join(current).strip()
    if last or items:
        # A single trailing comma is allowed (`[a, b,]`); an empty list stays empty.
        if last:
            items.append(last)
    if any(item == "" for item in items):
        raise ConfigSyntaxError(lineno, "empty flow sequence item", HINT_FLOW_ITEM)
    return [parse_scalar(item) for item in items]


def parse_scalar(value: str, lineno: int = 0) -> Any:
    value = value.strip()
    if value == "":
        return ""
    if (value[0:1], value[-1:]) in {('"', '"'), ("'", "'")}:
        return value[1:-1]
    low = value.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low in {"null", "none", "~"}:
        return None
    if value.startswith("[") and value.endswith("]"):
        return parse_flow_sequence(value, lineno)
    if value.startswith("{") and value.endswith("}"):
        if value[1:-1].strip() == "":
            return {}
        if ":" in value:
            raise ConfigSyntaxError(
                lineno, "flow mappings ('{a: 1}') are not supported", HINT_FLOW_MAPPING
            )
    return value


def split_key_value(text: str) -> tuple[str, str] | None:
    quote = ""
    for idx, ch in enumerate(text):
        if quote:
            if ch == quote:
                quote = ""
            continue
        if ch in {"'", '"'}:
            quote = ch
            continue
        if ch == ":":
            key = text[:idx].strip()
            val = text[idx + 1 :].strip()
            if key:
                return key, val
    return None


def _flow_value_offset(body: str) -> tuple[int, bool] | None:
    """Locate a value in `body` that opens a flow sequence.

    Returns (offset of the value within body, whether the owner line is a key or
    dash whose value is empty so a flow sequence may follow on the next line).
    `None` means the line neither holds nor introduces a flow sequence.
    """
    rest_offset = 0
    if body.startswith("- "):
        rest_offset = len(body) - len(body[2:].lstrip())
    rest = body[rest_offset:]
    if rest.startswith("["):
        return rest_offset, False
    kv = split_key_value(rest)
    if kv is None:
        return None
    _key, val = kv
    if val == "":
        return len(body), True
    if val.startswith("["):
        return len(body) - len(val), False
    return None


def logical_config_lines(text: str) -> list[tuple[int, str, bool]]:
    """Fold every multi-line flow sequence into one logical config line.

    Returns (1-based line number, text, folded) tuples. Unfolded lines are the
    original physical lines, untouched, so line-oriented readers see exactly
    what they saw before. Folded lines are comment-free and carry the owner
    line's indentation and key, e.g. `      allowed_tools: [read_file, search]`,
    whether the source put the list on the key's line, on the next more-indented
    line (Prettier), or across several lines. Readers must not strip comments
    from folded lines again.
    """
    lines = text.splitlines()
    out: list[tuple[int, str, bool]] = []
    index = 0
    while index < len(lines):
        original = lines[index]
        lineno = index + 1
        clean = strip_comment(original).rstrip()
        if not clean.strip():
            out.append((lineno, original, False))
            index += 1
            continue
        indent = len(clean) - len(clean.lstrip(" "))
        body = clean[indent:]
        located = _flow_value_offset(body)
        if located is None:
            out.append((lineno, original, False))
            index += 1
            continue
        offset, empty_value = located
        owner_indent = indent
        start_index = index
        if empty_value:
            probe = index + 1
            while probe < len(lines) and not strip_comment(lines[probe]).strip():
                probe += 1
            if probe >= len(lines):
                out.append((lineno, original, False))
                index += 1
                continue
            next_clean = strip_comment(lines[probe]).rstrip()
            next_indent = len(next_clean) - len(next_clean.lstrip(" "))
            if next_indent <= indent or not next_clean.lstrip(" ").startswith("["):
                out.append((lineno, original, False))
                index += 1
                continue
            head = body.rstrip() + " "
            start_index = probe
            first_fragment = lines[probe][next_indent:]
        else:
            head = body[:offset]
            first_fragment = original[indent + offset :]
            if _flow_depth_lenient(first_fragment) <= 0:
                # Closed (or legacy string) on its own line: unchanged behaviour.
                out.append((lineno, original, False))
                index += 1
                continue
        state = _new_flow_state()
        fragments = [_flow_scan(first_fragment, state, start_index + 1)]
        cursor = start_index + 1
        while not state["closed"]:
            if cursor >= len(lines):
                raise ConfigSyntaxError(
                    start_index + 1,
                    "unterminated flow sequence '[' (reached end of file)",
                    HINT_UNTERMINATED_FLOW,
                )
            physical = lines[cursor]
            if not physical.strip() or physical.strip().startswith("#"):
                cursor += 1
                continue
            cont_indent = len(physical) - len(physical.lstrip(" "))
            cont_body = physical[cont_indent:]
            closer_at_owner = cont_indent == owner_indent and cont_body.startswith("]")
            if cont_indent <= owner_indent and not closer_at_owner:
                raise ConfigSyntaxError(
                    start_index + 1,
                    f"unterminated flow sequence '[' (line {cursor + 1} is not "
                    "indented deeper than its key)",
                    HINT_UNTERMINATED_FLOW,
                )
            fragment = _flow_scan(cont_body, state, cursor + 1)
            if fragment:
                fragments.append(fragment)
            cursor += 1
        if state["trailing"]:
            raise ConfigSyntaxError(
                cursor, "unexpected content after flow sequence ']'", HINT_FLOW_TRAILING
            )
        joined = " ".join(fragment.strip() for fragment in fragments if fragment.strip())
        out.append((lineno, " " * indent + head + joined, True))
        index = cursor
    return out


def config_text_lines(text: str) -> list[tuple[int, str]]:
    """Comment-stripped logical lines (line number, text) for indentation readers."""
    result: list[tuple[int, str]] = []
    for lineno, line, folded in logical_config_lines(text):
        result.append((lineno, line if folded else strip_comment(line).rstrip()))
    return result


def load_simple_yaml(path: Path) -> Any:
    raw: list[tuple[int, str, int, bool]] = []
    for lineno, line, folded in logical_config_lines(path.read_text(encoding="utf-8")):
        if not folded:
            line = strip_comment(line).rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        raw.append((indent, line.lstrip(" "), lineno, folded))

    def scalar(value: str, lineno: int) -> Any:
        return parse_scalar(value, lineno)

    def parse_block(index: int, indent: int) -> tuple[Any, int]:
        if index >= len(raw):
            return {}, index
        cur_indent, text, lineno, _folded = raw[index]
        if cur_indent < indent:
            return {}, index
        if text.startswith("- "):
            return parse_list(index, cur_indent)
        return parse_map(index, cur_indent)

    def parse_map(index: int, indent: int) -> tuple[dict[str, Any], int]:
        result: dict[str, Any] = {}
        while index < len(raw):
            cur_indent, text, lineno, _folded = raw[index]
            if cur_indent < indent:
                break
            if cur_indent > indent:
                break
            if text.startswith("- "):
                break
            kv = split_key_value(text)
            if kv is None:
                raise ConfigSyntaxError(lineno, f"cannot parse line: {text}")
            key, val = kv
            index += 1
            if val == "":
                if index < len(raw) and raw[index][0] > cur_indent:
                    nested, index = parse_block(index, raw[index][0])
                    result[key] = nested
                else:
                    result[key] = {}
            else:
                result[key] = scalar(val, lineno)
        return result, index

    def parse_list(index: int, indent: int) -> tuple[list[Any], int]:
        result: list[Any] = []
        while index < len(raw):
            cur_indent, text, lineno, _folded = raw[index]
            if cur_indent < indent:
                break
            if cur_indent != indent or not text.startswith("- "):
                break
            rest = text[2:].strip()
            index += 1
            if rest == "":
                if index < len(raw) and raw[index][0] > cur_indent:
                    item, index = parse_block(index, raw[index][0])
                else:
                    item = None
                result.append(item)
                continue
            if rest.startswith("["):
                result.append(scalar(rest, lineno))
                continue
            kv = split_key_value(rest)
            if kv is None:
                result.append(scalar(rest, lineno))
                continue
            key, val = kv
            item: dict[str, Any] = {}
            if val == "":
                if index < len(raw) and raw[index][0] > cur_indent:
                    nested, index = parse_block(index, raw[index][0])
                    item[key] = nested
                else:
                    item[key] = {}
            else:
                item[key] = scalar(val, lineno)
            while index < len(raw) and raw[index][0] > cur_indent:
                next_indent, next_text, next_lineno, _next_folded = raw[index]
                if next_text.startswith("- "):
                    nested, index = parse_block(index, next_indent)
                    if isinstance(nested, list):
                        item.setdefault("_items", []).extend(nested)
                    continue
                kv2 = split_key_value(next_text)
                if kv2 is None:
                    raise ConfigSyntaxError(next_lineno, f"cannot parse line: {next_text}")
                k2, v2 = kv2
                index += 1
                if v2 == "":
                    if index < len(raw) and raw[index][0] > next_indent:
                        nested, index = parse_block(index, raw[index][0])
                        item[k2] = nested
                    else:
                        item[k2] = {}
                else:
                    item[k2] = scalar(v2, next_lineno)
            result.append(item)
        return result, index

    if not raw:
        return {}
    parsed, index = parse_block(0, raw[0][0])
    if index != len(raw):
        _indent, text, lineno, _folded = raw[index]
        previous = raw[index - 1][1] if index > 0 else ""
        previous_kv = split_key_value(previous[2:] if previous.startswith("- ") else previous)
        previous_value = previous_kv[1] if previous_kv else previous
        if previous_value in BLOCK_SCALAR_INDICATORS:
            hint = HINT_BLOCK_SCALAR
        elif previous_value.startswith("{"):
            hint = HINT_FLOW_MAPPING
        elif text.startswith("[") or previous_value.startswith("["):
            hint = HINT_UNTERMINATED_FLOW
        else:
            hint = HINT_MULTILINE_PLAIN
        raise ConfigSyntaxError(
            lineno, f"could not parse full config: unexpected indentation: {text}", hint
        )
    return parsed


def project_root() -> Path:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        if out:
            return Path(out)
    except Exception:
        pass
    return Path(os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())).resolve()


def config_path(args: argparse.Namespace) -> Path:
    override = getattr(args, "config", None) or os.environ.get("ORCH_CONFIG_FILE")
    if override:
        return Path(override).resolve()
    return project_root() / ".orchestration" / "config.yaml"


def load_config(args: argparse.Namespace) -> dict[str, Any]:
    path = config_path(args)
    if not path.is_file():
        fail(f"config file not found: {path}")
    try:
        data = load_simple_yaml(path)
    except EngineError as exc:
        hint = getattr(exc, "hint", "")
        fail(
            f"invalid config syntax in {path}: {exc}"
            + (f"\n  hint: {hint}" if hint else "")
        )
    if not isinstance(data, dict):
        fail("config root must be a mapping")
    data["_config_path"] = str(path)
    return data


def schema_version(cfg: dict[str, Any]) -> str:
    raw = cfg.get("schema_version")
    if raw is None or raw == "":
        return "1"
    return str(raw)


def as_list(value: Any) -> list[Any]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return [value]


def as_bool(value: Any, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "yes", "1", "on"}


def dict_from_named(value: Any, key_names: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    if value is None:
        return {}
    if isinstance(value, dict):
        out: dict[str, dict[str, Any]] = {}
        for name, body in value.items():
            if isinstance(body, dict):
                item = dict(body)
            else:
                item = {"value": body}
            item.setdefault(key_names[0], name)
            out[str(name)] = item
        return out
    if isinstance(value, list):
        out = {}
        for entry in value:
            if not isinstance(entry, dict):
                raise EngineError("named entries must be maps")
            name = None
            for key in key_names:
                if entry.get(key):
                    name = str(entry[key])
                    break
            if not name:
                raise EngineError(f"named entry missing one of: {', '.join(key_names)}")
            out[name] = dict(entry)
        return out
    raise EngineError("named block must be a map or a list of maps")


def workflow(cfg: dict[str, Any]) -> dict[str, Any]:
    wf = cfg.get("workflow", {})
    if not isinstance(wf, dict):
        raise EngineError("workflow must be a map")
    return wf


def branches(cfg: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if schema_version(cfg) == "1":
        out: dict[str, dict[str, Any]] = {}
        if cfg.get("integration_branch"):
            out["integration"] = {"role": "integration", "name": cfg["integration_branch"]}
        if cfg.get("production_branch"):
            out["production"] = {"role": "production", "name": cfg["production_branch"]}
        return out
    return dict_from_named(cfg.get("branches", {}), ("role", "name"))


def transitions(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    wf = workflow(cfg)
    items = wf.get("transitions", cfg.get("workflow_transitions", []))
    if not isinstance(items, list):
        raise EngineError("workflow transitions must be a list")
    return [dict(item) for item in items if isinstance(item, dict)]


def states(cfg: dict[str, Any]) -> list[str]:
    wf = workflow(cfg)
    return [str(item) for item in as_list(wf.get("states", cfg.get("workflow_states", [])))]


def initial_state(cfg: dict[str, Any]) -> str:
    wf = workflow(cfg)
    return str(wf.get("initial_state", cfg.get("workflow_initial_state", "")))


def terminal_states(cfg: dict[str, Any]) -> list[str]:
    wf = workflow(cfg)
    return [str(item) for item in as_list(wf.get("terminal_states", cfg.get("workflow_terminal_states", [])))]


def approvals(cfg: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return dict_from_named(cfg.get("approvals", cfg.get("approval_classes", {})), ("class", "name"))


def adapters(cfg: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return dict_from_named(cfg.get("adapters", {}), ("name", "adapter"))


def environments(cfg: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return dict_from_named(cfg.get("environments", {}), ("role", "name"))


def ci_categories(cfg: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return dict_from_named(cfg.get("ci_categories", {}), ("category", "name"))


def state_dir(cfg: dict[str, Any]) -> Path:
    wf = workflow(cfg)
    raw = wf.get("state_dir", cfg.get("candidate_state_dir", cfg.get("state_dir")))
    if not raw:
        raise EngineError("schema_version 2 requires workflow.state_dir or state_dir")
    path = Path(str(raw))
    if not path.is_absolute():
        path = project_root() / path
    return path


def validate_identifier(kind: str, value: str) -> None:
    if not IDENT_RE.match(value):
        raise EngineError(f"{kind} '{value}' must use only letters, digits, dot, underscore, or dash")


def validate_config(cfg: dict[str, Any]) -> None:
    version = schema_version(cfg)
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise EngineError(f"unsupported configuration schema_version '{version}'")
    worker_trust_profile = str(
        cfg.get("worker_trust_profile", "cooperative-worker")
    )
    if worker_trust_profile not in {"cooperative-worker", "isolated-worker"}:
        raise EngineError(
            "worker_trust_profile must be cooperative-worker or isolated-worker"
        )
    for key in (
        "security_required_when",
        "security_required_source_branches",
        "security_required_target_branches",
    ):
        value = cfg.get(key)
        if value is None or value == "":
            continue
        if not isinstance(value, (str, list)):
            raise EngineError(f"{key} must be a string or list of strings")
        if any(
            not isinstance(item, str) or not item.strip() for item in as_list(value)
        ):
            raise EngineError(f"{key} entries must be non-empty strings")
    if version == "1":
        if not cfg.get("integration_branch"):
            raise EngineError("legacy config requires integration_branch")
        if not cfg.get("production_branch"):
            raise EngineError("legacy config requires production_branch")
        return

    br = branches(cfg)
    for role, item in br.items():
        validate_identifier("branch role", role)
        if not item.get("name") and not item.get("template"):
            raise EngineError(f"branch role '{role}' requires name or template")

    st = states(cfg)
    if not st:
        raise EngineError("workflow.states must not be empty")
    seen_states: set[str] = set()
    for state in st:
        validate_identifier("state", state)
        if state in seen_states:
            raise EngineError(f"duplicate state '{state}'")
        seen_states.add(state)
    init = initial_state(cfg)
    if not init:
        raise EngineError("workflow.initial_state is required")
    if init not in seen_states:
        raise EngineError(f"initial state '{init}' is not declared")
    for state in terminal_states(cfg):
        if state not in seen_states:
            raise EngineError(f"terminal state '{state}' is not declared")
    state_dir(cfg)

    app = approvals(cfg)
    adp = adapters(cfg)
    env = environments(cfg)
    ci = ci_categories(cfg)
    for env_role, env_cfg in env.items():
        for key, value in env_cfg.items():
            if key.endswith("_adapter") and value and str(value) not in adp:
                raise EngineError(
                    f"environment role '{env_role}' references undefined adapter '{value}'"
                )
    ticket_cfg = cfg.get("ticket", {})
    if isinstance(ticket_cfg, dict) and ticket_cfg.get("adapter") and str(ticket_cfg["adapter"]) not in adp:
        raise EngineError(f"ticket integration references undefined adapter '{ticket_cfg['adapter']}'")
    items = transitions(cfg)
    names: set[str] = set()
    for tr in items:
        name = str(tr.get("name", ""))
        if not name:
            raise EngineError("transition missing name")
        validate_identifier("transition", name)
        if name in names:
            raise EngineError(f"duplicate transition '{name}'")
        names.add(name)
    for tr in items:
        name = str(tr.get("name", ""))
        frm = str(tr.get("from", ""))
        to = str(tr.get("to", ""))
        if frm not in seen_states:
            raise EngineError(f"transition '{name}' references unknown from state '{frm}'")
        if to not in seen_states:
            raise EngineError(f"transition '{name}' references unknown to state '{to}'")
        for field in ("source_role", "destination_role", "branch_role"):
            role = tr.get(field)
            if role and str(role) not in br:
                raise EngineError(f"transition '{name}' references undefined branch role '{role}'")
        for field in ("environment_role", "deploy_environment", "promote_environment"):
            role = tr.get(field)
            if role and str(role) not in env:
                raise EngineError(f"transition '{name}' references undefined environment role '{role}'")
        for cls in as_list(tr.get("required_approvals")):
            if str(cls) not in app:
                raise EngineError(f"transition '{name}' references undefined approval class '{cls}'")
        for cat in as_list(tr.get("required_ci")):
            if str(cat) not in ci:
                raise EngineError(f"transition '{name}' references undefined CI category '{cat}'")
        for field in ("adapter", "provider_adapter", "deployment_adapter", "ticket_adapter"):
            adapter = tr.get(field)
            if adapter and str(adapter) not in adp:
                raise EngineError(f"transition '{name}' references undefined adapter '{adapter}'")
        if as_bool(tr.get("adapter_required")) and not tr.get("adapter"):
            raise EngineError(f"transition '{name}' requires an adapter but none is configured")
        for req in as_list(tr.get("requires_completed")):
            if str(req) not in names:
                raise EngineError(f"transition '{name}' requires unknown prior transition '{req}'")


def render_template(template: str, variables: dict[str, str]) -> str:
    def repl(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in variables or variables[key] == "":
            raise EngineError(f"missing template variable '{key}'")
        return variables[key]

    return re.sub(r"\{([A-Za-z0-9_]+)\}", repl, template)


def branch_name(cfg: dict[str, Any], role: str, variables: dict[str, str]) -> str:
    br = branches(cfg)
    if role not in br:
        raise EngineError(f"undefined branch role '{role}'")
    item = br[role]
    if item.get("name"):
        return str(item["name"])
    if item.get("template"):
        return render_template(str(item["template"]), variables)
    raise EngineError(f"branch role '{role}' has no name or template")


def find_transition(cfg: dict[str, Any], name: str) -> dict[str, Any]:
    for tr in transitions(cfg):
        if str(tr.get("name")) == name:
            return tr
    raise EngineError(f"undefined transition '{name}'")


def parse_key_values(items: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise EngineError(f"expected key=value, got '{item}'")
        key, value = item.split("=", 1)
        out[key] = value
    return out


def safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value)


def candidate_dir(cfg: dict[str, Any], candidate_id: str) -> Path:
    return state_dir(cfg) / safe_id(candidate_id)


def state_file(cfg: dict[str, Any], candidate_id: str) -> Path:
    return candidate_dir(cfg, candidate_id) / "state.env"


def event_dir(cfg: dict[str, Any], candidate_id: str) -> Path:
    return candidate_dir(cfg, candidate_id) / "events"


def approval_dir(cfg: dict[str, Any], candidate_id: str) -> Path:
    return candidate_dir(cfg, candidate_id) / "approvals"


def read_kv(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise EngineError(f"state file not found: {path}")
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key] = value
    return out


def write_kv(path: Path, data: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(f"{key}={value}\n" for key, value in sorted(data.items()))
    path.write_text(body, encoding="utf-8")


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(value: str) -> dt.datetime | None:
    try:
        return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    except Exception:
        return None


def write_event(cfg: dict[str, Any], candidate_id: str, data: dict[str, str]) -> None:
    path = event_dir(cfg, candidate_id)
    path.mkdir(parents=True, exist_ok=True)
    stamp = utc_now().replace(":", "").replace("-", "")
    name = data.get("transition", data.get("event", "event"))
    (path / f"{stamp}-{safe_id(name)}.event").write_text(
        "".join(f"{key}={value}\n" for key, value in sorted(data.items())),
        encoding="utf-8",
    )


def events(cfg: dict[str, Any], candidate_id: str) -> list[dict[str, str]]:
    path = event_dir(cfg, candidate_id)
    if not path.is_dir():
        return []
    return [read_kv(item) for item in sorted(path.glob("*.event"))]


def state_has_entry_event(cfg: dict[str, Any], candidate_id: str, state: str) -> bool:
    for event in events(cfg, candidate_id):
        if event.get("to") == state:
            return True
    return False


def completed_transition(cfg: dict[str, Any], candidate_id: str, transition: str) -> bool:
    for event in events(cfg, candidate_id):
        if event.get("transition") == transition:
            return True
    return False


def approval_records(cfg: dict[str, Any], candidate_id: str) -> list[dict[str, str]]:
    path = approval_dir(cfg, candidate_id)
    if not path.is_dir():
        return []
    return [read_kv(item) for item in sorted(path.glob("*.approved"))]


def matching_approval(
    cfg: dict[str, Any],
    candidate_id: str,
    transition: dict[str, Any],
    approval_class: str,
    current: dict[str, str],
) -> dict[str, str] | None:
    approval_cfg = approvals(cfg)[approval_class]
    allowed_actor_types = [str(item) for item in as_list(approval_cfg.get("actor_types"))]
    max_age = approval_cfg.get("max_age_seconds")
    now = dt.datetime.now(dt.timezone.utc)
    for record in approval_records(cfg, candidate_id):
        if record.get("approval_class") != approval_class:
            continue
        if record.get("candidate_id") != candidate_id:
            continue
        approved_transition = record.get("transition", "")
        approved_state = record.get("state", "")
        if not approved_transition and not approved_state:
            continue
        if approved_transition and approved_transition != str(transition["name"]):
            continue
        if approved_state and approved_state != str(transition["to"]):
            continue
        actor_type = record.get("actor_type", "")
        if allowed_actor_types and actor_type not in allowed_actor_types:
            continue
        if as_bool(approval_cfg.get("human_required")) or as_bool(transition.get("human_approval")):
            if actor_type != "human":
                continue
        if current.get("candidate_sha") and record.get("candidate_sha") != current.get("candidate_sha"):
            continue
        if current.get("artifact_id") and record.get("artifact_id") not in {"", current.get("artifact_id")}:
            continue
        if max_age not in {None, ""}:
            approved_at = parse_time(record.get("approved_at", ""))
            if approved_at is None:
                continue
            try:
                if (now - approved_at).total_seconds() > int(str(max_age)):
                    continue
            except ValueError:
                continue
        return record
    return None


def require_config(args: argparse.Namespace) -> dict[str, Any]:
    cfg = load_config(args)
    try:
        validate_config(cfg)
    except EngineError as exc:
        fail(str(exc))
    return cfg


def budget_cap_warnings(cfg: dict[str, Any]) -> list[str]:
    """Explain configured LLM budgets that Orka clamps to a lower hard cap."""
    llm = cfg.get("llm")
    if not isinstance(llm, dict) or not isinstance(llm.get("budgets"), dict):
        return []
    scripts = str(Path(__file__).resolve().parent)
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    try:
        # Lazy: only configurations with LLM budgets load the API runner.
        import api_agent
    except Exception as exc:  # advisory only; never block validation
        return [f"WARNING could not check llm.budgets hard caps: {exc}"]
    config_file = cfg.get("_config_path")
    root = (
        Path(str(config_file)).resolve().parent.parent
        if config_file
        else project_root()
    )
    status = api_agent.host_budget_policy_status(root)
    notes = []
    if status.get("error"):
        notes.append(
            f"WARNING host budget policy unavailable ({status['error']}); "
            "compiled hard caps apply"
        )
    elif status.get("source") == "host-policy":
        caps = ", ".join(f"{key}={value}" for key, value in status["caps"].items())
        notes.append(
            f"NOTE host budget policy {status['policy_id']} raises hard caps: {caps}"
        )
    return notes + [
        f"WARNING llm.budgets.{item['key']}={item['configured']} exceeds the hard cap "
        f"{item['cap']}; Orka enforces {item['effective']}"
        for item in api_agent.budget_cap_violations(cfg, root)
    ]


def cmd_validate(args: argparse.Namespace) -> None:
    cfg = load_config(args)
    try:
        validate_config(cfg)
    except EngineError as exc:
        fail(str(exc))
    for warning in budget_cap_warnings(cfg):
        print(warning, file=sys.stderr)
    print(f"OK schema_version={schema_version(cfg)}")


def cmd_branch_name(args: argparse.Namespace) -> None:
    cfg = require_config(args)
    variables = parse_key_values(args.var)
    try:
        print(branch_name(cfg, args.role, variables))
    except EngineError as exc:
        fail(str(exc))


def guard_policy(cfg: dict[str, Any]) -> tuple[bool, list[str]]:
    version = schema_version(cfg)
    if version == "1":
        blocked = []
        if cfg.get("production_branch"):
            blocked.append(str(cfg["production_branch"]))
        return True, blocked
    mg = cfg.get("merge_guard", {})
    if not isinstance(mg, dict):
        mg = {}
    block_squash = as_bool(mg.get("block_squash"), False)
    blocked: list[str] = []
    variables: dict[str, str] = {}
    for role in as_list(mg.get("blocked_merge_roles")):
        blocked.append(branch_name(cfg, str(role), variables))
    return block_squash, blocked


def cmd_guard_policy(args: argparse.Namespace) -> None:
    cfg = require_config(args)
    try:
        block_squash, blocked = guard_policy(cfg)
    except EngineError as exc:
        fail(str(exc))
    print(f"block_squash={'true' if block_squash else 'false'}")
    for branch in blocked:
        print(f"blocked_branch={branch}")


def normalize_branch(value: str) -> str:
    value = value.strip()
    prefix = "refs/heads/"
    return value[len(prefix):] if value.startswith(prefix) else value


def branch_pattern_match(branch: str, pattern: str) -> bool:
    return fnmatch.fnmatchcase(normalize_branch(branch), normalize_branch(pattern))


def changed_paths(diff_text: str) -> list[str]:
    paths: set[str] = set()
    for line in diff_text.splitlines():
        if not line.startswith(("+++ ", "--- ")):
            continue
        value = line[4:].split("\t", 1)[0]
        if value == "/dev/null":
            continue
        if value.startswith(("a/", "b/")):
            value = value[2:]
        paths.add(value)
    return sorted(paths)


def security_gate_decision(
    cfg: dict[str, Any], source_branch: str, target_branch: str, diff_text: str
) -> dict[str, Any]:
    reasons: list[dict[str, str]] = []
    for pattern in as_list(cfg.get("security_required_source_branches")):
        pattern = str(pattern)
        if branch_pattern_match(source_branch, pattern):
            reasons.append(
                {
                    "kind": "source_branch",
                    "value": normalize_branch(source_branch),
                    "pattern": pattern,
                }
            )
    for pattern in as_list(cfg.get("security_required_target_branches")):
        pattern = str(pattern)
        if branch_pattern_match(target_branch, pattern):
            reasons.append(
                {
                    "kind": "target_branch",
                    "value": normalize_branch(target_branch),
                    "pattern": pattern,
                }
            )

    paths = changed_paths(diff_text)
    lowered_diff = diff_text.lower()
    for trigger_value in as_list(cfg.get("security_required_when")):
        trigger = str(trigger_value).strip()
        lowered = trigger.lower()
        matched_path = next(
            (
                path
                for path in paths
                if lowered in path.lower()
                or (
                    any(char in trigger for char in "*?[")
                    and fnmatch.fnmatchcase(path, trigger)
                )
            ),
            None,
        )
        if lowered == "always":
            reasons.append({"kind": "diff", "value": "always", "pattern": trigger})
        elif matched_path is not None:
            reasons.append({"kind": "diff_path", "value": matched_path, "pattern": trigger})
        elif lowered and lowered in lowered_diff:
            reasons.append({"kind": "diff_content", "value": trigger, "pattern": trigger})

    return {
        "required": bool(reasons),
        "source_branch": normalize_branch(source_branch),
        "target_branch": normalize_branch(target_branch),
        "reasons": reasons,
    }


def cmd_security_gate(args: argparse.Namespace) -> None:
    cfg = require_config(args)
    diff_path = Path(args.diff_file)
    if not diff_path.is_file():
        fail(f"diff file not found: {diff_path}")
    if not args.source_branch.strip() or not args.target_branch.strip():
        fail("security-gate requires non-empty source and target branches")
    diff_text = diff_path.read_text(encoding="utf-8", errors="replace")
    if not diff_text.strip():
        fail("security-gate requires non-empty raw diff evidence")
    decision = security_gate_decision(
        cfg,
        args.source_branch,
        args.target_branch,
        diff_text,
    )
    print(json.dumps(decision, sort_keys=True, separators=(",", ":")))


def transition_plan(cfg: dict[str, Any], transition_name: str, variables: dict[str, str]) -> list[tuple[str, str]]:
    transition = find_transition(cfg, transition_name)
    lines: list[tuple[str, str]] = [
        ("transition", str(transition["name"])),
        ("from", str(transition["from"])),
        ("to", str(transition["to"])),
    ]
    for field in ("source_role", "destination_role", "branch_role"):
        role = transition.get(field)
        if role:
            lines.append((field, str(role)))
            lines.append((field.replace("_role", "_branch"), branch_name(cfg, str(role), variables)))
    for field in ("branch_operation", "environment_role", "operation", "adapter", "deployment_adapter"):
        if transition.get(field):
            lines.append((field, str(transition[field])))
    for key in ("required_evidence", "required_ci", "required_approvals", "requires_completed", "cleanup_actions", "reconciliation_actions", "rollback_actions"):
        vals = [str(item) for item in as_list(transition.get(key))]
        if vals:
            lines.append((key, ",".join(vals)))
    if as_bool(transition.get("candidate_identity_required")):
        lines.append(("candidate_identity_required", "true"))
    if as_bool(transition.get("candidate_identity_updates")):
        lines.append(("candidate_identity_updates", "true"))
    if as_bool(transition.get("artifact_identity_required")):
        lines.append(("artifact_identity_required", "true"))
    if as_bool(transition.get("artifact_identity_updates")):
        lines.append(("artifact_identity_updates", "true"))
    if as_bool(transition.get("tag_required")):
        lines.append(("tag_required", "true"))
    return lines


def cmd_plan_transition(args: argparse.Namespace) -> None:
    cfg = require_config(args)
    if schema_version(cfg) == "1":
        fail("legacy schema has no configurable transition graph")
    variables = parse_key_values(args.var)
    try:
        for key, value in transition_plan(cfg, args.transition, variables):
            print(f"{key}={value}")
    except EngineError as exc:
        fail(str(exc))


def cmd_adapter_plan(args: argparse.Namespace) -> None:
    cfg = require_config(args)
    if schema_version(cfg) == "1":
        fail("legacy schema has no configurable transition graph")
    variables = parse_key_values(args.var)
    try:
        print("policy_engine=orchestration-engine.py")
        for key, value in transition_plan(cfg, args.transition, variables):
            print(f"{key}={value}")
    except EngineError as exc:
        fail(str(exc))


def cmd_init_candidate(args: argparse.Namespace) -> None:
    cfg = require_config(args)
    if schema_version(cfg) == "1":
        fail("candidate state requires schema_version 2")
    candidate = args.candidate_id
    sf = state_file(cfg, candidate)
    if sf.exists():
        fail(f"candidate '{candidate}' already exists")
    state = args.state or initial_state(cfg)
    if state not in states(cfg):
        fail(f"unknown initial candidate state '{state}'")
    data = {
        "candidate_id": candidate,
        "state": state,
        "created_at": utc_now(),
        "updated_at": utc_now(),
    }
    if args.candidate_sha:
        data["candidate_sha"] = args.candidate_sha
    if args.artifact_id:
        data["artifact_id"] = args.artifact_id
    write_kv(sf, data)
    write_event(cfg, candidate, {"event": "init", "candidate_id": candidate, "to": state, "at": utc_now()})
    print(f"candidate={candidate}")
    print(f"state={state}")


def cmd_record_approval(args: argparse.Namespace) -> None:
    cfg = require_config(args)
    if schema_version(cfg) == "1":
        fail("approvals require schema_version 2")
    app = approvals(cfg)
    if args.approval_class not in app:
        fail(f"undefined approval class '{args.approval_class}'")
    actor_types = [str(item) for item in as_list(app[args.approval_class].get("actor_types"))]
    if actor_types and args.actor_type not in actor_types:
        fail(f"actor_type '{args.actor_type}' is not allowed for approval class '{args.approval_class}'")
    if as_bool(app[args.approval_class].get("human_required")) and args.actor_type != "human":
        fail(f"approval class '{args.approval_class}' requires a human actor")
    if not state_file(cfg, args.candidate_id).is_file():
        fail(f"candidate '{args.candidate_id}' has no state")
    path = approval_dir(cfg, args.candidate_id)
    path.mkdir(parents=True, exist_ok=True)
    record = {
        "approval_class": args.approval_class,
        "actor_id": args.actor_id,
        "actor_type": args.actor_type,
        "approved_at": args.approved_at or utc_now(),
        "candidate_id": args.candidate_id,
        "transition": args.transition,
    }
    if args.state:
        record["state"] = args.state
    if args.candidate_sha:
        record["candidate_sha"] = args.candidate_sha
    if args.artifact_id:
        record["artifact_id"] = args.artifact_id
    file_name = f"{safe_id(args.transition)}-{safe_id(args.approval_class)}-{safe_id(args.actor_id)}.approved"
    write_kv(path / file_name, record)
    print(f"approval={path / file_name}")


def validate_transition_attempt(
    cfg: dict[str, Any],
    candidate_id: str,
    transition: dict[str, Any],
    current: dict[str, str],
    args: argparse.Namespace,
) -> dict[str, str]:
    state = current.get("state", "")
    if state != str(transition["from"]):
        raise EngineError(
            f"transition '{transition['name']}' cannot run from state '{state}', expected '{transition['from']}'"
        )
    if not state_has_entry_event(cfg, candidate_id, state):
        raise EngineError(f"state '{state}' has no recorded engine event; refusing possible manual state edit")

    evidence = parse_key_values(args.evidence)
    for key in as_list(transition.get("required_evidence")):
        key = str(key)
        if key not in evidence:
            raise EngineError(f"transition '{transition['name']}' missing required evidence '{key}'")
        if not Path(evidence[key]).is_file():
            raise EngineError(f"required evidence '{key}' file not found: {evidence[key]}")

    ci = parse_key_values(args.ci)
    for category in as_list(transition.get("required_ci")):
        category = str(category)
        if category not in ci:
            raise EngineError(f"transition '{transition['name']}' missing required CI category '{category}'")
        if ci[category].strip().lower() not in PASS_VALUES:
            raise EngineError(f"required CI category '{category}' is not green: {ci[category]}")

    for prior in as_list(transition.get("requires_completed")):
        if not completed_transition(cfg, candidate_id, str(prior)):
            raise EngineError(f"transition '{transition['name']}' requires prior transition '{prior}'")

    if as_bool(transition.get("candidate_identity_required")):
        candidate_sha = args.candidate_sha or current.get("candidate_sha")
        if not candidate_sha:
            raise EngineError(f"transition '{transition['name']}' requires candidate identity")
        if (
            current.get("candidate_sha")
            and args.candidate_sha
            and current["candidate_sha"] != args.candidate_sha
            and not as_bool(transition.get("candidate_identity_updates"))
        ):
            raise EngineError(
                f"candidate identity mismatch: state has {current['candidate_sha']}, attempted {args.candidate_sha}"
            )
        current["candidate_sha"] = candidate_sha
    elif args.candidate_sha:
        if current.get("candidate_sha") and current["candidate_sha"] != args.candidate_sha:
            raise EngineError(
                f"candidate identity mismatch: state has {current['candidate_sha']}, attempted {args.candidate_sha}"
            )
        current["candidate_sha"] = args.candidate_sha

    if as_bool(transition.get("artifact_identity_required")):
        artifact_id = args.artifact_id or current.get("artifact_id")
        if not artifact_id:
            raise EngineError(f"transition '{transition['name']}' requires artifact identity")
        if (
            current.get("artifact_id")
            and args.artifact_id
            and current["artifact_id"] != args.artifact_id
            and not as_bool(transition.get("artifact_identity_updates"))
        ):
            raise EngineError(
                f"artifact identity mismatch: state has {current['artifact_id']}, attempted {args.artifact_id}"
            )
        current["artifact_id"] = artifact_id
    elif args.artifact_id:
        if current.get("artifact_id") and current["artifact_id"] != args.artifact_id:
            raise EngineError(
                f"artifact identity mismatch: state has {current['artifact_id']}, attempted {args.artifact_id}"
            )
        current["artifact_id"] = args.artifact_id

    for approval_class in as_list(transition.get("required_approvals")):
        if matching_approval(cfg, candidate_id, transition, str(approval_class), current) is None:
            raise EngineError(
                f"transition '{transition['name']}' missing fresh approval '{approval_class}' bound to candidate"
            )

    for field in ("adapter", "provider_adapter", "deployment_adapter", "ticket_adapter"):
        adapter = transition.get(field)
        if adapter and str(adapter) not in adapters(cfg):
            raise EngineError(f"transition '{transition['name']}' references undefined adapter '{adapter}'")

    if as_bool(transition.get("tag_required")) and not args.tag:
        raise EngineError(f"transition '{transition['name']}' requires an immutable tag")
    if args.tag:
        try:
            subprocess.check_call(["git", "rev-parse", "-q", "--verify", f"refs/tags/{args.tag}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            raise EngineError(f"tag '{args.tag}' already exists")
        except subprocess.CalledProcessError:
            pass

    return current


def cmd_transition(args: argparse.Namespace) -> None:
    cfg = require_config(args)
    if schema_version(cfg) == "1":
        fail("transitions require schema_version 2")
    try:
        transition = find_transition(cfg, args.transition)
        current = read_kv(state_file(cfg, args.candidate_id))
        updated = validate_transition_attempt(cfg, args.candidate_id, transition, current, args)
    except EngineError as exc:
        fail(str(exc))

    updated["state"] = str(transition["to"])
    updated["updated_at"] = utc_now()
    if args.tag:
        updated["tag"] = args.tag
    if args.dry_run:
        print(f"DRY-RUN transition={transition['name']} from={transition['from']} to={transition['to']}")
        return
    write_kv(state_file(cfg, args.candidate_id), updated)
    event = {
        "event": "transition",
        "transition": str(transition["name"]),
        "candidate_id": args.candidate_id,
        "from": str(transition["from"]),
        "to": str(transition["to"]),
        "at": utc_now(),
    }
    if updated.get("candidate_sha"):
        event["candidate_sha"] = updated["candidate_sha"]
    if updated.get("artifact_id"):
        event["artifact_id"] = updated["artifact_id"]
    if args.tag:
        event["tag"] = args.tag
    write_event(cfg, args.candidate_id, event)
    print(f"transition={transition['name']}")
    print(f"state={transition['to']}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="orchestration policy engine")
    parser.add_argument("--config", help="path to .orchestration/config.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("validate-config")

    branch = sub.add_parser("branch-name")
    branch.add_argument("role")
    branch.add_argument("--var", action="append", default=[])

    sub.add_parser("guard-policy")

    security = sub.add_parser("security-gate")
    security.add_argument("--source-branch", required=True)
    security.add_argument("--target-branch", required=True)
    security.add_argument("--diff-file", required=True)

    plan = sub.add_parser("plan-transition")
    plan.add_argument("transition")
    plan.add_argument("--var", action="append", default=[])

    adapter = sub.add_parser("adapter-plan")
    adapter.add_argument("--host", choices=("claude", "codex"), required=True)
    adapter.add_argument("transition")
    adapter.add_argument("--var", action="append", default=[])

    init = sub.add_parser("init-candidate")
    init.add_argument("candidate_id")
    init.add_argument("--state")
    init.add_argument("--candidate-sha")
    init.add_argument("--artifact-id")

    approval = sub.add_parser("record-approval")
    approval.add_argument("candidate_id")
    approval.add_argument("transition")
    approval.add_argument("approval_class")
    approval.add_argument("actor_id")
    approval.add_argument("actor_type")
    approval.add_argument("--state")
    approval.add_argument("--candidate-sha")
    approval.add_argument("--artifact-id")
    approval.add_argument("--approved-at")

    trans = sub.add_parser("transition")
    trans.add_argument("candidate_id")
    trans.add_argument("transition")
    trans.add_argument("--evidence", action="append", default=[])
    trans.add_argument("--ci", action="append", default=[])
    trans.add_argument("--candidate-sha")
    trans.add_argument("--artifact-id")
    trans.add_argument("--tag")
    trans.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    commands = {
        "validate-config": cmd_validate,
        "branch-name": cmd_branch_name,
        "guard-policy": cmd_guard_policy,
        "security-gate": cmd_security_gate,
        "plan-transition": cmd_plan_transition,
        "adapter-plan": cmd_adapter_plan,
        "init-candidate": cmd_init_candidate,
        "record-approval": cmd_record_approval,
        "transition": cmd_transition,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
