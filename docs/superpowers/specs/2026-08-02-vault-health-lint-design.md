# Vault health lint design

Date: 2026-08-02

Issue: [#237](https://github.com/raffaelefarinaro/ciaobot/issues/237)

Status: approved for implementation planning

## Problem

`ciao vault-lint` currently finds broken wikilinks, orphaned pages, and near-duplicate filenames. It does not check two basic properties of a markdown vault:

1. Content pages should have valid YAML frontmatter with a non-empty `type`.
2. Relative Markdown links should point to files or directories that exist.

The weekly Workspace hygiene schedule already runs `ciao os-audit --json`, and `os-audit` already calls `vault_lint.run_validation()`. Adding a second `vault-lint` command to the schedule would duplicate the scan and create two reporting paths for the same findings.

## Goal

Extend the existing validation result with frontmatter and relative Markdown-link findings. Show the findings in both `ciao vault-lint` and `ciao os-audit`, using their current exit-code conventions.

The implementation must be deterministic, read-only, and suitable for commit gates and unattended Workspace hygiene runs.

## Non-goals

- Taxonomy-driven index grouping.
- Declaring or enforcing OKF conformance.
- Editing vault pages automatically.
- Adding another command to the Workspace hygiene schedule.
- Validating remote URLs or heading anchors.
- Replacing the current wikilink, orphan, or duplicate checks.

## File selection

The validator keeps the current directory exclusions for logs, templates, generated provider projections, virtual environments, dependency folders, caches, and version-control metadata.

Frontmatter validation applies to content pages only. The following filenames are exempt, case-insensitively:

- `index.md`
- `memory.md`
- `log.md`

`README.md` is not exempt. Project and folder README files are canonical content pages and should use the vault frontmatter schema.

Reserved files remain eligible for Markdown-link validation. A generated index or curated memory page can still contain a broken relative link.

## Frontmatter validation

Each content page produces at most one frontmatter finding. The result key is `frontmatter_errors`, with records shaped as:

```json
{
  "source": "personal/People/Alice.md",
  "kind": "missing_frontmatter",
  "message": "frontmatter is missing"
}
```

Supported `kind` values:

- `missing_frontmatter`: the file does not start with a complete `---` frontmatter block.
- `malformed_frontmatter`: YAML parsing fails or the parsed root is not a mapping.
- `missing_type`: the mapping has no `type`, or its string value is empty after trimming.
- `invalid_type`: `type` is present but is not a string.

This change does not enforce every field in the broader vault schema. Requiring `title`, `description`, dates, and tags would turn a focused health check into a migration project. Those fields can be added later if real drift shows the need.

## Relative Markdown-link validation

The result key is `broken_markdown_links`, with records shaped as:

```json
{
  "source": "personal/projects/active/example/README.md",
  "target": "./missing-notes.md",
  "resolved": "personal/projects/active/example/missing-notes.md",
  "kind": "missing_target"
}
```

The validator checks inline links and images, such as `[notes](./notes.md)` and `![diagram](images/flow.png)`, plus reference-style destinations such as `[notes]: ./notes.md`.

Before resolving a destination, it:

1. Removes an optional Markdown title.
2. Removes query parameters and fragments.
3. Decodes percent-escaped path characters.
4. Resolves the path relative to the source file's directory.

A target is valid when the resolved file or directory exists inside the vault root.

The validator ignores:

- `http`, `https`, `mailto`, `data`, and other URI schemes.
- Protocol-relative URLs beginning with `//`.
- Absolute filesystem or site-root paths.
- Pure in-page anchors such as `#status`.
- Empty destinations.
- Placeholder destinations containing `<` or `>`.
- Link syntax inside fenced code blocks or inline code spans.

A relative destination that escapes the vault root is reported with `kind: outside_vault`. The validator does not read or disclose whether the outside target exists.

The parser only needs to recognize Markdown link destinations. It does not need to render CommonMark or validate whether referenced headings exist.

## Data flow and reporting

`vault_lint.run_validation()` remains the single validation entry point. Its result gains two lists:

```text
run_validation(vault_root)
  -> existing wikilink, orphan, and duplicate checks
  -> frontmatter_errors
  -> broken_markdown_links
  -> one combined result dictionary
```

`ciao vault-lint` prints a section for each non-empty list and returns exit code `1` when any finding exists. A clean vault still returns `0` and prints `Vault is clean!`.

`ciao os-audit` already calls `run_validation()`. It will:

- Include both new lists under `vault_hygiene` in JSON output.
- Add their lengths to `actionable_count` and `total_issues`.
- Show both counts in the Markdown report.
- Keep unreadable files and validator crashes in `scan_errors`, preserving the current distinction between actionable findings and unreliable evidence.

No schedule change is required. The shipped Workspace hygiene routine will receive the new findings through its existing `os-audit` call.

## Performance and failure behavior

The scan should remain linear in the number of Markdown files and links. Build the set of valid vault paths once, then use constant-time membership checks while validating destinations.

Read each Markdown file once per `run_validation()` call. A small internal record can retain the relative path, raw text, and parsed frontmatter so the checks do not reopen files independently.

The validator must not write, normalize, or repair vault content. Malformed YAML and missing targets are findings, not exceptions. Existing unreadable-file behavior remains unchanged: `os-audit` records unreadable files as scan errors, while `vault-lint` continues scanning the files it can read.

## Tests

Add focused tests for:

- Valid frontmatter with a non-empty `type`.
- Missing, malformed, non-mapping, empty-type, and non-string-type frontmatter.
- Case-insensitive reserved filename exemptions.
- `README.md` remaining subject to frontmatter validation.
- Valid and broken relative links.
- Relative images and reference-style destinations.
- Query strings, fragments, percent-escaped paths, and optional titles.
- External URLs, absolute paths, pure anchors, placeholders, and code examples being ignored.
- A relative path that escapes the vault root.
- CLI output and exit codes with each new finding type.
- `os-audit` totals, JSON payloads, and Markdown summary counts.
- Existing wikilink, orphan, and duplicate tests remaining unchanged.

The implementation is complete when the focused tests and full Python suite pass, `ciao vault-lint` can run against the current vault, and the command reports any pre-existing content findings without modifying them.
