import copy
import hashlib
import importlib.util
import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import packaging
import packaging.markers
import packaging.tags
import packaging.utils
import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="macOS/Linux build helper")

REPO = Path(__file__).parents[1]
MODULE_PATH = REPO / "mac/scripts/engine_dependencies.py"
SPEC = importlib.util.spec_from_file_location("engine_dependencies", MODULE_PATH)
assert SPEC and SPEC.loader
ed = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ed)

REAL_PROJECT = REPO / "pyproject.toml"
REAL_LOCK = REPO / "uv.lock"
SDIST_SHA = "c" * 64
WHEEL_SHA = "d" * 64
PACKAGING = packaging.__version__


# ------------------------------------------------------------- fixtures


def _toml(value):
    return json.dumps(value)


def _inline(entry):
    return "{ " + ", ".join(f"{k} = {_toml(v)}" for k, v in entry.items()) + " }"


def _wheel_url(filename):
    return f"https://files.pythonhosted.org/packages/aa/bb/{filename}"


def pkg(name, version="1.0", deps=(), optional=None, wheels=("py3-none-any",), sdist=True, source=None):
    return {"name": name, "version": version, "deps": list(deps), "optional": optional or {},
            "wheels": list(wheels), "sdist": sdist, "source": source}


def req(name, extras=(), specifier="", marker=None):
    return {"name": name, "extras": list(extras), "specifier": specifier, "marker": marker}


def _wheel_filename(name, version, tag):
    return f"{name.replace('-', '_')}-{version}-{tag}.whl"


def render(packages, reqs, *, revision=5, root_version="1.0.0", packaging_version=None,
           test_pin=None, resolution_markers=None, root_source='{ editable = "." }'):
    packaging_version = packaging_version or PACKAGING
    test_pin = test_pin or PACKAGING
    lines = [
        "dependencies = [",
        *[f'  "{r["name"]}{"[" + ",".join(r["extras"]) + "]" if r["extras"] else ""}{r["specifier"]}'
          + (f'; {r["marker"]}' if r["marker"] else "") + '",' for r in reqs],
        "]",
    ]
    project = (
        '[project]\nname = "ciaobot"\n'
        f'version = "{root_version}"\n' + "\n".join(lines)
        + f'\n\n[project.optional-dependencies]\ntest = ["packaging=={test_pin}"]\n'
    )
    out = [
        "version = 1", f"revision = {revision}", 'requires-python = ">=3.12"',
        "resolution-markers = " + _toml(resolution_markers if resolution_markers is not None else [
            "python_full_version < '3.14' and sys_platform != 'win32'",
            "python_full_version < '3.14' and sys_platform == 'win32'",
        ]),
        "",
        "[[package]]", 'name = "ciaobot"', f'version = "{root_version}"', f"source = {root_source}",
        "dependencies = [",
        *["    " + _inline({"name": r["name"], **({"extra": r["extras"]} if r["extras"] else {})}) + ","
          for r in reqs],
        "]", "",
        "[package.metadata]", "requires-dist = [",
        *["    " + _inline({
            "name": r["name"],
            **({"extras": r["extras"]} if r["extras"] else {}),
            **({"marker": r["marker"]} if r["marker"] else {}),
            **({"specifier": r["specifier"]} if r["specifier"] else {}),
        }) + "," for r in reqs],
        "    " + _inline({"name": "packaging", "marker": "extra == 'test'", "specifier": f"=={test_pin}"}) + ",",
        "]", "",
    ]
    everything = [*packages, pkg("packaging", packaging_version)]
    for p in everything:
        out += ["[[package]]", f"name = {_toml(p['name'])}", f"version = {_toml(p['version'])}"]
        out.append("source = " + (p["source"] or '{ registry = "https://pypi.org/simple" }'))
        if p["deps"]:
            out += ["dependencies = ["] + ["    " + _inline(d) + "," for d in p["deps"]] + ["]"]
        if p["sdist"]:
            out.append("sdist = " + _inline({
                "url": _wheel_url(f"{p['name']}-{p['version']}.tar.gz"),
                "hash": f"sha256:{SDIST_SHA}", "size": 100}))
        if p["wheels"]:
            out.append("wheels = [")
            for tag in p["wheels"]:
                filename = _wheel_filename(p["name"], p["version"], tag)
                out.append("    " + _inline({
                    "url": _wheel_url(filename), "hash": f"sha256:{WHEEL_SHA}", "size": 200}) + ",")
            out.append("]")
        if p["optional"]:
            out.append("[package.optional-dependencies]")
            for ext, deps in p["optional"].items():
                out += [f"{ext} = ["] + ["    " + _inline(d) + "," for d in deps] + ["]"]
        out.append("")
    return project, "\n".join(out)


def write(tmp_path, project, lock):
    (tmp_path / "pyproject.toml").write_text(project)
    (tmp_path / "uv.lock").write_text(lock)
    return tmp_path / "pyproject.toml", tmp_path / "uv.lock"


def no_derived(monkeypatch, entries=()):
    inputs = SimpleNamespace(data={"derived_wheels": list(entries)}, sha256="e" * 64)
    monkeypatch.setattr(ed.engine_derived_wheel, "load_build_inputs", lambda: inputs)
    return inputs


def run_fixture(tmp_path, monkeypatch, packages, reqs, derived=(), **kwargs):
    no_derived(monkeypatch, derived)
    return ed.audit(*write(tmp_path, *render(packages, reqs, **kwargs)))


def derived_entry(name="b", version="1.0", tag="py2.py3-none-any", **overrides):
    entry = {
        "name": name, "version": version,
        "sdist": {"url": _wheel_url(f"{name}-{version}.tar.gz"), "sha256": SDIST_SHA, "size": 100},
        "wheel": {"filename": _wheel_filename(name, version, tag), "size": 50, "sha256": "f" * 64},
    }
    entry.update(overrides)
    return entry


def code_of(func, *args, **kwargs):
    with pytest.raises(ed.AuditError) as caught:
        func(*args, **kwargs)
    return caught.value.code


# ------------------------------------------------------------ real lock


def run_cli(*args):
    return subprocess.run(
        [sys.executable, "-I", str(MODULE_PATH), *args],
        capture_output=True, text=True, cwd=REPO, check=False)


def test_real_lock_has_no_blockers_and_keeps_web_push() -> None:
    first = run_cli("--project", str(REAL_PROJECT), "--lock", str(REAL_LOCK))
    assert first.returncode == 0, first.stderr
    report = json.loads(first.stdout)
    assert report["schema"] == 1 and report["kind"] == "lock-wheel-audit"
    assert report["installable"] is False and report["blockers"] == []
    rows = {row["name"]: row for row in report["selected"]}
    assert [row["name"] for row in report["selected"]] == sorted(rows)
    assert rows["pywebpush"]["origin"] == "lock"
    derived = rows["http-ece"]
    assert derived["origin"] == "derived" and derived["url"] is None
    assert derived["sha256"] == "696199016b82de2b91f936d01ddf636769174fcc2766f9b1a741c12676beea80"
    assert "ciaobot" not in rows and "packaging" not in rows
    target = set(map(str, ed.Target(ed.engine_runtime.load_runtime_pin()).tag_priority()))
    for row in rows.values():
        assert row["tags"] and set(row["tags"]) <= target
        assert row["size"] > 0 and len(row["sha256"]) == 64
    inputs = report["inputs"]
    assert inputs["lock_sha256"] == hashlib.sha256(REAL_LOCK.read_bytes()).hexdigest()
    assert inputs["pyproject_sha256"] == hashlib.sha256(REAL_PROJECT.read_bytes()).hexdigest()
    assert inputs["build_inputs_sha256"] == ed.engine_derived_wheel.load_build_inputs().sha256
    assert inputs["runtime_pin_sha256"] == hashlib.sha256(
        (REPO / "mac/engine-runtime.json").read_bytes()).hexdigest()
    second = run_cli("--project", str(REAL_PROJECT), "--lock", str(REAL_LOCK))
    assert second.stdout == first.stdout


def test_real_lock_without_derived_entry_blocks_on_http_ece(monkeypatch, capsys) -> None:
    no_derived(monkeypatch, [])
    assert ed.main(["--project", str(REAL_PROJECT), "--lock", str(REAL_LOCK)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["blockers"] == [{
        "code": "missing-compatible-wheel",
        "chain": "ciaobot → pywebpush==2.5.0 → http-ece==1.2.1",
    }]
    assert "http-ece" not in {row["name"] for row in report["selected"]}
    assert report["installable"] is False


# --------------------------------------------------------- selection


def test_selection_matches_independent_oracle(tmp_path, monkeypatch) -> None:
    packages = [
        pkg("a", deps=[{"name": "b"}, {"name": "c"}]),
        pkg("b", wheels=["py3-none-any"]),
        pkg("c", wheels=["cp312-cp312-macosx_13_0_arm64", "py3-none-any"]),
        pkg("unreachable"),
    ]
    report = run_fixture(tmp_path, monkeypatch, packages, [req("a", specifier="==1.0")])
    assert [row["name"] for row in report["selected"]] == ["a", "b", "c"]
    assert report["blockers"] == []
    by_name = {row["name"]: row for row in report["selected"]}
    assert by_name["c"]["filename"].endswith("cp312-cp312-macosx_13_0_arm64.whl")
    assert by_name["a"]["origin"] == "lock"
    assert by_name["a"]["url"] == _wheel_url("a-1.0-py3-none-any.whl")
    assert by_name["a"]["sha256"] == WHEEL_SHA and by_name["a"]["size"] == 200


@pytest.mark.parametrize("tag", [
    "cp312-cp312-macosx_13_0_arm64", "cp311-abi3-macosx_11_0_arm64", "cp32-abi3-macosx_10_9_universal2",
    "cp312-cp312-macosx_12_0_universal2", "py3-none-any", "py3-none-macosx_11_0_arm64",
    "py2.py3-none-any",
])
def test_compatible_tags_are_selected(tmp_path, monkeypatch, tag) -> None:
    report = run_fixture(tmp_path, monkeypatch, [pkg("a", wheels=[tag])], [req("a")])
    assert report["blockers"] == [] and report["selected"][0]["name"] == "a"


@pytest.mark.parametrize("tag", [
    "cp315-abi3-macosx_11_0_arm64", "cp312-cp312t-macosx_13_0_arm64", "cp312-cp312-macosx_13_0_x86_64",
    "cp312-cp312-manylinux_2_28_aarch64", "cp312-cp312-win_arm64", "cp312-cp312-macosx_14_0_arm64",
    "cp313-cp313-macosx_13_0_arm64", "cp312-cp312-macosx_13_0_x86_64.macosx_13_0_intel",
])
def test_incompatible_tags_are_blockers_not_sdist_fallbacks(tmp_path, monkeypatch, tag) -> None:
    report = run_fixture(tmp_path, monkeypatch, [pkg("a", wheels=[tag], sdist=True)], [req("a")])
    assert report["selected"] == []
    assert report["blockers"] == [{"code": "missing-compatible-wheel", "chain": "ciaobot → a==1.0"}]


def test_tag_priority_prefers_cpython_then_generic_and_breaks_ties_by_filename(tmp_path, monkeypatch) -> None:
    report = run_fixture(tmp_path, monkeypatch, [pkg(
        "a", wheels=["py3-none-any", "cp311-abi3-macosx_11_0_arm64", "cp312-cp312-macosx_13_0_universal2"])],
        [req("a")])
    assert report["selected"][0]["filename"].endswith("cp312-cp312-macosx_13_0_universal2.whl")
    report = run_fixture(tmp_path, monkeypatch, [pkg(
        "a", wheels=["py3-none-any", "cp311-abi3-macosx_11_0_arm64"])], [req("a")])
    assert report["selected"][0]["filename"].endswith("cp311-abi3-macosx_11_0_arm64.whl")


def test_tag_generation_receives_explicit_target_and_no_host_defaults(monkeypatch) -> None:
    seen = {}
    real_cpython, real_compat = packaging.tags.cpython_tags, packaging.tags.compatible_tags
    real_mac = packaging.tags.mac_platforms

    def spy(name, real):
        def inner(*args, **kwargs):
            seen[name] = (args, kwargs)
            return real(*args, **kwargs)
        return inner

    monkeypatch.setattr(packaging.tags, "cpython_tags", spy("cpython", real_cpython))
    monkeypatch.setattr(packaging.tags, "compatible_tags", spy("compat", real_compat))
    monkeypatch.setattr(packaging.tags, "mac_platforms", spy("mac", real_mac))
    monkeypatch.setattr(packaging.tags, "sys_tags", lambda *a, **k: pytest.fail("host tags"))
    ed.Target(ed.engine_runtime.load_runtime_pin()).tag_priority()
    assert seen["mac"][1] == {"version": (13, 0), "arch": "arm64"}
    assert seen["cpython"][1]["python_version"] == (3, 12) and seen["cpython"][1]["abis"] == ["cp312"]
    assert seen["compat"][1]["python_version"] == (3, 12) and seen["compat"][1]["interpreter"] == "cp312"
    assert seen["cpython"][1]["platforms"] == seen["compat"][1]["platforms"]


def test_report_ignores_host_defaults(tmp_path, monkeypatch) -> None:
    packages = [
        pkg("a", deps=[{"name": "mac-only", "marker": "sys_platform == 'darwin' and platform_machine == 'arm64'"},
                       {"name": "win-only", "marker": "sys_platform == 'win32'"}]),
        pkg("mac-only"), pkg("win-only", wheels=[]),
    ]
    baseline = run_fixture(tmp_path, monkeypatch, packages, [req("a")])
    assert [r["name"] for r in baseline["selected"]] == ["a", "mac-only"]
    linux = {"sys_platform": "linux", "platform_system": "Linux", "platform_machine": "x86_64",
             "os_name": "posix", "python_version": "3.9", "python_full_version": "3.9.1",
             "implementation_name": "cpython", "platform_python_implementation": "CPython",
             "implementation_version": "3.9.1", "platform_release": "6.1", "platform_version": "#1"}
    monkeypatch.setattr(packaging.markers, "default_environment", lambda: dict(linux))
    monkeypatch.setattr(packaging.tags, "sys_tags", lambda *a, **k: pytest.fail("host tags"))
    assert run_fixture(tmp_path, monkeypatch, packages, [req("a")]) == baseline


def test_marker_evaluation_receives_full_fixed_environment(tmp_path, monkeypatch) -> None:
    seen = []
    real = packaging.markers.Marker.evaluate

    def spy(self, environment=None, context="metadata"):
        seen.append(dict(environment))
        return real(self, environment, context)

    monkeypatch.setattr(packaging.markers.Marker, "evaluate", spy)
    run_fixture(tmp_path, monkeypatch, [pkg("a", deps=[{"name": "b", "marker": "os_name == 'posix'"}]), pkg("b")],
                [req("a")])
    assert seen and seen[0]["python_full_version"] == "3.12.16" and seen[0]["platform_machine"] == "arm64"
    assert seen[0]["sys_platform"] == "darwin" and "platform_release" not in seen[0]


# ------------------------------------------------------------- graph


def test_extras_are_followed_and_test_extra_and_windows_edges_are_excluded(tmp_path, monkeypatch) -> None:
    packages = [
        pkg("sdk", deps=[{"name": "core"}], optional={"otel": [{"name": "otel-api"}], "never": [{"name": "nope"}]}),
        pkg("core"), pkg("otel-api"), pkg("nope"),
        pkg("server", deps=[{"name": "h11"}, {"name": "winlib", "marker": "sys_platform == 'win32'"}],
            optional={"standard": [{"name": "uv", "marker": "sys_platform != 'win32'"},
                                   {"name": "winext", "marker": "sys_platform == 'win32'"}]}),
        pkg("h11"), pkg("uv"), pkg("winlib", wheels=[]), pkg("winext", wheels=[]),
    ]
    report = run_fixture(tmp_path, monkeypatch, packages, [req("sdk", ["otel"]), req("server", ["standard"])])
    assert [r["name"] for r in report["selected"]] == ["core", "h11", "otel-api", "sdk", "server", "uv"]
    assert report["blockers"] == []


def test_cycles_and_repeated_extras_terminate(tmp_path, monkeypatch) -> None:
    packages = [
        pkg("a", deps=[{"name": "b", "extra": ["x"]}, {"name": "b"}],
            optional={"x": [{"name": "b", "extra": ["x"]}]}),
        pkg("b", deps=[{"name": "a", "extra": ["x"]}], optional={"x": [{"name": "a"}, {"name": "b"}]}),
    ]
    report = run_fixture(tmp_path, monkeypatch, packages, [req("a", ["x"])])
    assert [r["name"] for r in report["selected"]] == ["a", "b"]


def test_blocker_chain_is_shortest_stable_and_independent_of_input_order(tmp_path, monkeypatch) -> None:
    packages = [
        pkg("a", deps=[{"name": "x"}, {"name": "m1"}]),
        pkg("m1", deps=[{"name": "m2"}]), pkg("m2", deps=[{"name": "bad"}]),
        pkg("x", deps=[{"name": "bad"}]),
        pkg("bad", wheels=["cp312-cp312-win_amd64"]),
    ]
    first = run_fixture(tmp_path, monkeypatch, packages, [req("a")])
    assert first["blockers"] == [{"code": "missing-compatible-wheel",
                                  "chain": "ciaobot → a==1.0 → x==1.0 → bad==1.0"}]
    reordered = run_fixture(tmp_path, monkeypatch, list(reversed(copy.deepcopy(packages))), [req("a")])
    assert {k: v for k, v in reordered.items() if k != "inputs"} == {k: v for k, v in first.items() if k != "inputs"}


def test_blocker_chain_shows_enabling_extra_and_equal_length_ties_are_lexicographic(tmp_path, monkeypatch) -> None:
    packages = [
        pkg("a", deps=[{"name": "z"}, {"name": "b"}]),
        pkg("b", deps=[{"name": "bad"}]), pkg("z", deps=[{"name": "bad"}]),
        pkg("bad", wheels=[]),
        pkg("e", optional={"x": [{"name": "bad2"}]}), pkg("bad2", wheels=[]),
    ]
    report = run_fixture(tmp_path, monkeypatch, packages, [req("a"), req("e", ["x"])])
    assert [b["chain"] for b in report["blockers"]] == [
        "ciaobot → a==1.0 → b==1.0 → bad==1.0",
        "ciaobot → e[x]==1.0 → bad2==1.0",
    ]


# ------------------------------------------------------ derived wheels


def _derived_packages():
    return [pkg("a", deps=[{"name": "b"}]), pkg("b", wheels=[])]


def test_derived_wheel_fills_missing_wheel(tmp_path, monkeypatch) -> None:
    report = run_fixture(tmp_path, monkeypatch, _derived_packages(), [req("a")], derived=[derived_entry()])
    row = {r["name"]: r for r in report["selected"]}["b"]
    assert row["origin"] == "derived" and row["url"] is None and row["sha256"] == "f" * 64
    assert row["size"] == 50 and row["tags"] == ["py3-none-any"]
    assert report["inputs"]["build_inputs_sha256"] == "e" * 64 and report["blockers"] == []


@pytest.mark.parametrize("mutate", [
    lambda e: e["sdist"].update(sha256="9" * 64),
    lambda e: e["sdist"].update(size=101),
    lambda e: e["sdist"].update(url=_wheel_url("other-1.0.tar.gz")),
    lambda e: e.update(version="2.0"),
])
def test_derived_wheel_requires_matching_lock_sdist(tmp_path, monkeypatch, mutate) -> None:
    entry = derived_entry()
    mutate(entry)
    no_derived(monkeypatch, [entry])
    inputs = write(tmp_path, *render(_derived_packages(), [req("a")]))
    assert code_of(ed.audit, *inputs) == "derived-wheel-mismatch"


def test_derived_entry_for_unreachable_or_incompatible_wheel_is_mismatch(tmp_path, monkeypatch) -> None:
    packages = _derived_packages() + [pkg("ghost", wheels=[])]
    no_derived(monkeypatch, [derived_entry("ghost")])
    assert code_of(ed.audit, *write(tmp_path, *render(packages, [req("a")]))) == "derived-wheel-mismatch"
    no_derived(monkeypatch, [derived_entry(tag="cp312-cp312-win_amd64")])
    assert code_of(ed.audit, *write(tmp_path, *render(_derived_packages(), [req("a")]))) == "derived-wheel-mismatch"
    no_derived(monkeypatch, [derived_entry("absent")])
    assert code_of(ed.audit, *write(tmp_path, *render(_derived_packages(), [req("a")]))) == "derived-wheel-mismatch"


def test_unneeded_derived_entry_is_an_error(tmp_path, monkeypatch) -> None:
    no_derived(monkeypatch, [derived_entry()])
    packages = [pkg("a", deps=[{"name": "b"}]), pkg("b", wheels=["py3-none-any"])]
    assert code_of(ed.audit, *write(tmp_path, *render(packages, [req("a")]))) == "derived-wheel-unneeded"


# ------------------------------------------------- packaging prerequisite


def test_packaging_version_must_match_lock_and_test_pin(tmp_path, monkeypatch) -> None:
    no_derived(monkeypatch)
    packages = [pkg("a")]
    assert code_of(ed.audit, *write(tmp_path, *render(packages, [req("a")], packaging_version="25.0"))) \
        == "parser-prerequisite"
    assert code_of(ed.audit, *write(tmp_path, *render(packages, [req("a")], test_pin="25.0"))) \
        == "parser-prerequisite"
    monkeypatch.setattr(packaging, "__version__", "25.0")
    assert code_of(ed.audit, *write(tmp_path, *render(packages, [req("a")]))) == "parser-prerequisite"


def test_missing_packaging_is_a_prerequisite_refusal(tmp_path, monkeypatch, capsys) -> None:
    no_derived(monkeypatch)
    monkeypatch.setitem(sys.modules, "packaging", None)
    inputs = write(tmp_path, *render([pkg("a")], [req("a")]))
    assert code_of(ed.audit, *inputs) == "parser-prerequisite"
    assert ed.main(["--project", str(inputs[0]), "--lock", str(inputs[1])]) == 2
    assert capsys.readouterr().err == "lock wheel audit failed: parser-prerequisite\n"


# ----------------------------------------------------------- invalid input


def _audit(tmp_path, monkeypatch, project, lock):
    no_derived(monkeypatch)
    return code_of(ed.audit, *write(tmp_path, project, lock))


def test_tampered_or_inconsistent_inputs_are_refused(tmp_path, monkeypatch) -> None:
    project, lock = render([pkg("a"), pkg("b")], [req("a", specifier="==1.0")])
    assert _audit(tmp_path, monkeypatch, project.replace("==1.0", "==2.0"), lock) == "invalid-input"
    assert _audit(tmp_path, monkeypatch, project.replace('"a==1.0"', '"a==1.0", "b"'), lock) == "invalid-input"
    assert _audit(tmp_path, monkeypatch, project.replace('"ciaobot"', '"other"'), lock) == "invalid-input"
    assert _audit(tmp_path, monkeypatch, project, lock.replace('version = "1.0.0"', 'version = "9.9"', 1)) \
        == "invalid-input"
    bumped = render([pkg("a", "3.0")], [req("a", specifier="==1.0")])
    assert _audit(tmp_path, monkeypatch, *bumped) == "invalid-input"


BASE_WHEEL_URL = "https://files.pythonhosted.org/packages/aa/bb/b-1.0-py3-none-any.whl"


@pytest.mark.parametrize("edit", [
    lambda lock: lock.replace("revision = 5", "revision = 3"),
    lambda lock: lock.replace("version = 1\n", "version = 2\n", 1),
    lambda lock: lock.replace('requires-python = ">=3.12"', 'requires-python = ">=3.13"'),
    lambda lock: lock + "\n[[broken",
    lambda lock: lock.replace('{ name = "b" }', '{ name = "missing" }'),
    lambda lock: lock.replace('{ name = "b" }', '{ name = "b", marker = "platform_release >= \'1\'" }'),
    lambda lock: lock.replace('{ name = "b" }', '{ name = "b", marker = "not a marker ((" }'),
    lambda lock: lock.replace('{ name = "b" }', '{ name = "b", extra = ["nope"] }'),
    lambda lock: lock.replace('{ name = "b" }', '{ name = "b", version = "1.0" }'),
    lambda lock: lock.replace(BASE_WHEEL_URL, BASE_WHEEL_URL.replace("https://", "https://user:pw@")),
    lambda lock: lock.replace(BASE_WHEEL_URL, BASE_WHEEL_URL.replace("https://", "http://")),
    lambda lock: lock.replace(BASE_WHEEL_URL, BASE_WHEEL_URL + "?token=1"),
    lambda lock: lock.replace(BASE_WHEEL_URL, BASE_WHEEL_URL.replace("files.pythonhosted.org", "example.com")),
    lambda lock: lock.replace(f"sha256:{WHEEL_SHA}", "sha256:zz"),
    lambda lock: lock.replace("size = 200", "size = 0"),
    lambda lock: lock.replace(BASE_WHEEL_URL, BASE_WHEEL_URL.replace("b-1.0", "c-1.0")),
    lambda lock: lock.replace(BASE_WHEEL_URL, BASE_WHEEL_URL.replace("-py3-none-any", "-py3-none")),
])
def test_malformed_lock_is_refused(tmp_path, monkeypatch, edit) -> None:
    project, lock = render([pkg("a", deps=[{"name": "b"}]), pkg("b")], [req("a")])
    edited = edit(lock)
    assert edited != lock
    assert _audit(tmp_path, monkeypatch, project, edited) == "invalid-input"


def test_third_party_editable_and_off_registry_sources_are_refused(tmp_path, monkeypatch) -> None:
    for source in ('{ editable = "../x" }', '{ registry = "https://example.com/simple" }',
                   '{ git = "https://example.com/x.git" }', '{ path = "x.whl" }'):
        project, lock = render([pkg("a", source=source)], [req("a")])
        assert _audit(tmp_path, monkeypatch, project, lock) == "invalid-input"


def test_duplicate_or_missing_roots_and_missing_extra_are_refused(tmp_path, monkeypatch) -> None:
    project, lock = render([pkg("a"), pkg("a", "2.0")], [req("a")])
    assert _audit(tmp_path, monkeypatch, project, lock) == "invalid-input"
    project, lock = render([pkg("a")], [req("a", ["absent"])])
    assert _audit(tmp_path, monkeypatch, project, lock) == "invalid-input"
    project, lock = render([pkg("a")], [req("a")], root_source='{ registry = "https://pypi.org/simple" }')
    assert _audit(tmp_path, monkeypatch, project, lock) == "invalid-input"


def test_unsupported_resolution_markers_are_refused(tmp_path, monkeypatch) -> None:
    project, lock = render([pkg("a")], [req("a")], resolution_markers=[
        "python_full_version >= '3.15'", "python_full_version == '3.14.*'"])
    assert _audit(tmp_path, monkeypatch, project, lock) == "invalid-input"


def test_input_files_must_be_bounded_regular_files(tmp_path, monkeypatch) -> None:
    no_derived(monkeypatch)
    project, lock = write(tmp_path, *render([pkg("a")], [req("a")]))
    link = tmp_path / "link.lock"
    link.symlink_to(lock)
    assert code_of(ed.audit, project, link) == "invalid-input"
    fifo = tmp_path / "fifo.lock"
    os.mkfifo(fifo)
    assert code_of(ed.audit, project, fifo) == "invalid-input"
    assert code_of(ed.audit, project, tmp_path / "absent.lock") == "invalid-input"
    assert code_of(ed.audit, project, tmp_path) == "invalid-input"
    monkeypatch.setattr(ed, "MAX_INPUT_BYTES", 100)
    assert code_of(ed.audit, project, lock) == "resource-limit"


def test_runtime_pin_hash_is_the_bytes_that_were_validated(tmp_path, monkeypatch) -> None:
    no_derived(monkeypatch)
    pin_copy = tmp_path / "engine-runtime.json"
    pin_copy.write_bytes((REPO / "mac/engine-runtime.json").read_bytes())
    monkeypatch.setattr(ed.engine_runtime, "MANIFEST", pin_copy)
    inputs = write(tmp_path, *render([pkg("a")], [req("a")]))
    report = ed.audit(*inputs)
    assert report["inputs"]["runtime_pin_sha256"] == hashlib.sha256(pin_copy.read_bytes()).hexdigest()
    pin_copy.write_bytes(b"{")
    assert code_of(ed.audit, *inputs) == "invalid-input"


# --------------------------------------------------------- resource limits


def test_compressed_tag_bombs_are_refused_before_the_parser_expands_them(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(packaging.utils, "parse_wheel_filename",
                        lambda *a, **k: pytest.fail("parser reached"))
    bomb = "a-1.0-" + ".".join(f"cp3{i}" for i in range(65)) + "-none-any.whl"
    assert code_of(ed._preflight_wheel_filename, bomb, [0]) == "resource-limit"
    product = "a-1.0-" + ".".join(f"p{i}" for i in range(20)) + "-" + ".".join(f"a{i}" for i in range(20)) \
        + "-" + ".".join(f"m{i}" for i in range(20)) + ".whl"
    assert code_of(ed._preflight_wheel_filename, product, [0]) == "resource-limit"
    assert code_of(ed._preflight_wheel_filename, "a-1.0-" + "x" * 5000 + "-none-any.whl", [0]) == "resource-limit"
    monkeypatch.setattr(ed, "MAX_TOTAL_TAGS", 3)
    assert code_of(ed._preflight_wheel_filename, "a-1.0-py2.py3-none-any.whl", [2]) == "resource-limit"
    no_derived(monkeypatch)
    project, lock = render([pkg("a")], [req("a")])
    bomb_lock = lock.replace("a-1.0-py3-none-any", bomb[:-4])
    assert code_of(ed.audit, *write(tmp_path, project, bomb_lock)) == "resource-limit"


@pytest.mark.parametrize("limit,value", [
    ("MAX_PACKAGES", 2), ("MAX_EDGES", 2), ("MAX_STATES", 2), ("MAX_OPERATIONS", 2),
    ("MAX_EXTRAS_PER_PACKAGE", 1), ("MAX_TARGET_TAGS", 5),
])
def test_work_limits_refuse_with_no_partial_report(tmp_path, monkeypatch, capsys, limit, value) -> None:
    packages = [
        pkg("a", deps=[{"name": "b", "extra": ["x", "y"]}, {"name": "c"}]),
        pkg("b", optional={"x": [{"name": "c"}], "y": [{"name": "c"}]}), pkg("c"),
    ]
    inputs = write(tmp_path, *render(packages, [req("a")]))
    no_derived(monkeypatch)
    assert ed.audit(*inputs)["blockers"] == []
    monkeypatch.setattr(ed, limit, value)
    assert code_of(ed.audit, *inputs) == "resource-limit"
    assert ed.main(["--project", str(inputs[0]), "--lock", str(inputs[1])]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == "lock wheel audit failed: resource-limit\n"


# ----------------------------------------------------------- isolation


def test_audit_never_touches_the_network_or_subprocesses(tmp_path, monkeypatch) -> None:
    def boom(*_a, **_k):
        raise AssertionError("forbidden call")

    inputs = write(tmp_path, *render([pkg("a")], [req("a")]))
    no_derived(monkeypatch)
    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(os, "system", boom)
    assert ed.audit(*inputs)["selected"][0]["name"] == "a"


def test_audit_does_not_write_anything(tmp_path, monkeypatch) -> None:
    inputs = write(tmp_path, *render([pkg("a")], [req("a")]))
    no_derived(monkeypatch)
    before = sorted(p.name for p in tmp_path.iterdir())
    ed.audit(*inputs)
    assert sorted(p.name for p in tmp_path.iterdir()) == before


# --------------------------------------------------------------------- CLI


def test_cli_usage_errors_use_the_fixed_line() -> None:
    for args in ([], ["--project", "x"], ["--lock", "y", "--project"], ["--proj", "x", "--lock", "y"],
                 ["--project", "x", "--lock", "y", "--extra"]):
        result = run_cli(*args)
        assert result.returncode == 2 and result.stdout == ""
        assert result.stderr == "lock wheel audit failed: usage\n"


def test_cli_invalid_input_exits_two_without_echoing_input(tmp_path) -> None:
    secret = tmp_path / "secret-name-xyz.toml"
    secret.write_text("not toml [[")
    result = run_cli("--project", str(secret), "--lock", str(secret))
    assert result.returncode == 2 and result.stdout == ""
    assert result.stderr == "lock wheel audit failed: invalid-input\n"


def test_cli_exit_one_prints_the_report(tmp_path, monkeypatch, capsys) -> None:
    no_derived(monkeypatch)
    project, lock = write(tmp_path, *render([pkg("a", wheels=[])], [req("a")]))
    assert ed.main(["--project", str(project), "--lock", str(lock)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["installable"] is False and report["blockers"][0]["chain"] == "ciaobot \u2192 a==1.0"
