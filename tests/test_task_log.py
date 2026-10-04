"""The delegation log a task carries in its own body (#1064)."""

from __future__ import annotations

from ciao.task_log import LOG_CLOSE, LOG_OPEN, attempt_label, render_item, strip_log, upsert_item

A = "a" * 32
B = "b" * 32


def _item(attempt_id: str, **fields: str) -> str:
    base = dict(
        attempt_id=attempt_id, state="running", outcome="", summary="", detail="",
        created_at="2026-10-04T09:00:00+00:00", ended_at="", chat_id="chat-1",
        chat_title="Do it", archive_path="",
    )
    base.update(fields)
    return render_item(**base)


def test_labels_put_engine_failures_over_the_agents_word() -> None:
    assert attempt_label("ready_for_review", "done") == "Agent says done"
    assert attempt_label("needs_you", "blocked") == "Blocked"
    assert attempt_label("needs_you", "needs_input") == "Needs input"
    assert attempt_label("needs_you", "", "the turn ended without a report") == "Unfinished"
    assert attempt_label("needs_you", "") == "Waiting on you"
    assert attempt_label("interrupted", "done") == "Interrupted"
    assert attempt_label("running", "done") == "Working"


def test_a_task_with_no_log_gets_one_at_the_end_and_the_description_is_untouched() -> None:
    body = upsert_item("The description.\n", A, _item(A))
    assert body.startswith("The description.\n\n" + LOG_OPEN)
    assert body.rstrip().endswith(LOG_CLOSE)
    assert f"<!-- attempt:{A} -->" in body
    assert strip_log(body) == "The description.\n"
    assert strip_log("") == ""


def test_an_attempt_is_rewritten_in_place_and_a_new_one_goes_first() -> None:
    body = upsert_item("D", A, _item(A))
    body = upsert_item(body, A, _item(A, state="needs_you", outcome="blocked", summary="Need a key.\n\nSecond para."))
    assert body.count(f"attempt:{A}") == 1
    assert "**Blocked**" in body and "  Need a key." in body and "  Second para." in body
    body = upsert_item(body, B, _item(B, chat_title="Second try"))
    assert body.index(f"attempt:{B}") < body.index(f"attempt:{A}")
    # Rewriting the older one keeps the order and the other item.
    body = upsert_item(body, A, _item(A, state="interrupted", archive_path="vault/t.md"))
    assert body.index(f"attempt:{B}") < body.index(f"attempt:{A}")
    assert "archived at `vault/t.md`" in body
    assert body.count(LOG_OPEN) == 1


def test_hand_edits_outside_the_attempts_line_survive_a_rewrite() -> None:
    body = upsert_item("D", A, _item(A))
    body = body.replace("## Delegation log", "## Delegation log\n\nMy own note.")
    body = upsert_item(body, A, _item(A, state="stopped"))
    assert "My own note." in body
    assert "**Stopped**" in body
