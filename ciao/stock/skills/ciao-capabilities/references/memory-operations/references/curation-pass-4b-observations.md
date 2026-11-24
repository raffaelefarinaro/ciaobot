# Pass 4b: propose recurring observations

`Workspace/Observations.md` counts weak patterns per distinct chat. Run `ciao observations --json`, then:

1. Merge, when the worklist plans `merge` (weekly): two Active observations about the same subject that say the same thing in different words become one with `ciao observation-merge KEEP DROP` (keep the clearer wording). Merging can bring an entry to three chats.
2. Judge, then propose, every entry whose `due` is `propose`. An entry earns a proposal only if accepting it would change what an agent does: it implies a concrete instruction ("when X, do Y") beyond following the user's request as written, and no `ciao:memory` or `ciao:profile` entry in `AGENTS.md` already says it. A description of how the user phrases, bundles or launches requests fails the test. Propose a passing entry with `ciao observation-propose ID`: that files a `[profile]` or `[people <Name>]` proposal citing the chats and moves the entry to Resolved; report each under **What needs you**. Resolve a failing entry with `ciao observation-propose ID --drop` (no proposal) and list it, with the one-line reason, in the run report's housekeeping, not under **What needs you**.
3. Prune with `ciao observation-prune` when the worklist plans it.
4. Record each key with `ciao curation-progress`.

Do not write an observation into a region or a person note yourself, and do not edit `Observations.md` by hand: the commands keep the per-chat counts honest.
