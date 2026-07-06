"""Tests for the /api/workspace-file endpoint.

The handler resolves a relative or absolute path, canonicalises it, and
serves it only when it lands inside ``config.workspace_root`` and matches
the allowed extension list. These tests spin up a minimal Starlette app
around the handler with a tmp workspace to exercise each guard.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.web.routes_api import workspace_file


@dataclass
class _FakeConfig:
    workspace_root: Path
    # Extra read-only roots the viewer may serve (e.g. `~/repos` for
    # project-linked repos). Defaults to empty so existing tests keep the
    # original "single workspace root" semantics.
    extra_workspace_roots: list[Path] = None  # type: ignore[assignment]
    # When True, the viewer drops the root sandbox and serves any
    # allowlisted-extension file on the system (opt-in via env).
    unrestricted_file_viewer: bool = False

    def __post_init__(self) -> None:
        if self.extra_workspace_roots is None:
            self.extra_workspace_roots = []


def _make_client(
    workspace_root: Path,
    extra_workspace_roots: list[Path] | None = None,
    unrestricted_file_viewer: bool = False,
) -> TestClient:
    app = Starlette(routes=[Route("/api/workspace-file", workspace_file, methods=["GET"])])
    app.state.config = _FakeConfig(
        workspace_root=workspace_root,
        extra_workspace_roots=list(extra_workspace_roots or []),
        unrestricted_file_viewer=unrestricted_file_viewer,
    )
    return TestClient(app)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "docs").mkdir()
    (ws / "docs" / "readme.md").write_text("# hello\n", encoding="utf-8")
    (ws / "script.py").write_text("print('ok')\n", encoding="utf-8")
    (ws / "photo.jpg").write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")
    return ws


def test_valid_relative_path_returns_content(workspace: Path) -> None:
    client = _make_client(workspace)
    resp = client.get("/api/workspace-file", params={"path": "docs/readme.md"})
    assert resp.status_code == 200
    assert resp.text == "# hello\n"
    assert "text/plain" in resp.headers["content-type"]


def test_excalidraw_file_returns_content(workspace: Path) -> None:
    drawing = workspace / "diagram.excalidraw"
    drawing.write_text(
        '{"type":"excalidraw","version":2,"source":"https://excalidraw.com"}',
        encoding="utf-8",
    )
    client = _make_client(workspace)
    resp = client.get("/api/workspace-file", params={"path": "diagram.excalidraw"})
    assert resp.status_code == 200
    assert '"type":"excalidraw"' in resp.text
    assert "text/plain" in resp.headers["content-type"]


def test_valid_absolute_path_inside_workspace_returns_content(workspace: Path) -> None:
    client = _make_client(workspace)
    resp = client.get(
        "/api/workspace-file",
        params={"path": str(workspace / "docs" / "readme.md")},
    )
    assert resp.status_code == 200
    assert resp.text == "# hello\n"


def test_line_suffix_is_stripped(workspace: Path) -> None:
    client = _make_client(workspace)
    resp = client.get("/api/workspace-file", params={"path": "docs/readme.md:42"})
    assert resp.status_code == 200
    assert resp.text == "# hello\n"


def test_missing_path_returns_400(workspace: Path) -> None:
    client = _make_client(workspace)
    resp = client.get("/api/workspace-file")
    assert resp.status_code == 400


def test_traversal_returns_403(workspace: Path, tmp_path: Path) -> None:
    # Place a secret at tmp_path/secret.md (outside the workspace)
    outside = tmp_path / "secret.md"
    outside.write_text("do not read me", encoding="utf-8")
    client = _make_client(workspace)
    resp = client.get("/api/workspace-file", params={"path": "../secret.md"})
    assert resp.status_code == 403


def test_absolute_path_outside_workspace_returns_403(workspace: Path, tmp_path: Path) -> None:
    outside = tmp_path / "other.md"
    outside.write_text("nope", encoding="utf-8")
    client = _make_client(workspace)
    resp = client.get("/api/workspace-file", params={"path": str(outside)})
    assert resp.status_code == 403


def test_symlink_escape_returns_403(workspace: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("not inside", encoding="utf-8")
    link = workspace / "escape.md"
    link.symlink_to(outside)
    client = _make_client(workspace)
    resp = client.get("/api/workspace-file", params={"path": "escape.md"})
    assert resp.status_code == 403


def test_missing_file_returns_404(workspace: Path) -> None:
    client = _make_client(workspace)
    resp = client.get("/api/workspace-file", params={"path": "docs/nope.md"})
    assert resp.status_code == 404


def test_disallowed_extension_returns_415(workspace: Path) -> None:
    client = _make_client(workspace)
    resp = client.get("/api/workspace-file", params={"path": "photo.jpg"})
    assert resp.status_code == 415


def test_file_too_large_returns_413(workspace: Path) -> None:
    big = workspace / "big.md"
    big.write_bytes(b"x" * (2 * 1024 * 1024 + 1))
    client = _make_client(workspace)
    resp = client.get("/api/workspace-file", params={"path": "big.md"})
    assert resp.status_code == 413


def test_directory_path_returns_404(workspace: Path) -> None:
    client = _make_client(workspace)
    resp = client.get("/api/workspace-file", params={"path": "docs"})
    assert resp.status_code == 404


# ── extra workspace roots (e.g. ~/repos) ──────────────────────────────────


def test_extra_root_absolute_path_returns_content(workspace: Path, tmp_path: Path) -> None:
    """Files under a configured extra root resolve via absolute path. This
    is the path that comes back from the PWA viewer when the user clicks a
    `~/repos/<name>/<file>` link in chat."""
    repos = tmp_path / "repos"
    repos.mkdir()
    (repos / "myrepo").mkdir()
    (repos / "myrepo" / "README.md").write_text("# repo readme\n", encoding="utf-8")

    client = _make_client(workspace, extra_workspace_roots=[repos])
    resp = client.get(
        "/api/workspace-file",
        params={"path": str(repos / "myrepo" / "README.md")},
    )
    assert resp.status_code == 200
    assert resp.text == "# repo readme\n"


def test_extra_root_does_not_anchor_relative_paths(workspace: Path, tmp_path: Path) -> None:
    """Relative paths still anchor to the primary workspace root, never to
    extras. A bare `REPO_ONLY_FILE.md` request hits `<ws>/REPO_ONLY_FILE.md`, not
    `<extra-root>/REPO_ONLY_FILE.md`. This avoids same-name shadowing surprises
    when both the workspace and a linked repo have a `REPO_ONLY_FILE.md`."""
    repos = tmp_path / "repos"
    repos.mkdir()
    (repos / "REPO_ONLY_FILE.md").write_text("# repo (should not be served)\n", encoding="utf-8")

    client = _make_client(workspace, extra_workspace_roots=[repos])
    resp = client.get("/api/workspace-file", params={"path": "REPO_ONLY_FILE.md"})
    # No REPO_ONLY_FILE.md under the workspace fixture, so this must 404 rather than
    # silently fall through to the repos root.
    assert resp.status_code == 404


def test_path_outside_all_roots_returns_403(workspace: Path, tmp_path: Path) -> None:
    """A file that lives under neither the workspace nor any extra root is
    still forbidden. Adding extras must not loosen the boundary for paths
    outside them."""
    repos = tmp_path / "repos"
    repos.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    secret = elsewhere / "secret.md"
    secret.write_text("nope", encoding="utf-8")

    client = _make_client(workspace, extra_workspace_roots=[repos])
    resp = client.get("/api/workspace-file", params={"path": str(secret)})
    assert resp.status_code == 403


def test_extra_root_still_enforces_extension_allowlist(workspace: Path, tmp_path: Path) -> None:
    """The extension allowlist is the same regardless of which root the
    file lives under; .env-style files in repos shouldn't be readable just
    because the repo is linked."""
    repos = tmp_path / "repos"
    repos.mkdir()
    secret = repos / ".env"
    secret.write_text("API_KEY=super-secret", encoding="utf-8")

    client = _make_client(workspace, extra_workspace_roots=[repos])
    resp = client.get("/api/workspace-file", params={"path": str(secret)})
    # Either 415 (unsupported type) or 404 if Path() rejects the dotfile;
    # both are acceptable as long as the secret never lands in the body.
    assert resp.status_code in (404, 415)


def test_extra_root_symlink_escape_returns_403(workspace: Path, tmp_path: Path) -> None:
    """A symlink inside an extra root that points outside both roots must
    still 403, just like inside the primary workspace."""
    repos = tmp_path / "repos"
    repos.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("not allowed", encoding="utf-8")
    link = repos / "escape.md"
    link.symlink_to(outside)

    client = _make_client(workspace, extra_workspace_roots=[repos])
    resp = client.get("/api/workspace-file", params={"path": str(link)})
    assert resp.status_code == 403


def test_fuzzy_suffix_match(workspace: Path) -> None:
    """A suffix path match resolves to the correct nested file."""
    nested_dir = workspace / "subdir" / "docs"
    nested_dir.mkdir(parents=True)
    (nested_dir / "target.md").write_text("fuzzy target content", encoding="utf-8")

    client = _make_client(workspace)
    resp = client.get("/api/workspace-file", params={"path": "docs/target.md"})
    assert resp.status_code == 200
    assert resp.text == "fuzzy target content"


def test_fuzzy_suffix_match_wrong_extension(workspace: Path) -> None:
    """A suffix path match with typo extension still resolves to the correct file."""
    nested_dir = workspace / "subdir" / "docs"
    nested_dir.mkdir(parents=True)
    (nested_dir / "target.md").write_text("fuzzy target content", encoding="utf-8")

    client = _make_client(workspace)
    resp = client.get("/api/workspace-file", params={"path": "docs/target.nd"})
    assert resp.status_code == 200
    assert resp.text == "fuzzy target content"


def test_fuzzy_filename_match(workspace: Path) -> None:
    """A bare filename match resolves to the correct nested file."""
    nested_dir = workspace / "subdir" / "docs"
    nested_dir.mkdir(parents=True)
    (nested_dir / "target.md").write_text("fuzzy target content", encoding="utf-8")

    client = _make_client(workspace)
    resp = client.get("/api/workspace-file", params={"path": "target.md"})
    assert resp.status_code == 200
    assert resp.text == "fuzzy target content"


def test_fuzzy_filename_match_wrong_extension(workspace: Path) -> None:
    """A bare filename match with wrong extension still resolves to the correct file."""
    nested_dir = workspace / "subdir" / "docs"
    nested_dir.mkdir(parents=True)
    (nested_dir / "target.md").write_text("fuzzy target content", encoding="utf-8")

    client = _make_client(workspace)
    resp = client.get("/api/workspace-file", params={"path": "target.nd"})
    assert resp.status_code == 200
    assert resp.text == "fuzzy target content"


def test_fuzzy_filename_match_no_extension(workspace: Path) -> None:
    """A bare filename match without any extension still resolves to the correct file."""
    nested_dir = workspace / "subdir" / "docs"
    nested_dir.mkdir(parents=True)
    (nested_dir / "target.md").write_text("fuzzy target content", encoding="utf-8")

    client = _make_client(workspace)
    resp = client.get("/api/workspace-file", params={"path": "target"})
    assert resp.status_code == 200
    assert resp.text == "fuzzy target content"


def test_fuzzy_match_multiple_options_sorts_by_closeness(workspace: Path) -> None:
    """Multiple matches are sorted by match quality, primary root, and shortest path."""
    (workspace / "dir1").mkdir()
    (workspace / "dir2").mkdir()
    (workspace / "dir1" / "testfile.md").write_text("first option", encoding="utf-8")
    (workspace / "dir2" / "testfile.md").write_text("second option", encoding="utf-8")

    client = _make_client(workspace)
    resp = client.get("/api/workspace-file", params={"path": "testfile.md"})
    assert resp.status_code == 200
    # "dir1/testfile.md" is sorted first alphabetically/by path
    assert resp.text == "first option"


def test_unrestricted_serves_file_outside_workspace(workspace: Path, tmp_path: Path) -> None:
    """In unrestricted mode, an absolute path outside the workspace (and any
    extra root) still serves, as long as the extension is allowlisted."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    outside = elsewhere / "notes.md"
    outside.write_text("from outside", encoding="utf-8")

    client = _make_client(workspace, unrestricted_file_viewer=True)
    resp = client.get("/api/workspace-file", params={"path": str(outside)})
    assert resp.status_code == 200
    assert resp.text == "from outside"


def test_unrestricted_relative_path_still_anchors_to_workspace(workspace: Path, tmp_path: Path) -> None:
    """Relative paths still anchor to the workspace root in unrestricted mode;
    the catch-all ``/`` only admits absolute paths. A relative name that
    doesn't exist anywhere in the workspace (so fuzzy can't rescue it) must
    NOT resolve to a same-named file living outside the workspace."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "unique_outside.md").write_text("not anchored here", encoding="utf-8")

    client = _make_client(workspace, unrestricted_file_viewer=True)
    resp = client.get("/api/workspace-file", params={"path": "unique_outside.md"})
    assert resp.status_code == 404


def test_unrestricted_still_enforces_extension_allowlist(workspace: Path, tmp_path: Path) -> None:
    """Unrestricted mode loosens the root check only. The extension allowlist
    still blocks .env / secret files regardless of where they live."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    secret = elsewhere / ".env"
    secret.write_text("API_KEY=super-secret", encoding="utf-8")

    client = _make_client(workspace, unrestricted_file_viewer=True)
    resp = client.get("/api/workspace-file", params={"path": str(secret)})
    assert resp.status_code in (404, 415)
    assert "super-secret" not in resp.text


def test_unrestricted_disabled_by_default(workspace: Path, tmp_path: Path) -> None:
    """Without the opt-in, a path outside the workspace still 403s."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    outside = elsewhere / "notes.md"
    outside.write_text("from outside", encoding="utf-8")

    client = _make_client(workspace)
    resp = client.get("/api/workspace-file", params={"path": str(outside)})
    assert resp.status_code == 403
