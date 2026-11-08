"""The needs-you kind registries must agree with each other and with the PWA.

A kind is registered in several tables (ActionKind, the validators, the
handlers, the agent set and the PWA's union and list). Nothing else checks that
they agree, and a kind missing from one table fails without an error message.
"""

from __future__ import annotations

import re
from pathlib import Path

from ciao import chat_actions
from ciao.chat_actions import ACTION_KINDS
from ciao.web.chat_actions_runtime import ACTION_HANDLERS

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Kinds in ActionKind with no validator and no handler: validate_params refuses
#: them as unsupported_kind. `limit` is reserved for the engine's usage-limit
#: card, which is not built yet.
UNSUPPORTED_KINDS = frozenset({"limit"})

#: Supported kinds the agent may not raise with `ciao chat needs-action`. None today.
ENGINE_ONLY_KINDS: frozenset[str] = frozenset()


def test_every_kind_with_a_validator_has_a_handler_and_vice_versa() -> None:
    kinds = set(ACTION_KINDS)
    assert UNSUPPORTED_KINDS <= kinds
    assert set(chat_actions._VALIDATORS) == kinds - UNSUPPORTED_KINDS
    assert set(ACTION_HANDLERS) == kinds - UNSUPPORTED_KINDS


def test_agent_kinds_and_per_kind_tables_name_only_supported_kinds() -> None:
    supported = set(ACTION_KINDS) - UNSUPPORTED_KINDS
    assert chat_actions.AGENT_ACTION_KINDS == supported - ENGINE_ONLY_KINDS
    assert set(chat_actions._KEY_PARAMS) <= supported
    assert set(chat_actions.ENGINE_ONLY_PARAMS) <= supported


def test_frontend_kind_union_and_list_match_the_backend_literal() -> None:
    types_path = REPO_ROOT / "web" / "src" / "lib" / "types.ts"
    lib_path = REPO_ROOT / "web" / "src" / "lib" / "chatActions.ts"

    types_text = types_path.read_text(encoding="utf-8")
    union = re.search(r"export type ChatActionKind =([^\n]+)", types_text)
    assert union is not None, f"ChatActionKind not found in {types_path}"
    union_kinds = re.findall(r"'([a-z_]+)'", union.group(1))
    assert sorted(union_kinds) == sorted(ACTION_KINDS)

    lib_text = lib_path.read_text(encoding="utf-8")
    listed = re.search(r"export const CHAT_ACTION_KINDS[^=]*=\s*\[([^\]]*)\]", lib_text)
    assert listed is not None, f"CHAT_ACTION_KINDS not found in {lib_path}"
    listed_kinds = re.findall(r"'([a-z_]+)'", listed.group(1))
    assert sorted(listed_kinds) == sorted(ACTION_KINDS)
