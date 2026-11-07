"""The needs-you action contract: what a chat's transient action may carry.

A chat action is a card the engine or the agent puts in front of the user when
something needs them: an MCP server wants a sign-in, a command has to be run in
the user's own terminal. It is transient and held in memory only, like
``ciao/terminal_offer.py``: a restart, an archive, a new session, a handover or a
dismissal ends it, and it never reaches the chat registry.

This module is the *text and shape* half and it is deliberately pure: it
validates params per kind, computes the dedupe key, builds the payload and
bounds free text. It knows nothing about chats, events, persistence or the wire.

Params are validated per kind. A refusal carries a closed reason from
:data:`ChatActionErrorReason` and never echoes the rejected value, because a
sign-in URL or a command can carry a credential. Ids are random per new action.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, get_args
from urllib.parse import urlsplit

from ciao.execution_modes import MODE_RANK
from ciao.terminal_offer import TerminalOfferError, normalize_terminal_offer

ActionKind = Literal[
    "mcp_auth",
    "command",
    "gws_auth",
    "permission_mode",
    "device_code",
    "system_setting",
    "restart",
    "limit",
]
ActionState = Literal["open", "working", "waiting", "done", "failed"]
ActionOrigin = Literal["engine", "agent"]

ACTION_KINDS: tuple[str, ...] = get_args(ActionKind)
ACTION_STATES: tuple[str, ...] = get_args(ActionState)

#: The kinds an agent may raise with `ciao chat needs-action`. `limit` is the
#: engine's own and is never agent-raisable.
AGENT_ACTION_KINDS: frozenset[str] = frozenset(
    {"command", "gws_auth", "permission_mode", "device_code", "system_setting", "restart", "mcp_auth"}
)

ChatActionErrorReason = Literal[
    "invalid_kind",          # not one of ACTION_KINDS
    "unsupported_kind",      # a known kind with no validator/handler (or no callback) registered yet
    "invalid_type",          # a param value (or the body) has the wrong type
    "invalid_encoding",      # E1 command: not valid UTF-8 text
    "missing_param", "unknown_param",   # unknown_param also covers an engine-only key raised by the agent
    "invalid_param",         # the generic bad value: a str outside a kind's closed set (provider, mode, setting, reason, tool, ...)
    "empty",                 # E1 command: empty after normalization
    "too_long", "invalid_characters", "invalid_url",
    "invalid_body",          # a start/callback body the kind does not accept (E1 command steps)
    "invalid_state",         # start/continue/callback in the wrong state
    "not_local",             # J: a host-only start from a non-loopback caller
    "already_used",          # D1: a second paste-back for the same action
    "no_pending_sign_in",    # F: gws paste-back with no pending sign-in
    "bad_callback_url",      # F: gws paste-back address with the wrong scheme/host/port
    "state_mismatch",        # F: gws paste-back address from a different sign-in
]

#: The mcp_auth provider values the validator accepts.
_MCP_PROVIDERS = ("claude", "opencode")

#: Free-text bounds. A server name is a short identifier; a URL may be long.
_SERVER_MAX = 128
_PROVIDER_MAX = 16
_URL_MAX = 4096
_SESSION_ID_MAX = 64
_NOTE_MAX = 200
_ERROR_MAX = 300

#: The id is an identifier, not a credential: 16 random bytes (128 bits).
_ACTION_ID_BYTES = 16


class ChatActionError(ValueError):
    """A typed refusal from the chat-action contract.

    ``reason`` is one of :data:`ChatActionErrorReason`. The message is the
    reason and nothing else, so the rejected value never reaches a log line.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class ChatAction:
    id: str
    kind: ActionKind
    origin: ActionOrigin
    state: ActionState
    params: Mapping[str, str]
    error: str = ""
    created_at: float = 0.0


def _text(
    raw: Mapping[str, object],
    name: str,
    *,
    required: bool,
    limit: int,
) -> str | None:
    """Return the stripped text of ``raw[name]``, or ``None`` when an optional value is absent or empty."""
    if name not in raw:
        if required:
            raise ChatActionError("missing_param")
        return None
    value = raw[name]
    if not isinstance(value, str):
        raise ChatActionError("invalid_type")
    if len(value) > limit:
        raise ChatActionError("too_long")
    if not all(character.isprintable() for character in value):
        raise ChatActionError("invalid_characters")
    stripped = value.strip()
    if len(stripped) > limit:
        raise ChatActionError("too_long")
    if not stripped:
        if required:
            raise ChatActionError("missing_param")
        return None
    return stripped


#: The helper terminal's id for a Claude sign-in: the engine writes it, never the agent.
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _validate_mcp_auth(raw: Mapping[str, object]) -> dict[str, str]:
    if set(raw) - {"server", "provider", "url", "session_id"}:
        raise ChatActionError("unknown_param")
    server = _text(raw, "server", required=True, limit=_SERVER_MAX)
    provider = _text(raw, "provider", required=True, limit=_PROVIDER_MAX)
    if server is None or provider is None:
        raise ChatActionError("missing_param")
    if provider not in _MCP_PROVIDERS:
        raise ChatActionError("invalid_param")
    validated = {"server": server, "provider": provider}
    url = _text(raw, "url", required=False, limit=_URL_MAX)
    if url is not None:
        parts = urlsplit(url)
        if parts.scheme not in ("https", "http") or not parts.netloc:
            raise ChatActionError("invalid_url")
        validated["url"] = url
    session_id = _text(raw, "session_id", required=False, limit=_SESSION_ID_MAX)
    if session_id is not None:
        if not _SESSION_ID_RE.match(session_id):
            raise ChatActionError("invalid_param")
        validated["session_id"] = session_id
    return validated


_COMMAND_REASONS: dict[str, ChatActionErrorReason] = {
    "invalid_type": "invalid_type",
    "invalid_encoding": "invalid_encoding",
    "too_long": "too_long",
    "empty": "empty",
    "invalid_characters": "invalid_characters",
}


def _validate_command(raw: Mapping[str, object]) -> dict[str, str]:
    """The `command` kind: a shell command the user reviews, then runs.

    The text is model-authored. The shared text contract refuses every
    control character (ESC included), so a command can never close a
    bracketed paste early when the PWA types it. A refusal names a reason
    and never the text.
    """
    if set(raw) - {"command", "note"}:
        raise ChatActionError("unknown_param")
    value = raw.get("command")
    if not isinstance(value, str):
        raise ChatActionError("invalid_type")
    try:
        command = normalize_terminal_offer(value)
    except TerminalOfferError as exc:
        raise ChatActionError(_COMMAND_REASONS[exc.reason]) from None
    params = {"command": command}
    if raw.get("note") is not None:
        params["note"] = normalize_note(raw["note"])
    return params


#: The restart reasons: a changed setting, or a staged update. Ciaobot has no
#: MCP restart: an MCP change is picked up by `ciao chat reload`.
RESTART_REASONS: tuple[str, ...] = ("config", "update")


def _validate_restart(raw: Mapping[str, object]) -> dict[str, str]:
    """The `restart` kind: a card that points at the Settings control that restarts or updates."""
    if set(raw) - {"reason", "note"}:
        raise ChatActionError("unknown_param")
    if "reason" not in raw:
        raise ChatActionError("missing_param")
    reason = raw["reason"]
    if not isinstance(reason, str):
        raise ChatActionError("invalid_type")
    if reason not in RESTART_REASONS:
        raise ChatActionError("invalid_param")
    params = {"reason": reason}
    if raw.get("note") is not None:
        params["note"] = normalize_note(raw["note"])
    return params


def _validate_permission_mode(raw: Mapping[str, object]) -> dict[str, str]:
    """The `permission_mode` kind: a mode the user approves for this chat.

    The mode is a closed set (``MODE_RANK``). The PWA's ``manual`` is refused:
    an agent asks for ``normal``. The key is the kind alone, so one request
    is open per chat and a re-raise replaces it.
    """
    if set(raw) - {"mode", "note"}:
        raise ChatActionError("unknown_param")
    if "mode" not in raw:
        raise ChatActionError("missing_param")
    mode = raw["mode"]
    if not isinstance(mode, str):
        raise ChatActionError("invalid_type")
    if mode not in MODE_RANK:
        raise ChatActionError("invalid_param")
    params = {"mode": mode}
    if raw.get("note") is not None:
        params["note"] = normalize_note(raw["note"])
    return params


#: The device-code tools the card may sign in (a closed set; exact match).
_DEVICE_TOOLS = ("gh", "az")
_DEVICE_CODE_RE = re.compile(r"^[A-Z0-9-]{4,16}$")
_DEVICE_URL_MAX = 200


def _validate_device_code(raw: Mapping[str, object]) -> dict[str, str]:
    """The `device_code` kind: a CLI sign-in the user finishes on any device.

    ``tool`` is a closed set; the command and its arguments come from the
    engine's table, never from here. The engine-only keys are checked too,
    because ``update_action`` revalidates the merged params.
    """
    if set(raw) - {"tool", "note", "session_id", "code", "url", "entered"}:
        raise ChatActionError("unknown_param")
    if "tool" not in raw:
        raise ChatActionError("missing_param")
    tool = raw["tool"]
    if not isinstance(tool, str):
        raise ChatActionError("invalid_type")
    if tool not in _DEVICE_TOOLS:
        raise ChatActionError("invalid_param")
    params = {"tool": tool}
    if raw.get("note") is not None:
        params["note"] = normalize_note(raw["note"])
    session_id = _text(raw, "session_id", required=False, limit=_SESSION_ID_MAX)
    if session_id is not None:
        if not _SESSION_ID_RE.match(session_id):
            raise ChatActionError("invalid_param")
        params["session_id"] = session_id
    code = _text(raw, "code", required=False, limit=16)
    if code is not None:
        if not _DEVICE_CODE_RE.match(code):
            raise ChatActionError("invalid_param")
        params["code"] = code
    url = _text(raw, "url", required=False, limit=_DEVICE_URL_MAX)
    if url is not None:
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.netloc:
            raise ChatActionError("invalid_url")
        params["url"] = url
    entered = _text(raw, "entered", required=False, limit=1)
    if entered is not None:
        if entered != "1":
            raise ChatActionError("invalid_param")
        params["entered"] = entered
    return params


_VALIDATORS: dict[str, Callable[[Mapping[str, object]], dict[str, str]]] = {
    "mcp_auth": _validate_mcp_auth,
    "command": _validate_command,
    "restart": _validate_restart,
    "permission_mode": _validate_permission_mode,
    "device_code": _validate_device_code,
}
_KEY_PARAMS: dict[str, tuple[str, ...]] = {"mcp_auth": ("server",), "device_code": ("tool",)}

#: Params only the engine writes (through ``update_action``). The kind's
#: validator accepts them; an agent raise may never carry them.
ENGINE_ONLY_PARAMS: dict[str, frozenset[str]] = {
    "mcp_auth": frozenset({"url", "session_id"}),
    "device_code": frozenset({"session_id", "code", "url", "entered"}),
}


def check_agent_params(kind: str, raw: Mapping[str, object]) -> None:
    """Refuse an agent raise that carries an engine-only param."""
    if set(raw) & ENGINE_ONLY_PARAMS.get(kind, frozenset()):
        raise ChatActionError("unknown_param")


def new_action_id() -> str:
    """A fresh random action id, never a counter."""
    return secrets.token_hex(_ACTION_ID_BYTES)


def validate_params(kind: str, raw: Mapping[str, object]) -> dict[str, str]:
    """Validate and normalize ``raw`` for ``kind``. Refuses before any state changes."""
    if kind not in ACTION_KINDS:
        raise ChatActionError("invalid_kind")
    validator = _VALIDATORS.get(kind)
    if validator is None:
        raise ChatActionError("unsupported_kind")
    return validator(raw)


def action_key(kind: str, params: Mapping[str, str]) -> str:
    """The dedupe key: the kind plus its key params, or the kind alone."""
    names = _KEY_PARAMS.get(kind, ())
    return ":".join([kind, *(params[name] for name in names)])


def action_payload(action: ChatAction) -> dict[str, object]:
    """The wire shape of one action. A fresh dict and a fresh params dict."""
    return {
        "id": action.id,
        "kind": action.kind,
        "origin": action.origin,
        "state": action.state,
        "params": dict(action.params),
        "error": action.error,
        "created_at": action.created_at,
    }


def _printable_text(text: object, *, limit: int) -> str:
    """Return ``text`` stripped, refusing non-text, invisible characters and overlong values."""
    if not isinstance(text, str):
        raise ChatActionError("invalid_type")
    if not all(character.isprintable() for character in text):
        raise ChatActionError("invalid_characters")
    stripped = text.strip()
    if len(stripped) > limit:
        raise ChatActionError("too_long")
    return stripped


def normalize_note(text: object) -> str:
    """A short user-visible note: empty is fine, otherwise printable and at most 200 characters."""
    return _printable_text(text, limit=_NOTE_MAX)


def normalize_error(text: str) -> str:
    """The first non-empty line of a failure message, printable and short.

    An error is one line on a card. Non-printable characters are dropped, runs of
    whitespace collapse, and anything longer than 300 characters is cut to 299
    characters plus an ellipsis.
    """
    first = next((line for line in text.splitlines() if line.strip()), "")
    printable = "".join(character for character in first if character.isprintable())
    collapsed = " ".join(printable.split())
    if len(collapsed) > _ERROR_MAX:
        return collapsed[: _ERROR_MAX - 1] + "…"
    return collapsed
