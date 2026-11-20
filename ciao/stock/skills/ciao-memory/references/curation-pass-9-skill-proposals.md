# Pass 9: skill proposals

Report the proposals `curation-begin` planned for this run, and nothing else. Settle nothing: `ciao skill-proposal-remove` records a decision (implemented, or decided against), and an unattended run can make neither. You did not edit the skill, and a judgement that a proposal is not worth building is a judgement about somebody's work that a person has to make.

1. Each planned item names exactly one proposal file and its key. Read only the files you were planned: never the whole queue, and not the routed drafts: do not run `ciao skill-drafts`.
2. Report each one under **What needs you** with the one change it proposes.
3. Append that report with `ciao curation-log-append --holder <lease.holder> --log curation --text-file <file>`. Only after it succeeds, record that file's key with `ciao curation-progress --holder <lease.holder> --key <key>`. Never record a key for a file you did not report.
4. You may add evidence: a new sighting of the same finding accumulates on the record, which stays queued. A settled record stays settled and is not reopened.

Proposals that belong in a bounded region stay queued too; that is the existing queue's job, not a settlement. Deferred proposals are the next run's work, so do not list them.
