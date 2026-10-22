# Automations and webhook triggers

## Automations

- One primitive covers every cadence. An automation dispatches its prompt as a chat turn at a time of day (daily/weekly/monthly/once, timezone-aware), every N minutes, or only when the user clicks Run. Configure from the **Automations page** or directly in chat (`ciao schedule create|preview|update` carries the full field semantics in `ciao help`; `ciao schedule pause|resume|run|delete` handles the lifecycle).
- **Two targets, and the choice matters.** Point an automation at a **project** and each run opens a fresh chat there with its own model and provider — right for briefings, reports, and maintenance. Point it at an existing **chat** (chosen on the Automations page; the CLI binds to projects only) and every run continues that conversation, inheriting its model and mode — right when continuity between runs is the point ("check my PRs every 10 minutes and tell me what changed").
- **Every N minutes** is `frequency="interval"` with `interval_minutes` (minimum 1). Combined with a chat it is what used to be called a loop, and behaves the same way: a run that comes due while the chat is still working is skipped and retried shortly after, never queued, and the cadence resumes after a restart rather than replaying what it missed. Combined with a project it opens a fresh chat each time.
- **When to create one**: the agent creates automations conversationally when the user asks — this is an agent action, not a PWA-only button. Show a concise draft (cadence, next_run, target workspace, target project) and get confirmation before creating, unless the user already explicitly asked to apply it. Call `ciao schedule preview` first to validate. Ask which workspace/project when it isn't obvious from the request. An automation always belongs to one logical workspace and shows under Automations for that workspace.
- Time-of-day runs that were due while the app was off are caught up on the next launch; each workspace shows how many it missed. A run that dispatched but stopped before it finished (a restart mid-turn, a killed model process) counts as missed too — it is recovered once on the next launch, and stays listed as missed if that recovery fails as well. A run that completed is never repeated. Interval runs are not replayed — their cadence just resumes.
- System maintenance schedules ship with the app and appear on the Automations page next to the user's own. The **Session insights** switch in Settings → General stops the memory pass for newly archived chats.

## Webhook triggers (the other direction: events in, not times out)

Automations run because a time arrived. A **webhook trigger** runs because
something outside called a URL: a CI provider, a monitoring agent, a
home-automation box. Configure them on the Automations page or in chat with
`ciao webhook list|create|update|rotate|delete` (`ciao help` for the flags), and
the sender's own call is a short `curl` recipe in `PWA_API.md`.

- **Each trigger has its own revocable secret**, shown once when it is created
  or rotated and never readable again (only a hash is kept). Create one,
  hand the secret to the sender, enable it; rotate it the moment it is exposed —
  rotation kills the previous secret immediately, so a sender holding the old one
  stops working on the spot. Deleting a trigger destroys its secret for good, and
  archiving a workspace destroys every trigger secret in it, so restoring that
  name later cannot bring an old secret back to life.
- **The ingress is a separate machine surface**, not the dashboard: it takes that
  one trigger's bearer secret and an idempotency key, never the PWA password and
  never a browser session cookie. The dashboard password is not a webhook
  credential and cannot be used as one.
- **A new trigger does nothing until it is enabled**, on purpose: you can
  configure it fully while it cannot be called.
- **The sender's only input is event text.** It cannot choose the workspace,
  project, chat, model or permission mode.
- **A webhook event is an ordinary new chat.** The turn it launches runs in the
  trigger's own project with the same permission mode and model a new chat gets
  (Settings → Providers); there is no separate per-trigger mode. If it
  needs an approval, that is an ordinary approval card waiting for you in
  **Needs-you**. If your new-chat default is bypass, webhook turns run in bypass
  too.
- **Instructions plus content.** The trigger's instructions say what to do; the
  sender's text carries the content to work on (an error, an order number, a
  link) and arrives quoted as data, so it cannot add instructions of its own.
- **What can send events:** anything that can make an HTTP POST with a custom
  header and JSON body — a GitHub Actions step, an n8n HTTP Request node,
  Zapier/Make webhook actions, Home Assistant, a script. GitHub's built-in repo
  webhooks cannot (they send their own payload and an HMAC signature, not a
  bearer secret). The sender must be able to reach the engine, so a cloud
  service needs a route onto the user's Tailscale network.
- **`202 accepted` means the event was recorded**, not that the work happened:
  the chat it becomes is an ordinary one you open yourself. If the launch could
  not happen (a deleted project, a disabled trigger), the receipt says so and
  nothing is retried behind your back.
- Reusable for "whenever X happens, open a chat and do Y" — say what the sender
  should POST and Ciaobot can set up the trigger, or configure it yourself when
  the user wants to do it by hand.
- **Network blips:** if a routine's job cannot reach an external service, the run re-tries itself in the same chat after 10, 20 and 40 minutes; Automations shows "Retrying" meanwhile and "failed" if all three attempts miss.
