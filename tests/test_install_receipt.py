from __future__ import annotations

import json
import os
import stat
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from ciao.install_receipt import (
    InstallReceipt,
    main,
    read_receipt,
    receipt_matches_running,
    running_receipt,
    write_receipt,
)


def _receipt(**overrides: Any) -> InstallReceipt:
    fields: dict[str, Any] = {
        "version": "0.9.2",
        "executable": "/u/.local/bin/ciao",
        "python": sys.executable,
        "service_backend": "launchd",
        "service_label": "com.ciao.server",
        "installed_at": "2026-09-25T16:00:00+00:00",
    }
    fields.update(overrides)
    return InstallReceipt(**fields)


def _payload(**overrides: Any) -> dict[str, Any]:
    """The on-disk JSON of a receipt, for the malformed-input cases."""
    data: dict[str, Any] = {
        "version": "0.9.2",
        "executable": "/u/.local/bin/ciao",
        "python": sys.executable,
        "service_backend": "launchd",
        "service_label": "com.ciao.server",
        "installed_at": "2026-09-25T16:00:00+00:00",
        "previous_version": "",
        "previous_executable": "",
        "schema": 1,
    }
    data.update(overrides)
    return data


def _write_json(path: Path, data: object) -> Path:
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_write_then_read_round_trips(tmp_path: Path) -> None:
    receipt = _receipt(previous_version="0.9.1", previous_executable="/u/.local/bin/ciao-old")

    written = write_receipt(receipt, tmp_path / "r.json")

    assert written == tmp_path / "r.json"
    assert read_receipt(tmp_path / "r.json") == receipt


def test_write_is_private_and_atomic(tmp_path: Path) -> None:
    # A pre-existing file left loose by a permissive umask (or an older
    # writer) must be tightened, not trusted.
    target = tmp_path / "state" / "install-receipt.json"
    target.parent.mkdir(parents=True)
    target.parent.chmod(0o755)
    _write_json(target, _payload())
    target.chmod(0o644)

    write_receipt(_receipt(), target)

    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
    assert list(target.parent.glob("*.tmp")) == []


def test_read_missing_returns_none(tmp_path: Path) -> None:
    assert read_receipt(tmp_path / "absent.json") is None


def test_read_corrupt_json_returns_none(tmp_path: Path) -> None:
    path = tmp_path / "r.json"
    path.write_text("{not json", encoding="utf-8")

    assert read_receipt(path) is None


def test_read_non_object_returns_none(tmp_path: Path) -> None:
    path = tmp_path / "r.json"
    path.write_text("[1, 2]", encoding="utf-8")

    assert read_receipt(path) is None


def test_read_wrong_schema_returns_none(tmp_path: Path) -> None:
    # A receipt from a future installer must not be half-interpreted today.
    assert read_receipt(_write_json(tmp_path / "r.json", _payload(schema=2))) is None


def test_read_invalid_backend_returns_none(tmp_path: Path) -> None:
    assert (
        read_receipt(
            _write_json(tmp_path / "r.json", _payload(service_backend="cron"))
        )
        is None
    )


def test_read_missing_field_returns_none(tmp_path: Path) -> None:
    data = _payload()
    data.pop("python")

    assert read_receipt(_write_json(tmp_path / "r.json", data)) is None


def test_read_ignores_unknown_keys(tmp_path: Path) -> None:
    assert read_receipt(_write_json(tmp_path / "r.json", _payload(extra=1))) == _receipt()


def test_read_non_string_previous_returns_none(tmp_path: Path) -> None:
    # `str([1])` is a plausible-looking release that is simply wrong, so an
    # optional field is type-checked like a required one.
    path = _write_json(tmp_path / "r.json", _payload(previous_version=[1]))

    assert read_receipt(path) is None


def test_write_rejects_invalid_backend(tmp_path: Path) -> None:
    target = tmp_path / "state" / "r.json"

    with pytest.raises(ValueError):
        write_receipt(_receipt(service_backend="cron"), target)

    assert not target.exists()


def test_matches_running_same_interpreter() -> None:
    assert receipt_matches_running(_receipt()) is True


def test_matches_running_other_interpreter(tmp_path: Path) -> None:
    receipt = _receipt(python=str(tmp_path / "other" / "python3"))

    assert receipt_matches_running(receipt) is False


def test_matches_running_rejects_other_venv_on_same_base(tmp_path: Path) -> None:
    # Two environments whose interpreters are symlinks to the same base Python,
    # exactly like two venvs or two uv tool envs on one machine. Resolving the
    # interpreter file would make them compare equal; comparing the environment
    # must not.
    def _link(env: str, name: str) -> str:
        path = tmp_path / env / "bin" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(sys.executable, path)
        return str(path)

    env_a = _link("env_a", "python")
    env_b = _link("env_b", "python")
    receipt = _receipt(python=env_a)

    assert receipt_matches_running(receipt, executable=env_b) is False
    assert receipt_matches_running(receipt, executable=env_a) is True
    # Same environment, different entry name inside it.
    assert receipt_matches_running(receipt, executable=_link("env_a", "python3")) is True


def test_running_receipt_uses_default_path(tmp_path: Path) -> None:
    receipt = _receipt()
    write_receipt(receipt)

    assert running_receipt() == receipt

    # A receipt for some other engine on the same machine must not count.
    write_receipt(_receipt(python=str(tmp_path / "other" / "python3")))

    assert running_receipt() is None


def test_main_write_cli(tmp_path: Path) -> None:
    target = tmp_path / "receipt.json"

    code = main(
        [
            "write",
            "--version",
            "0.9.2",
            "--executable",
            "/x/ciao",
            "--service-backend",
            "launchd",
            "--service-label",
            "com.ciao.server",
            "--path",
            str(target),
        ]
    )

    assert code == 0
    receipt = read_receipt(target)
    assert receipt is not None
    assert receipt.python == sys.executable
    assert datetime.fromisoformat(receipt.installed_at) is not None


def test_main_write_cli_rejects_bad_backend(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        main(
            [
                "write",
                "--version",
                "0.9.2",
                "--executable",
                "/x/ciao",
                "--service-backend",
                "cron",
            ]
        )
