"""Generic dependency version checks used by the release workflow.

The weekly dependency changelog review is workspace-owned and lives outside
the public app. This module keeps only the deterministic manifest and registry
helpers needed when preparing a Ciaobot release.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

# The Claude SDK tracks the bundled CLI shipped by the app, so same-major
# updates can be applied by the release workflow without model deliberation.
AUTO_UPDATE_KEYS = ("claude-agent-sdk",)


def parse_dependency_spec(dep_str: str) -> tuple[str | None, str]:
    match = re.match(r"^([a-zA-Z0-9_\-]+(\[[a-zA-Z0-9_\-,]+\])?)", dep_str)
    if not match:
        return None, ""
    name_with_extras = match.group(1)
    name = re.sub(r"\[.*\]", "", name_with_extras).strip()
    spec = dep_str[len(name_with_extras):].strip()
    return name, spec or "*"


def parse_pyproject_dependencies(toml_path: Path) -> dict[str, str]:
    try:
        import tomllib
    except ImportError:  # pragma: no cover - Python 3.12+ is required
        return {}
    if not toml_path.exists():
        return {}
    try:
        with toml_path.open("rb") as handle:
            data = tomllib.load(handle)
        deps: dict[str, str] = {}
        project = data.get("project", {})
        for dep_str in project.get("dependencies", []):
            name, spec = parse_dependency_spec(dep_str)
            if name:
                deps[name] = spec
        for dep_list in project.get("optional-dependencies", {}).values():
            for dep_str in dep_list:
                name, spec = parse_dependency_spec(dep_str)
                if name:
                    deps[name] = spec
        return deps
    except Exception:  # noqa: BLE001 - release checks must fail open
        return {}


def parse_npm_dependencies(pkg_json_path: Path) -> dict[str, str]:
    if not pkg_json_path.exists():
        return {}
    try:
        data = json.loads(pkg_json_path.read_text(encoding="utf-8"))
        deps: dict[str, str] = {}
        for section in ("dependencies", "devDependencies"):
            deps.update(data.get(section, {}))
        return deps
    except Exception:  # noqa: BLE001 - release checks must fail open
        return {}


def update_pyproject_toml_dependency(
    toml_path: Path, package_name: str, new_version: str
) -> bool:
    if not toml_path.exists():
        return False
    content = toml_path.read_text(encoding="utf-8")
    pattern = (
        rf'(\s*"{re.escape(package_name)}'
        rf'(?:\[[a-zA-Z0-9_\-,]+\])?==)[0-9a-zA-Z\.\-]+(")'
    )
    new_content, count = re.subn(
        pattern, lambda match: f"{match.group(1)}{new_version}{match.group(2)}", content
    )
    if count:
        toml_path.write_text(new_content, encoding="utf-8")
        return True
    return False


def update_npm_dependency(pkg_json_path: Path, package_name: str, new_version: str) -> bool:
    if not pkg_json_path.exists():
        return False
    try:
        data = json.loads(pkg_json_path.read_text(encoding="utf-8"))
        updated = False
        for section in ("dependencies", "devDependencies"):
            if package_name not in data.get(section, {}):
                continue
            current_spec = data[section][package_name]
            prefix = "^" if current_spec.startswith("^") else "~" if current_spec.startswith("~") else ""
            data[section][package_name] = f"{prefix}{new_version}"
            updated = True
        if updated:
            pkg_json_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        return updated
    except Exception:  # noqa: BLE001 - release checks must fail open
        return False


def parse_semver(ver_str: str) -> list[int]:
    cleaned = re.sub(r"[^0-9\.]", "", ver_str.lstrip("vV").split("-")[0])
    parts: list[int] = []
    for part in cleaned.split("."):
        try:
            parts.append(int(part))
        except ValueError:
            pass
    while len(parts) < 3:
        parts.append(0)
    return parts[:3]


def is_newer_and_safe_update(old_ver: str, new_ver: str) -> bool:
    old_parts = parse_semver(old_ver)
    new_parts = parse_semver(new_ver)
    return new_parts > old_parts and new_parts[0] == old_parts[0]


def install_updated_packages(
    updated_python: bool, updated_npm: bool, workspace_root: Path
) -> tuple[bool, str]:
    errors: list[str] = []
    if updated_python:
        try:
            venv_python = workspace_root / ".venv" / "bin" / "python"
            python_cmd = str(venv_python) if venv_python.exists() else sys.executable
            subprocess.run(
                [python_cmd, "-m", "pip", "install", "-e", ".[test]"],
                cwd=str(workspace_root),
                check=True,
            )
        except Exception as exc:  # noqa: BLE001 - report to release flow
            errors.append(f"pip install failed: {exc}")
    if updated_npm:
        try:
            subprocess.run(
                ["npm", "install"], cwd=str(workspace_root / "web"), check=True
            )
        except Exception as exc:  # noqa: BLE001 - report to release flow
            errors.append(f"npm install failed: {exc}")
    if errors:
        return False, "; ".join(errors)
    return True, "dependencies installed"


def regenerate_python_lock(workspace_root: Path) -> None:
    """Refresh the checked-in lockfile after changing Python dependencies."""
    subprocess.run(["uv", "lock"], cwd=str(workspace_root), check=True)


def _restore_dependency_files(
    pyproject_path: Path,
    package_json_path: Path,
    package_lock_path: Path,
    originals: tuple[str, str, str | None],
) -> None:
    original_pyproject, original_package_json, original_package_lock = originals
    pyproject_path.write_text(original_pyproject, encoding="utf-8")
    package_json_path.write_text(original_package_json, encoding="utf-8")
    if original_package_lock is None:
        package_lock_path.unlink(missing_ok=True)
    else:
        package_lock_path.write_text(original_package_lock, encoding="utf-8")


def get_latest_pypi_version(package_name: str) -> str | None:
    try:
        response = httpx.get(f"https://pypi.org/pypi/{package_name}/json", timeout=10.0)
        if response.status_code == 200:
            return (response.json().get("info") or {}).get("version") or None
    except Exception:  # noqa: BLE001 - release checks must fail open
        pass
    return None


def get_latest_npm_version(package_name: str) -> str | None:
    try:
        response = httpx.get(
            f"https://registry.npmjs.org/{package_name}/latest", timeout=10.0
        )
        if response.status_code == 200:
            return response.json().get("version") or None
    except Exception:  # noqa: BLE001 - release checks must fail open
        pass
    return None


def _pinned_version(spec: str) -> str | None:
    match = re.search(r"[0-9]+(?:\.[0-9]+)*[0-9a-zA-Z.\-]*", spec or "")
    return match.group(0) if match else None


@dataclass(frozen=True, slots=True)
class AvailableUpdate:
    key: str
    ecosystem: str
    current: str
    latest: str
    is_safe: bool
    auto: bool


def check_available_updates(workspace_root: Path) -> list[AvailableUpdate]:
    """Return directly-declared dependencies with newer registry versions."""
    python_deps = parse_pyproject_dependencies(workspace_root / "pyproject.toml")
    npm_deps = parse_npm_dependencies(workspace_root / "web" / "package.json")
    updates: list[AvailableUpdate] = []

    def consider(name: str, spec: str, ecosystem: str, latest: str | None) -> None:
        current = _pinned_version(spec)
        if not current or not latest or parse_semver(latest) <= parse_semver(current):
            return
        updates.append(
            AvailableUpdate(
                key=name,
                ecosystem=ecosystem,
                current=current,
                latest=latest,
                is_safe=is_newer_and_safe_update(current, latest),
                auto=name in AUTO_UPDATE_KEYS,
            )
        )

    for name, spec in sorted(python_deps.items()):
        consider(name, spec, "python", get_latest_pypi_version(name))
    for name, spec in sorted(npm_deps.items()):
        consider(name, spec, "npm", get_latest_npm_version(name))
    return updates


def apply_auto_updates(
    workspace_root: Path, updates: list[AvailableUpdate], *, reinstall: bool = True
) -> list[str]:
    """Apply only the release workflow's explicitly auto-approved updates."""
    pyproject_path = workspace_root / "pyproject.toml"
    package_json_path = workspace_root / "web" / "package.json"
    package_lock_path = workspace_root / "web" / "package-lock.json"
    originals = (
        pyproject_path.read_text(encoding="utf-8"),
        package_json_path.read_text(encoding="utf-8"),
        package_lock_path.read_text(encoding="utf-8")
        if package_lock_path.exists()
        else None,
    )
    applied: list[str] = []
    updated_python = False
    updated_npm = False
    for update in updates:
        if not update.auto or not update.is_safe:
            continue
        if update.ecosystem == "python" and update_pyproject_toml_dependency(
            pyproject_path, update.key, update.latest
        ):
            updated_python = True
            applied.append(f"{update.key} (Python: {update.current} -> {update.latest})")
        elif update.ecosystem == "npm" and update_npm_dependency(
            package_json_path, update.key, update.latest
        ):
            updated_npm = True
            applied.append(f"{update.key} (NPM: {update.current} -> {update.latest})")

    try:
        if updated_python:
            regenerate_python_lock(workspace_root)
        if reinstall and (updated_python or updated_npm):
            installed, error = install_updated_packages(
                updated_python, updated_npm, workspace_root
            )
            if not installed:
                raise RuntimeError(error)
    except Exception:
        _restore_dependency_files(
            pyproject_path, package_json_path, package_lock_path, originals
        )
        raise
    return applied
