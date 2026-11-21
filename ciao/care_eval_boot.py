"""Boot a care-measurement engine on a caller-supplied starting vault.

``IsolatedEngine.boot`` always copies the generic ``_fixture`` vault into the
workspace and then lays the task overlay over it. A care run needs its own
starting vault instead, so this module writes the scenario's seed (see
:mod:`ciao.care_eval_seed`) as the overlay and passes an empty directory as the
vault fixture, which leaves nothing of ``_fixture`` behind.

Boots an engine process when :func:`boot_care` is called; the tests replace
``IsolatedEngine.boot`` and never reach it. Makes no model call.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from ciao import care_eval_seed as seed
from ciao.curation_run import DEFAULT_MAX_SECONDS
from ciao.task_eval import EvalTask, IsolatedEngine
from ciao.task_eval_contract import Scenario

OVERLAY_DIRNAME = "overlay"
EMPTY_VAULT_DIRNAME = "empty-vault"


def seed_overlay(scenario: Scenario, root: Path, *, dispatch_date: date) -> seed.SeedManifest:
    """Write the scenario's seed into a fresh ``root`` laid out like the agent root.

    ``root`` must not exist yet. Its guide sits at the top and its vault under
    ``memory-vault``, so the task overlay lands them at ``ws/personal`` as the
    engine expects.
    """
    root.mkdir(parents=True)
    layout = seed.Layout(agent_root=root, vault_root=root / seed.VAULT_DIRNAME)
    return seed.build_seed(scenario, layout, dispatch_date=dispatch_date)


def care_task(scenario: Scenario, overlay: Path) -> EvalTask:
    """The minimal task ``IsolatedEngine.boot`` requires: no prompt, the seed as overlay."""
    return EvalTask(
        id=f"care-{scenario}",
        tier="care",
        category="care",
        prompt="",
        entry={"kind": "schedule"},
        setup=[],
        requires=[],
        timeout_s=DEFAULT_MAX_SECONDS,
        checks=[],
        overlay=overlay,
    )


def boot_care(
    scenario: Scenario,
    *,
    provider: str,
    model: str,
    dispatch_date: date,
    scratch: Path,
    mode: str | None = None,
) -> IsolatedEngine:
    """Boot one isolated engine on the scenario's seed, with no fixture vault.

    ``scratch`` is a caller-owned directory that must not already hold a seed.
    The seed and the empty vault are written there before the boot; the engine
    copies them into its own throwaway workspace.
    """
    scratch.mkdir(parents=True, exist_ok=True)
    overlay = scratch / OVERLAY_DIRNAME
    seed_overlay(scenario, overlay, dispatch_date=dispatch_date)
    empty = scratch / EMPTY_VAULT_DIRNAME
    empty.mkdir(exist_ok=True)
    return IsolatedEngine.boot(
        care_task(scenario, overlay),
        provider=provider,
        model=model,
        mode=mode,
        vault_fixture=empty,
    )
