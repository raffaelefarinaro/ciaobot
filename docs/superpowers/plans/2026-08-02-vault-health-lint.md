# Vault Health Lint Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend `ciao vault-lint` and `ciao os-audit` with focused frontmatter and relative Markdown-link validation for issue #237.

**Architecture:** Keep `ciao.vault_lint.run_validation()` as the single read-only scan entry point. Build an in-memory record for each readable Markdown file, run the existing wikilink/orphan/duplicate checks over those records, then add frontmatter and Markdown-link findings to the same result. The CLI and OS audit only format and count the returned lists.

**Tech Stack:** Python 3.12, `pathlib`, `urllib.parse`, PyYAML 6, pytest.

---

## Constraints

- Work only in the issue worktree and branch:
  - Worktree: `/Users/raffaelefarinaro/repos/ciao/.worktrees/ciaobot-issue-237-vault-health`
  - Branch: `feat/issue-237-vault-health`
- Read `README.md` and `docs/superpowers/specs/2026-08-02-vault-health-lint-design.md` before editing.
- Preserve all existing wikilink, orphan, duplicate, exclusion, and unreadable-file behavior unless this plan explicitly changes it.
- Do not add a schedule, taxonomy validation, OKF validation, URL fetching, heading-anchor validation, or auto-repair.
- Do not modify the real vault during implementation or verification. The final live-vault command is read-only.
- Do not restart the server, rebuild the PWA, push the branch, open a PR, or close the issue.
- Use `rtk` before every shell command, as required by the repository instructions.

## Task 1: Add frontmatter validation and a one-read file model

**Files:**

- Modify: `tests/test_vault_lint.py`
- Modify: `ciao/vault_lint.py`

- [ ] **Step 1: Add focused frontmatter tests**

Add a helper near the top of `tests/test_vault_lint.py` so new fixtures can opt into valid frontmatter without obscuring the individual cases:

```python
def _page(body: str = "", *, page_type: object = "note") -> str:
    rendered_type = yaml.safe_dump(
        {"type": page_type},
        sort_keys=False,
    ).strip()
    return f"---\n{rendered_type}\n---\n{body}"
```

Import `yaml`, then add parameterized tests that assert the exact one-finding-per-file contract:

```python
@pytest.mark.parametrize(
    ("content", "kind"),
    [
        ("# No frontmatter\n", "missing_frontmatter"),
        ("---\ntype: [\n---\n", "malformed_frontmatter"),
        ("---\n- note\n---\n", "malformed_frontmatter"),
        ("---\ntitle: Missing type\n---\n", "missing_type"),
        ("---\ntype: '   '\n---\n", "missing_type"),
        ("---\ntype: 42\n---\n", "invalid_type"),
    ],
)
def test_frontmatter_validation_reports_one_error_per_page(
    tmp_path: Path,
    content: str,
    kind: str,
) -> None:
    vault = tmp_path / "memory-vault"
    vault.mkdir()
    (vault / "Page.md").write_text(content, encoding="utf-8")

    issues = vault_lint.run_validation(vault)

    assert issues["frontmatter_errors"] == [
        {
            "source": "Page.md",
            "kind": kind,
            "message": {
                "missing_frontmatter": "frontmatter is missing",
                "malformed_frontmatter": "frontmatter is malformed",
                "missing_type": "frontmatter type is missing or empty",
                "invalid_type": "frontmatter type must be a string",
            }[kind],
        }
    ]
```

Add tests for a valid page and the filename rules:

```python
def test_valid_frontmatter_has_no_error(tmp_path: Path) -> None:
    vault = tmp_path / "memory-vault"
    vault.mkdir()
    (vault / "Page.md").write_text(_page("# Page\n"), encoding="utf-8")

    assert vault_lint.run_validation(vault)["frontmatter_errors"] == []


def test_reserved_frontmatter_exemptions_are_case_insensitive(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "memory-vault"
    vault.mkdir()
    for name in ("INDEX.md", "memory.md", "LoG.Md"):
        (vault / name).write_text("No frontmatter\n", encoding="utf-8")

    assert vault_lint.run_validation(vault)["frontmatter_errors"] == []


def test_readme_still_requires_frontmatter(tmp_path: Path) -> None:
    vault = tmp_path / "memory-vault"
    vault.mkdir()
    (vault / "README.md").write_text("# Project\n", encoding="utf-8")

    errors = vault_lint.run_validation(vault)["frontmatter_errors"]
    assert [(item["source"], item["kind"]) for item in errors] == [
        ("README.md", "missing_frontmatter")
    ]
```

- [ ] **Step 2: Run the focused tests and confirm they fail for the missing result key**

Run:

```bash
rtk .venv/bin/python -m pytest tests/test_vault_lint.py -q -p no:cacheprovider
```

Expected: FAIL, with `KeyError: 'frontmatter_errors'` or an equivalent assertion failure because the validator does not return that list yet.

- [ ] **Step 3: Introduce readable file records and frontmatter validation**

In `ciao/vault_lint.py`, add imports and constants:

```python
from dataclasses import dataclass

import yaml

_FRONTMATTER_RE = re.compile(
    r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)",
    re.DOTALL,
)
_FRONTMATTER_EXEMPT = {"index.md", "memory.md", "log.md"}


@dataclass(frozen=True)
class _VaultFile:
    path: Path
    relative: Path
    content: str
```

Add a helper that returns at most one exact finding:

```python
def _frontmatter_error(file: _VaultFile) -> dict[str, str] | None:
    if file.relative.name.lower() in _FRONTMATTER_EXEMPT:
        return None

    match = _FRONTMATTER_RE.match(file.content)
    if match is None:
        return {
            "source": file.relative.as_posix(),
            "kind": "missing_frontmatter",
            "message": "frontmatter is missing",
        }

    try:
        metadata = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        metadata = None
    if not isinstance(metadata, dict):
        return {
            "source": file.relative.as_posix(),
            "kind": "malformed_frontmatter",
            "message": "frontmatter is malformed",
        }

    page_type = metadata.get("type")
    if page_type is None or (isinstance(page_type, str) and not page_type.strip()):
        return {
            "source": file.relative.as_posix(),
            "kind": "missing_type",
            "message": "frontmatter type is missing or empty",
        }
    if not isinstance(page_type, str):
        return {
            "source": file.relative.as_posix(),
            "kind": "invalid_type",
            "message": "frontmatter type must be a string",
        }
    return None
```

Refactor `run_validation()` so it initializes both new result keys now, collects every included readable Markdown file once, and reuses `record.content` for existing wikilink checks:

```python
issues: dict[str, list[Any]] = {
    "broken_links": [],
    "orphans": [],
    "duplicates": [],
    "frontmatter_errors": [],
    "broken_markdown_links": [],
}

vault_files: list[_VaultFile] = []
for path in sorted(vault_root.rglob("*.md")):
    try:
        rel = path.relative_to(vault_root)
    except ValueError:
        continue
    if any(part in EXCLUDE_DIRS for part in rel.parts):
        continue
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        continue
    vault_files.append(_VaultFile(path=path, relative=rel, content=content))
```

Then preserve the old selection semantics with explicit subsets:

```python
link_target_files = [
    file
    for file in vault_files
    if file.relative.name not in {"INDEX.md", "MEMORY.md"}
]
files_to_scan = [
    file
    for file in link_target_files
    if not _is_template(file.path.stem)
]
```

Use `file.relative.as_posix()` where the old code used `str(rel)`. Remove the second `path.read_text()` in the wikilink loop and read workspace `MEMORY.md` content from a `records_by_path` dictionary when available. This is the required one-read behavior:

```python
records_by_path = {file.path: file for file in vault_files}
for mem_file in memory_roots:
    record = records_by_path.get(mem_file)
    if record is not None:
        memory_links.update(_links_in(record.content))
```

Finally, after records are loaded, append frontmatter findings in deterministic path order:

```python
for file in vault_files:
    error = _frontmatter_error(file)
    if error is not None:
        issues["frontmatter_errors"].append(error)
```

Do not exempt `README.md`. Do not scan excluded directories. Keep reserved files in `vault_files` so Task 2 can validate their Markdown links.

- [ ] **Step 4: Run all vault-lint tests**

Run:

```bash
rtk .venv/bin/python -m pytest tests/test_vault_lint.py -q -p no:cacheprovider
```

Expected: PASS. Existing tests may now return frontmatter findings, but their assertions about existing keys must remain unchanged.

- [ ] **Step 5: Commit the frontmatter slice**

Run:

```bash
rtk git add ciao/vault_lint.py tests/test_vault_lint.py
rtk git commit -m "feat: validate vault frontmatter"
```

Expected: one commit containing only the validator and focused tests.

## Task 2: Validate relative Markdown destinations

**Files:**

- Modify: `tests/test_vault_lint.py`
- Modify: `ciao/vault_lint.py`

- [ ] **Step 1: Add link behavior tests**

Add a helper that creates a one-file vault with valid frontmatter:

```python
def _single_page_vault(tmp_path: Path, body: str) -> Path:
    vault = tmp_path / "memory-vault"
    vault.mkdir()
    (vault / "Page.md").write_text(_page(body), encoding="utf-8")
    return vault
```

Add tests for valid and missing links, images, and reference definitions:

```python
def test_relative_markdown_links_images_and_references(tmp_path: Path) -> None:
    vault = _single_page_vault(
        tmp_path,
        "[ok](notes/ok.md)\n"
        "![missing image](images/missing.png)\n"
        "[ref]: notes/missing.md\n",
    )
    (vault / "notes").mkdir()
    (vault / "notes" / "ok.md").write_text(_page("ok"), encoding="utf-8")

    broken = vault_lint.run_validation(vault)["broken_markdown_links"]

    assert [(item["target"], item["kind"]) for item in broken] == [
        ("images/missing.png", "missing_target"),
        ("notes/missing.md", "missing_target"),
    ]
```

Add a normalization test:

```python
def test_markdown_link_normalization(tmp_path: Path) -> None:
    vault = _single_page_vault(
        tmp_path,
        "[title](notes/ok.md \"Optional title\")\n"
        "[query](notes/ok.md?raw=1#section)\n"
        "[encoded](notes/space%20name.md)\n",
    )
    notes = vault / "notes"
    notes.mkdir()
    (notes / "ok.md").write_text(_page(), encoding="utf-8")
    (notes / "space name.md").write_text(_page(), encoding="utf-8")

    assert vault_lint.run_validation(vault)["broken_markdown_links"] == []
```

Add ignored-destination and code tests:

```python
def test_markdown_link_ignores_non_local_and_documented_examples(
    tmp_path: Path,
) -> None:
    vault = _single_page_vault(
        tmp_path,
        "[web](https://example.com/a)\n"
        "[mail](mailto:test@example.com)\n"
        "[protocol relative](//example.com/a)\n"
        "[root](/docs/a.md)\n"
        "[anchor](#status)\n"
        "[empty]()\n"
        "[placeholder](<path/<name>.md>)\n"
        "`[inline](missing-inline.md)`\n"
        "```md\n[fenced](missing-fenced.md)\n```\n",
    )

    assert vault_lint.run_validation(vault)["broken_markdown_links"] == []
```

Add escape handling and reserved-file coverage:

```python
def test_escaped_markdown_link_is_ignored(tmp_path: Path) -> None:
    vault = _single_page_vault(tmp_path, r"\[example](missing.md)")
    assert vault_lint.run_validation(vault)["broken_markdown_links"] == []


def test_reserved_file_markdown_links_are_still_validated(tmp_path: Path) -> None:
    vault = tmp_path / "memory-vault"
    vault.mkdir()
    (vault / "INDEX.md").write_text("[missing](missing.md)\n", encoding="utf-8")

    broken = vault_lint.run_validation(vault)["broken_markdown_links"]
    assert [(item["source"], item["target"]) for item in broken] == [
        ("INDEX.md", "missing.md")
    ]
```

Add the outside-vault privacy test:

```python
def test_markdown_link_outside_vault_does_not_probe_target(tmp_path: Path) -> None:
    vault = _single_page_vault(tmp_path, "[outside](../private.md)\n")
    (tmp_path / "private.md").write_text("secret", encoding="utf-8")

    broken = vault_lint.run_validation(vault)["broken_markdown_links"]

    assert broken == [
        {
            "source": "Page.md",
            "target": "../private.md",
            "resolved": "../private.md",
            "kind": "outside_vault",
        }
    ]
```

- [ ] **Step 2: Run the new link tests and confirm they fail**

Run:

```bash
rtk .venv/bin/python -m pytest tests/test_vault_lint.py -q -p no:cacheprovider
```

Expected: FAIL because `broken_markdown_links` is still always empty.

- [ ] **Step 3: Add destination extraction and resolution**

In `ciao/vault_lint.py`, add imports and deliberately limited destination regexes:

```python
from urllib.parse import unquote, urlsplit

_INLINE_MARKDOWN_LINK_RE = re.compile(
    r"!?\[[^\]\n]*\]\(\s*(<[^>\n]+>|[^\s)]+)"
    r"(?:\s+(?:\"[^\"\n]*\"|'[^'\n]*'|\([^()\n]*\)))?\s*\)"
)
_REFERENCE_DESTINATION_RE = re.compile(
    r"(?m)^[ \t]{0,3}\[[^\]\n]+\]:[ \t]*(<[^>\n]+>|[^\s]+)"
)
```

Reuse the existing code-stripping behavior through one helper, then keep `_links_in()` on top of it:

```python
def _without_code(text: str) -> str:
    return _INLINE_CODE_RE.sub("", _FENCE_RE.sub("", text))
```

Add destination extraction. It must preserve source order across inline and reference destinations, ignore escaped inline syntax, and remove angle brackets only when they enclose the entire destination:

```python
def _markdown_destinations_in(text: str):
    stripped = _without_code(text)
    matches = [
        (match.start(), match.group(1), match)
        for pattern in (_INLINE_MARKDOWN_LINK_RE, _REFERENCE_DESTINATION_RE)
        for match in pattern.finditer(stripped)
    ]
    for _, raw, match in sorted(matches, key=lambda item: item[0]):
        if match.start() > 0 and stripped[match.start() - 1] == "\\":
            continue
        target = raw.strip()
        if target.startswith("<") and target.endswith(">"):
            target = target[1:-1].strip()
        yield target
```

Add a pure helper for each candidate. Check containment before any existence check:

```python
def _markdown_link_error(
    *,
    vault_root: Path,
    file: _VaultFile,
    target: str,
) -> dict[str, str] | None:
    if not target or "<" in target or ">" in target:
        return None
    if target.startswith(("//", "/", "#")):
        return None

    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc:
        return None
    decoded_path = unquote(parsed.path)
    if not decoded_path:
        return None

    root = vault_root.resolve()
    resolved = (file.path.parent / decoded_path).resolve(strict=False)
    try:
        relative = resolved.relative_to(root)
    except ValueError:
        return {
            "source": file.relative.as_posix(),
            "target": target,
            "resolved": Path(decoded_path).as_posix(),
            "kind": "outside_vault",
        }

    if resolved.exists():
        return None
    return {
        "source": file.relative.as_posix(),
        "target": target,
        "resolved": relative.as_posix(),
        "kind": "missing_target",
    }
```

Iterate every readable record, including `INDEX.md`, `MEMORY.md`, and `log.md`, but still excluding files under `EXCLUDE_DIRS`:

```python
for file in vault_files:
    for target in _markdown_destinations_in(file.content):
        error = _markdown_link_error(
            vault_root=vault_root,
            file=file,
            target=target,
        )
        if error is not None:
            issues["broken_markdown_links"].append(error)
```

Keep findings deterministic by using the already sorted file records and source-order destinations. The optional-title syntax is consumed by the extraction regex and is not part of `target`.

- [ ] **Step 4: Run the full validator test file**

Run:

```bash
rtk .venv/bin/python -m pytest tests/test_vault_lint.py -q -p no:cacheprovider
```

Expected: PASS, including all prior wikilink, orphan, duplicate, and exclusion tests.

- [ ] **Step 5: Commit Markdown-link validation**

Run:

```bash
rtk git add ciao/vault_lint.py tests/test_vault_lint.py
rtk git commit -m "feat: validate vault markdown links"
```

Expected: one commit containing the link validator and its tests.

## Task 3: Report and count the new findings

**Files:**

- Modify: `tests/test_vault_lint.py`
- Modify: `tests/test_os_audit.py`
- Modify: `ciao/cli.py`
- Modify: `ciao/os_audit.py`

- [ ] **Step 1: Add CLI output and exit-code tests**

Import `argparse` and `from ciao import cli` in `tests/test_vault_lint.py`. Add a test that calls the command function directly and isolates the two new sections:

```python
def test_vault_lint_cli_reports_new_findings(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vault = tmp_path / "memory-vault"
    vault.mkdir()
    (vault / "Page.md").write_text("[missing](missing.md)\n", encoding="utf-8")

    result = cli._vault_lint_command(argparse.Namespace(vault_root=vault))

    output = capsys.readouterr().out
    assert result == 1
    assert "### Frontmatter Errors" in output
    assert "Page.md" in output
    assert "missing_frontmatter" in output
    assert "### Broken Markdown Links" in output
    assert "missing.md" in output
    assert "missing_target" in output
```

Add a clean test using valid frontmatter and no links:

```python
def test_vault_lint_cli_clean_exit(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vault = tmp_path / "memory-vault"
    vault.mkdir()
    (vault / "Page.md").write_text(_page(), encoding="utf-8")

    result = cli._vault_lint_command(argparse.Namespace(vault_root=vault))

    assert result == 0
    assert capsys.readouterr().out.strip() == "Vault is clean!"
```

- [ ] **Step 2: Add OS-audit payload, total, and Markdown tests**

First, keep `test_run_os_audit_counts_every_actionable_finding` focused on its existing eleven findings by wrapping its three content pages in valid frontmatter:

```python
(ideas / "same.md").write_text(
    "---\ntype: idea\n---\n# One\n\n[[missing-target]]\n",
    encoding="utf-8",
)
(resources / "same.md").write_text(
    "---\ntype: resource\n---\n# Two\n",
    encoding="utf-8",
)
proposals.write_text(
    "---\ntype: note\n---\n- [memory] pending fact  _(from: Decisions)_\n",
    encoding="utf-8",
)
```

Then add a separate focused test:

```python
def test_os_audit_counts_and_formats_new_vault_findings(tmp_path: Path) -> None:
    workspace, vault, runtime, bounded = _healthy_roots(tmp_path)
    page = vault / "personal" / "Page.md"
    page.parent.mkdir(parents=True)
    page.write_text("[missing](missing.md)\n", encoding="utf-8")

    report = run_os_audit(
        workspace_dir=workspace,
        vault_root=vault,
        runtime_dir=runtime,
        memory_dir=bounded,
    )

    assert len(report["vault_hygiene"]["frontmatter_errors"]) == 1
    assert len(report["vault_hygiene"]["broken_markdown_links"]) == 1
    assert report["total_issues"] == 2
    assert report["status"] == "needs_attention"
    markdown = format_audit_markdown(report)
    assert "Frontmatter errors: 1" in markdown
    assert "Broken Markdown links: 1" in markdown
```

- [ ] **Step 3: Run the reporting tests and confirm they fail**

Run:

```bash
rtk .venv/bin/python -m pytest tests/test_vault_lint.py tests/test_os_audit.py -q -p no:cacheprovider
```

Expected: FAIL because the CLI has no new sections and OS audit does not count or format the new findings.

- [ ] **Step 4: Add CLI sections**

In `ciao/cli.py::_vault_lint_command`, add these blocks before the orphan section:

```python
if issues["frontmatter_errors"]:
    has_issues = True
    print("### Frontmatter Errors\n")
    for item in issues["frontmatter_errors"]:
        print(
            f"- `{item['source']}`: {item['message']} "
            f"(`{item['kind']}`)"
        )
    print()

if issues["broken_markdown_links"]:
    has_issues = True
    print("### Broken Markdown Links\n")
    for item in issues["broken_markdown_links"]:
        print(
            f"- `{item['source']}` links to `{item['target']}`: "
            f"`{item['kind']}` (resolved: `{item['resolved']}`)"
        )
    print()
```

This keeps the current `0`/`1` contract because both blocks set `has_issues`.

- [ ] **Step 5: Add OS-audit defaults, totals, and Markdown counts**

In both empty/failure dictionaries in `ciao/os_audit.py::_vault_audit`, add:

```python
"frontmatter_errors": [],
"broken_markdown_links": [],
```

In `run_os_audit()`, add both lengths to `actionable_count`:

```python
+ len(vault_result.get("frontmatter_errors", []))
+ len(vault_result.get("broken_markdown_links", []))
```

In `format_audit_markdown()`, add after the broken-wikilink line:

```python
f"- Frontmatter errors: {len(report['vault_hygiene'].get('frontmatter_errors', []))}",
f"- Broken Markdown links: {len(report['vault_hygiene'].get('broken_markdown_links', []))}",
```

- [ ] **Step 6: Run focused reporting tests**

Run:

```bash
rtk .venv/bin/python -m pytest tests/test_vault_lint.py tests/test_os_audit.py -q -p no:cacheprovider
```

Expected: PASS.

- [ ] **Step 7: Commit the reporting slice**

Run:

```bash
rtk git add ciao/cli.py ciao/os_audit.py tests/test_vault_lint.py tests/test_os_audit.py
rtk git commit -m "feat: report vault health findings"
```

Expected: one commit containing CLI and audit integration plus tests.

## Task 4: Document and verify the completed capability

**Files:**

- Modify: `docs/ARCHITECTURE.md`

- [ ] **Step 1: Update the architecture description**

Change the `ciao/vault_lint.py` line in `docs/ARCHITECTURE.md` to:

```text
vault_lint.py                Read-only vault health validation for frontmatter, Markdown links, wikilinks, orphans, and duplicate stems. CLI: `ciao vault-lint`.
```

Do not change install steps, environment variables, endpoints, commands, or schedules. No other documentation file needs an update for this focused capability extension.

- [ ] **Step 2: Run formatting and focused validation**

Run:

```bash
rtk git diff --check
rtk .venv/bin/python -m pytest tests/test_vault_lint.py tests/test_os_audit.py -q -p no:cacheprovider
```

Expected: no whitespace errors and all focused tests pass.

- [ ] **Step 3: Run the full Python suite**

Run:

```bash
rtk .venv/bin/python -m pytest -q -p no:cacheprovider
```

Expected: the entire suite passes. Record the exact pass/skip counts and warnings in the delegate report.

- [ ] **Step 4: Run the validator against the current vault without modifying it**

Run:

```bash
rtk .venv/bin/ciao vault-lint --vault-root /Users/raffaelefarinaro/repos/ciao/memory-vault
```

Expected: exit `0` if clean or exit `1` if pre-existing findings exist. Report only counts by section and a few sanitized path examples. Do not repair, rewrite, or stage vault files.

- [ ] **Step 5: Commit the documentation update**

Run:

```bash
rtk git add docs/ARCHITECTURE.md
rtk git commit -m "docs: describe vault health validation"
```

Expected: one documentation-only commit.

- [ ] **Step 6: Final implementation audit**

Run:

```bash
rtk git status --short --branch
rtk git log --oneline origin/main..HEAD
rtk git diff --stat origin/main...HEAD
rtk rg -n "TODO|TBD|FIXME|placeholder" ciao/vault_lint.py ciao/cli.py ciao/os_audit.py tests/test_vault_lint.py tests/test_os_audit.py docs/ARCHITECTURE.md
```

Expected:

- The worktree is clean.
- The design and implementation-plan commits are present, followed by the small implementation commits.
- No unresolved placeholders or debugging leftovers exist.
- No unrelated files changed.

Report the commit list, exact test commands/results, live-vault finding counts, and any design deviation. Do not push or open a pull request.
