"""HEIC/HEIF photo uploads (iPhone photos) reach the agent as a JPEG it can see.

The seams: what ``sips`` is asked to run, what the provider is handed (a JPEG
image block plus both paths in the prompt), and the fallback when this host
cannot convert (the original attached as a file, with a notice, never dropped).
"""

from __future__ import annotations

import base64
import subprocess
import sys
import time
import uuid
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.config import CiaoConfig
from ciao.models import AgentRequest
from ciao.providers.acp.agents import ClaudeAcpProvider
from ciao.providers.base import build_prompt
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web import heif_images
from ciao.web.heif_images import (
    HeifConversionError,
    HeifNotConverted,
    convert_heif_to_jpeg,
    is_heif,
)
from ciao.web.project_chats import ProjectChatManager
from ciao.web.routes_api import chat_images, desktop_drop_import

FIXTURE = Path(__file__).parent / "fixtures" / "heic" / "orientation-6.heic"
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16 + b"jpeg-body"
# The first bytes of a real HEIC: an ftyp box with the "heic" major brand.
HEIC_HEAD = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic"


def _make_manager(tmp_path: Path) -> ProjectChatManager:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
    )
    return ProjectChatManager(
        config,
        state_store=StateStore(config.state_path, tmp_path, config.media_root),
        transcript_store=TranscriptStore(runtime, tmp_path / "transcripts"),
        path=runtime / "web_projects.json",
    )


def _client(pcm: ProjectChatManager) -> TestClient:
    app = Starlette(routes=[
        Route("/api/chats/{chat_id}/images", chat_images, methods=["POST"]),
        Route("/api/desktop-drop", desktop_drop_import, methods=["POST"]),
    ])
    app.state.project_chat_manager = pcm
    app.state.config = pcm._config
    return TestClient(app)


def _fake_converter(calls: list[tuple[Path, Path]]):  # type: ignore[no-untyped-def]
    def convert(source: Path, target: Path) -> None:
        calls.append((source, target))
        target.write_bytes(JPEG)
    return convert


def _failing_converter(source: Path, target: Path) -> None:
    raise HeifConversionError("this computer has no HEIC converter (only macOS has one built in)")


# ── detection ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", ["IMG_0001.HEIC", "photo.heic", "photo.HEIF", "burst.heics", "x.hif"])
def test_heif_is_recognised_by_extension_in_any_case(name: str) -> None:
    assert is_heif(b"", name)


def test_heif_is_recognised_by_its_bytes_under_a_wrong_name() -> None:
    # iOS can hand over a HEIC as "image.jpg", or with no name at all.
    assert is_heif(HEIC_HEAD, "image.jpg")
    assert is_heif(HEIC_HEAD, "")
    assert not is_heif(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8, "shot.png")
    assert not is_heif(JPEG, "photo.jpg")


# ── the sips call ───────────────────────────────────────────────────────────


def test_conversion_runs_sips_with_an_argument_list_and_a_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "web_1.heic"
    source.write_bytes(HEIC_HEAD)
    target = tmp_path / "web_1.jpg"
    seen: dict = {}
    srgb_calls: list[list[str]] = []

    def fake_run(argv, **kwargs):  # type: ignore[no-untyped-def]
        if argv[1] == "-m":
            srgb_calls.append(argv)
            Path(argv[-1]).write_bytes(Path(argv[3]).read_bytes())
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        seen["script"] = Path(argv[2]).read_text(encoding="utf-8")
        (Path(argv[4]) / "converted.jpg").write_bytes(JPEG)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(heif_images.sys, "platform", "darwin")
    monkeypatch.setattr(heif_images, "SIPS", tmp_path / "sips")
    (tmp_path / "sips").write_text("")
    monkeypatch.setattr(heif_images.subprocess, "run", fake_run)

    convert_heif_to_jpeg(source, target)

    argv = seen["argv"]
    assert argv[0] == str(tmp_path / "sips")
    assert argv[1] == "-j" and argv[3] == "-o"
    assert argv[-1] == str(source)
    assert seen["kwargs"]["timeout"] == heif_images.CONVERT_TIMEOUT_S
    assert "shell" not in seen["kwargs"]
    assert seen["kwargs"]["stdin"] == subprocess.DEVNULL
    # The script draws the photo upright and bounded, and writes a JPEG.
    assert "getProperty('Orientation')" in seen["script"]
    assert f"MAX_EDGE = {heif_images.MAX_EDGE_PX}" in seen["script"]
    assert "'public.jpeg'" in seen["script"]
    assert f"> {heif_images.MAX_SOURCE_PIXELS})" in seen["script"]
    # Then the JPEG is matched to sRGB, again from an argument list.
    [srgb] = srgb_calls
    assert srgb[:3] == [str(tmp_path / "sips"), "-m", str(heif_images.SRGB_PROFILE)]
    assert srgb[3] == str(Path(argv[4]) / "converted.jpg") and srgb[4] == "--out"
    assert target.read_bytes() == JPEG
    # The scratch directory holding the script and output is gone.
    assert not Path(argv[2]).exists()


def test_conversion_refuses_a_relative_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(heif_images.sys, "platform", "darwin")
    monkeypatch.setattr(heif_images, "SIPS", tmp_path / "sips")
    (tmp_path / "sips").write_text("")
    monkeypatch.setattr(heif_images.subprocess, "run", pytest.fail)
    with pytest.raises(HeifConversionError):
        convert_heif_to_jpeg(Path("-o.heic"), tmp_path / "out.jpg")


def test_conversion_without_sips_fails_cleanly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(heif_images.sys, "platform", "linux")
    monkeypatch.setattr(heif_images.subprocess, "run", pytest.fail)
    with pytest.raises(HeifConversionError, match="no HEIC converter"):
        convert_heif_to_jpeg(tmp_path / "a.heic", tmp_path / "a.jpg")


@pytest.mark.parametrize(
    ("outcome", "message"),
    [
        ("timeout", "longer than"),
        ("exit", "could not read"),
        ("too-many-pixels", "million pixels"),
        ("srgb-fails", "sRGB"),
        ("not-jpeg", "did not produce a JPEG"),
    ],
)
def test_conversion_failures_leave_no_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str, message: str
) -> None:
    def fake_run(argv, **kwargs):  # type: ignore[no-untyped-def]
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        if argv[1] == "-m":
            if outcome == "srgb-fails":
                return subprocess.CompletedProcess(argv, 1, b"", b"Error")
            Path(argv[-1]).write_bytes(Path(argv[3]).read_bytes())
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        if outcome == "too-many-pixels":
            # The script prints its marker and throws: sips still exits 0.
            return subprocess.CompletedProcess(argv, 0, b"ciao:too-many-pixels\n", b"JS Error")
        if outcome in {"not-jpeg", "srgb-fails"}:
            body = b"\x89PNG" if outcome == "not-jpeg" else JPEG
            (Path(argv[4]) / "converted.jpg").write_bytes(body)
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        return subprocess.CompletedProcess(argv, 13, b"", b"Error")

    monkeypatch.setattr(heif_images.sys, "platform", "darwin")
    monkeypatch.setattr(heif_images, "SIPS", tmp_path / "sips")
    (tmp_path / "sips").write_text("")
    monkeypatch.setattr(heif_images.subprocess, "run", fake_run)
    target = tmp_path / "a.jpg"
    with pytest.raises(HeifConversionError, match=message):
        convert_heif_to_jpeg(tmp_path / "a.heic", target)
    assert not target.exists()


def test_at_most_two_conversions_run_at_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    lock = threading.Lock()
    running = 0
    peak = 0
    release = threading.Event()

    def fake_run(argv, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal running, peak
        if argv[1] == "-m":
            Path(argv[-1]).write_bytes(Path(argv[3]).read_bytes())
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        with lock:
            running += 1
            peak = max(peak, running)
        release.wait(5)
        (Path(argv[4]) / "converted.jpg").write_bytes(JPEG)
        with lock:
            running -= 1
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(heif_images.sys, "platform", "darwin")
    monkeypatch.setattr(heif_images, "SIPS", tmp_path / "sips")
    (tmp_path / "sips").write_text("")
    monkeypatch.setattr(heif_images.subprocess, "run", fake_run)
    threads = [
        threading.Thread(target=convert_heif_to_jpeg, args=(tmp_path / f"{i}.heic", tmp_path / f"{i}.jpg"))
        for i in range(4)
    ]
    for thread in threads:
        thread.start()
    time.sleep(0.3)
    with lock:
        assert running == 2
    release.set()
    for thread in threads:
        thread.join(5)
    assert peak == 2
    assert all((tmp_path / f"{i}.jpg").exists() for i in range(4))


@pytest.mark.skipif(sys.platform != "darwin" or not Path("/usr/bin/sips").is_file(), reason="needs macOS sips")
def test_real_sips_turns_a_rotated_heic_into_an_upright_jpeg(tmp_path: Path) -> None:
    # The fixture is 400x200 pixels tagged EXIF orientation 6 (shown 200x400).
    source = tmp_path / "photo.heic"
    source.write_bytes(FIXTURE.read_bytes())
    target = tmp_path / "photo.jpg"

    convert_heif_to_jpeg(source, target)

    assert target.read_bytes()[:3] == b"\xff\xd8\xff"
    dims = subprocess.run(
        ["/usr/bin/sips", "-g", "pixelWidth", "-g", "pixelHeight", str(target)],
        capture_output=True, text=True, timeout=30, check=True,
    ).stdout
    assert "pixelWidth: 200" in dims and "pixelHeight: 400" in dims
    # No camera metadata survives: in particular no orientation tag to apply twice.
    assert b"GPS" not in target.read_bytes()
    profile = subprocess.run(
        ["/usr/bin/sips", "-g", "profile", str(target)],
        capture_output=True, text=True, timeout=30, check=True,
    ).stdout
    assert "sRGB" in profile


# ── what the provider receives ─────────────────────────────────────────────


def test_a_heic_upload_reaches_the_agent_as_a_jpeg_with_both_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[Path, Path]] = []
    monkeypatch.setattr("ciao.web.project_chats.convert_heif_to_jpeg", _fake_converter(calls))
    pcm = _make_manager(tmp_path)

    saved = pcm.save_image_upload(HEIC_HEAD, "IMG_0001.HEIC")

    media = pcm._config.media_root.resolve()
    assert saved.path.parent == media and saved.path.suffix == ".jpg"
    assert saved.original_path == saved.path.with_suffix(".heic")
    assert saved.original_path.read_bytes() == HEIC_HEAD
    assert calls == [(saved.original_path, saved.path)]

    # A send resolves the ref again: the original is found beside it.
    attachment = pcm.resolve_image_ref(saved.path.name)
    assert attachment is not None
    assert attachment.mime_type == "image/jpeg"
    assert attachment.original_path == saved.original_path

    request = AgentRequest(prompt="What is this?", model="m", mode="auto", provider="claude", images=[attachment])
    prompt = build_prompt(request)
    assert f"file: {saved.path}; converted to JPEG, original: {saved.original_path})" in prompt
    caps = {"promptCapabilities": {"image": True}}
    blocks = ClaudeAcpProvider(tmp_path)._blocks(SimpleNamespace(agent_capabilities=caps), request)
    images = [block for block in blocks if block["type"] == "image"]
    assert images == [{"type": "image", "mimeType": "image/jpeg", "data": base64.b64encode(JPEG).decode("ascii")}]


def test_a_mislabelled_heic_is_converted_and_kept_as_heic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ciao.web.project_chats.convert_heif_to_jpeg", _fake_converter([]))
    pcm = _make_manager(tmp_path)

    saved = pcm.save_image_upload(HEIC_HEAD, "image.jpg")

    assert saved.mime_type == "image/jpeg"
    assert saved.original_path is not None and saved.original_path.suffix == ".heic"


def test_a_plain_image_has_no_original(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    saved = pcm.save_image_upload(b"\x89PNG\r\n\x1a\n", "shot.png")
    attachment = pcm.resolve_image_ref(saved.path.name)
    assert attachment is not None and attachment.original_path is None
    assert "converted to JPEG" not in build_prompt(
        AgentRequest(prompt="x", model="m", mode="auto", images=[attachment])
    )


def test_deleting_the_chat_removes_the_original_too(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ciao.web.project_chats.convert_heif_to_jpeg", _fake_converter([]))
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("Photos", workspace="work")
    chat = pcm.create_chat(project.project_id, title="Photo")
    saved = pcm.save_image_upload(HEIC_HEAD, "IMG_0001.HEIC")
    chat.user_turn_count = 1
    chat.user_turn_images = {"0": [saved.path.name]}

    assert pcm.delete_chat(chat.chat_id) is True

    assert not saved.path.exists()
    assert saved.original_path is not None and not saved.original_path.exists()


# ── the upload routes ──────────────────────────────────────────────────────


def test_images_route_converts_several_photos(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Path, Path]] = []
    monkeypatch.setattr("ciao.web.project_chats.convert_heif_to_jpeg", _fake_converter(calls))
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("Photos", workspace="work")
    chat = pcm.create_chat(project.project_id, title="Photo")

    response = _client(pcm).post(
        f"/api/chats/{chat.chat_id}/images",
        files=[
            ("a", ("IMG_1.HEIC", HEIC_HEAD, "image/heic")),
            ("b", ("IMG_2.heif", HEIC_HEAD, "")),
            ("c", ("shot.png", b"\x89PNG\r\n\x1a\n", "image/png")),
        ],
    )

    assert response.status_code == 200
    body = response.json()
    assert [entry["mime_type"] for entry in body] == ["image/jpeg", "image/jpeg", "image/png"]
    assert [entry["filename"] for entry in body] == ["IMG_1.HEIC", "IMG_2.heif", "shot.png"]
    assert all(entry["ref"].startswith("web_") for entry in body)
    assert len(calls) == 2


def test_images_route_attaches_an_unconvertible_photo_as_a_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ciao.web.project_chats.convert_heif_to_jpeg", _failing_converter)
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("Photos", workspace="work")
    chat = pcm.create_chat(project.project_id, title="Photo")

    response = _client(pcm).post(
        f"/api/chats/{chat.chat_id}/images",
        files=[("a", ("IMG_1.HEIC", HEIC_HEAD, "image/heic"))],
    )

    assert response.status_code == 200
    [entry] = response.json()
    assert "ref" not in entry and "error" not in entry
    assert entry["filename"] == "IMG_1.HEIC"
    assert entry["file_ref"]["name"] == "IMG_1.HEIC"
    assert entry["notice"] == (
        "The agent can read it by path but cannot see it as an image: "
        "this computer has no HEIC converter (only macOS has one built in)."
    )
    assert str(tmp_path) not in response.text
    # The opaque ref expands, for the agent only, to the saved original.
    token = f"ciao-drop:{entry['file_ref']['ref']}"
    expanded = Path(pcm.expand_file_refs(token, chat.chat_id))
    assert expanded.parent == (pcm._config.media_root / "unconverted" / chat.chat_id).resolve()
    assert expanded.read_bytes() == HEIC_HEAD
    assert not any(pcm._config.media_root.glob("*.heic"))
    # It is not an image ref, but it goes with the chat all the same.
    assert pcm.delete_chat(chat.chat_id) is True
    assert not expanded.exists()


def test_images_route_attaches_a_heic_over_the_image_limit_as_a_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Before HEIC uploaded as an image it went up as a plain file (up to the
    # 50 MB attachment limit); one over the 10 MB image limit still does.
    calls: list[tuple[Path, Path]] = []
    monkeypatch.setattr("ciao.web.project_chats.convert_heif_to_jpeg", _fake_converter(calls))
    monkeypatch.setattr("ciao.web.routes_api.MAX_IMAGE_SIZE_BYTES", 64)
    monkeypatch.setattr("ciao.web.project_chats.MAX_IMAGE_SIZE_BYTES", 64)
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("Photos", workspace="work")
    chat = pcm.create_chat(project.project_id, title="Photo")
    big = HEIC_HEAD + b"\x00" * 128

    response = _client(pcm).post(
        f"/api/chats/{chat.chat_id}/images",
        files=[
            ("a", ("IMG_1.HEIC", big, "image/heic")),
            ("b", ("shot.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 128, "image/png")),
        ],
    )

    heic, png = response.json()
    assert calls == []
    assert heic["notice"].endswith(f"{heif_images.HEIF_TOO_LARGE}.")
    expanded = Path(pcm.expand_file_refs(f"ciao-drop:{heic['file_ref']['ref']}", chat.chat_id))
    assert expanded.read_bytes() == big
    # Other images keep the image limit.
    assert "error" in png and "ref" not in png


def test_images_route_bounds_a_heic_by_the_attachment_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ciao.web.project_chats.convert_heif_to_jpeg", _fake_converter([]))
    monkeypatch.setattr("ciao.web.chat_service._PROJECT_UPLOAD_MAX_BYTES", 64)
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("Photos", workspace="work")
    chat = pcm.create_chat(project.project_id, title="Photo")

    response = _client(pcm).post(
        f"/api/chats/{chat.chat_id}/images",
        files=[("a", ("IMG_1.HEIC", HEIC_HEAD + b"\x00" * 128, "image/heic"))],
    )

    [entry] = response.json()
    assert "error" in entry and "ref" not in entry and "file_ref" not in entry


def test_fork_copies_a_converted_photo_with_its_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ciao.web.project_chats.convert_heif_to_jpeg", _fake_converter([]))
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("Photos", workspace="work")
    source = pcm.create_chat(project.project_id, title="Photo")
    saved = pcm.save_image_upload(HEIC_HEAD, "IMG_0001.HEIC")
    source.user_turn_count = 1
    source.user_turn_images = {"0": [saved.path.name]}

    fork = pcm.fork_chat(
        source.chat_id,
        messages=[
            {"role": "user", "content": "What is this?", "turn_index": 0, "images": [saved.path.name]},
            {"role": "assistant", "content": "A photo."},
        ],
        turn_index=0,
    )

    copied = pcm.resolve_image_ref(fork.user_turn_images["0"][0])
    assert copied is not None and copied.path != saved.path
    assert copied.original_path is not None and copied.original_path.read_bytes() == HEIC_HEAD
    assert copied.original_path != saved.original_path


def _grant(pcm: ProjectChatManager, paths: list[Path]) -> str:
    grant_id = str(uuid.uuid4())
    grant_dir = pcm._config.state_path.parent / "desktop-drop-grants"
    grant_dir.mkdir(parents=True, exist_ok=True)
    (grant_dir / f"{grant_id}.json").write_text(
        json.dumps({"created_at": time.time(), "paths": [str(path) for path in paths]}),
        encoding="utf-8",
    )
    return grant_id


def test_desktop_drop_converts_a_heic_to_an_image_ref(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ciao.web.project_chats.convert_heif_to_jpeg", _fake_converter([]))
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("Photos", workspace="work")
    chat = pcm.create_chat(project.project_id, title="Photo")
    dropped = tmp_path / "Desktop" / "IMG_1.HEIC"
    dropped.parent.mkdir()
    dropped.write_bytes(HEIC_HEAD)

    response = _client(pcm).post("/api/desktop-drop", json={
        "grant_id": _grant(pcm, [dropped]), "project_id": project.project_id, "chat_id": chat.chat_id,
    })

    body = response.json()
    assert response.status_code == 200
    assert len(body["image_refs"]) == 1 and body["image_refs"][0].endswith(".jpg")
    assert body["file_refs"] == [] and body["errors"] == [] and body["notices"] == []


def test_desktop_drop_hands_over_the_dropped_heic_when_it_cannot_convert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ciao.web.project_chats.convert_heif_to_jpeg", _failing_converter)
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("Photos", workspace="work")
    chat = pcm.create_chat(project.project_id, title="Photo")
    dropped = tmp_path / "Desktop" / "IMG_1.HEIC"
    dropped.parent.mkdir()
    dropped.write_bytes(HEIC_HEAD)

    response = _client(pcm).post("/api/desktop-drop", json={
        "grant_id": _grant(pcm, [dropped]), "project_id": project.project_id, "chat_id": chat.chat_id,
    })

    body = response.json()
    assert body["image_refs"] == [] and body["errors"] == []
    assert [ref["name"] for ref in body["file_refs"]] == ["IMG_1.HEIC"]
    assert [notice["filename"] for notice in body["notices"]] == ["IMG_1.HEIC"]
    expanded = pcm.expand_file_refs(f"ciao-drop:{body['file_refs'][0]['ref']}", chat.chat_id)
    assert expanded == str(dropped.resolve())
    # The media copy made for the conversion attempt is not left behind.
    media = pcm._config.media_root
    assert not media.exists() or not any(media.glob("*.heic"))


def test_desktop_drop_hands_over_a_heic_over_the_image_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[Path, Path]] = []
    monkeypatch.setattr("ciao.web.project_chats.convert_heif_to_jpeg", _fake_converter(calls))
    monkeypatch.setattr("ciao.web.routes_api.MAX_IMAGE_SIZE_BYTES", 64)
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("Photos", workspace="work")
    chat = pcm.create_chat(project.project_id, title="Photo")
    dropped = tmp_path / "Desktop" / "IMG_big.HEIC"
    dropped.parent.mkdir()
    dropped.write_bytes(HEIC_HEAD + b"\x00" * 128)

    response = _client(pcm).post("/api/desktop-drop", json={
        "grant_id": _grant(pcm, [dropped]), "project_id": project.project_id, "chat_id": chat.chat_id,
    })

    body = response.json()
    assert calls == [] and body["errors"] == [] and body["image_refs"] == []
    assert body["notices"][0]["message"].endswith(f"{heif_images.HEIF_TOO_LARGE}.")
    expanded = pcm.expand_file_refs(f"ciao-drop:{body['file_refs'][0]['ref']}", chat.chat_id)
    assert expanded == str(dropped.resolve())


def test_heif_not_converted_carries_the_original() -> None:
    exc = HeifNotConverted(Path("/x/web_1.heic"), "reason")
    assert isinstance(exc, ValueError) and exc.original == Path("/x/web_1.heic") and exc.reason == "reason"
