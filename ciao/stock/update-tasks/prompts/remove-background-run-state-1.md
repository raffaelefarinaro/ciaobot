# Delete the old background-run state folder

An earlier engine ran model-authored commands itself and kept
their registry and logs in a `background/` folder inside this install's runtime
directory. That runner is gone and nothing reads the folder, so this task
deletes it.

The runtime directory is the install's `.runtime` folder: the one that holds
`state.json` and `update-tasks.json`. The folder to remove is
`<runtime>/background/`, and only that folder.

## Steps

1. List it first and tell the operator what is in it (a `state.json` and
   `bg-*.log` files). They are logs of commands that already finished.
2. Delete the folder and everything in it, for example
   `rm -r <runtime>/background`. Do not touch anything else in the runtime
   directory, and do not follow a symlink out of it.
3. List the runtime directory again and confirm `background/` is gone.

If the folder is already missing, the task is already done: say so and stop.
Running this twice is harmless.

This task never changes any workspace, vault or chat, and it needs no restart.
