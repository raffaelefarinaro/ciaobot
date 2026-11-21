"""Stop during a claimed native continuation: one session/cancel, no live card, no wake afterwards.

Runs the real AcpSession ``cancel`` (which the Stop of a background chat uses), the manager's Stop
path (``_finish_background_stop``), and the real drain reading the session's late queue.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from ciao.web.chat_streaming import ChatStreaming
from tests.test_continuation_claim_session import _end, _text
from tests.test_shell_native_continuation import NATIVE_TEXT, _Host, _setup


def test_stop_during_a_claimed_continuation_sends_one_cancel_and_no_card_or_wake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, session, recorder, broker = _setup(monkeypatch, window=0.1)
    cancels: list[str] = []

    async def cancel(session_id: str) -> None:
        cancels.append(session_id)

    async def flushed(answered: Any) -> None:
        return None

    session._connection = SimpleNamespace(conn=SimpleNamespace(cancel=cancel), wait_until_responses_flushed=flushed)
    session._broker = SimpleNamespace(cancel_all=lambda: [])
    # The chat's provider, as the Stop path sees it: its abort is the session's cancel.
    live = SimpleNamespace(
        current_session_id=session.session_id,
        abort_session=lambda session_id: session.cancel(),
    )
    manager._providers["chat-1"].provider = live
    manager._streaming = SimpleNamespace(_record_wake_turn=lambda *args, **kwargs: None)  # the partial is not announced
    manager._flush_result_announce = lambda chat_id: None  # type: ignore[method-assign]  # the chat's announce side
    host = _Host(manager, session, recorder, broker)

    async def main() -> None:
        drain = asyncio.create_task(ChatStreaming(host).drain_between_turns("chat-1", "p"))  # type: ignore[arg-type]
        _end(session, "call-a", "job a")
        session._touch_late([_text(NATIVE_TEXT)], substantive=True)  # the continuation claims A
        # Stop, through the manager's real path: it drops the held state, then aborts the session.
        await manager._finish_background_stop("chat-1")
        await asyncio.sleep(0.3)  # A's window would have closed here
        drain.cancel()
        await asyncio.gather(drain, return_exceptions=True)

    asyncio.run(main())
    assert cancels == [session.session_id]  # exactly one session/cancel
    assert manager._recorder.started == [] and manager._recorder.queued == []  # no wake afterwards
    assert session._ended_shells == {}
    assert manager._shell_finished_pending == {} and manager._shell_notice_due == {}
    assert manager._claimed_notices == {} and manager._claimed_shell_ids == {}
    assert session._late_speaking is False and session._late_open is False
