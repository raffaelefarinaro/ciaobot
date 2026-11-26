from __future__ import annotations

from pathlib import Path

import pytest

from ciao import cli
from ciao.config import CiaoConfig, RuntimeStateUnreadable


def test_cli_reports_unreadable_runtime_state_without_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def denied(*args: object, **kwargs: object) -> None:
        raise RuntimeStateUnreadable(Path("/x/.runtime/workspaces.json"))

    monkeypatch.setattr(CiaoConfig, "from_env", classmethod(lambda cls, *a, **k: denied()))
    monkeypatch.setenv("CIAO_WORKSPACE", str(tmp_path))

    code = cli.main(["os-audit", "--json", "--scope", "workspace"])

    assert code == 77
    err = capsys.readouterr().err
    assert err.startswith("ciao: cannot read Ciaobot runtime state at")
    assert "Traceback" not in err
