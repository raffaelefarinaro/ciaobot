from __future__ import annotations

import sys
from pathlib import Path

import pytest

from ciao.providers.stdio_rpc import RpcProcessError, RpcResponseError, StdioJsonRpcPeer


@pytest.mark.asyncio
async def test_stdio_rpc_correlates_responses_and_surfaces_server_messages(
    tmp_path: Path,
) -> None:
    script = tmp_path / "rpc_peer.py"
    script.write_text(
        """
import json, sys
for raw in sys.stdin:
    message = json.loads(raw)
    method = message.get('method')
    if method == 'echo':
        print(json.dumps({'id': message['id'], 'result': message['params']}), flush=True)
    elif method == 'fail':
        print(json.dumps({'id': message['id'], 'error': {'code': 9, 'message': 'nope'}}), flush=True)
    elif method == 'notify-me':
        print(json.dumps({'method': 'event/test', 'params': {'ok': True}}), flush=True)
        print(json.dumps({'id': message['id'], 'result': {}}), flush=True)
""",
        encoding="utf-8",
    )
    peer = StdioJsonRpcPeer(
        [sys.executable, str(script)], cwd=tmp_path, name="fake rpc"
    )
    await peer.start()
    assert await peer.request("echo", {"value": 42}) == {"value": 42}
    with pytest.raises(RpcResponseError, match="nope"):
        await peer.request("fail")
    await peer.request("notify-me")
    assert await peer.next_message(timeout=1) == {
        "method": "event/test",
        "params": {"ok": True},
    }
    await peer.close()
    assert not peer.running


@pytest.mark.asyncio
async def test_stdio_rpc_rejects_malformed_provider_output(tmp_path: Path) -> None:
    script = tmp_path / "bad_peer.py"
    script.write_text("print('not json', flush=True)\n", encoding="utf-8")
    peer = StdioJsonRpcPeer(
        [sys.executable, str(script)], cwd=tmp_path, name="bad rpc"
    )
    await peer.start()
    message = await peer.next_message(timeout=2)
    assert message["_process_exit"] is True
    assert "malformed protocol line" in message["error"]
    with pytest.raises(RpcProcessError):
        await peer.request("after-exit")
    await peer.close()

