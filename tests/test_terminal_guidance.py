"""Drift tests for the terminal and install/settings capability guidance.

The guidance is prose, so these tests pin the facts it has to keep true
rather than its sentences: the MCP command names the Settings inventory
actually hands the user, the Settings tab labels the app actually renders,
the label the terminal button actually carries, and the boundaries a
needs-action command keeps. None of them is a snapshot of the wording, and none
of them is skipped.

Ownership: this file checks the ``ciao chat needs-action command`` command form on
both sides — the form the guidance documents and the form the CLI parser
accepts — so the two cannot drift apart.
"""

from __future__ import annotations

import contextlib
import io
import json
import re
from pathlib import Path

import ciao
import pytest
from ciao import agent_cli

STOCK = Path(ciao.__file__).parent / "stock"
CAPABILITIES = STOCK / "skills" / "ciao-capabilities"
REPO = Path(__file__).resolve().parents[1]

#: Where the guidance must not send a user. The settings sidebar renders no
#: tab called Home — it renders General — and Automations is a page of its
#: own rather than a tab. The general check below covers both arrows; this
#: list names the paths that were actually wrong.
WRONG_SETTINGS_PATHS = ("Settings → Home", "Settings -> Home", "Settings → Automations")

#: Claims that would mislead about what the terminal and the offer do.
WRONG_TERMINAL_CLAIMS = (
    "Repository section",
    "the agent cannot offer",
    "executes the command",
    "safe to run",
    "the card holds the token",
)

#: The boundaries the offer keeps, in the words the guidance uses for them:
#: the card types the text only on the user's click and never presses Enter,
#: it never runs anything, it never brings the shell's output back, the
#: engine has not checked the text, and the provider's own transcript may
#: still hold the tool arguments — so the guidance never promises that a
#: suggested command leaves no trace. ("Never unlocks" used to be one of
#: them: the terminal used to ask for the password again, and the card had no
#: business doing that. There is no unlock secret any more, so the boundary
#: that still matters is the one above.)
OFFER_BOUNDARIES = (
    "never runs",
    "press",
    "Enter",
    "never brings",
    "output",
    "has not checked",
    "transcript",
    "never promise",
)


def _reference(name: str) -> str:
    return (CAPABILITIES / "references" / name).read_text(encoding="utf-8")


def _skill() -> str:
    return (CAPABILITIES / "SKILL.md").read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    """The body of one ``##`` section."""
    start = text.index(f"## {heading}")
    body = text[start + len("## ") :]
    end = body.find("\n## ")
    return body if end == -1 else body[:end]


def _offer_help() -> str:
    """The offer subcommand's own help, as argparse renders it."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        with pytest.raises(SystemExit) as caught:
            agent_cli.build_parser().parse_args(["chat", "needs-action", "--help"])
    assert caught.value.code == 0, "argparse did not print the offer's help"
    return buffer.getvalue()


def _refused(argv: list[str]) -> bool:
    """Whether the real parser refuses ``argv`` with argparse's usage error."""
    with contextlib.redirect_stderr(io.StringIO()):
        with pytest.raises(SystemExit) as caught:
            agent_cli.build_parser().parse_args(argv)
    return caught.value.code == 2


def _settings_tab_labels() -> set[str]:
    """The labels the settings sidebar renders."""
    nav = (REPO / "web" / "src" / "lib" / "settingsNav.ts").read_text(encoding="utf-8")
    labels = set(re.findall(r"label: '([^']+)'", nav))
    assert labels, "settingsNav.ts no longer parses into tab labels"
    return labels


def _terminal_button_label() -> str:
    """The label the terminal button actually carries."""
    button = (REPO / "web" / "src" / "components" / "BrowserTerminalButton.vue").read_text(
        encoding="utf-8"
    )
    labels = re.findall(r">([^<>{}]*?)</button>", button)
    assert labels, "BrowserTerminalButton.vue no longer carries a button label"
    return str(labels[0]).strip()


def _inventory_command_names(monkeypatch: pytest.MonkeyPatch) -> set[str]:
    """The MCP command names the Settings MCP buttons type for the user.

    Taken from the backend constants the buttons use, so a command the
    buttons start typing has to be documented.
    """
    # Settings -> MCP servers and Chat details render one button per agent that
    # types the agent's launch command (`mcp_command` in /api/settings/providers)
    # and then the follow-up input `mcp_slash`.
    from ciao import setup_status
    from ciao import opencode_cli as opencode

    # The bare form, as the server hands it out when the user's terminal
    # resolves the binary.
    monkeypatch.setattr("ciao.tool_path.resolve_on_terminal_path", lambda cmd: f"/usr/bin/{cmd}")
    return {
        setup_status.claude_launch_command(""),
        opencode.opencode_launch_command(""),
        setup_status.MCP_SLASH_COMMAND,
        opencode.OPENCODE_MCP_SLASH,
    }


def test_settings_inventory_commands_are_the_ones_the_guidance_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guidance must name the commands the Settings panel hands out."""
    emitted = _inventory_command_names(monkeypatch)
    assert emitted, "the inventory hands out no MCP command"
    guidance = _reference("install-and-settings.md")
    missing = sorted(command for command in emitted if command not in guidance)
    assert not missing, f"the guidance does not name {missing}"


def test_guidance_names_every_settings_tab_the_sidebar_renders() -> None:
    """Every real tab label is usable in the guidance."""
    guidance = _reference("install-and-settings.md")
    missing = sorted(label for label in _settings_tab_labels() if label not in guidance)
    assert not missing, f"the guidance never names the {missing} tab"


def test_every_settings_path_names_a_real_tab() -> None:
    """A settings path has to start with a tab the sidebar really renders.

    Both arrow forms are checked, because the guidance is written by hand
    and the ASCII arrow has crept in before.
    """
    first_words = {label.split()[0] for label in _settings_tab_labels()}
    guidance = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((CAPABILITIES / "references").glob("*.md"))
    )
    heads = re.findall(r"(?<!System )Settings (?:→|->) ([A-Za-z][A-Za-z]*)", guidance)
    assert heads, "no settings path to check"
    unknown = sorted({head for head in heads if head not in first_words})
    assert not unknown, f"these settings paths name no real tab: {unknown}"


def test_the_paths_point_at_the_tab_the_sidebar_renders() -> None:
    """The real tab is named, and the Automations page stays findable."""
    guidance = _reference("install-and-settings.md")
    for wrong in WRONG_SETTINGS_PATHS:
        assert wrong not in guidance, f"{wrong!r} is not a path in the app"
    assert "Settings → General" in guidance
    assert "Automations" in guidance, "the Automations page still has to be findable"


def test_guidance_cites_the_official_provider_mcp_sources() -> None:
    """Version-sensitive commands need a source the reader can check."""
    guidance = _reference("install-and-settings.md")
    assert "https://code.claude.com/docs/en/mcp" in guidance
    assert "https://opencode.ai/v2/docs/mcp-servers" in guidance


def test_terminal_guidance_avoids_misleading_claims() -> None:
    """The terminal and the offer keep the boundaries they promise."""
    guidance = _reference("terminal.md")
    for claim in WRONG_TERMINAL_CLAIMS:
        assert claim not in guidance, f"{claim!r} misstates the terminal"
    skill = _skill()
    for claim in ("the agent cannot offer", "safe to run"):
        assert claim not in skill, f"{claim!r} misstates the offer"


def test_terminal_guidance_names_the_entry_the_app_offers() -> None:
    """The button label in the guidance is the label the component renders."""
    guidance = _reference("terminal.md")
    assert _terminal_button_label() in guidance


def test_the_offer_keeps_its_boundaries() -> None:
    """The offer is guidance the user acts on, and the guidance says so."""
    section = _section(_reference("terminal.md"), "Asking the user to run a command")
    missing = sorted(term for term in OFFER_BOUNDARIES if term not in section)
    assert not missing, f"the offer section never mentions {missing}"


def test_guidance_documents_the_offer_command_form() -> None:
    """The offer's documented form.

    The parser side of the same form is checked in
    ``test_the_documented_offer_form_is_the_form_the_parser_accepts``.
    """
    guidance = _reference("terminal.md")
    assert "ciao chat needs-action command --command-file" in guidance
    assert "Suggested command — review before running" in guidance
    assert "placeholder" in guidance, "the guidance must say secrets stay placeholders"


def test_the_documented_offer_form_is_the_form_the_parser_accepts() -> None:
    """The guidance's command form is the form the real parser takes."""
    guidance = _reference("terminal.md")
    assert "ciao chat needs-action command --command-file FILE" in guidance
    # The flag and its metavar as one token, so a metavar change to
    # FILEPATH (a substring match) cannot pass unnoticed.
    assert re.search(r"--command-file\s+FILE(?!\w)", _offer_help()), (
        "the parser renders another form"
    )
    parsed = agent_cli.build_parser().parse_args(
        ["chat", "needs-action", "command", "--command-file", "command.txt"]
    )
    assert parsed.command_file == "command.txt"


def test_the_offer_takes_a_command_file_and_nothing_else() -> None:
    """``--command-file`` is required, and there is no target and no inline text.

    A ``--chat`` argument would let a caller put a command on a screen the
    user did not ask for, and an inline ``--command`` would put the text on a
    command line, so the parser refuses both.
    """
    assert _refused(["chat", "needs-action"]), "the kind must be required"
    assert _refused(
        ["chat", "needs-action", "command", "--command-file", "command.txt", "--chat", "other"]
    ), "the offer takes no target"
    # With the required file present, the only objection left is `--command`
    # itself; without it argparse would refuse for the missing file and the
    # assertion could never observe an inline option.
    assert _refused(
        ["chat", "needs-action", "command", "--command-file", "command.txt", "--command", "echo hello"]
    ), "the offer takes no inline command"
    assert _refused(
        ["chat", "needs-action", "command", "--command-file", "command.txt", "echo hello"]
    ), "the offer takes no positional text"


def test_the_cli_takes_no_mcp_auth_kind_and_no_server_flag() -> None:
    assert _refused(["chat", "needs-action", "mcp_auth", "--server", "linear"])
    assert _refused(["chat", "needs-action", "restart", "--reason", "config", "--server", "linear"])
    assert _refused(["chat", "needs-action", "restart", "--reason", "config", "--provider", "claude"])


def test_the_offer_reaches_the_calling_chat_only(tmp_path: Path) -> None:
    """The parsed form resolves to the operation the guidance names."""
    command_file = tmp_path / "command.txt"
    command_file.write_text("echo hello\n", encoding="utf-8")
    parsed = agent_cli.build_parser().parse_args(
        ["chat", "needs-action", "command", "--command-file", str(command_file)]
    )
    operation, arguments = agent_cli.resolve(parsed)
    assert operation == "chat_needs_action"
    assert arguments["params"]["command"] == "echo hello"
    assert "chat_id" not in arguments, "a request belongs to the calling chat"
    assert "chat_id" not in arguments["params"]


def test_the_offer_is_a_documented_cli_command() -> None:
    """The command is in the CLI's own command table."""
    commands = json.loads(
        (STOCK / "skills" / "ciao-cli" / "commands.json").read_text(encoding="utf-8")
    )
    assert commands.get("chat needs-action") == "chat_needs_action"
    assert "chat terminal-offer" not in commands


def test_capability_skill_points_at_the_guidance() -> None:
    """Discoverability: the skill names both surfaces and the offer."""
    skill = _skill()
    assert "references/terminal.md" in skill
    assert "references/install-and-settings.md" in skill
    assert "needs-action" in skill
    assert "terminal-offer" not in skill


def test_chat_mcp_setup_runs_in_the_workspace_folder() -> None:
    """A chat-offered MCP command starts in the workspace folder.

    The chat's terminal opens in the linked repository, and a project-scoped
    add writes into the folder the shell is in, so the guidance must prefix
    the command with the same ``cd`` Settings' Run in terminal uses.
    """
    guidance = _reference("install-and-settings.md")
    assert "cd -- '<workspace folder>' && " in guidance
    assert "never the linked repository" in guidance


def _assert_mcp_recovery_location(index: str, settings: str, cli: str) -> None:
    settings_mcp = "\n".join(
        line for line in settings.splitlines()
        if line.startswith((
            "- **Asked in a chat", "- **Say the next step", "- **MCP servers in Settings",
            "- **Adding a server", "- **Claude Code**", "- **OpenCode**",
            "- **A workspace's `.mcp.json`", "- **Reloading after a change",
            "- **The manual bridge", "- **Never report a step",
        ))
    )
    reload = cli.split("## Reloading tools", 1)[1].split("## Projects", 1)[0]
    row = next(line for line in cli.splitlines() if line.startswith("| `chat needs-action KIND"))
    intro = row.split(" `command --command-file", 1)[0]
    for text in (index, settings_mcp, reload):
        assert "Settings → MCP" in text
    for text in (index, settings_mcp, reload, intro):
        assert not re.search(
            r"(?:mcp|sign[ -]in|auth(?:entication)?).{0,60}"
            r"(?:card|tile|above the composer|fold(?:ed|s|ing)?)\b",
            text, re.IGNORECASE,
        )
    assert "mcp_auth" not in row
    assert "actions show as a card above the composer" in intro
    assert "device_code" in row


def _assert_manual_bridge_claims_no_managed_sign_in(text: str) -> None:
    assert "no managed MCP sign-in" in text
    assert "no callback paste-back" in text
    assert "signing in is the CLI's own" in text
    assert "manually pastes and runs" in text
    assert not re.search(r"Managed remote OAuth", text, re.IGNORECASE)
    assert not re.search(r"supports callback paste-back", text, re.IGNORECASE)


def _mcp_sources() -> tuple[str, str, str]:
    return (
        _section(_skill(), "Connecting providers"),
        _reference("install-and-settings.md"),
        agent_cli._help_text("chat"),
    )


def test_guidance_places_mcp_signin_in_chat_details_not_inline_cards() -> None:
    _assert_mcp_recovery_location(*_mcp_sources())


@pytest.mark.parametrize(("source", "old", "new"), [
    (0, "whose button runs", "whose button; MCP authentication card above the composer, runs"),
    (1, "A server that still needs a sign-in is signed in with the CLI's own MCP command.", "A server that still needs a sign-in is signed in with the CLI's own MCP command. Sign-in requests fold under one row."),
    (2, " `permission_mode --mode", " `mcp_auth --server NAME`: ask the user to sign in to an MCP server. `permission_mode --mode"),
    (2, "direct the user to Settings → MCP, whose button runs the agent's own MCP command", "direct the user to the MCP authentication card above the composer, which runs the agent's own MCP command"),
])
def test_mcp_location_contract_rejects_inline_mutations(source: int, old: str, new: str) -> None:
    texts = list(_mcp_sources())
    assert old in texts[source]
    texts[source] = texts[source].replace(old, new)
    with pytest.raises(AssertionError):
        _assert_mcp_recovery_location(*texts)


def test_sidebar_terminal_guidance_keeps_user_control_boundaries() -> None:
    terminal = _reference("terminal.md")
    for fact in ("Chat details → Open → Terminal", "Open in Terminal", "engine runs as", "agent cannot drive", "cannot read", "bypass permissions", "Repository", "Workspace", "repository is not required"):
        assert fact in terminal


def test_manual_bridge_claims_no_managed_mcp_sign_in() -> None:
    guidance = _reference("install-and-settings.md")
    bridge = next(line for line in guidance.splitlines() if line.startswith("- **The manual bridge.**"))
    _assert_manual_bridge_claims_no_managed_sign_in(bridge)


def test_mcp_recovery_copies_only_and_requires_user_terminal_step() -> None:
    guidance = _reference("install-and-settings.md")
    bridge = next(line for line in guidance.splitlines() if line.startswith("- **The manual bridge.**"))
    for fact in ("only copies the command", "Chat details → Open → Terminal",
                 "Workspace if offered", "review, paste and run"):
        assert fact in bridge
    offer = next(line for line in guidance.splitlines() if line.startswith("- **The agent offers, the user runs.**"))
    assert "suggestion card types" in offer


@pytest.mark.parametrize(("old", "new"), [
    ("no managed MCP sign-in", "managed MCP sign-in"),
    ("no callback paste-back", "callback paste-back"),
    ("signing in is the CLI's own", "signing in is Ciaobot's own"),
    ("manually pastes and runs", "never pastes or runs"),
])
def test_manual_bridge_contract_rejects_contradictions(old: str, new: str) -> None:
    guidance = _reference("install-and-settings.md")
    bridge = next(line for line in guidance.splitlines() if line.startswith("- **The manual bridge.**"))
    assert old in bridge
    with pytest.raises(AssertionError):
        _assert_manual_bridge_claims_no_managed_sign_in(bridge.replace(old, new))
