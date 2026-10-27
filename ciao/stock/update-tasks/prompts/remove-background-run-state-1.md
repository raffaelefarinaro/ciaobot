# Delete the old background-run state folder

An earlier engine ran model-authored commands itself and kept
their registry and logs in a `background/` folder inside this install's runtime
directory. That runner is gone and nothing reads the folder, so this task
deletes it.

The runtime directory is the install's `.runtime` folder: the one that holds
`state.json` and `update-tasks.json`. The folder to remove is
`<runtime>/background/`, and only that folder.

## Steps

1. Resolve the one absolute path: the `background` folder directly inside the
   runtime directory, beside its `state.json`. Do not use a glob or a pattern.
2. Check it before deleting, and stop and ask the operator if any check fails:
   the path ends in a folder named exactly `background`; its parent holds
   `state.json`; the path is a real folder, not a symlink (if it is a symlink,
   remove only the link itself).
3. List it and tell the operator what is in it (a `state.json` and `bg-*.log`
   files). They are logs of commands that already finished.
4. Delete exactly that path and everything in it with the platform's native
   recursive remove, quoting the path (`rm -r "<path>"` on macOS and Linux,
   `Remove-Item -Recurse -LiteralPath "<path>"` in PowerShell). Do not touch
   anything else in the runtime directory.
5. List the runtime directory again and confirm `background` is gone.

If the folder is already missing, the task is already done: say so and stop.
Running this twice is harmless.

This task never changes any workspace, vault or chat, and it needs no restart.
