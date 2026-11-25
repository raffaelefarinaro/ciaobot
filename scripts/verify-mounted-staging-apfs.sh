#!/usr/bin/env bash
# Reproducible disposable-APFS evidence for mounted-folder staging.
# Creates and mounts a throwaway disk image, runs one real folder publication
# against it, and never touches a user vault or provider.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"
[ -x "$PYTHON" ] || PYTHON=python3

if [ "$(uname -s)" != Darwin ]; then
  echo "UNAVAILABLE: mounted-APFS evidence is macOS-only on this host ($(uname -s))" >&2
  exit 0
fi

WORK="$(mktemp -d "${TMPDIR:-/tmp}/ciao-apfs-evidence.XXXXXX")"
IMG="$WORK/evidence.dmg"
VOL="/Volumes/CiaoMountedEvidence$$"
MOUNTED=0
cleanup() {
  if [ "$MOUNTED" = 1 ]; then hdiutil detach "$VOL" >/dev/null 2>&1 || true; fi
  rm -f "$IMG"
  rm -rf "$WORK"
}
trap cleanup EXIT

hdiutil create -quiet -size 32m -fs APFS -volname "$(basename "$VOL")" -attach "$IMG" >/dev/null
MOUNTED=1

"$PYTHON" - "$VOL" "$WORK" <<'PY'
import asyncio, json, os, stat, sys
from pathlib import Path
from types import SimpleNamespace
from ciao import share_proposals, share_publish
from ciao.config import SharedBrainLink

mount = Path(sys.argv[1])
root = Path(sys.argv[2])
vault = root / "vault"; vault.mkdir()
install = root / "install"; install.mkdir()
shared = mount / "notes"; shared.mkdir(parents=True)
target = mount / "symlink-target"; target.mkdir()
os.chmod(target, 0o755)
link = SharedBrainLink(
    name="mounted", path=str(shared.resolve()),
    identity=f"folder:{shared.resolve()}", lifetime_id="evidence", kind="folder",
)

class Config:
    workspace_root = install
    state_path = install / ".runtime" / "state.json"
    vault_root = vault
    def __init__(self):
        self.vaults = {"home": vault}
        self.links = [link]
        self.workspaces = {"home": SimpleNamespace(shared_brains=self.links)}
    def workspace_names(self): return list(self.vaults)
    def workspace_vault_root(self, name): return self.vaults[name]
    def workspace(self, name): return self.workspaces.get(name)
    def chats_roots(self): return []

config = Config()
registry = config.state_path.parent / "workspaces.json"
registry.parent.mkdir(parents=True)
registry.write_text(json.dumps([{
    "name": "home", "vault_root": str(vault),
    "shared_brains": [{
        "name": link.name, "path": link.path, "identity": link.identity,
        "lifetime_id": link.lifetime_id, "kind": "folder",
    }],
}]))
config._workspace_registry_bytes = registry.read_bytes()

staging_name = share_publish.MOUNT_STAGING_DIR
staging_path = mount / staging_name
staging_path.symlink_to(target, target_is_directory=True)
blocked = share_proposals.draft_share(
    config, workspace="home", relative_path="blocked.md", operation="create",
    proposed=b"blocked\n", request_key="evidence-symlink",
)
blocked_result = asyncio.run(share_publish.publish_share(
    config, workspace="home", proposal_id=blocked.proposal_id,
    expected_revision=blocked.artifact["review_token"],
))
print("symlink_outcome", blocked_result.outcome)
print("symlink_target_mode", oct(stat.S_IMODE(target.stat().st_mode)))
staging_path.unlink()

draft = share_proposals.draft_share(
    config, workspace="home", relative_path="evidence.md", operation="create",
    proposed=b"exact mounted bytes\n", request_key="evidence-publish",
)
result = asyncio.run(share_publish.publish_share(
    config, workspace="home", proposal_id=draft.proposal_id,
    expected_revision=draft.artifact["review_token"],
))
attempt = share_publish._artifact_for(config, "home", draft.proposal_id)["publication"]["attempts"][-1]
receipt = share_publish.get_share_receipt(
    config, workspace="home", proposal_id=draft.proposal_id,
    expected_revision=draft.artifact["review_token"],
)
print("publish_outcome", result.outcome)
print("file_bytes", (shared / "evidence.md").read_bytes())
print("staging_root", attempt["staging_root"])
print("staging_mode", oct(stat.S_IMODE(os.stat(attempt["staging_root"]).st_mode)))
print("staging_child_left", (Path(attempt["staging_root"]) / attempt["staging_name"]).exists())
print("shared_temp_entries", [p.name for p in shared.iterdir() if p.name.startswith(".ciao-share")])
print("receipt_scope", receipt["scope"], receipt["local_only"], receipt["sync_claimed"])
PY

echo "OK: disposable APFS evidence complete"
