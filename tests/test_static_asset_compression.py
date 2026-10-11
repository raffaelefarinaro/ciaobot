from __future__ import annotations

import gzip

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Mount
from starlette.testclient import TestClient

from ciao.web.app import CacheControlStaticFiles, HashedAssetCompression
from ciao.web.security import SecurityHeadersMiddleware


def _client(tmp_path):
    asset_dir = tmp_path / "assets"
    asset_dir.mkdir()
    (asset_dir / "index-AbCd1234.js").write_bytes(b"const payload = 'hello';\n" * 80)
    (asset_dir / "small-12345678.css").write_text("a{color:red}", encoding="utf-8")
    (asset_dir / "custom.js").write_text("const unbundled = 'not hashed';\n" * 80, encoding="utf-8")
    (asset_dir / "logo.svg").write_text("<svg></svg>", encoding="utf-8")
    app = Starlette(routes=[Mount(
        "/assets",
        app=HashedAssetCompression(CacheControlStaticFiles(directory=asset_dir)),
    )], middleware=[Middleware(SecurityHeadersMiddleware)])
    return TestClient(app)


def test_gzip_asset_is_decodable_and_keeps_static_headers(tmp_path) -> None:
    client = _client(tmp_path)
    with client.stream(
        "GET", "/assets/index-AbCd1234.js", headers={"Accept-Encoding": "br, GZIP; q=1"},
    ) as response:
        encoded = b"".join(response.iter_raw())

    assert response.status_code == 200
    assert response.headers["content-encoding"] == "gzip"
    assert response.headers["vary"] == "Accept-Encoding"
    assert response.headers["content-type"].startswith("text/javascript")
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert response.headers["etag"].startswith('W/')
    assert response.headers["content-length"] == str(len(encoded))
    assert response.headers["x-content-type-options"] == "nosniff"
    assert gzip.decompress(encoded) == b"const payload = 'hello';\n" * 80


def test_identity_and_small_assets_are_valid_varying_representations(tmp_path) -> None:
    client = _client(tmp_path)
    identity = client.get("/assets/index-AbCd1234.js", headers={"Accept-Encoding": "identity"})
    small = client.get("/assets/small-12345678.css", headers={"Accept-Encoding": "gzip"})

    assert "content-encoding" not in identity.headers
    assert identity.headers["vary"] == "Accept-Encoding"
    assert identity.content == b"const payload = 'hello';\n" * 80
    assert "content-encoding" not in small.headers
    assert small.headers["vary"] == "Accept-Encoding"
    assert small.text == "a{color:red}"


def test_zero_quality_and_non_gzip_tokens_do_not_enable_compression(tmp_path) -> None:
    client = _client(tmp_path)
    for accepted in ("gzip;q=0", "xgzip", "GZIP;q=0"):
        response = client.get("/assets/index-AbCd1234.js", headers={"Accept-Encoding": accepted})
        assert "content-encoding" not in response.headers
        assert response.headers["vary"] == "Accept-Encoding"


def test_conditional_and_range_responses_keep_static_file_semantics(tmp_path) -> None:
    client = _client(tmp_path)
    path = "/assets/index-AbCd1234.js"
    initial = client.get(path)
    etag = initial.headers["etag"]

    conditional = client.get(path, headers={"Accept-Encoding": "gzip", "If-None-Match": etag})
    ranged = client.get(path, headers={"Accept-Encoding": "gzip", "Range": "bytes=0-9"})

    assert conditional.status_code == 304
    assert conditional.headers["etag"].removeprefix('W/') == etag.removeprefix('W/')
    assert "content-encoding" not in conditional.headers
    assert conditional.headers["vary"] == "Accept-Encoding"
    assert ranged.status_code == 206
    assert ranged.content == b"const payl"
    assert "content-encoding" not in ranged.headers
    assert ranged.headers["vary"] == "Accept-Encoding"


def test_only_hashed_js_and_css_paths_are_compressed(tmp_path) -> None:
    client = _client(tmp_path)
    response = client.get("/assets/logo.svg", headers={"Accept-Encoding": "gzip"})
    unbundled = client.get("/assets/custom.js", headers={"Accept-Encoding": "gzip"})

    assert "content-encoding" not in response.headers
    assert "vary" not in response.headers
    assert "content-encoding" not in unbundled.headers
    assert "vary" not in unbundled.headers
