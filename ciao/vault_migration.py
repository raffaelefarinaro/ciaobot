"""One-off migration of a vault onto the canonical `type:` vocabulary.

Needed at three moments, all of which land on an existing vault:

* **Upgrade.** Notes written before the vocabulary existed carry whatever type
  the agent invented at the time — `doc` beside `document`, `project-log` beside
  `log`. Those are `unknown_type` findings from the moment `vault-lint` starts
  checking, and `os-audit` exits 1 on them, so an upgrade that shipped the check
  without this would hand every existing install a permanently unhealthy audit.
* **Onboarding an existing vault** (``CIAO_VAULT_MODE=existing``). Same problem,
  except the vault was never Ciaobot-shaped to begin with.
* **Fresh install.** Nothing to do: the vault is created conformant, and the
  receipt records that so nothing rescans on every boot.

What it will and will not do
----------------------------
It applies only **aliased** types — a rename whose target is named in
``TYPE_ALIASES``, which is a mechanical substitution with no judgement in it, the
same bar the workspace-hygiene routine already uses for "low-risk, unambiguous
fixes". A type with no alias target is **reported and left alone**: choosing a
category for it is the user's call, and guessing would bury a real decision in a
migration.

Frontmatter is rewritten one line at a time, and only when the current value is
exactly the alias being replaced, so a hand-edit racing the migration cannot be
clobbered.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ciao.vault_index import TYPE_ALIASES, scan_vault, vocabulary_report

logger = logging.getLogger(__name__)

RECEIPT_NAME = "vault-vocabulary.json"
RECEIPT_VERSION = 1

_FRONTMATTER_RE = re.compile(r"\A(---[ \t]*\r?\n)(.*?)(\r?\n---[ \t]*(?:\r?\n|\Z))", re.DOTALL)


def receipt_path(runtime_root: Path) -> Path:
    return Path(runtime_root) / "migration" / RECEIPT_NAME


def read_receipt(runtime_root: Path) -> dict[str, Any] | None:
    path = receipt_path(runtime_root)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def write_receipt(runtime_root: Path, summary: dict[str, Any]) -> Path:
    path = receipt_path(runtime_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": RECEIPT_VERSION,
        "completed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "renamed": summary.get("renamed", []),
        "unresolved": summary.get("unresolved", {}),
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
    return path


def _retype_frontmatter(text: str, *, expect: str, replacement: str) -> str | None:
    """Return ``text`` with its frontmatter ``type`` rewritten, or None.

    None means "not rewritten": no frontmatter, or the ``type`` line no longer
    holds ``expect``. Only the one line changes — every other key, its order, and
    the body are preserved byte for byte.
    """
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        return None
    opening, block, closing = match.group(1), match.group(2), match.group(3)
    lines = block.split("\n")
    for index, line in enumerate(lines):
        if not line.startswith("type:"):
            continue
        current = line[len("type:") :].strip().strip("\"'")
        if current != expect:
            return None
        lines[index] = f"type: {replacement}"
        rewritten = opening + "\n".join(lines) + closing
        return rewritten + text[match.end() :]
    return None


def migrate_vault_vocabulary(
    vault_root: Path,
    *,
    apply: bool = False,
) -> dict[str, Any]:
    """Bring a vault's frontmatter types onto the canonical vocabulary.

    With ``apply=False`` (the default) nothing is written and the return value
    describes what would change. Idempotent: a second run over a migrated vault
    reports nothing, because the values it would rewrite are already canonical.
    """
    root = Path(vault_root)
    summary: dict[str, Any] = {
        "vault_root": str(root),
        "applied": bool(apply),
        "renamed": [],
        "planned": [],
        "unresolved": {},
        "failed": [],
    }
    if not root.is_dir():
        summary["skipped"] = "vault root does not exist"
        return summary

    drift = vocabulary_report(scan_vault(root))["type_drift"]
    for raw_type, record in sorted(drift.items()):
        target = TYPE_ALIASES.get(raw_type, "")
        if not target:
            # No canonical equivalent: a real categorisation decision, which
            # belongs to the user. Reported so `vault-lint` findings are
            # explainable rather than mysterious.
            summary["unresolved"][raw_type] = record["paths"]
            continue
        for relative in record["paths"]:
            # `Entry.path` carries the vault directory name; the file lives
            # under the vault root, so strip that first segment back off.
            path = root / Path(relative).relative_to(Path(relative).parts[0])
            change = {"path": str(path), "from": raw_type, "to": target}
            if not apply:
                summary["planned"].append(change)
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                summary["failed"].append({**change, "error": str(exc)})
                continue
            rewritten = _retype_frontmatter(text, expect=raw_type, replacement=target)
            if rewritten is None:
                summary["failed"].append({**change, "error": "type line did not match"})
                continue
            try:
                path.write_text(rewritten, encoding="utf-8")
            except OSError as exc:
                summary["failed"].append({**change, "error": str(exc)})
                continue
            summary["renamed"].append(change)
    return summary


def migrate_if_needed(
    vault_root: Path,
    runtime_root: Path,
    *,
    apply: bool = True,
) -> dict[str, Any]:
    """Run the migration once per install and record a receipt.

    Called from the install/upgrade path. Returns a summary with ``skipped`` set
    when the receipt is already present, so the caller can report a no-op
    instead of rescanning the vault on every boot.
    """
    existing = read_receipt(runtime_root)
    if existing is not None:
        return {"skipped": "already migrated", "receipt": existing}
    summary = migrate_vault_vocabulary(vault_root, apply=apply)
    if "skipped" in summary:
        # No vault yet (bootstrap). Leave no receipt so the real vault still
        # gets migrated once it exists.
        return summary
    write_receipt(runtime_root, summary)
    return summary
