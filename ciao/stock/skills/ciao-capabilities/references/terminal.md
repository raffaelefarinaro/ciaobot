# The browser terminal

## What the terminal is

- **A shell in the app, running on the engine's machine.** The terminal panel drives a real shell on the computer Ciaobot runs on, under the account the engine runs as, and streams its bytes to the browser. Nothing runs in the browser.
- **It is the user's shell, not the agent's.** The agent's own tools are governed by the workspace's **File access** setting. The terminal panel is a separate surface that the user opens; the agent cannot drive it, and there is no `ciao terminal` command.
- **It is not sandboxed and not confined.** The shell is the engine account's own shell, with the engine's environment minus secrets. The workspace's File access setting does not apply to it: it is not restricted to the workspace folder, and it can reach anything the engine's account can reach. Say so plainly when a user asks whether it is contained.
- **A session belongs to one chat or one project.** The shell opens in the folder the engine resolves for that target: the chat's code-mode worktree, else the project's linked repository, else the workspace's agent root. The caller names a target and never a folder, a path or a command.
- **Every chat and every project has a way in, and a repository is not required.** Chat details carries a **Terminal** section of its own and a project page carries one too; neither sits inside the repository's, so a chat or a project with no linked folder still has one. The button is **Open in Terminal**: it opens the panel, and nothing runs until the user starts a session.
- **On macOS and Linux** the shell is the account's login shell in a real pty; on Windows it is PowerShell in a pseudoconsole.

## When to use it, and when not to

- **Use it** when the user wants to watch or drive a shell themselves: read a log, run a one-off command, watch a build finish. Point them at the **Terminal** section in the chat's details or on the project page and the **Open in Terminal** button there, and tell them it asks for the password again.
- **Do not offer it** as a way to give the agent a shell it does not have, since the agent has its own tools under the workspace's File access; to read a file, where the file viewer and the pinned panel are the surfaces; or to run something long in the background, where a background run started with `ciao run` is the surface and it reports back to the chat.
- **Do not confuse it with the native terminal window.** `/api/host/terminal` opens a real terminal *window* on the engine's desktop, and only a browser on the engine's own machine may ask for it. The browser terminal is a panel in the app and works from any device. Two different features with two different sets of rules.
- When a user asks "can you open a terminal for me?", the honest answer is that the agent cannot open the panel's shell: the user opens it from the chat or project they are in. The agent can say which folder it will open in and what the rules are, and it can offer a command for the user to review and run.

## Offering a command

- **The agent can offer a command; the user runs it.** `ciao chat terminal-offer --command-file FILE` puts a suggested command into the chat that asked for it, as a card. The file holds the literal command text, and the offer belongs to that one chat.
- **The card is guidance, not a verdict.** It is labelled **Suggested command — review before running**, because the text is model-authored and the engine has not checked it. The card copies the command, opens the terminal, and can be dismissed. It never types the command, never runs it, never unlocks the terminal, and never brings the shell's output back into the chat.
- **The plain entry is always there.** **Open in Terminal** works with or without a card, so an offer adds a shortcut and is never a gate.
- **The offer is transient.** It lives in the running engine only: it is not written into the chat's saved state, the audit log or a backup, it is not copied into a fork or a continued chat, and it is gone after a restart, a new provider session, an archive, a handover, or the user dismissing it. The provider's own transcript may still hold the tool arguments, so never promise that a suggested command leaves no trace anywhere.
- **No secret value goes in the card.** Put placeholders in the command: a real token, key or password belongs in the CLI or in a Settings field, never in a file the chat can read, in the card, or in a reply.

## The HTTP surface

Five routes. All of them carry the same door, in this order: the terminal's own policy, which is this engine's exact origin and HTTPS unless the caller is on the engine's own machine; then the signed session cookie; then a live terminal grant. A refusal never names a credential, and every answer is `no-store`.

- `GET /api/terminal/status` — whether this browser may use the terminal at all: `{available, reason, platform, host, unlock_expires_in, detached_ttl_seconds}`. It reports and it never grants, and it carries no credential.
- `POST /api/terminal/unlock` — the password again, with exactly `{"password": "..."}` in the body. On success the answer is `{ok: true, expires_in: 900}` and a `Set-Cookie` carrying the grant; the grant itself never appears in the JSON. A wrong password is `403 wrong_password`, and five failures from one address inside fifteen minutes close that address's window, after which every attempt, right or wrong, is `429 rate_limited` until the window clears.
- `GET /api/terminal/sessions?chat_id=…` or `?project_id=…` — the sessions opened for exactly that target. The record is `{session_id, folder, cols, rows, seq, attached, exited, exit_code}`: no output bytes and no connection id. Exactly one of the two target keys may be named, and a target that does not exist is a 404 rather than an empty list.
- `POST /api/terminal/sessions` — open a shell, with exactly `{"chat_id": "…"}` or exactly `{"project_id": "…"}` in the body, and the answer `{session: info}`. A body that names a folder, a path or a command is not a body this route accepts.
- `DELETE /api/terminal/sessions/{session_id}` — end the session: the shell is closed and the record removed. Ending a session that is not there is a 404, not a no-op.

The engine keeps at most four terminal sessions at once, and that count includes sessions whose shell has already exited but whose output and status are still on record.

## The socket

`WS /ws/terminal/{session_id}` carries the bytes. The credentials are cookies, the signed session and the `ciao_terminal` grant, and nothing in the query string is one. Every refusal is a close code, never a body.

- **The first frame** is the attach frame, due within five seconds and exactly `{"type": "attach", "offset": <int>, "takeover": <bool>}`. The offset is the absolute count of bytes the client has already consumed, not the index of the last byte, and it must not be in the future.
- **Then the server sends** `ready`, with `session_id`, `folder`, `cols`, `rows`, `seq`, `replay_start` and `truncated`; the replay as `output` frames; and `replay_end`, with `seq` and `truncated`. After that come live `output` frames, and `exit`, with the shell's `code`, when the shell ends. `pong` answers a `ping`, and `error`, with a `reason`, reports a refusal that leaves the socket open.
- **The client sends** `input`, strict base64 and at most 64 KiB decoded; `resize`, with `cols` and `rows`, between 20 and 500 columns and 5 and 200 rows; and `ping`.
- **The close codes** are `4403` for the terminal's own policy, `4401` for a missing or revoked grant, `4404` for a session that is gone, `4409` for a session another connection holds, `4413` for a client that fell behind the replay ring, and `1002` for a frame the protocol does not describe. The app's own socket authorization sits between the policy and the grant check, and answers `4001` for a missing session and `4003` for a foreign origin.

## The rules that matter

- **The engine decides the folder.** The caller names a target, and `ciao.host_terminal.resolve_folder` resolves it. The same resolution decides whether the target exists: 400 for a target that names nothing, 404 for one that does not exist, 409 for one whose folder is gone.
- **One controller per session.** The controller is a random connection id minted per attachment, never a cookie and never a browser. A second connection needs `"takeover": true` and is otherwise closed with `4409`. Two tabs of one browser are two controllers, so the second one cannot silently steal the first one's shell.
- **Takeover is explicit.** Nothing displaces an attachment on its own; the new connection says so in its attach frame, and the old one then finds out with a `4409` of its own.
- **A disconnect detaches; it never ends.** The panel's two endings are different on purpose: **Hide** leaves the shell running, **End** stops it. A hidden shell keeps running and the session stays recoverable; only the explicit end route kills a shell. A session nobody is attached to is pruned fifteen minutes after it became detached, and that deadline is fixed: output does not push it back.
- **The replay is bounded.** The engine keeps the last 256 KiB of a session's output. An attach whose offset has already been evicted is answered with a truncated snapshot of what is left, and the client resets its renderer and replays from `replay_start`; the bytes that are gone are gone. A live read that fell behind the ring is refused with `4413` rather than answered with a hole in the stream.
- **The grant is a browser credential, never an agent's.** The unlock's bearer travels in one host-only, HttpOnly, SameSite=Strict `ciao_terminal` cookie and nowhere else: not in the JSON, not in a URL, not in a log line. The agent cannot read that cookie — it lives in the user's browser, not in the chat's session — cannot mint a grant, and must not treat the terminal API as if it were a `ciao` command. The local HTTP API is the PWA's and the operator's surface, not an in-chat agent's.
- **Interrupting a running program is platform-specific.** On macOS and Linux the shell is an interactive tty, so Ctrl+C is a byte the line discipline turns into an interrupt for the foreground job. The Windows terminal is a pseudoconsole where the child has no process group of its own, and whether an interrupt reaches a running program there is not settled yet. Do not promise it on Windows.

## What a session says, and what it does not

- **A record says** the folder a session opens in, its window size, the byte position its output has reached, whether somebody is attached, whether the shell has exited and with which code.
- **A record does not say** the output. No record, no refusal message and no log line carries the terminal's bytes, and none carries the connection id that holds a session. The agent has no way to read a session's bytes at all, so what the shell prints reaches neither the chat nor the memory pass unless the user pastes it there themselves. Terminal output is the user's screen: do not claim to have seen it, and do not quote it from a record.
- **A refusal says** its reason, and the reasons are a closed vocabulary: `not_found`, `not_controller`, `taken_over`, `capacity`, `invalid_offset`, `invalid_limit`, `slow_consumer`, `invalid_target`, `invalid_size`, `invalid_input`, `spawn_failed`, `closed` and `io_failed`. A frame the protocol does not describe is answered with `bad_frame`, and the socket closes with `1002`.
