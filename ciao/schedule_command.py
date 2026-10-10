"""Run a schedule's optional shell command before its model turn.

A schedule with a ``command`` runs that command first, with no model involved.
Empty stdout on exit 0 means nothing happened: the run is recorded as done and
no chat is opened. Anything else (output, a non-zero exit, a timeout) opens the
run chat with the command's outcome prepended to the schedule's prompt.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

#: Wall-clock budget for one command run. A command past it is killed and the
#: run is handed to the model with a timeout note.
SCHEDULE_COMMAND_TIMEOUT_S = 600

#: Head plus tail kept from the command's output when it is prepended to the
#: prompt, so a chatty command cannot flood the run chat's context.
SCHEDULE_COMMAND_OUTPUT_CAP = 8000


@dataclass(frozen=True, slots=True)
class CommandRun:
    """The outcome of one scheduled command run."""

    command: str
    exit_code: int | None  # None when the command was killed on timeout
    stdout: str
    stderr: str
    timed_out: bool

    @property
    def quiet(self) -> bool:
        """True when the run needs no chat: exit 0 and nothing printed."""
        return not self.timed_out and self.exit_code == 0 and not self.stdout.strip()

    def extra(self) -> dict[str, object]:
        """The run-record fields describing this command, for job_runs."""
        return {
            "command_exit_code": self.exit_code,
            "command_timed_out": self.timed_out,
            "command_output_chars": len(self.stdout) + len(self.stderr),
        }

    def compose_prompt(self, prompt: str) -> str:
        """The schedule's prompt with the command's outcome in front of it."""
        if self.timed_out:
            status = f"timed out after {SCHEDULE_COMMAND_TIMEOUT_S} seconds and was killed"
        else:
            status = f"exited with code {self.exit_code}"
        body = self.stdout
        if self.exit_code != 0 and self.stderr:
            labelled = f"[stderr]\n{self.stderr}"
            body = f"{body}\n{labelled}" if body else labelled
        output = _cap_output(body.rstrip("\n")) if body.strip() else "(no output)"
        return (
            f"The scheduled command `{self.command}` ran before this run and {status}.\n\n"
            f"Command output:\n```\n{output}\n```\n\n"
            f"{prompt}"
        )


def _cap_output(text: str, cap: int = SCHEDULE_COMMAND_OUTPUT_CAP) -> str:
    """Keep the first and last ``cap // 2`` characters of an over-long output."""
    if len(text) <= cap:
        return text
    half = cap // 2
    omitted = len(text) - 2 * half
    return f"{text[:half]}\n... [{omitted} characters omitted] ...\n{text[-half:]}"


async def run_schedule_command(command: str, workspace_root: Path) -> CommandRun:
    """Run ``command`` in the workspace with the operator's environment.

    The workspace is the working directory, and ``CIAO_WORKSPACE`` is set the
    way the CLI sets it for the engine. Output is read through asyncio pipes so
    the engine loop keeps running while the command works.
    """
    env = dict(os.environ)
    env["CIAO_WORKSPACE"] = str(workspace_root)
    process = await asyncio.create_subprocess_shell(
        command,
        cwd=str(workspace_root),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            process.communicate(), timeout=SCHEDULE_COMMAND_TIMEOUT_S
        )
    except TimeoutError:
        # The shell is the only process we can reach portably; a grandchild it
        # spawned may outlive it, so the pipes are not drained after the kill.
        process.kill()
        await process.wait()
        return CommandRun(
            command=command, exit_code=None, stdout="", stderr="", timed_out=True
        )
    return CommandRun(
        command=command,
        exit_code=process.returncode,
        stdout=stdout_b.decode("utf-8", errors="replace"),
        stderr=stderr_b.decode("utf-8", errors="replace"),
        timed_out=False,
    )


def normalize_schedule_command(value: object) -> str:
    """Validate a command field: a string, stripped. Empty means no command.

    Raises ValueError for a non-string so a malformed API body is refused rather
    than silently stored as a shell command.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("command must be a string")
    return value.strip()
