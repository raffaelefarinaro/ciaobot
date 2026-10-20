from __future__ import annotations

from ciao.keep_awake import KeepAwakeController


class FakeAssertion:
    def __init__(self) -> None:
        self.acquires = 0
        self.releases = 0

    def acquire(self) -> None:
        self.acquires += 1

    def release(self) -> None:
        self.releases += 1


def test_one_assertion_covers_overlapping_agent_work() -> None:
    assertion = FakeAssertion()
    controller = KeepAwakeController(assertion)

    controller.work_started("turn:chat-a")
    assert assertion.acquires == 0

    controller.set_enabled(True)
    controller.work_started("turn:chat-b")
    controller.work_started("background-run:run-1")
    assert assertion.acquires == 1
    assert assertion.releases == 0

    controller.work_finished("turn:chat-a")
    controller.work_finished("turn:chat-a")
    controller.work_finished("turn:chat-b")
    assert assertion.releases == 0

    controller.work_finished("background-run:run-1")
    assert assertion.releases == 1


def test_toggle_applies_immediately_to_existing_work() -> None:
    assertion = FakeAssertion()
    controller = KeepAwakeController(assertion)
    controller.work_started("subagent:chat-a")

    controller.set_enabled(True)
    assert assertion.acquires == 1

    controller.set_enabled(False)
    assert assertion.releases == 1

    controller.set_enabled(True)
    assert assertion.acquires == 2


def test_shutdown_releases_and_ignores_later_activity() -> None:
    assertion = FakeAssertion()
    controller = KeepAwakeController(assertion, enabled=True)
    controller.work_started("turn:chat-a")

    controller.close()
    controller.work_started("turn:chat-b")
    controller.work_finished("turn:chat-a")

    assert assertion.acquires == 1
    assert assertion.releases == 1
    assert controller.active_work == frozenset()


def test_power_backend_failures_do_not_fail_agent_work() -> None:
    class FailingAssertion:
        def acquire(self) -> None:
            raise OSError("power request denied")

        def release(self) -> None:
            raise OSError("power request unavailable")

    controller = KeepAwakeController(FailingAssertion(), enabled=True)
    controller.work_started("turn:chat-a")
    controller.work_finished("turn:chat-a")
    controller.close()

    assert controller.active_work == frozenset()

    class FailingReleaseAssertion:
        def acquire(self) -> None:
            return

        def release(self) -> None:
            raise OSError("power request unavailable")

    release_controller = KeepAwakeController(FailingReleaseAssertion(), enabled=True)
    release_controller.work_started("turn:chat-b")
    release_controller.work_finished("turn:chat-b")
    release_controller.close()
    assert release_controller.active_work == frozenset()
