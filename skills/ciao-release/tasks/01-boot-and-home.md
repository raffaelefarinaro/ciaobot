# 01 — Boot, unlock, first paint

**Goal:** prove the freshly installed engine actually serves a working PWA, and
get the session unlocked so tasks 02–05 run unattended.

**Preconditions:** `/ciao-dev-install` has just finished. The engine is live on
the operator's real workspace. The operator is at the machine.

## Round 1 — navigate, stop at the door

```js
const task = await taskSpace("ciaobot release walkthrough");
console.log({ spaceId: task.spaceId });
const page = task.page("p1");
await page.goto("http://127.0.0.1:8443/");
await page.waitForLoadState("domcontentloaded");
console.log({ url: await page.url(), title: await page.title() });
console.log(await page.snapshot());
```

**Then stop and ask the operator to type the dashboard password into the visible
browser window.** Do not read `PWA_AUTH_TOKEN` from the workspace `.env` and do
not type it yourself — that puts the password in this transcript for no benefit.
The `LoginView` is at `web/src/components/LoginView.vue`; the field is
`autocomplete="current-password"`.

If the page is *not* a login form, say so and skip the unlock — an install with
`PWA_AUTH_REQUIRED=false` has no door.

## Round 2 — the app loads

```js
const task = await taskSpace("ciaobot release walkthrough");
const page = task.page("p1");
await page.waitForSelector("loc=css:aside, loc=css:nav", { timeout: 20000 });
const shot = await page.screenshot({ path: "/tmp/ciao-01-home.png", fullPage: false });
console.log({ url: await page.url(), shot });
console.log(await page.snapshot({ scope: "full_page" }));
```

A full-page snapshot on a cold boot is a lot of output. Take `scope: "full_page"`
**once**, here, where you are establishing what the app looks like; use the
default viewport snapshot from here on.

**What must be true:**

- No `EngineOfflineView` and no `StartupView` spinner still running — those mean
  the engine is not answering, which is a real failure, not a slow start. Wait
  up to ~20s before calling it.
- The sidebar renders: workspaces, projects, and the "what needs you" lanes.
- The **device setup card is gone** if setup is already done. It is meant to
  auto-hide (`HomeSetupCard`) — a card still showing for a configured install is
  a regression worth reporting.
- `HousekeepingStrip` shows the package-update tile with an **"Update in
  Settings"** button leading, and release notes as the secondary link.

## Round 3 — is the served build the build we cut?

The engine can be serving a stale bundle. Compare the asset hash the *served*
shell names against the one on disk:

```js
const task = await taskSpace("ciaobot release walkthrough");
const page = task.page("p1");
const served = await page.evaluate(() => {
  const m = [...document.querySelectorAll("script[src],link[href]")]
    .map((n) => n.getAttribute("src") || n.getAttribute("href") || "")
    .filter((s) => s.includes("/assets/"));
  return m;
});
console.log({ servedAssets: served });
```

```bash
grep -oE 'assets/index-[A-Za-z0-9_-]+\.js' ciao/web/static/index.html
```

They must match. A mismatch means `npm run build` ran before the last source
change, or the install copied older package data — rebuild, reinstall, restart.

## Report

- Engine answered, and the served asset hash matches disk.
- Anything that looked wrong, wrong at this width, or slow to settle — with a
  screenshot path.

Carry `spaceId` forward into task 02. Do not create a second TaskSpace.
