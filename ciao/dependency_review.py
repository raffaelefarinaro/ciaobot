"""Weekly dependency-changelog review as a DAG pipeline.

Replaces the hand-rolled ``sched-depcheck`` prompt with a deterministic
:mod:`ciao.dag` pipeline so the run gets per-node timing in the Automation
page and a *gated* baseline write (the old prompt did the write agentically,
so a flaky turn could skip it and silently drift the baseline).

Shape (sequential, because :mod:`ciao.dag` has no fan-out):

    read_baseline ──always──▶ installed ──always──▶ research ──ok──▶ write_baseline
                                                        └─fail─▶ (stop; baseline untouched)

The 7-way release research stays parallel by living *inside* one ``subagent``
node: the spawned ``claude -p`` process fans out its own Agent calls, one per
repo, exactly like the old prompt. The DAG isolates the deterministic
edges around it (load the baseline, persist the merged baseline) so a research
hiccup can't corrupt ``.runtime/dependency_baseline.json``.

Trigger: ``python3 -m ciao.dependency_review`` (the schedule prompt runs this,
mirroring how ``sched-skillevo`` invokes ``ciao.skill_evolution``).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib
    except ImportError:
        tomllib = None

import httpx

from ciao.dag import Edge, Node, NodeResult, run as run_dag

logger = logging.getLogger(__name__)

_DEFAULT_BASELINE = Path(".runtime/dependency_baseline.json")

# Dependencies that should be bumped to the latest release during release prep
# without agent deliberation: the Claude Code / agent SDK tracks the CLI we ship
# against, so keeping it current is routine housekeeping rather than a judgement
# call. Everything else is surfaced for review but left to the operator/model.
AUTO_UPDATE_KEYS = ("claude-agent-sdk",)

# Known repository URLs for direct dependencies to avoid lookup overhead
KNOWN_REPOS = {
    "openai": "https://github.com/openai/openai-python/releases",
    "claude-agent-sdk": "https://github.com/anthropics/claude-agent-sdk-python/releases",
    "notebooklm-py": "https://github.com/teng-lin/notebooklm-py/releases",
    "gws": "https://github.com/googleworkspace/cli/releases",
    "defuddle": "https://github.com/kepano/defuddle/releases",
    "claude-code": "https://github.com/anthropics/claude-code/releases",
    "apfel": "https://github.com/Arthur-Ficial/apfel/releases",
    "starlette": "https://github.com/encode/starlette/releases",
    "uvicorn": "https://github.com/encode/uvicorn/releases",
    "itsdangerous": "https://github.com/pallets/itsdangerous/releases",
    "pyyaml": "https://github.com/yaml/pyyaml/releases",
    "pywebpush": "https://github.com/web-push-libs/pywebpush/releases",
    "cryptography": "https://github.com/pyca/cryptography/releases",
    "httpx": "https://github.com/encode/httpx/releases",
    "python-pptx": "https://github.com/scanny/python-pptx/releases",
    "pytest": "https://github.com/pytest-dev/pytest/releases",
    "pytest-asyncio": "https://github.com/pytest-dev/pytest-asyncio/releases",
    "mlx-whisper": "https://github.com/ml-explore/mlx-examples/releases",
    "vue": "https://github.com/vuejs/core/releases",
    "vue-router": "https://github.com/vuejs/router/releases",
    "pinia": "https://github.com/vuejs/pinia/releases",
    "marked": "https://github.com/markedjs/marked/releases",
    "marked-highlight": "https://github.com/markedjs/marked-highlight/releases",
    "dompurify": "https://github.com/cure53/DOMPurify/releases",
    "highlight.js": "https://github.com/highlightjs/highlight.js/releases",
    "@excalidraw/excalidraw": "https://github.com/excalidraw/excalidraw/releases",
    "react": "https://github.com/facebook/react/releases",
    "react-dom": "https://github.com/facebook/react/releases",
    "typescript": "https://github.com/microsoft/TypeScript/releases",
    "vite": "https://github.com/vitejs/vite/releases",
    "vitest": "https://github.com/vitest-dev/vitest/releases",
    "vue-tsc": "https://github.com/vuejs/language-tools/releases",
}


def get_workspace_root(baseline_path: Path) -> Path:
    p = baseline_path.resolve()
    for parent in [p.parent, p.parent.parent, p.parent.parent.parent]:
        if (parent / "pyproject.toml").exists():
            return parent
    return Path.cwd()


def parse_dependency_spec(dep_str: str) -> tuple[str | None, str]:
    m = re.match(r"^([a-zA-Z0-9_\-]+(\[[a-zA-Z0-9_\-,]+\])?)", dep_str)
    if not m:
        return None, ""
    name_with_extras = m.group(1)
    name = re.sub(r"\[.*\]", "", name_with_extras).strip()
    spec = dep_str[len(name_with_extras):].strip()
    return name, spec or "*"


def parse_pyproject_dependencies(toml_path: Path) -> dict[str, str]:
    if not toml_path.exists() or tomllib is None:
        return {}
    try:
        with toml_path.open("rb") as f:
            data = tomllib.load(f)
        deps = {}
        project = data.get("project", {})
        for dep_str in project.get("dependencies", []):
            name, spec = parse_dependency_spec(dep_str)
            if name:
                deps[name] = spec
        optional = project.get("optional-dependencies", {})
        for group, dep_list in optional.items():
            for dep_str in dep_list:
                name, spec = parse_dependency_spec(dep_str)
                if name:
                    deps[name] = spec
        return deps
    except Exception:
        return {}


def parse_npm_dependencies(pkg_json_path: Path) -> dict[str, str]:
    if not pkg_json_path.exists():
        return {}
    try:
        data = json.loads(pkg_json_path.read_text(encoding="utf-8"))
        deps = {}
        for k in ("dependencies", "devDependencies"):
            for name, spec in data.get(k, {}).items():
                deps[name] = spec
        return deps
    except Exception:
        return {}


def get_pypi_github_url(package_name: str) -> str | None:
    try:
        r = httpx.get(f"https://pypi.org/pypi/{package_name}/json", timeout=5.0)
        if r.status_code == 200:
            data = r.json()
            info = data.get("info", {})
            urls = info.get("project_urls") or {}
            for name, url in urls.items():
                if "github.com" in url.lower():
                    base = url.split("/issues")[0].split("/pulls")[0].rstrip("/")
                    return f"{base}/releases"
            hp = info.get("home_page") or ""
            if "github.com" in hp.lower():
                return f"{hp.rstrip('/')}/releases"
    except Exception:
        pass
    return None


def get_npm_github_url(package_name: str) -> str | None:
    try:
        r = httpx.get(f"https://registry.npmjs.org/{package_name}/latest", timeout=5.0)
        if r.status_code == 200:
            data = r.json()
            repo = data.get("repository") or {}
            url = ""
            if isinstance(repo, dict):
                url = repo.get("url") or ""
            elif isinstance(repo, str):
                url = repo
            if "github.com" in url.lower():
                cleaned = url.replace("git+", "").replace("git://", "https://").replace(".git", "")
                return f"{cleaned.rstrip('/')}/releases"
    except Exception:
        pass
    return None


def get_installed_python_version(name: str) -> str:
    try:
        from importlib.metadata import version
        return version(name)
    except Exception:
        return "not installed"


def get_installed_npm_version(workspace_root: Path, name: str) -> str:
    try:
        pkg_json = workspace_root / "web" / "node_modules" / name / "package.json"
        if pkg_json.exists():
            data = json.loads(pkg_json.read_text(encoding="utf-8"))
            return data.get("version", "unknown")
    except Exception:
        pass
    return "not installed"


def get_installed_cli_version(cmd: str) -> str:
    from ciao.tool_path import resolve_tool

    # Resolve against the login-shell PATH so npm/brew-installed CLIs are found
    # even when the server was launched by a GUI/launchd job with a stripped PATH.
    binary = resolve_tool(cmd) or cmd
    try:
        res = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=2.0)
        if res.returncode == 0:
            return res.stdout.strip()
    except Exception:
        pass
    return "not installed"


def compile_tracked_tools(workspace_root: Path) -> list[dict[str, str]]:
    pyproject_path = workspace_root / "pyproject.toml"
    package_json_path = workspace_root / "web" / "package.json"

    python_deps = parse_pyproject_dependencies(pyproject_path)
    npm_deps = parse_npm_dependencies(package_json_path)

    always_tracked = ["gws", "claude-code", "defuddle", "apfel"]
    all_keys = set(always_tracked) | set(python_deps.keys()) | set(npm_deps.keys())

    tools = []
    for key in sorted(all_keys):
        repo_url = KNOWN_REPOS.get(key)
        if not repo_url:
            if key in npm_deps:
                repo_url = get_npm_github_url(key)
            else:
                repo_url = get_pypi_github_url(key)
        if not repo_url:
            repo_url = f"https://github.com/search?q={key}+releases"
        tools.append({"key": key, "repo": repo_url})
    return tools


# Fallback base list for non-workspace context / static references
DEFAULT_TRACKED_TOOLS = (
    {"key": "openai", "repo": "https://github.com/openai/openai-python/releases"},
    {"key": "claude-agent-sdk", "repo": "https://github.com/anthropics/claude-agent-sdk-python/releases"},
    {"key": "notebooklm-py", "repo": "https://github.com/teng-lin/notebooklm-py/releases"},
    {"key": "gws", "repo": "https://github.com/googleworkspace/cli/releases"},
    {"key": "defuddle", "repo": "https://github.com/kepano/defuddle/releases"},
    {"key": "claude-code", "repo": "https://github.com/anthropics/claude-code/releases"},
    {"key": "apfel", "repo": "https://github.com/Arthur-Ficial/apfel/releases"},
)

# Populated dynamically where possible
try:
    TRACKED_TOOLS = tuple(compile_tracked_tools(get_workspace_root(_DEFAULT_BASELINE)))
except Exception:
    TRACKED_TOOLS = DEFAULT_TRACKED_TOOLS


def update_pyproject_toml_dependency(toml_path: Path, package_name: str, new_version: str) -> bool:
    if not toml_path.exists():
        return False
    content = toml_path.read_text(encoding="utf-8")
    pattern = rf'(\s*"{re.escape(package_name)}(?:\[[a-zA-Z0-9_\-,]+\])?==)[0-9a-zA-Z\.\-]+(")'
    new_content, count = re.subn(
        pattern, lambda m: f"{m.group(1)}{new_version}{m.group(2)}", content
    )
    if count > 0:
        toml_path.write_text(new_content, encoding="utf-8")
        return True
    return False


def update_npm_dependency(pkg_json_path: Path, package_name: str, new_version: str) -> bool:
    if not pkg_json_path.exists():
        return False
    try:
        data = json.loads(pkg_json_path.read_text(encoding="utf-8"))
        updated = False
        for k in ("dependencies", "devDependencies"):
            if k in data and package_name in data[k]:
                current_spec = data[k][package_name]
                prefix = ""
                if current_spec.startswith("^"):
                    prefix = "^"
                elif current_spec.startswith("~"):
                    prefix = "~"
                data[k][package_name] = f"{prefix}{new_version}"
                updated = True
        if updated:
            pkg_json_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            return True
    except Exception:
        pass
    return False


def parse_semver(ver_str: str) -> list[int]:
    ver_str = ver_str.lstrip("vV")
    cleaned = re.sub(r"[^0-9\.]", "", ver_str.split("-")[0])
    parts = []
    for p in cleaned.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            pass
    while len(parts) < 3:
        parts.append(0)
    return parts[:3]


def is_newer_and_safe_update(old_ver: str, new_ver: str) -> bool:
    try:
        old_parts = parse_semver(old_ver)
        new_parts = parse_semver(new_ver)
        if new_parts > old_parts:
            # Only update minor/patch versions automatically to avoid breaking changes
            return new_parts[0] == old_parts[0]
    except Exception:
        pass
    return False


def install_updated_packages(updated_python: bool, updated_npm: bool, workspace_root: Path) -> None:
    if updated_python:
        logger.info("Re-installing python workspace dependencies...")
        try:
            venv_bin = workspace_root / ".venv" / "bin" / "pip"
            pip_cmd = str(venv_bin) if venv_bin.exists() else "pip"
            subprocess.run([pip_cmd, "install", "-e", ".[test]"], cwd=str(workspace_root), check=True)
            logger.info("Python dependencies updated successfully.")
        except Exception as e:
            logger.error("Failed to run pip install: %s", e)
    if updated_npm:
        logger.info("Running npm install in web directory...")
        try:
            subprocess.run(["npm", "install"], cwd=str(workspace_root / "web"), check=True)
            logger.info("NPM dependencies updated successfully.")
        except Exception as e:
            logger.error("Failed to run npm install: %s", e)


def get_latest_pypi_version(package_name: str) -> str | None:
    """Return the latest released version on PyPI, or None on any failure."""
    try:
        r = httpx.get(f"https://pypi.org/pypi/{package_name}/json", timeout=10.0)
        if r.status_code == 200:
            info = r.json().get("info") or {}
            return info.get("version") or None
    except Exception:
        pass
    return None


def get_latest_npm_version(package_name: str) -> str | None:
    """Return the latest released version on the npm registry, or None."""
    try:
        r = httpx.get(f"https://registry.npmjs.org/{package_name}/latest", timeout=10.0)
        if r.status_code == 200:
            return r.json().get("version") or None
    except Exception:
        pass
    return None


def _pinned_version(spec: str) -> str | None:
    """Extract a concrete version from a dependency spec.

    Handles Python pins (``==1.2.3``) and npm ranges (``^1.2.3``, ``~1.2.3``).
    Returns None for open specs like ``*`` where no version is declared.
    """
    m = re.search(r"[0-9]+(?:\.[0-9]+)*[0-9a-zA-Z.\-]*", spec or "")
    return m.group(0) if m else None


@dataclass(frozen=True, slots=True)
class AvailableUpdate:
    key: str
    ecosystem: str  # "python" | "npm"
    current: str
    latest: str
    is_safe: bool  # same-major (minor/patch) bump, safe to take automatically
    auto: bool  # in AUTO_UPDATE_KEYS: bump without deliberation


def check_available_updates(workspace_root: Path) -> list[AvailableUpdate]:
    """Compare declared dependencies against the latest published versions.

    Queries PyPI / the npm registry for every directly-declared dependency and
    returns the ones with a newer release available, flagging same-major
    ("safe") bumps and the auto-update packages so the release flow can present
    them for review.
    """
    pyproject_path = workspace_root / "pyproject.toml"
    package_json_path = workspace_root / "web" / "package.json"
    python_deps = parse_pyproject_dependencies(pyproject_path)
    npm_deps = parse_npm_dependencies(package_json_path)

    updates: list[AvailableUpdate] = []

    def _consider(name: str, spec: str, ecosystem: str, latest: str | None) -> None:
        current = _pinned_version(spec)
        if not current or not latest:
            return
        if parse_semver(latest) <= parse_semver(current):
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
        _consider(name, spec, "python", get_latest_pypi_version(name))
    for name, spec in sorted(npm_deps.items()):
        _consider(name, spec, "npm", get_latest_npm_version(name))

    return updates


def apply_auto_updates(
    workspace_root: Path, updates: list[AvailableUpdate], *, reinstall: bool = True
) -> list[str]:
    """Bump the auto-update dependencies (Claude SDK) to their latest release.

    Rewrites ``pyproject.toml`` / ``web/package.json`` in place and optionally
    reinstalls so checks run against the new version. Returns human-readable
    labels for the changes applied.
    """
    pyproject_path = workspace_root / "pyproject.toml"
    package_json_path = workspace_root / "web" / "package.json"

    applied: list[str] = []
    updated_python = False
    updated_npm = False
    for u in updates:
        if not u.auto:
            continue
        if u.ecosystem == "python":
            if update_pyproject_toml_dependency(pyproject_path, u.key, u.latest):
                updated_python = True
                applied.append(f"{u.key} (Python: {u.current} -> {u.latest})")
        elif u.ecosystem == "npm":
            if update_npm_dependency(package_json_path, u.key, u.latest):
                updated_npm = True
                applied.append(f"{u.key} (NPM: {u.current} -> {u.latest})")

    if reinstall and (updated_python or updated_npm):
        install_updated_packages(updated_python, updated_npm, workspace_root)
    return applied


def _resolve_model(requested: str) -> str:
    if requested != "sonnet":
        return requested
    override = os.environ.get("CIAO_OLLAMA_SONNET_MODEL", "").strip()
    if override:
        return override
    return requested


def _routing_env_for_research_model(model: str) -> dict[str, str]:
    """Return provider env overrides for the depcheck research subprocess."""
    try:
        from ciao.config import CiaoConfig
        from ciao.providers.routing import routing_env_for_model

        config = CiaoConfig.from_env()
        return routing_env_for_model(model, config)
    except Exception as exc:  # noqa: BLE001 - depcheck can still run Anthropic
        logger.debug("depcheck routing env unavailable for %s: %s", model, exc)
        return {}


def _read_baseline(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {"_meta": {}, "tools": {}}
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("baseline unreadable (%s); treating as empty", exc)
        return {"_meta": {}, "tools": {}}


def _extract_json_block(text: str) -> dict[str, Any] | None:
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidates = []
    if fenced:
        candidates.append(fenced.group(1))
    brace = re.search(r"\{.*\}", text, re.DOTALL)
    if brace:
        candidates.append(brace.group(0))
    for cand in candidates:
        try:
            parsed = json.loads(cand)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue
    return None


def _build_research_prompt(baseline: dict[str, Any], first_run: bool) -> str:
    tools_lines = []
    base_tools = baseline.get("tools", {})
    for t in TRACKED_TOOLS:
        bv = base_tools.get(t["key"], {}).get("version", "unknown")
        tools_lines.append(f"- {t['key']}: baseline={bv} | releases: {t['repo']}")
    tools_block = "\n".join(tools_lines)
    depth = "the last 2 releases" if first_run else "releases newer than the baseline version"
    rendered = f"""Dependency changelog review. Compare each tool's GitHub releases against our tracked BASELINE version (NOT the installed version).

Tracked tools:
{tools_block}

For each tool, dispatch a parallel web lookup of its releases page and inspect {depth}.
Verify:
1. Version and release date of the latest release.
2. Breaking changes, deprecation warnings, or compatibility requirements affecting a Python web/agent server or a Vue PWA.
3. Bug fixes and new features/APIs we should adopt to make use of those new versions.

Return ONLY a JSON object in a ```json fenced block with this exact shape:
{{
  "tools": {{
    "<tool-key>": {{
      "version": "<latest reviewed version>",
      "release_date": "<YYYY-MM-DD or empty>",
      "notes": "<one-line summary, or 'Nothing notable.'>",
      "actionable": "<a concrete next step if any, e.g. code/compatibility updates to adopt, else empty string>"
    }}
  }},
  "summary": "<2-3 sentence overall review highlighting updates, potential breaking compatibility issues, and recommendations for new API adoption>"
}}
Use the exact tool keys listed above. Include every tracked tool, even when nothing changed (carry the baseline version forward)."""
    return rendered.replace("{", "{{").replace("}", "}}")


def build_review_dag(
    *,
    baseline_path: Path,
    research_model: str,
    research_timeout_s: float,
) -> tuple[list[Node], list[Edge], dict[str, Any]]:
    global TRACKED_TOOLS
    workspace_root = get_workspace_root(baseline_path)
    try:
        TRACKED_TOOLS = tuple(compile_tracked_tools(workspace_root))
    except Exception:
        pass

    holder: dict[str, Any] = {"baseline": {}, "written": None, "research_json": None}

    def read_baseline_node(ctx: dict[str, Any]) -> tuple[bool, str]:
        holder["baseline"] = _read_baseline(baseline_path)
        n = len(holder["baseline"].get("tools", {}))
        return True, f"baseline loaded: {n} tracked tool(s)"

    def collect_installed_versions_node(ctx: dict[str, Any]) -> tuple[bool, str]:
        pyproject_path = workspace_root / "pyproject.toml"
        package_json_path = workspace_root / "web" / "package.json"

        python_deps = parse_pyproject_dependencies(pyproject_path)
        npm_deps = parse_npm_dependencies(package_json_path)

        versions = {}
        for t in TRACKED_TOOLS:
            key = t["key"]
            if key == "gws":
                versions[key] = get_installed_cli_version("gws")
            elif key == "claude-code":
                versions[key] = get_installed_cli_version("claude")
            elif key == "defuddle":
                versions[key] = get_installed_cli_version("defuddle")
            elif key == "apfel":
                versions[key] = get_installed_cli_version("apfel")
            elif key in npm_deps:
                versions[key] = get_installed_npm_version(workspace_root, key)
            else:
                versions[key] = get_installed_python_version(key)

        ctx["installed_versions"] = versions
        return True, f"collected installed versions for {len(versions)} tools"

    def write_baseline_node(ctx: dict[str, Any]) -> tuple[bool, str]:
        research = ctx.get("research")
        raw = getattr(research, "output", None) if research else None
        if not raw:
            return False, "no research output to persist"
        parsed = _extract_json_block(raw)
        if not parsed or "tools" not in parsed:
            return False, "research output missing parseable {tools: ...} JSON"
        holder["research_json"] = parsed
        baseline = holder["baseline"] or {"_meta": {}, "tools": {}}
        baseline.setdefault("tools", {})

        pyproject_path = workspace_root / "pyproject.toml"
        package_json_path = workspace_root / "web" / "package.json"

        python_deps = parse_pyproject_dependencies(pyproject_path)
        npm_deps = parse_npm_dependencies(package_json_path)

        updated_python = False
        updated_npm = False
        updated_tools_list = []

        for key, info in parsed["tools"].items():
            if not isinstance(info, dict):
                continue
            existing = baseline["tools"].get(key, {})
            merged = {
                "version": info.get("version", existing.get("version", "unknown")),
                "release_date": info.get("release_date", existing.get("release_date", "")),
                "notes": info.get("notes") or existing.get("notes", ""),
            }
            baseline["tools"][key] = merged

            latest_version = info.get("version")
            if not latest_version or latest_version == "unknown":
                continue

            if key in python_deps:
                declared = python_deps[key]
                m = re.search(r"([0-9a-zA-Z\.\-]+)$", declared)
                current_ver = m.group(1) if m else None
                if current_ver and latest_version != current_ver:
                    if is_newer_and_safe_update(current_ver, latest_version):
                        if update_pyproject_toml_dependency(pyproject_path, key, latest_version):
                            updated_python = True
                            updated_tools_list.append(f"{key} (Python: {current_ver} -> {latest_version})")
            elif key in npm_deps:
                declared = npm_deps[key]
                m = re.search(r"([0-9a-zA-Z\.\-]+)$", declared)
                current_ver = m.group(1) if m else None
                if current_ver and latest_version != current_ver:
                    if is_newer_and_safe_update(current_ver, latest_version):
                        if update_npm_dependency(package_json_path, key, latest_version):
                            updated_npm = True
                            updated_tools_list.append(f"{key} (NPM: {current_ver} -> {latest_version})")

        baseline.setdefault("_meta", {})
        baseline["_meta"]["last_reviewed_summary"] = parsed.get("summary", "")
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(json.dumps(baseline, indent=2) + "\n")
        holder["written"] = baseline

        msg = f"baseline written: {len(baseline['tools'])} tool(s)"
        if updated_tools_list:
            msg += f". Updated config files for: {', '.join(updated_tools_list)}"
            install_updated_packages(updated_python, updated_npm, workspace_root)

        return True, msg

    first_run = not bool(_read_baseline(baseline_path).get("tools"))
    research_prompt = _build_research_prompt(_read_baseline(baseline_path), first_run)

    from ciao.providers.claude import get_bundled_claude_path
    import shutil
    cli_path = get_bundled_claude_path() or shutil.which("claude") or "claude"
    research_env = _routing_env_for_research_model(research_model)

    nodes: list[Node] = [
        Node(id="read_baseline", kind="gate", payload={"fn": read_baseline_node}),
        Node(id="installed", kind="gate", payload={"fn": collect_installed_versions_node}),
        Node(
            id="research",
            kind="subagent",
            model=research_model,
            timeout_s=research_timeout_s,
            payload={"prompt": research_prompt, "cli": cli_path, "env": research_env},
        ),
        Node(id="write_baseline", kind="gate", payload={"fn": write_baseline_node}),
    ]
    edges: list[Edge] = [
        Edge(src="read_baseline", dst="installed", when="always"),
        Edge(src="installed", dst="research", when="always"),
        Edge(src="research", dst="write_baseline", when="ok"),
    ]
    return nodes, edges, holder


def run_review(
    *,
    baseline_path: Path | None = None,
    research_model: str = "sonnet",
    research_timeout_s: float = 1800.0,
) -> dict[str, Any]:
    baseline_path = baseline_path or _DEFAULT_BASELINE
    nodes, edges, holder = build_review_dag(
        baseline_path=baseline_path,
        research_model=_resolve_model(research_model),
        research_timeout_s=research_timeout_s,
    )
    run_dag(nodes, edges, job="dependency_review", label="depcheck")
    return holder


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Weekly dependency changelog review (DAG pipeline).",
    )
    parser.add_argument("--baseline", type=Path, default=_DEFAULT_BASELINE)
    parser.add_argument(
        "--model",
        default="sonnet",
        help=(
            "model for the research subagent (claude -p). The literal "
            "'sonnet' is rewritten to $CIAO_OLLAMA_SONNET_MODEL when set, "
            "so scheduling with --model sonnet reaches the configured "
            "Ollama tier instead of the bundled CLI's sonnet-4.6 alias."
        ),
    )
    parser.add_argument("--timeout", type=float, default=1800.0, help="research node timeout (s)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate + print the DAG structure without calling the model",
    )
    args = parser.parse_args(argv)
    resolved_model = _resolve_model(args.model)

    nodes, edges, _holder = build_review_dag(
        baseline_path=args.baseline,
        research_model=resolved_model,
        research_timeout_s=args.timeout,
    )
    if args.dry_run:
        from ciao.dag import _start_node, _validate

        _validate(nodes, edges)
        start = _start_node(nodes, edges)
        print(f"depcheck DAG OK: {len(nodes)} nodes, {len(edges)} edges, start='{start}'")
        for n in nodes:
            print(f"  - {n.id} ({n.kind}{', model=' + n.model if n.model else ''})")
        for e in edges:
            print(f"  edge {e.src} --{e.when}--> {e.dst}")
        return 0

    holder = run_review(
        baseline_path=args.baseline,
        research_model=resolved_model,
        research_timeout_s=args.timeout,
    )
    summary = (holder.get("research_json") or {}).get("summary", "(no summary)")
    print(f"📦 Dependency review\n{summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
