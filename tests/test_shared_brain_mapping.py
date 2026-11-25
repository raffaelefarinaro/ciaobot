from dataclasses import replace
import json

from ciao import shared_brain_guide as guide
from ciao import shared_brain_mapping as mapping
from ciao.config import SharedBrainLink, shared_brain_row, shared_brains_from_rows


def make_guide(root, path="notes/decisions.md"):
    document = {"version": 1, "scope": "Team decisions", "exclusions": [], "types": {
        "decision": {"kind": "section", "path": path, "section": "Decisions", "operation": "append"}}}
    (root / guide.GUIDE_NAME).write_text("```ciao-guide\n" + json.dumps(document) + "\n```\n")
    return guide.read_guide(root, brain_identity="github.com/acme/notes")


def test_mapping_roundtrip_and_guide_edit_requires_new_review(tmp_path):
    load = make_guide(tmp_path)
    link = SharedBrainLink("team", str(tmp_path), "github.com/acme/notes")
    choice = mapping.approve(link, load, ["decision"], expected_pin=load.guide.pin)
    saved = replace(link, guide_mapping=choice, guide_mapping_generation=choice.generation)
    restored = shared_brains_from_rows([shared_brain_row(saved)])[0]
    assert restored.guide_mapping == choice
    assert mapping.resolve(restored, load, note_type="decision").status == "ok"
    assert mapping.resolve(restored, make_guide(tmp_path, "notes/other.md"), note_type="decision").status == "guide_changed"
    assert mapping.resolve(replace(restored, lifetime_id="another"), load, note_type="decision").status == "not_consented"


def test_mapping_requires_exact_guide_pin_and_explicit_types(tmp_path):
    load = make_guide(tmp_path)
    link = SharedBrainLink("team", str(tmp_path), "github.com/acme/notes")
    import pytest
    with pytest.raises(ValueError):
        mapping.approve(link, load, ["unknown"], expected_pin=load.guide.pin)
    with pytest.raises(ValueError):
        mapping.approve(link, load, ["decision"], expected_pin=replace(load.guide.pin, digest="0" * 64))
    assert mapping.resolve(link, load, note_type="decision").status == "not_consented"


def test_restore_drops_mapping_even_for_folder(tmp_path):
    from ciao.shared_brain import reset_link_for_restore
    load = make_guide(tmp_path)
    link = SharedBrainLink("team", str(tmp_path), "github.com/acme/notes", kind="folder")
    choice = mapping.approve(link, load, ["decision"], expected_pin=load.guide.pin)
    restored = reset_link_for_restore(replace(link, guide_mapping=choice, guide_mapping_generation=choice.generation))
    assert restored.lifetime_id != link.lifetime_id
    assert restored.guide_mapping is None
