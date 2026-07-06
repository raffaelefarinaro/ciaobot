"""Public-repo extraction contracts and preflight scanning."""

from __future__ import annotations

import argparse
import fnmatch
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path


PUBLIC_EXPORT_ALLOWLIST: tuple[str, ...] = (
    "ciao/",
    "web/",
    "scripts/",
    "tests/",
    "docs/",
    ".github/workflows/",
    "deploy/homebrew/",
    "pyproject.toml",
    "README.md",
    "INTEGRATIONS.md",
    "PWA_API.md",
    ".env.example",
    "LICENSE",
    "LICENSE.md",
    "SECURITY.md",
    "CONTRIBUTING.md",
)

PUBLIC_EXPORT_OVERLAYS: dict[str, str] = {
    "ciao/stock/public/CLAUDE.md": "CLAUDE.md",
}

_FORBIDDEN_PATH_PREFIXES: tuple[str, ...] = (
    "memory-vault/",
    "secrets/",
    ".claude/",
    ".runtime/",
    ".mcp.json",
    ".env",
)

_FORBIDDEN_PATH_GLOBS: tuple[str, ...] = (
    "data-platform-*.json",
    "**/data-platform-*.json",
    "client_secret*.json",
    "**/client_secret*.json",
    "**/*service-account*.json",
    "scripts/morning-briefing.py",
    "scripts/gws-personal.sh",
    "scripts/gws-work.sh",
    "scripts/work_chat_transcripts.py",
    "scripts/gws-auth-bridge.sh",
    "scripts/gws-secrets.py",
)

_SKIP_DIRS = {
    ".git",
    ".venv",
    "__pycache__",
    "node_modules",
    "dist",
    "build",
}


@dataclass(frozen=True, slots=True)
class PublicReleaseFinding:
    kind: str
    path: str
    detail: str


@dataclass(slots=True)
class PublicReleaseReport:
    root: Path
    findings: list[PublicReleaseFinding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.findings


def load_private_patterns(path: Path | str) -> tuple[str, ...]:
    pattern_path = Path(path).expanduser()
    lines = pattern_path.read_text(encoding="utf-8").splitlines()
    return tuple(
        line.strip()
        for line in lines
        if line.strip() and not line.lstrip().startswith("#")
    )


def _env_private_patterns() -> tuple[str, ...]:
    raw = os.environ.get("CIAO_PUBLIC_PRIVATE_PATTERNS", "")
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _normalize_rel(path: str | Path) -> str:
    rel = Path(path).as_posix()
    while rel.startswith("./"):
        rel = rel[2:]
    return rel


def is_public_export_allowlisted(path: str | Path) -> bool:
    rel = _normalize_rel(path)
    for entry in PUBLIC_EXPORT_ALLOWLIST:
        if entry.endswith("/"):
            if rel == entry[:-1] or rel.startswith(entry):
                return True
        elif rel == entry:
            return True
    return False


def _forbidden_path_detail(rel: str) -> str:
    for prefix in _FORBIDDEN_PATH_PREFIXES:
        if rel == prefix.rstrip("/") or rel.startswith(prefix):
            return prefix
    for pattern in _FORBIDDEN_PATH_GLOBS:
        if fnmatch.fnmatch(rel, pattern):
            return pattern
    return ""


def _iter_files(root: Path):
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel_parts = path.relative_to(root).parts
        if any(part in _SKIP_DIRS for part in rel_parts):
            continue
        yield path


def _read_text(path: Path) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    if b"\x00" in data:
        return ""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return ""


def _should_skip_file(rel: str) -> bool:
    if _forbidden_path_detail(rel):
        return True
    parts = Path(rel).parts
    return any(part in _SKIP_DIRS for part in parts)


def export_public_tree(source: Path | str, destination: Path | str) -> list[str]:
    source_root = Path(source).expanduser().resolve()
    dest_root = Path(destination).expanduser().resolve()
    if dest_root.exists() and any(dest_root.iterdir()):
        raise ValueError("destination must be empty")
    dest_root.mkdir(parents=True, exist_ok=True)

    copied: list[str] = []
    replaced_targets = set(PUBLIC_EXPORT_OVERLAYS.values())
    for path in _iter_files(source_root):
        rel = path.relative_to(source_root).as_posix()
        if rel in replaced_targets:
            continue
        if not is_public_export_allowlisted(rel) or _should_skip_file(rel):
            continue
        target = dest_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())
        copied.append(rel)
    for overlay_src, overlay_dest in PUBLIC_EXPORT_OVERLAYS.items():
        source_path = source_root / overlay_src
        if not source_path.is_file():
            continue
        target = dest_root / overlay_dest
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source_path.read_bytes())
        copied.append(overlay_dest)
    return sorted(copied)


def scan_public_export(
    root: Path | str,
    *,
    private_patterns: tuple[str, ...] | list[str] | None = None,
) -> PublicReleaseReport:
    root_path = Path(root).expanduser().resolve()
    report = PublicReleaseReport(root=root_path)
    patterns = tuple(private_patterns) if private_patterns is not None else _env_private_patterns()
    for path in _iter_files(root_path):
        rel = path.relative_to(root_path).as_posix()
        forbidden = _forbidden_path_detail(rel)
        if forbidden:
            report.findings.append(
                PublicReleaseFinding("forbidden_path", rel, forbidden)
            )
        text = _read_text(path)
        if not text:
            continue
        for pattern in patterns:
            if pattern in text:
                report.findings.append(
                    PublicReleaseFinding("private_string", rel, pattern)
                )
    report.findings.sort(key=lambda finding: (finding.path, finding.kind, finding.detail))
    return report


def main(argv: list[str] | None = None) -> int:
    argv_list = list(sys.argv[1:] if argv is None else argv)
    if argv_list and argv_list[0] not in {"scan", "export", "-h", "--help"}:
        argv_list.insert(0, "scan")
    parser = argparse.ArgumentParser(description="Prepare and scan public Ciaobot exports.")
    subparsers = parser.add_subparsers(dest="command")

    scan_parser = subparsers.add_parser(
        "scan",
        help="Scan an extracted public Ciaobot tree for private paths/strings.",
    )
    scan_parser.add_argument("root", nargs="?", default=".")
    scan_parser.add_argument(
        "--private-patterns",
        type=Path,
        help="File with one private string pattern per line. Blank lines and # comments are ignored.",
    )

    export_parser = subparsers.add_parser(
        "export",
        help="Copy the allowlisted public tree to an empty destination.",
    )
    export_parser.add_argument("source")
    export_parser.add_argument("destination")
    args = parser.parse_args(argv_list)
    if args.command == "export":
        copied = export_public_tree(args.source, args.destination)
        for rel in copied:
            print(rel)
        return 0

    root = getattr(args, "root", ".")
    pattern_file = getattr(args, "private_patterns", None)
    private_patterns = load_private_patterns(pattern_file) if pattern_file else None
    report = scan_public_export(root, private_patterns=private_patterns)
    for finding in report.findings:
        print(f"{finding.kind}\t{finding.path}\t{finding.detail}")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
