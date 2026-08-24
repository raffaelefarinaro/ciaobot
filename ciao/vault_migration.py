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

import hashlib
import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ciao.vault_index import TYPE_ALIASES, scan_vault, vocabulary_report

logger = logging.getLogger(__name__)

# The pre-keying receipt name. Still read, because every install upgrading into
# per-vault keying has one, and still written by a caller that names no vault —
# but it accounts for at most one vault. See `_install_receipt`.
RECEIPT_NAME = "vault-vocabulary.json"
RECEIPT_VERSION = 2

_FRONTMATTER_RE = re.compile(r"\A(---[ \t]*\r?\n)(.*?)(\r?\n---[ \t]*(?:\r?\n|\Z))", re.DOTALL)


def _vault_key(vault_root: Path) -> str:
    """A stable, readable filename fragment for one vault's absolute path.

    The parent directory's name is what a human reads (``personal``, ``work``),
    and the digest is what makes it unambiguous: two roots can hold vaults with
    the same leaf name, and the leaf alone is configurable (`CIAO_VAULT_ROOT`).
    """
    resolved = Path(vault_root).expanduser().resolve()
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:12]
    label = re.sub(r"[^A-Za-z0-9._-]+", "-", resolved.parent.name).strip("-")[:24]
    return f"{label}-{digest}" if label else digest


def receipt_path(runtime_root: Path, vault_root: Path | None = None) -> Path:
    """Where the receipt for ``vault_root`` lives under this runtime root.

    Keyed on the VAULT, not the runtime root, because those are not the same
    unit: launchd bakes one absolute ``CIAO_RUNTIME_ROOT`` into the plist, so
    every workspace's vault shares a single runtime directory while the thing
    being migrated is one vault at a time.
    """
    base = Path(runtime_root) / "migration"
    if vault_root is None:
        return base / RECEIPT_NAME
    return base / f"vault-vocabulary.{_vault_key(vault_root)}.json"


def _read_receipt_file(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _vault_count(runtime_root: Path) -> int:
    """How many vaults this install has: one per agent root.

    Asked of ``config.agent_roots_for``, which holds the single definition of
    "has this install re-rooted" — before the re-rooting there is one shared
    vault, after it there is one per registered workspace. Only the COUNT is
    used, so the workspace root handed over does not matter: that function reads
    the registry and the re-root receipt and never touches the path.
    """
    try:
        from ciao.config import agent_roots_for  # noqa: PLC0415

        runtime = Path(runtime_root)
        return max(1, len(agent_roots_for(runtime.parent, runtime)))
    except Exception:  # noqa: BLE001 — an unreadable registry means "one vault"
        logger.exception("vault vocabulary: could not count the agent roots")
        return 1


def _install_receipt(runtime_root: Path) -> dict[str, Any] | None:
    """One view over every vault's receipt, or None while any vault is left.

    "Is this install's vocabulary migration complete, and what did it leave for
    the user?" — which is a different question from the per-vault gate, and the
    only one an unkeyed caller can be asking. It must answer None while a vault
    is still unmigrated, or the coarse gate in ``sync_skills`` closes on the
    first root and the rest are never offered.

    A pre-keying receipt does not record which vault it covers, so it accounts
    for a single-vault install — where there is nothing else it could be about —
    and for nothing else. On a multi-vault install it counts for no vault, which
    is what makes an upgraded install migrate each of them once.
    """
    base = Path(runtime_root) / "migration"
    keyed = [
        data
        for data in (
            _read_receipt_file(path) for path in sorted(base.glob("vault-vocabulary.*.json"))
        )
        if data is not None
    ]
    covered = {str(data.get("vault_root") or "") for data in keyed} - {""}
    expected = _vault_count(runtime_root)
    if not covered:
        legacy = _read_receipt_file(base / RECEIPT_NAME)
        return legacy if legacy is not None and expected == 1 else None
    if len(covered) < expected:
        return None

    unresolved: dict[str, list[Any]] = {}
    renamed: list[Any] = []
    for data in keyed:
        renamed.extend(data.get("renamed") or [])
        for raw_type, paths in (data.get("unresolved") or {}).items():
            unresolved.setdefault(str(raw_type), []).extend(paths or [])
    stamps = [str(data.get("completed_at") or "") for data in keyed]
    return {
        "schema_version": RECEIPT_VERSION,
        "completed_at": max(stamps) if stamps else "",
        "vaults": sorted(covered),
        "renamed": renamed,
        "unresolved": unresolved,
    }


def read_receipt(runtime_root: Path, vault_root: Path | None = None) -> dict[str, Any] | None:
    """The receipt for ONE vault, or the install-wide view when none is named.

    Name the vault. One runtime root serves every workspace's vault — launchd
    bakes a single ``CIAO_RUNTIME_ROOT`` into the plist — so a receipt found
    without naming a vault belongs to SOME vault, not necessarily to the one the
    caller is about to skip. Answering with it is the bug this keying fixes:
    `main.py`'s per-root ``update_skills`` loop wrote the receipt on the first
    root and every later root short-circuited as "already migrated", so the
    second workspace's vault kept its legacy ``type:`` values forever and
    ``vault-lint``/``os-audit`` failed for it permanently, with no path to a fix.

    A keyed read never trusts a pre-keying receipt: it does not say which vault
    it covered, so it cannot claim one. The cost is one idempotent rescan per
    vault after the upgrade. Unkeyed reads get :func:`_install_receipt`.
    """
    if vault_root is None:
        return _install_receipt(runtime_root)
    return _read_receipt_file(receipt_path(runtime_root, vault_root))


def write_receipt(
    runtime_root: Path, summary: dict[str, Any], *, vault_root: Path | None = None
) -> Path:
    """Record one vault's migration. The vault comes from ``summary`` if unnamed.

    ``migrate_vault_vocabulary`` always reports ``vault_root``, so the keyed path
    is reached without every caller having to thread it through by hand.
    """
    vault = vault_root if vault_root is not None else summary.get("vault_root") or None
    path = receipt_path(runtime_root, Path(vault) if vault is not None else None)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": RECEIPT_VERSION,
        "completed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        # Recorded, not just hashed into the filename: a reader must be able to
        # tell which vault a receipt covers without recomputing the key.
        "vault_root": str(vault) if vault is not None else "",
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
    """Run the migration once per VAULT and record a receipt.

    Called from the install/upgrade path, once per agent root. Returns a summary
    with ``skipped`` set when this vault's receipt is already present, so the
    caller can report a no-op instead of rescanning the vault on every boot.

    Gated per vault rather than per runtime root: the unit that gets migrated is
    a vault, and a multi-workspace install has several of them behind one
    ``CIAO_RUNTIME_ROOT``. Keyed on the runtime root, the first root's receipt
    short-circuited every later root, which left the other workspaces' notes on
    the legacy vocabulary with no way to ever reach them again.
    """
    existing = read_receipt(runtime_root, vault_root)
    if existing is not None:
        return {"skipped": "already migrated", "receipt": existing}
    summary = migrate_vault_vocabulary(vault_root, apply=apply)
    if "skipped" in summary:
        # No vault yet (bootstrap). Leave no receipt so the real vault still
        # gets migrated once it exists.
        return summary
    write_receipt(runtime_root, summary, vault_root=vault_root)
    return summary
