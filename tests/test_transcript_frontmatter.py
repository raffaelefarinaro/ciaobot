from __future__ import annotations

from pathlib import Path

import yaml

from ciao.frontmatter import split_frontmatter
from ciao.transcripts import TranscriptStore


def test_archive_frontmatter_is_valid_yaml(tmp_path: Path) -> None:
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    store = TranscriptStore(runtime, tmp_path / "transcripts")
    text = store._render_markdown(
        {"provider": "claude", "context_label": "Memory pass · Topic: next steps", "turns": []}
    )
    parts = split_frontmatter(text)
    assert parts is not None
    fm = yaml.safe_load(parts[0])
    assert fm["context"] == "Memory pass · Topic: next steps"
    assert fm["tags"] == ["transcript", "claude"]
