# OS Audit Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development or executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Make `ciao os-audit` a trustworthy, automation-ready health check and integrate it into the existing weekly workspace-hygiene routine.

**Architecture:** Keep audit-specific collection and health aggregation in `ciao/os_audit.py`, while following the canonical Ciaobot workspace, skill projection, memory proposal, and job-run schemas. Preserve the existing API keys where practical, add explicit scan diagnostics, and classify the overall result as `healthy`, `needs_attention`, or `error`.

**Tech Stack:** Python 3.12+, pytest, argparse, Starlette, packaged JSON schedules, Markdown documentation.

### Task 1: Lock the audit contract with regression tests

**Files:**

- Modify: `tests/test_os_audit.py`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_agent_assets.py`

- [ ] Add tests proving that linked `AGENTS.md` and `CLAUDE.md` files are scanned once.
- [ ] Add tests distinguishing informational duplicate-rule overlaps from obvious opposite-polarity conflicts.
- [ ] Add tests proving canonical, Claude, and Codex skill projections are deduplicated by skill name.
- [ ] Add tests proving missing `SKILL.md`, vault orphans, duplicates, proposals, expired memory, and unresolved job failures contribute to `total_issues`.
- [ ] Add tests proving missing or unreadable roots and malformed audit inputs produce `status=error`.
- [ ] Add CLI tests for `CIAO_WORKSPACE`, workspace-relative vault resolution, and exit codes 0, 1, and 2.
- [ ] Add an endpoint test proving the configured runtime directory is used.
- [ ] Run the focused tests and confirm they fail for the expected old behavior.

Run:

```bash
pytest -q tests/test_os_audit.py tests/test_cli.py tests/test_agent_assets.py
```

Expected before implementation: failures for the new audit contract.

### Task 2: Fix memory expiration accounting

**Files:**

- Modify: `tests/test_memory_injector.py`
- Modify: `ciao/memory_injector.py`
- Modify: `ciao/os_audit.py`

- [ ] Add a failing test for an expired-only file that must not be described as empty.
- [ ] Add a failing test proving stored usage includes expired entries while their content stays out of the prompt.
- [ ] Add a failing test proving malformed expiration tags appear in audit diagnostics.
- [ ] Preserve the existing rule that an entry remains active through its stated expiration date and expires the following day.
- [ ] Implement stored-versus-active usage rendering and expired-entry cleanup guidance.
- [ ] Run the memory and audit tests until green.

Run:

```bash
pytest -q tests/test_memory_injector.py tests/test_os_audit.py
```

Expected after implementation: all focused tests pass.

### Task 3: Harden audit collectors and health aggregation

**Files:**

- Modify: `ciao/os_audit.py`

- [ ] Validate workspace and vault roots before scanning.
- [ ] Deduplicate skills by name across `skills/`, `.claude/skills/`, and `.agents/skills/`.
- [ ] Use the existing 15 KiB skill budget.
- [ ] Resolve instruction-file aliases before comparing rules.
- [ ] Record exact duplicate rules as informational overlaps and obvious opposite-polarity rules as conflicts.
- [ ] Scan direct and per-workspace proposal queues, counting only canonical `[memory]` and `[user]` proposal bullets.
- [ ] Read real `started_at` and `ended_at` job timestamps, merge the latest-job index, and count only latest unresolved failures.
- [ ] Surface malformed or unreadable inputs as scan errors.
- [ ] Count every actionable finding once and return `error` when scan errors prevent a reliable health result.
- [ ] Update Markdown formatting for the new fields.

### Task 4: Fix CLI and API integration

**Files:**

- Modify: `ciao/cli.py`
- Modify: `ciao/web/agent_assets.py`

- [ ] Resolve the default workspace from `CIAO_WORKSPACE`.
- [ ] Resolve a relative vault root against the selected workspace and honor `CIAO_VAULT_ROOT`.
- [ ] Return CLI exit 0 for healthy, 1 for needs-attention, and 2 for audit errors.
- [ ] Pass the server’s configured runtime directory into the API audit.
- [ ] Run CLI and endpoint tests until green.

### Task 5: Integrate the audit into weekly hygiene

**Files:**

- Modify: `ciao/stock/schedules.json`
- Modify: `tests/test_schedules.py`

- [ ] Add a failing schedule test requiring `ciao os-audit --json`.
- [ ] Replace the separate `vault-lint` invocation because `os-audit` already includes vault validation.
- [ ] Keep `vault-index --write`, low-risk repairs, and report-only handling for ambiguous findings.
- [ ] Explicitly tell the scheduled agent that exit 1 means findings were reported, not that command execution failed.

### Task 6: Update product and contributor documentation

**Files:**

- Modify: `README.md`
- Modify: `docs/ARCHITECTURE.md`
- Modify: `docs/DEVELOPMENT.md`
- Modify: `ciao/system_prompt.md`
- Modify: `PWA_API.md` if the response contract changes

- [ ] Document `ciao os-audit`, its status and exit-code contract, and the weekly integration.
- [ ] Document `[expires: YYYY-MM-DD]`, including stored usage and daily cleanup behavior.
- [ ] Correct the shipped-system-routine table so it lists workspace hygiene instead of the removed weekly-review routine.
- [ ] Document the audit module in the architecture map and contributor command list.

### Task 7: Verify the complete change

- [ ] Run the focused audit, memory, CLI, API, schedule, and documentation tests.
- [ ] Run the full Python test suite in a clean environment.
- [ ] Inspect `git diff --check` and the final diff.
- [ ] Confirm unrelated pre-existing working-tree changes remain intact.
- [ ] Dispatch the documentation updater for an independent synchronization check.

Run:

```bash
pytest -q
git diff --check
```

Expected: all tests pass, no whitespace errors, and only intended files plus pre-existing user work are modified.
