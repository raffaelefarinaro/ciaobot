from ciao.web.chat_broker import ChatStream
from tests.test_project_chats_queue import _make_manager
from ciao.web import project_chats


def test_take_and_drain_are_exclusive():
    stream = ChatStream("hello")
    entry_id = stream.enqueue("message", images=["image.png"])
    entry = stream.take_pending(entry_id)
    assert entry is not None
    assert (entry["id"], entry["text"], entry["images"]) == (entry_id, "message", ["image.png"])
    assert stream.take_pending(entry_id) is None
    assert stream.take_pending("unknown") is None
    assert stream.drain_one() is None
    entry_id = stream.enqueue("second")
    assert stream.drain_one()["id"] == entry_id
    assert stream.take_pending(entry_id) is None


def test_parked_take_receipt_and_ack(tmp_path):
    manager = _make_manager(tmp_path)
    project = manager.create_project("queue", workspace="work")
    chat = manager.create_chat(project.project_id, title="queue")
    chat.pending_queue = [{"id": "a", "text": "draft", "images": ["a.png"]}]
    expected = {"entry_id": "a", "text": "draft", "images": ["a.png"]}
    assert manager.take_queue_for_edit(chat.chat_id, "a") == expected
    assert chat.to_dict()["pending_takes"] == [expected]
    assert not _make_manager(tmp_path)._chats[chat.chat_id].pending_queue
    manager.queue_take_ack(chat.chat_id, "a")
    manager.queue_take_ack(chat.chat_id, "a")
    assert chat.to_dict()["pending_takes"] == []


def test_steering_entry_cannot_be_taken():
    stream = ChatStream("hello")
    entry_id = stream.enqueue("message")
    stream._pending[0]["steer_id"] = "delivery"
    assert stream.take_pending(entry_id) is None
    assert stream.pending[0]["id"] == entry_id


def test_live_take_records_receipt_and_refuses_steering(tmp_path):
    manager = _make_manager(tmp_path)
    project = manager.create_project("queue", workspace="work")
    chat = manager.create_chat(project.project_id, title="queue")
    stream = ChatStream("hello")
    manager._broker.register(chat.chat_id, stream)
    entry_id = stream.enqueue("message", images=["x.png"])
    stream._pending[0]["steer_id"] = "delivery"
    assert manager.take_queue_for_edit(chat.chat_id, entry_id) is None
    assert chat.to_dict()["pending_takes"] == []
    stream._pending[0]["steer_id"] = ""
    assert manager.take_queue_for_edit(chat.chat_id, entry_id) == {
        "entry_id": entry_id, "text": "message", "images": ["x.png"]
    }
    assert stream.drain_one() is None
    assert len(chat.to_dict()["pending_takes"]) == 1


def test_receipt_ttl_is_pruned_on_read_and_write(tmp_path, monkeypatch):
    manager = _make_manager(tmp_path)
    project = manager.create_project("queue", workspace="work")
    chat = manager.create_chat(project.project_id, title="queue")
    clock = [100.0]
    monkeypatch.setattr(project_chats.time, "monotonic", lambda: clock[0])
    chat.pending_queue = [{"id": "a", "text": "a"}, {"id": "b", "text": "b"}]
    assert manager.take_queue_for_edit(chat.chat_id, "a")
    clock[0] += project_chats._TAKEN_EDIT_TTL_S + 1
    assert chat.to_dict()["pending_takes"] == []
    assert manager.take_queue_for_edit(chat.chat_id, "b")
    assert [e["entry_id"] for e in chat.to_dict()["pending_takes"]] == ["b"]
