"""Enforce that every CIAO_* env var referenced in ciao/ is documented in
INTEGRATIONS.md, or explicitly allowlisted as an internal IPC marker.

Mirrors the route-coverage pattern in test_pwa_api_docs.py: any new env var
must either land in the doc or in INTERNAL_VARS with a one-line reason."""
from __future__ import annotations

import re
from pathlib import Path


# Env vars that intentionally don't appear in INTEGRATIONS.md. These are
# internal markers/IPC between processes, not operator-tunable knobs. Each
# entry needs a one-line reason so future contributors can decide whether
# their new var really belongs here.
INTERNAL_VARS: dict[str, str] = {
    "CIAO_ACTIVE_PROJECT": "per-turn context injected by the SDK hook; not operator-settable",
    "CIAO_CHAT_ID": "subprocess IPC marker for the chat the spawned CLI belongs to",
    "CIAO_MODEL": "subprocess IPC marker for the model selected for the spawned chat",
    "CIAO_PROVIDER": "subprocess IPC marker for the provider selected for the spawned chat",
    "CIAO_MODEL_BUCKET": "subprocess IPC marker for the model bucket selected for the spawned chat",
    "CIAO_CONTEXT_BEGIN": "subprocess IPC delimiter wrapping injected context",
    "CIAO_CONTEXT_END": "subprocess IPC delimiter wrapping injected context",
    "CIAO_RESTART_EXIT_CODE": "internal exit-code convention for restart-requesting handlers",
    "CIAO_RUNTIME_ROOT": "test-only override for the runtime/ directory",
    "CIAO_PARENT_CHAT_ID": "subprocess IPC marker for the parent chat ID of an agent handoff",
    "CIAO_PROVIDER_SUBCHAT_ID": "subprocess IPC marker for the provider sub-chat ID",
}


def _scan_ciao_env_refs() -> set[str]:
    """Return all CIAO_* names referenced in ciao/ and scripts/."""
    repo = Path(__file__).resolve().parents[1]
    out: set[str] = set()
    for root in (repo / "ciao", repo / "scripts", repo / "deploy"):
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix not in {".py", ".sh", ".service"}:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for match in re.finditer(r"\bCIAO_[A-Z][A-Z0-9_]*", text):
                out.add(match.group(0))
    return out


def test_ciao_env_vars_documented_or_allowlisted() -> None:
    repo = Path(__file__).resolve().parents[1]
    doc = (repo / "INTEGRATIONS.md").read_text(encoding="utf-8")

    referenced = _scan_ciao_env_refs()
    missing: list[str] = []
    for name in sorted(referenced):
        if name in INTERNAL_VARS:
            continue
        # The doc must mention the var as a backticked literal so partial
        # matches inside longer names don't slip through.
        if f"`{name}`" in doc or f"`{name}=" in doc:
            continue
        missing.append(name)

    assert missing == [], (
        "CIAO_* env vars referenced in code but missing from INTEGRATIONS.md "
        "(add a description or allowlist in INTERNAL_VARS with a one-line "
        f"reason): {missing}"
    )

    # The allowlist must not drift either: every entry must still be
    # referenced somewhere in the source, or it's stale and should be
    # removed.
    stale = sorted(set(INTERNAL_VARS) - referenced)
    assert stale == [], (
        "INTERNAL_VARS has entries for env vars no longer referenced in "
        f"ciao/ or scripts/: {stale}"
    )
