"""Vault hygiene linter logic."""

from __future__ import annotations

import re
from pathlib import Path

# Match [[Target]], ignoring optional #anchors and |labels
WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")

def run_validation(vault_root: Path) -> dict:
    """Scan the vault directory and find broken wikilinks, orphans, and duplicates."""
    issues = {
        "broken_links": [],
        "orphans": [],
        "duplicates": []
    }
    
    valid_targets = set()
    files_to_scan = []
    incoming_links = {}
    
    # Exclude directories
    exclude_dirs = {"Logs", "Templates", ".obsidian"}
    exclude_files = {"INDEX.md", "MEMORY.md"}
    
    normalized_names = {}
    
    for path in vault_root.rglob("*.md"):
        try:
            rel = path.relative_to(vault_root)
        except ValueError:
            continue
            
        if any(p in exclude_dirs for p in rel.parts):
            continue
        if rel.name in exclude_files:
            continue
            
        target_stem = path.stem
        target_rel = str(rel.with_suffix(""))
        valid_targets.add(target_stem)
        valid_targets.add(target_rel)
        files_to_scan.append((path, str(rel)))
        
        incoming_links[target_stem] = []
        incoming_links[target_rel] = []
        
        norm = target_stem.lower().replace("-", "").replace("_", "")
        normalized_names.setdefault(norm, []).append(str(rel))

    for norm, paths in normalized_names.items():
        if len(paths) > 1:
            issues["duplicates"].append(paths)
        
    for path, rel_str in files_to_scan:
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
            
        for match in WIKILINK_RE.finditer(content):
            target = match.group(1).strip()
            if target in valid_targets:
                incoming_links.setdefault(target, []).append(rel_str)
            else:
                issues["broken_links"].append({
                    "source": rel_str,
                    "target": target
                })
                
    # Check for memory files links (roots)
    memory_md = vault_root / "personal" / "MEMORY.md"
    memory_work_md = vault_root / "work" / "MEMORY.md"
    memory_links = set()
    for mem_file in (memory_md, memory_work_md):
        if mem_file.exists():
            try:
                mem_content = mem_file.read_text(encoding="utf-8")
                for match in WIKILINK_RE.finditer(mem_content):
                    memory_links.add(match.group(1).strip())
            except OSError:
                pass

    orphan_candidate_dirs = {"People", "Projects", "Ideas", "Resources", "Places", "projects", "references"}

    for path, rel_str in files_to_scan:
        stem = path.stem
        rel_path = Path(rel_str)
        rel_no_sfx = str(rel_path.with_suffix(""))
        
        if not any(part in orphan_candidate_dirs for part in rel_path.parts):
            continue
            
        has_incoming = False
        if incoming_links.get(stem) or incoming_links.get(rel_no_sfx):
            has_incoming = True
        if stem in memory_links or rel_no_sfx in memory_links:
            has_incoming = True
            
        if not has_incoming:
            issues["orphans"].append(rel_str)
            
    return issues
