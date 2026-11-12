"""A deterministic stand-in for the model that runs a curated memory pass.

Used by tests/test_longmemeval_harness.py as `--extract-cmd`. It does what the
pass prompt asks with fixed rules instead of judgement, through the same doors a
real pass uses: it reads the archived transcript the prompt names, writes
through the `ciao` agent CLI (which reaches the harness's temporary control
plane), files proposals with `ciao memory-proposal-add`, and writes vault notes
as files. No network, no model.

Rules, by the user's own words in each turn:
- "my cat" / "my dog": a `ciao:profile` entry via `ciao memory update`;
- "I live in" / "I moved": a `[memory]` proposal for review;
- "I work": a note under `<workspace vault>/people/`;
- anything else: dropped, which is the point of curation.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

USER_BLOCK = re.compile(r"### User\s*\n+```text\n(.*?)\n```", re.DOTALL)


def ciao(*args: str) -> None:
    subprocess.run([sys.executable, "-m", "ciao", *args], check=True, capture_output=True)


def main() -> int:
    prompt = Path(sys.argv[1]).read_text(encoding="utf-8")
    match = re.search(r"archived transcript is at (\S+?)\. ", prompt)
    if match is None:
        print("no archive in the prompt", file=sys.stderr)
        return 2
    archive = Path(match.group(1))
    if not archive.is_absolute():
        archive = Path(os.environ["CIAO_WORKSPACE"]) / archive
    scratch = Path(sys.argv[1]).parent / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    vault = Path(os.environ["CIAO_VAULT_ROOT"]) / os.environ["CIAO_ACTIVE_WORKSPACE"]
    changed = []
    for number, text in enumerate(USER_BLOCK.findall(archive.read_text(encoding="utf-8"))):
        lowered = text.lower()
        if "my cat" in lowered or "my dog" in lowered:
            ciao("memory", "update", "--region", "profile", "--action", "add", "--entry", text)
            changed.append("profile entry")
        elif "i live in" in lowered or "i moved" in lowered:
            fact = scratch / f"fact-{number}.txt"
            reason = scratch / f"reason-{number}.txt"
            fact.write_text(text, encoding="utf-8")
            reason.write_text("A duplicate check would settle it.", encoding="utf-8")
            ciao(
                "memory-proposal-add",
                "--kind",
                "memory",
                "--text-file",
                str(fact),
                "--decide",
                "nightly",
                "--decide-reason-file",
                str(reason),
            )
            changed.append("memory proposal")
        elif "i work" in lowered:
            note = vault / "people" / "me.md"
            note.parent.mkdir(parents=True, exist_ok=True)
            note.write_text(
                f"---\ntype: person\ntitle: Me\n---\n# Me\n\n- {text}\n", encoding="utf-8"
            )
            changed.append("people note")
    print("Changed: " + (", ".join(changed) or "nothing"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
