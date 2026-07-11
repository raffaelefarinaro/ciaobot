from __future__ import annotations

from pathlib import Path

from ciao.public_release import (
    PUBLIC_EXPORT_ALLOWLIST,
    PublicReleaseFinding,
    export_public_tree,
    load_private_patterns,
    is_public_export_allowlisted,
    main,
    scan_public_export,
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_public_export_allowlist_names_expected_public_artifacts() -> None:
    assert "ciao/" in PUBLIC_EXPORT_ALLOWLIST
    assert "web/" in PUBLIC_EXPORT_ALLOWLIST
    assert "tests/" in PUBLIC_EXPORT_ALLOWLIST
    assert ".github/workflows/" in PUBLIC_EXPORT_ALLOWLIST
    assert "pyproject.toml" in PUBLIC_EXPORT_ALLOWLIST
    assert "README.md" in PUBLIC_EXPORT_ALLOWLIST
    assert not is_public_export_allowlisted("memory-vault/personal/MEMORY.md")
    assert not is_public_export_allowlisted(".env")
    assert not is_public_export_allowlisted(".mcp.json")


def test_public_export_scan_flags_private_paths(tmp_path: Path) -> None:
    _write(tmp_path / "ciao" / "main.py", "print('ok')\n")
    _write(tmp_path / "memory-vault" / "personal" / "MEMORY.md", "private\n")
    _write(tmp_path / ".env", "TOKEN=secret\n")
    _write(tmp_path / "secrets" / "client_secret_gws.json", "{}\n")

    report = scan_public_export(tmp_path)

    assert PublicReleaseFinding("forbidden_path", "memory-vault/personal/MEMORY.md", "memory-vault/") in report.findings
    assert PublicReleaseFinding("forbidden_path", ".env", ".env") in report.findings
    assert PublicReleaseFinding("forbidden_path", "secrets/client_secret_gws.json", "secrets/") in report.findings


def test_public_export_scan_flags_private_strings(tmp_path: Path) -> None:
    _write(tmp_path / "README.md", "Ciaobot public docs\n")
    _write(tmp_path / "ciao" / "config.py", "contact='private-person@example.com'\n")
    _write(tmp_path / "web" / "src" / "x.ts", "const host = 'private-host.example.com'\n")
    _write(tmp_path / "tests" / "fixture.py", "company = 'PrivateCo'\n")

    report = scan_public_export(
        tmp_path,
        private_patterns=(
            "private-person@example.com",
            "private-host.example.com",
            "PrivateCo",
        ),
    )

    assert PublicReleaseFinding("private_string", "ciao/config.py", "private-person@example.com") in report.findings
    assert PublicReleaseFinding("private_string", "web/src/x.ts", "private-host.example.com") in report.findings
    assert PublicReleaseFinding("private_string", "tests/fixture.py", "PrivateCo") in report.findings


def test_public_export_cli_returns_nonzero_for_findings(tmp_path: Path, capsys) -> None:
    _write(tmp_path / ".env", "TOKEN=secret\n")

    rc = main([str(tmp_path)])

    captured = capsys.readouterr()
    assert rc == 1
    assert "forbidden_path\t.env\t.env" in captured.out


def test_private_patterns_can_load_from_file(tmp_path: Path) -> None:
    pattern_file = tmp_path / "patterns.txt"
    pattern_file.write_text(
        "\n# comments ignored\nprivate-person@example.com\n\nPrivateCo\n",
        encoding="utf-8",
    )

    assert load_private_patterns(pattern_file) == ("private-person@example.com", "PrivateCo")


def test_public_release_module_does_not_embed_private_raffa_markers() -> None:
    source = Path(__file__).parents[1] / "ciao" / "public_release.py"
    text = source.read_text(encoding="utf-8")

    forbidden_literals = (
        "private-person@example.com",
        "private-host.example.com",
        "PrivateCo",
        "/Users/private-person",
    )
    for literal in forbidden_literals:
        assert literal not in text


def test_export_public_tree_copies_only_allowlisted_paths(tmp_path: Path) -> None:
    source = tmp_path / "source"
    dest = tmp_path / "dest"
    _write(source / "ciao" / "main.py", "print('public')\n")
    _write(source / "web" / "src" / "main.ts", "console.log('public')\n")
    _write(source / "scripts" / "vault_index.py", "print('public')\n")
    _write(source / ".github" / "workflows" / "ci.yml", "name: Ciaobot CI\n")
    _write(source / "scripts" / "morning-briefing.py", "private workspace helper\n")
    _write(source / "pyproject.toml", "[project]\nname='ciao'\n")
    _write(source / "memory-vault" / "personal" / "MEMORY.md", "private\n")
    _write(source / ".env", "TOKEN=secret\n")
    _write(source / ".runtime" / "state.json", "{}\n")
    _write(source / "deploy" / "com.ciao.server.plist", "private paths\n")
    _write(source / "deploy" / "homebrew" / "ciaobot.rb", "class Ciaobot < Formula\nend\n")

    copied = export_public_tree(source, dest)

    assert sorted(copied) == [
        ".github/workflows/ci.yml",
        "ciao/main.py",
        "pyproject.toml",
        "scripts/vault_index.py",
        "web/src/main.ts",
    ]
    assert (dest / "ciao" / "main.py").is_file()
    assert not (dest / "memory-vault").exists()
    assert not (dest / ".env").exists()
    assert not (dest / ".runtime").exists()
    assert not (dest / "deploy" / "com.ciao.server.plist").exists()
    assert not (dest / "scripts" / "morning-briefing.py").exists()


def test_env_example_is_exported_but_real_dotenv_files_are_forbidden(tmp_path: Path) -> None:
    source = tmp_path / "source"
    dest = tmp_path / "dest"
    _write(source / ".env.example", "PWA_AUTH_TOKEN=changeme\n")
    _write(source / ".env", "PWA_AUTH_TOKEN=secret\n")
    _write(source / ".env.local", "PWA_AUTH_TOKEN=secret\n")

    # `.env.example` is allowlisted and ships; `.env`/`.env.local` never do.
    assert is_public_export_allowlisted(".env.example")
    copied = export_public_tree(source, dest)
    assert ".env.example" in copied
    assert ".env" not in copied
    assert ".env.local" not in copied
    assert (dest / ".env.example").is_file()

    # Scanning must not flag the template, but must still flag real dotenv files.
    report = scan_public_export(dest)
    assert report.ok
    forbidden = scan_public_export(source)
    flagged = {finding.path for finding in forbidden.findings if finding.kind == "forbidden_path"}
    assert ".env" in flagged
    assert ".env.local" in flagged
    assert ".env.example" not in flagged


def test_export_public_tree_uses_generic_claude_overlay(tmp_path: Path) -> None:
    source = tmp_path / "source"
    dest = tmp_path / "dest"
    _write(source / "CLAUDE.md", "PrivateCo assistant prompt\n")
    _write(source / "ciao" / "stock" / "public" / "CLAUDE.md", "Generic public contributor prompt\n")

    copied = export_public_tree(source, dest)

    assert "CLAUDE.md" in copied
    assert (dest / "CLAUDE.md").read_text(encoding="utf-8") == "Generic public contributor prompt\n"


def test_export_public_tree_refuses_non_empty_destination(tmp_path: Path) -> None:
    source = tmp_path / "source"
    dest = tmp_path / "dest"
    _write(source / "README.md", "public\n")
    _write(dest / "old.txt", "existing\n")

    try:
        export_public_tree(source, dest)
    except ValueError as exc:
        assert "destination must be empty" in str(exc)
    else:
        raise AssertionError("non-empty destination should fail")
