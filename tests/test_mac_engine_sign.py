import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="macOS/Linux build helper")

REPO = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location("engine_sign", REPO / "mac/scripts/engine_sign.py")
assert SPEC and SPEC.loader
es = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(es)

THIN = bytes.fromhex("cffaedfe") + b"\x0c\x00\x00\x01" + b"\0" * 40
# A thin 64-bit Mach-O whose cputype is x86_64, so not an arm64 file.
THIN_X86 = bytes.fromhex("cffaedfe") + (0x01000007).to_bytes(4, "little") + b"\0" * 40
ADHOC = "CodeDirectory v=20400 flags=0x2(adhoc)\nSignature=adhoc\nTeamIdentifier=not set\n"
TEAM = "AXBFS96Y9F"
IDENTITY = f"Developer ID Application: Someone Example ({TEAM})"
DIST = (
    "CodeDirectory v=20500 size=1000 flags=0x10000(runtime) hashes=3+7 location=embedded\n"
    f"Authority={IDENTITY}\nAuthority=Developer ID Certification Authority\nAuthority=Apple Root CA\n"
    f"Timestamp=Nov 18, 2026 at 10:00:00\nTeamIdentifier={TEAM}\n"
)
SIGNED_SET = ["bin/python3.12", "lib/b.dylib", "lib/mislabelled.txt", "lib/x/deep/a.so"]


def fat_arm64() -> bytes:
    head = b"\xca\xfe\xba\xbe" + (1).to_bytes(4, "big")
    entry = (0x0100000C).to_bytes(4, "big") + (0).to_bytes(4, "big") + (64).to_bytes(4, "big") \
        + (64).to_bytes(4, "big") + (3).to_bytes(4, "big")
    return head + entry + b"\0" * 36 + THIN[:4] + b"\0" * 60


class Fake:
    def __init__(self, engine: Path):
        self.engine = engine
        self.calls: list[list[str]] = []
        self.verify_fail: set[str] = set()
        self.display_override: dict[str, str] = {}
        self.default_display = ADHOC
        self.also = None  # callable run while signing

    def __call__(self, argv):
        self.calls.append(list(argv))
        path = argv[-1]
        rel = os.path.relpath(path, self.engine) if path.startswith(str(self.engine)) else path
        if "--force" in argv:
            with open(path, "ab") as handle:
                handle.write(b"\1")
            if self.also:
                self.also()
            return 0, ""
        if "--verify" in argv:
            return (1 if rel in self.verify_fail else 0), ""
        if "-d" in argv:
            return 0, self.display_override.get(rel, self.default_display)
        raise AssertionError(argv)

    def signs(self):
        return [c[-1] for c in self.calls if "--force" in c]


def make_engine(tmp_path: Path) -> Path:
    engine = tmp_path / "Ciaobot.app/Contents/Resources/engine"
    for rel, data in {
        "bin/python3.12": THIN, "lib/x/deep/a.so": THIN, "lib/b.dylib": fat_arm64(),
        "lib/fake.so": b"#!/bin/sh\nnot macho\n", "lib/mislabelled.txt": THIN,
    }.items():
        (engine / rel).parent.mkdir(parents=True, exist_ok=True)
        (engine / rel).write_bytes(data)
    (engine / "bin/ciao").write_text("#!/bin/sh\necho ciao 1.2.3\n")
    (engine / "bin/ciao").chmod(0o755)
    (engine / "provenance.json").write_text(
        json.dumps({"kind": "ciaobot-engine-provenance", "engine": {"version": "1.2.3"}}))
    os.symlink("python3.12", engine / "bin/python3")
    return engine


@pytest.fixture
def rig(tmp_path, monkeypatch):
    engine = make_engine(tmp_path)
    fake = Fake(engine)
    monkeypatch.setattr(es, "_platform_check", lambda: None)
    monkeypatch.setattr(es, "_run", fake)
    monkeypatch.setattr(es, "_applications_roots", lambda: (str(tmp_path / "NoApplications"),))
    return engine, fake


def rels(engine, paths):
    return [os.path.relpath(p, engine) for p in paths]


def test_signs_exactly_the_macho_files_by_magic(rig):
    engine, fake = rig
    assert es.main(["sign", "--adhoc", str(engine)]) == 0
    assert sorted(rels(engine, fake.signs())) == SIGNED_SET


def test_signing_order_is_deepest_first(rig):
    engine, fake = rig
    assert es.main(["sign", "--adhoc", str(engine)]) == 0
    assert rels(engine, fake.signs()) == ["lib/x/deep/a.so", "bin/python3.12", "lib/b.dylib", "lib/mislabelled.txt"]


def test_adhoc_argv_is_exact(rig):
    engine, fake = rig
    assert es.main(["sign", "--adhoc", str(engine)]) == 0
    sign_calls = [c for c in fake.calls if "--force" in c]
    assert sign_calls
    for call in sign_calls:
        assert call == [es.CODESIGN, "--force", "--sign", "-", "--timestamp=none", call[-1]]
    for call in fake.calls:
        assert not {"--deep", "--options", "runtime", "--entitlements"} & set(call)


def test_every_signed_file_is_verified(rig):
    engine, fake = rig
    fake.verify_fail = {"lib/b.dylib"}
    assert es.main(["sign", "--adhoc", str(engine)]) == 1
    verified = {c[-1] for c in fake.calls if "--verify" in c and "--verbose=2" in c}
    assert str(engine / "lib/b.dylib") in verified


@pytest.mark.parametrize("case", ["provenance-touched", "new-file", "target-symlinked"])
def test_signing_may_change_only_signed_files(rig, case):
    engine, fake = rig
    step = [0]

    def sneaky():
        if case == "provenance-touched":
            with open(engine / "provenance.json", "a") as handle:
                handle.write(" ")
        elif case == "new-file":
            (engine / "lib/a.so.cstemp").write_bytes(b"x")
        else:
            # A target that stops being a regular file mid-signing, with the symlink pointing at
            # an already-signed target: the path set stays the same, the codesign calls all
            # "succeed", and only the regular-file rule catches it.
            step[0] += 1
            if step[0] == 2:  # bin/python3.12 has just been signed; lib/b.dylib comes next
                (engine / "lib/b.dylib").unlink()
                os.symlink("../bin/python3.12", engine / "lib/b.dylib")

    fake.also = sneaky
    assert es.main(["sign", "--adhoc", str(engine)]) == 1


@pytest.mark.parametrize(
    "case",
    ["missing-ciao", "ciao-symlink", "ciao-0644", "python-not-macho", "python-x86", "bad-kind", "bad-version"])
def test_layout_is_checked_before_any_codesign(rig, case):
    engine, fake = rig
    ciao = engine / "bin/ciao"
    if case == "missing-ciao":
        ciao.unlink()
    elif case == "ciao-symlink":
        ciao.unlink()
        (engine / "bin/real-ciao").write_text("#!/bin/sh\n")
        os.symlink("real-ciao", ciao)
    elif case == "ciao-0644":
        ciao.chmod(0o644)
    elif case == "python-not-macho":
        (engine / "bin/python3.12").write_bytes(b"#!/bin/sh\n")
    elif case == "python-x86":
        # A thin Mach-O is still a Mach-O of the wrong architecture: refused at layout.
        (engine / "bin/python3.12").write_bytes(THIN_X86)
    else:
        data = json.loads((engine / "provenance.json").read_text())
        if case == "bad-kind":
            data["kind"] = "other"
        else:
            data["engine"]["version"] = "1.2.x"
        (engine / "provenance.json").write_text(json.dumps(data))
    assert es.main(["sign", "--adhoc", str(engine)]) == 1
    assert fake.calls == []


def test_engine_path_shape(rig, tmp_path):
    engine, fake = rig
    other = tmp_path / "elsewhere"
    other.mkdir()
    assert es.main(["sign", "--adhoc", str(other)]) == 1
    # a symlinked Resources directory
    linked = tmp_path / "Linked.app/Contents"
    linked.mkdir(parents=True)
    os.symlink(engine.parent, linked / "Resources")
    assert es.main(["sign", "--adhoc", str(linked / "Resources/engine")]) == 1
    # a symlinked app bundle
    linked_app = tmp_path / "AppLink.app"
    os.symlink(engine.parent.parent.parent, linked_app)
    assert es.main(["sign", "--adhoc", str(linked_app / "Contents/Resources/engine")]) == 1
    # a symlinked Contents directory
    linked_contents = tmp_path / "ContentsLink.app"
    linked_contents.mkdir()
    os.symlink(engine.parent.parent, linked_contents / "Contents")
    assert es.main(["sign", "--adhoc", str(linked_contents / "Contents/Resources/engine")]) == 1
    assert fake.calls == []


def test_engine_path_shape_refused_in_check_mode(rig, tmp_path):
    engine, fake = rig
    linked_contents = tmp_path / "ContentsLink.app"
    linked_contents.mkdir()
    os.symlink(engine.parent.parent, linked_contents / "Contents")
    assert es.main(["check", str(linked_contents / "Contents/Resources/engine")]) == 1
    assert fake.calls == []


def test_engine_under_applications_is_refused(rig, monkeypatch, tmp_path):
    engine, fake = rig
    monkeypatch.setattr(es, "_applications_roots", lambda: (str(tmp_path),))
    assert es.main(["sign", "--adhoc", str(engine)]) == 1
    assert fake.calls == []


def test_check_refuses_an_unsigned_macho(rig):
    engine, fake = rig
    assert es.main(["check", str(engine)]) == 0
    fake.display_override = {"lib/x/deep/a.so": "CodeDirectory v=20400 flags=0x0(none)\nSignature=none\n"}
    assert es.main(["check", str(engine)]) == 1


def test_check_refuses_a_failing_verify_and_writes_nothing(rig):
    engine, fake = rig
    fake.verify_fail = {"lib/b.dylib"}
    assert es.main(["check", str(engine)]) == 1
    assert fake.signs() == []


def test_inventory_prints_the_full_inventory(rig, capsys):
    """`inventory` is the read-only seam `package-notarized.sh` compares across the version probe."""
    engine, fake = rig
    assert es.main(["inventory", str(engine)]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    rows = json.loads(captured.out)
    paths = {row["path"]: row for row in rows}
    for expected in ["bin/ciao", "bin/python3.12", "bin/python3", "lib/b.dylib", "provenance.json"]:
        assert expected in paths
    # Every regular file records its size and sha256, whatever its contents.
    python = paths["bin/python3.12"]
    assert python["size"] == len(THIN) and len(python["sha256"]) == 64
    assert paths["bin/ciao"]["size"] == len(b"#!/bin/sh\necho ciao 1.2.3\n")
    # A symlink records its target instead of a digest.
    assert paths["bin/python3"] == {"path": "bin/python3", "symlink": "python3.12"}
    assert "sha256" in paths["bin/ciao"] and paths["bin/ciao"].get("symlink") is None
    assert fake.calls == []


def test_inventory_detects_a_changed_file(rig, capsys):
    engine, _fake = rig
    assert es.main(["inventory", str(engine)]) == 0
    before = json.loads(capsys.readouterr().out)
    with open(engine / "provenance.json", "ab") as handle:
        handle.write(b" ")
    assert es.main(["inventory", str(engine)]) == 0
    after = json.loads(capsys.readouterr().out)
    assert before != after
    before_by_path = {row["path"]: row for row in before}
    after_by_path = {row["path"]: row for row in after}
    assert after_by_path["provenance.json"]["size"] == before_by_path["provenance.json"]["size"] + 1
    assert after_by_path["provenance.json"]["sha256"] != before_by_path["provenance.json"]["sha256"]


def test_inventory_refuses_an_engine_shaped_wrong(rig, capsys, tmp_path):
    engine, _fake = rig
    other = tmp_path / "elsewhere"
    other.mkdir()
    assert es.main(["inventory", str(other)]) == 1
    assert capsys.readouterr() == ("", "engine sign failed\n")
    assert es.main(["inventory"]) == 2
    assert "engine sign failed: usage" in capsys.readouterr().err


def test_cli(rig, capsys):
    engine, _fake = rig
    assert es.main(["sign", "--distribution", str(engine)]) == 2
    assert es.main(["sign", "--distribution", TEAM, str(engine)]) == 2
    assert es.main(["check", "--distribution", str(engine)]) == 2
    assert es.main(["sign", "--adhoc"]) == 2
    assert es.main([]) == 2
    assert "engine sign failed: usage" in capsys.readouterr().err
    assert es.main(["check", str(engine.parent)]) == 1
    assert capsys.readouterr().err == "engine sign failed\n"
    assert es.main(["check", str(engine)]) == 0
    assert capsys.readouterr().err == ""


def dist_requirement(team):
    return (
        "anchor apple generic and certificate 1[field.1.2.840.113635.100.6.2.6]"
        " and certificate leaf[field.1.2.840.113635.100.6.1.13]"
        f' and certificate leaf[subject.OU] = "{team}"')


def test_distribution_argv_is_exact(rig):
    engine, fake = rig
    fake.default_display = DIST
    assert es.main(["sign", "--distribution", TEAM, IDENTITY, str(engine)]) == 0
    sign_calls = [c for c in fake.calls if "--force" in c]
    assert sorted(rels(engine, [c[-1] for c in sign_calls])) == SIGNED_SET
    for call in sign_calls:
        assert call == [es.CODESIGN, "--force", "--sign", IDENTITY, "--options", "runtime", "--timestamp", call[-1]]
    for call in fake.calls:
        assert not {"--deep", "--entitlements", "--timestamp=none"} & set(call)
    assert rels(engine, [c[-1] for c in sign_calls]) == [
        "lib/x/deep/a.so", "bin/python3.12", "lib/b.dylib", "lib/mislabelled.txt"]


def test_distribution_verification_pins_the_team(rig):
    engine, fake = rig
    fake.default_display = DIST
    assert es.main(["check", "--distribution", TEAM, str(engine)]) == 0
    target = str(engine / "bin/python3.12")
    assert [es.CODESIGN, "--verify", "--strict", "-R=" + dist_requirement(TEAM), target] in fake.calls
    assert fake.signs() == []


@pytest.mark.parametrize("case", [
    "adhoc", "adhoc-line-on-an-otherwise-full-signature", "wrong-team", "no-runtime", "no-timestamp",
    "not-developer-id", "verify-fails"])
def test_distribution_check_refuses_a_weak_signature(rig, case):
    engine, fake = rig
    fake.default_display = DIST
    assert es.main(["check", "--distribution", TEAM, str(engine)]) == 0
    bad = {
        "adhoc": ADHOC,
        "adhoc-line-on-an-otherwise-full-signature": DIST + "Signature=adhoc\n",
        "wrong-team": DIST.replace(f"TeamIdentifier={TEAM}", "TeamIdentifier=ZZZZZZZZZZ"),
        "no-runtime": DIST.replace("0x10000(runtime)", "0x0(none)"),
        "no-timestamp": DIST.replace("Timestamp=Nov 18, 2026 at 10:00:00\n", ""),
        "not-developer-id": DIST.replace(f"Authority={IDENTITY}", "Authority=Apple Development: X"),
        "verify-fails": DIST,
    }[case]
    fake.display_override = {"lib/x/deep/a.so": bad}
    if case == "verify-fails":
        fake.verify_fail = {"lib/x/deep/a.so"}
    assert es.main(["check", "--distribution", TEAM, str(engine)]) == 1


def test_adhoc_engine_fails_the_distribution_check(rig):
    engine, _fake = rig
    assert es.main(["check", str(engine)]) == 0
    assert es.main(["check", "--distribution", TEAM, str(engine)]) == 1


@pytest.mark.parametrize("identity", [
    "-", "Apple Development: Someone (AXBFS96Y9F)", f"Developer ID Application: Someone ({TEAM.lower()})",
    "Developer ID Application: Someone (ZZZZZZZZZZ)"])
def test_distribution_sign_refuses_a_wrong_identity_before_codesign(rig, identity):
    engine, fake = rig
    assert es.main(["sign", "--distribution", TEAM, identity, str(engine)]) == 1
    assert fake.calls == []


def test_distribution_sign_refuses_a_malformed_team(rig):
    engine, fake = rig
    assert es.main(["sign", "--distribution", "team", "Developer ID Application: X (team)", str(engine)]) == 1
    assert es.main(["check", "--distribution", "team", str(engine)]) == 1
    assert fake.calls == []


def test_distribution_sign_refuses_a_signature_that_does_not_check(rig):
    """The post-sign check uses the distribution rule: an ad-hoc display after signing is refused."""
    engine, fake = rig
    fake.default_display = ADHOC
    assert es.main(["sign", "--distribution", TEAM, IDENTITY, str(engine)]) == 1


@pytest.mark.parametrize("case", ["provenance-touched", "new-file"])
def test_distribution_signing_may_change_only_signed_files(rig, case):
    engine, fake = rig
    fake.default_display = DIST

    def sneaky():
        if case == "provenance-touched":
            with open(engine / "provenance.json", "a") as handle:
                handle.write(" ")
        else:
            (engine / "lib/a.so.cstemp").write_bytes(b"x")

    fake.also = sneaky
    assert es.main(["sign", "--distribution", TEAM, IDENTITY, str(engine)]) == 1
