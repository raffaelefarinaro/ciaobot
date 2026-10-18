# Pass 4b: propose recurring observations

`Workspace/Observations.md` counts weak patterns per distinct chat. Run `ciao observations --json`, then:

1. Merge, when the worklist plans `merge` (weekly): two Active observations about the same subject that say the same thing in different words become one with `ciao observation-merge KEEP DROP` (keep the clearer wording). Merging can bring an entry to three chats.
2. Propose every entry whose `due` is `propose` with `ciao observation-propose ID`. That files a `[profile]` or `[people <Name>]` proposal citing the chats and moves the entry to Resolved. Report each proposal under **What needs you**.
3. Prune with `ciao observation-prune` when the worklist plans it.
4. Record each key with `ciao curation-progress`.

Do not write an observation into a region or a person note yourself, and do not edit `Observations.md` by hand: the commands keep the per-chat counts honest.
