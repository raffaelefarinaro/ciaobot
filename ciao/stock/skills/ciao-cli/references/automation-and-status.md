## Schedules

| Command | Purpose | Guard |
|---|---|---|
| `schedule list` | List schedules in the active workspace with their next run. | — |
| `schedule preview ...flags` | Validate fields and compute `next_run` without saving. | Run before `create` for a new recurring schedule; a missing or invalid `next_run` means the fields don't validate yet. |
| `schedule create ...flags` | Create a validated schedule (recurring, one-off, or manual-only). Days take `mon,tue,wed,thu,fri,sat,sun` (full names and any case accepted; any other value is refused). | Show the user a draft — `next_run`, workspace, project — and get confirmation unless they already asked to apply it. `--frequency interval` is refused from an unattended run (`unattended_forbidden`); use `loop start` for a bounded check in this chat. |
| `schedule update ID ...flags` | Update an existing schedule; only fields you pass change. | A system schedule (`scope=system`) only accepts workspace, model, provider and archive-policy changes — anything else raises `system_schedule_read_only`. Pause or resume one with `schedule pause|resume`; there is no `--enabled` flag. |
| `schedule pause ID` | Pause a schedule without deleting it. | Destructive. |
| `schedule resume ID` | Resume a paused schedule. | Destructive. |
| `schedule run ID` | Dispatch a schedule immediately through the normal chat pipeline. | Destructive. |
| `schedule delete ID` | Delete a removable user schedule. | Destructive; a system schedule cannot be deleted (`schedule_not_removable`). |

Shared `...flags` for `schedule create/update/preview`: `--prompt --daily-time --timezone --frequency {daily,weekly,monthly,manual,once,interval} --interval-minutes --days-of-week mon,tue --day-of-month --run-at-date --project --title --description --provider --model --archive-policy {manual,auto} --command`. `--command` is operator-gated, see Calling convention. There is no `--chat` binding flag — schedules bind to a project and workspace, never to one chat. For an interval that wakes this chat and ends on its own, use `loop` (below).

## Loops

A loop wakes **this chat** every interval and ends on its own. Use it for "check every 5 minutes until 14:30" inside a chat. A project routine uses `schedule`.

| Command | Purpose | Guard |
|---|---|---|
| `loop start --every EVERY [--until UNTIL \| --max-runs N] [--prompt TEXT \| --prompt-file FILE]` | Start a loop in this chat. `EVERY` is `5m`, `1h` or `1h30m`. `UNTIL` is a duration (`90m`) or the next `HH:MM` in the default zone. | Give one of `--until` or `--max-runs`, and one prompt source. Every 1 minute to 24 hours; at most 288 runs; the loop ends at its expiry or one interval after its last run. At most 3 per chat and 20 engine-wide. |
| `loop list` | This chat's loops: id, cadence, runs against the cap, end, next run, `last_status`. | Read-only. Other chats' loops never appear. |
| `loop stop [ID]` | Stop one of this chat's loops; `ID` may be omitted when there is exactly one. | Deletes the loop. Another chat's loop is `loop_not_found`. |

- A wake-up cannot start a loop, and a plan-mode chat cannot start one.
- `last_status` `skipped` means a wake-up needed an approval and was denied. Tell the user; do not retry it.
- Stop the loop when its work is done. To change its cadence or end, stop it and start a new one.

## Webhook triggers

A trigger is a URL an external sender POSTs to. Ciaobot answers the event with a
durable receipt and launches an ordinary chat in the trigger's own project — the
sender never chooses a chat, a model, a mode or a workspace, and the turn is not
unattended, so an approval it raises is an ordinary approval card.

| Command | Purpose | Guard |
|---|---|---|
| `webhook list` | List this workspace's triggers as public records. | Read-only. Never a secret: only a hash of it is stored. |
| `webhook create --name NAME [--instructions-file FILE] [--project P]` | Configure a trigger and mint its secret. | Returns the trigger **and its secret, shown once**: hand it to the sender now, because only its hash is kept and it cannot be read back. Created **disabled** — nothing authorizes until `webhook update --enable`. |
| `webhook update ID --revision REV [--name N] [--instructions-file FILE] [--enable\|--disable]` | Edit one trigger; only the fields you pass change. `--enable`/`--disable` is the off switch, and it reaches an event accepted a moment earlier. | `webhook_revision_conflict` (retryable) means the trigger moved: re-read and re-plan. The target and the mode are not editable — delete and recreate instead. |
| `webhook rotate ID --revision REV` | Replace the trigger's secret and return the new one, shown once. | **Revokes on rotate**: the old secret stops authorizing immediately. Keeps `enabled` as it was; rotating is not a way to enable a disabled trigger. |
| `webhook delete ID --revision REV` | Delete the trigger and destroy its verifier. | Destructive and irreversible: the sender needs a new trigger. Recorded receipts for past events are kept. |

The sender side is not this CLI: an external system calls
`POST /hooks/v1/<trigger_id>` with `Authorization: Bearer <that secret>` and an
`Idempotency-Key`, and a body of exactly `{"text": "..."}`. `202` means the event
is **recorded**, not that a turn finished. Both recipes are in `PWA_API.md`.

A trigger is created disabled, so ask the user before `webhook update --enable`,
then tell them the sender needs the secret as a bearer token.

`webhook create` and `webhook rotate` show the secret once. Hand it to the user for
the intended sender; never save a copy in notes, memory or source files. Do not
repeat it in later summaries or diagnostics: it is a credential, not vault content.

Instructions are operator prose that the launched turn reads as its own, so
they travel as a file (`--instructions-file`) rather than as a shell argument.

## Workspace and status

| Command | Purpose | Guard |
|---|---|---|
| `context get` | Return the active workspace, project, chat, provider, and control surface, plus local server/startup status. | — |
| `context logs [--lines N]` | Installed version plus recent failed background jobs whose recorded workspace is exactly the active one, already redacted, capped and fenced as untrusted data. Engine-wide error/debug/service logs are omitted (`scope`/`omitted` say why) because they have no immutable workspace provenance; `lines` (1–1000) is retained but no longer caps a log tail. | Read-only. Never assume the omission means there were no engine errors. |
| `stats [--range 7\|30\|90\|365]` | Usage statistics for the active workspace (default 30 days): today, period totals and the preceding period, daily user messages for 182 days, top projects, the memory proposal funnel, messages by hour and turns per model. | Read-only. |
| `gws status` | Report whether the active workspace's Google Workspace account is connected and its token is valid. | Read-only; never triggers a live re-auth check — use it to warn the user their login expired, not to force one. |
| `workspace list` | List all configured logical workspaces — names, vault roots, defaults — not just the active one. | — |
| `repo candidates` | List git checkouts one level under ~/repos, ~/code, ~/src, ~/dev, ~/Developer, ~/projects, to offer for `project update --repo`. | Read-only; a checkout elsewhere can still be linked by path. |
