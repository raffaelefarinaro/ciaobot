# Workspace care: the run lifecycle

This file orders every command a care run sends after `curation-begin`. Nothing else orders them.

Every follow-up takes `--holder <lease.holder>` from `curation-begin`. Exit 75 from any command means the lease is no longer yours: stop at once, run no further command, and report the run as not finished. Exit 1 or 2 from a follow-up is a failure, handled in step 4.

## 1. Begin

`ciao curation-begin --json`. Then:

- Exit 1 is a state error: stop. Read no vault file and run no follow-up.
- `"empty": true`: the lease is already released. Reply with the one-line no-op and stop.
- Otherwise keep `lease.holder` and work the passes under `planned`.

## 2. Passes

Run only the passes and keys under `planned`; `deferred` is the next run's work. Each planned pass reads its reference file and does what it says. Do not record progress during a pass.

A blocked-only night has `planned` empty and `blocked_count` above zero. No pass runs, so there is no technical report: go to step 4 with `--status ok`. Name the blocked logs under **What needs you**.

## 3. One technical report, then progress

Write one technical report for all the work this run attempted: the passes that ran, what changed, what failed, and that finalization is pending. Append it once:

`ciao curation-log-append --holder <lease.holder> --log curation --text-file <file>`

- Exit 0: the report is in the log. Go to 3b.
- Exit 1 or 2: the report did not land. Do not retry the append, because a retry can duplicate the report. Record no progress and go to step 4 with `--status failed`.

3b. Only after that append succeeded, record the keys of the work that completed, including `--key hygiene:vault-index` and `--key hygiene:os-audit` when their checks succeeded:

`ciao curation-progress --holder <lease.holder> --key <key>`, once per key.

An unattempted or failed pass records no key. A failure here goes to step 4 with `--status failed`.

## 4. End

`ciao curation-end --holder <lease.holder> --status ok|failed`

The counts are derived from the run's own receipts; you do not pass them. `completed` can exceed `planned` when the run accepted rows it found during the run, because those are work the plan did not name.

Use `failed` after any failure in steps 2 to 3b, and add `--reason "bounded operation failure"`. The end releases the lease and stamps the weekly marker itself, only when both weekly checks are recorded and the status is `ok`. The output's `full_pass_advanced` says whether it did; if it is false, say the weekly pass is still due. You never write that marker by hand.

If the end exits 1 or 2, say finalization is unconfirmed and stop. Do not clear the lease or edit the state file.

## What needs you

One rule for the chat reply: each deferred item gets one numbered line, except skill proposals. Skill proposals get one aggregate line with the count and the note that they wait in the queue. Blocked oversized logs get one line each, naming the log and that an operator archives it by hand. A failed run gets one line naming the failed step.
