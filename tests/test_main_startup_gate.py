"""A runtime root that still holds a node-mode `node_state.json` refuses to start."""

from __future__ import annotations

from pathlib import Path

import pytest

from ciao import main


def test_refuses_when_a_legacy_node_state_file_exists(tmp_path: Path) -> None:
    (tmp_path / "node_state.json").write_text('{"role": "host"}', encoding="utf-8")

    with pytest.raises(SystemExit) as raised:
        main.refuse_legacy_node_state(tmp_path)

    assert str(tmp_path / "node_state.json") in str(raised.value)
    assert (tmp_path / "node_state.json").exists()


def test_starts_when_there_is_no_legacy_node_state_file(tmp_path: Path) -> None:
    main.refuse_legacy_node_state(tmp_path)
