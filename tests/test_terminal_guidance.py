"""Drift tests for the terminal and install/settings capability guidance.

The guidance is prose, so these tests pin the facts it has to keep true
rather than its sentences: the MCP command names the Settings inventory
actually hands the user, the Settings tab labels the app actually renders,
the label the terminal button actually carries, and the boundaries the
terminal offer keeps. None of them is a snapshot of the wording, and none
of them is skipped.

Ownership: this file checks the ``ciao chat terminal-offer`` command form on
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
#: it never types, runs or starts anything, it never brings the shell's
#: output back, the engine has not checked the text, and the provider's own
#: transcript may still hold the tool arguments — so the guidance never
#: promises that a suggested command leaves no trace. ("Never unlocks" used
#: to be one of them: the terminal used to ask for the password again, and
#: the card had no business doing that. There is no unlock secret any more,
#: so the boundary that still matters is the one above.)
OFFER_BOUNDARIES = (
    "never types",
    "never runs",
    "never starts a shell",
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
            agent_cli.build_parser().parse_args(["chat", "terminal-offer", "--help"])
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


def _inventory_command_names() -> set[str]:
    """The MCP command names the Settings inventory puts in front of the user.

    Derived from the inventory rather than listed here, so a command it
    starts handing out has to be documented, and one it stops handing out
    does not have to be un-documented.
    """
    # The Settings page builds its commands in the PWA (web/src/lib/shellQuote.ts,
    # `mcpCommand`); the inventory only supplies the `cd` prefix.
    builder = (REPO / "web" / "src" / "lib" / "shellQuote.ts").read_text(encoding="utf-8")
    names = set(re.findall(r"\b((?:claude|opencode) mcp [a-z][a-z-]*)", builder))
    assert {"claude mcp add", "opencode mcp add"} <= names, (
        "shellQuote.ts no longer parses into MCP commands"
    )
    return names


def test_settings_inventory_commands_are_the_ones_the_guidance_names() -> None:
    """The guidance must name the commands the Settings panel hands out."""
    emitted = _inventory_command_names()
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
    assert "https://github.com/googleworkspace/cli" in guidance


def test_google_workspace_oauth_docs_match_supported_client_type() -> None:
    integrations = (REPO / "INTEGRATIONS.md").read_text(encoding="utf-8")
    oauth_setup = integrations.split("**Getting `client_secret.json`.**", 1)[1].split("\n\n", 1)[0]
    assert "Desktop app" in oauth_setup
    assert "Web application clients are not supported" in oauth_setup


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
    section = _section(_reference("terminal.md"), "Offering a command")
    missing = sorted(term for term in OFFER_BOUNDARIES if term not in section)
    assert not missing, f"the offer section never mentions {missing}"


def test_guidance_documents_the_offer_command_form() -> None:
    """The offer's documented form.

    The parser side of the same form is checked in
    ``test_the_documented_offer_form_is_the_form_the_parser_accepts``.
    """
    guidance = _reference("terminal.md")
    assert "ciao chat terminal-offer --command-file" in guidance
    assert "Suggested command — review before running" in guidance
    assert "placeholder" in guidance, "the guidance must say secrets stay placeholders"


def test_the_documented_offer_form_is_the_form_the_parser_accepts() -> None:
    """The guidance's command form is the form the real parser takes."""
    guidance = _reference("terminal.md")
    assert "ciao chat terminal-offer --command-file FILE" in guidance
    # The flag and its metavar as one token, so a metavar change to
    # FILEPATH (a substring match) cannot pass unnoticed.
    assert re.search(r"--command-file\s+FILE(?!\w)", _offer_help()), (
        "the parser renders another form"
    )
    parsed = agent_cli.build_parser().parse_args(
        ["chat", "terminal-offer", "--command-file", "command.txt"]
    )
    assert parsed.command_file == "command.txt"


def test_the_offer_takes_a_command_file_and_nothing_else() -> None:
    """``--command-file`` is required, and there is no target and no inline text.

    A ``--chat`` argument would let a caller put a command on a screen the
    user did not ask for, and an inline ``--command`` would put the text on a
    command line, so the parser refuses both.
    """
    assert _refused(["chat", "terminal-offer"]), "--command-file must be required"
    assert _refused(
        ["chat", "terminal-offer", "--command-file", "command.txt", "--chat", "other"]
    ), "the offer takes no target"
    # With the required file present, the only objection left is `--command`
    # itself; without it argparse would refuse for the missing file and the
    # assertion could never observe an inline option.
    assert _refused(
        ["chat", "terminal-offer", "--command-file", "command.txt", "--command", "echo hello"]
    ), "the offer takes no inline command"
    assert _refused(
        ["chat", "terminal-offer", "--command-file", "command.txt", "echo hello"]
    ), "the offer takes no positional text"


def test_the_offer_reaches_the_calling_chat_only(tmp_path: Path) -> None:
    """The parsed form resolves to the operation the guidance names."""
    command_file = tmp_path / "command.txt"
    command_file.write_text("echo hello\n", encoding="utf-8")
    parsed = agent_cli.build_parser().parse_args(
        ["chat", "terminal-offer", "--command-file", str(command_file)]
    )
    operation, arguments = agent_cli.resolve(parsed)
    assert operation == "chat_terminal_offer"
    assert arguments["command"] == "echo hello"
    assert "chat_id" not in arguments, "an offer belongs to the calling chat"


def test_the_offer_is_a_documented_cli_command() -> None:
    """The command is in the CLI's own command table."""
    commands = json.loads(
        (STOCK / "skills" / "ciao-cli" / "commands.json").read_text(encoding="utf-8")
    )
    assert commands.get("chat terminal-offer") == "chat_terminal_offer"


def test_capability_skill_points_at_the_guidance() -> None:
    """Discoverability: the skill names both surfaces and the offer."""
    skill = _skill()
    assert "references/terminal.md" in skill
    assert "references/install-and-settings.md" in skill
    assert "terminal-offer" in skill


def test_chat_mcp_setup_runs_in_the_workspace_folder() -> None:
    """A chat-offered MCP command starts in the workspace folder.

    The chat's terminal opens in the linked repository, and a project-scoped
    add writes into the folder the shell is in, so the guidance must prefix
    the command with the same ``cd`` Settings' Run in terminal uses.
    """
    guidance = _reference("install-and-settings.md")
    assert "cd -- '<workspace folder>' && " in guidance
    assert "Set-Location -LiteralPath '<workspace folder>'; " in guidance
    assert "never the linked repository" in guidance

    # The prefix forms the server builds (ciao/web/mcp_inventory.py `_shell_prefix`);
    # the source text is checked, not the helper, so both platforms see the same forms.
    server = (REPO / "ciao" / "web" / "mcp_inventory.py").read_text(encoding="utf-8")
    assert 'f"cd -- {shlex.quote(root)} && "' in server
    assert "f\"Set-Location -LiteralPath '{_double_ps_quotes(root)}'; \"" in server
