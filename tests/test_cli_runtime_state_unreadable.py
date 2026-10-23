from __future__ import annotations

from pathlib import Path

import pytest

from ciao import cli
from ciao.workspace_reroot import RuntimeStateUnreadable


def test_cli_reports_unreadable_runtime_state_without_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def denied(runtime_root: Path) -> None:
        raise RuntimeStateUnreadable(Path("/x/.runtime/migration/workspace-rooting.json"))

    monkeypatch.setattr("ciao.workspace_reroot.read_receipt", denied)
    monkeypatch.setenv("CIAO_WORKSPACE", str(tmp_path))

    code = cli.main(["os-audit", "--json", "--scope", "workspace"])

    assert code == 77
    err = capsys.readouterr().err
    assert err.startswith("ciao: cannot read Ciaobot runtime state at")
    assert "Traceback" not in err
