---
name: ciao-release-notes
description: Write Ciaobot's human-readable release notes for a version range — Heads up, New features, Improvements, Bug fixes with issue links — from the merged PRs and closed issues, for the CHANGELOG section and the GitHub release. Called by /ciao-release before the cut; also used to rewrite the notes of a past release. Trigger on "release notes", "write the changelog", "what's new in vX", "rewrite the notes for vX".
---

# Ciaobot release notes

> Contributor/project skill — lives in the repo's `skills/` folder, **not**
> `ciao/stock/skills/`. It is for people working *on* Ciaobot.

Release notes are for the people who **use** Ciaobot, not for the people who
build it. They answer three questions in this order: *do I need to do
anything?*, *what can I do now that I couldn't before?*, *what's better or
fixed?* The commit list answers none of them, so it is never the notes; it is
linked at the bottom for anyone who wants it.

`/ciao-release` calls this skill after its plan-only pass, writes the result to
a file, and cuts with `--notes-file <file>`. `ciao.release` adds the
`## vX.Y.Z - date` heading and puts the notes in `CHANGELOG.md`;
`release-on-main.yml` copies that section into the GitHub release, which is
what the website's "What's new" link and the in-app update tile point at.

## The format

The body only — no `## ` heading of your own (the tool adds it, and a second
`## ` would cut the GitHub release short). Sections are `###`, in this order,
and an empty section is left out entirely:

```markdown
**Heads up:** <the change a user must act on or will trip over, and exactly
what to do. One sentence when there is one change; when there are several, a
bullet each, starting with a bold label. Omit the line when there is none.>

### New features
- **<Name of the capability>:** <what you can now do, in one sentence>.

### Improvements
- <What works better or differently, from the user's side>.

### Bug fixes
- <What was broken, in the user's words> ([#NNNN](https://github.com/raffaelefarinaro/ciaobot/issues/NNNN))

[Full list of changes](https://github.com/raffaelefarinaro/ciaobot/compare/vPREV...vX.Y.Z)
```

## The rules

Learned from the release notes people actually read (Linear, Raycast, Things,
GitHub Desktop, Obsidian):

1. **Lead with the benefit, in the user's words.** "Rename a workspace from
   Settings" — not "add PATCH name support to the registry". Name the screen
   where it lives (Settings → Workspaces, the task board, a chat).
2. **One line per item.** If it needs two sentences, the second is how to use
   it. No paragraphs, no "we're excited to".
3. **No internals.** No file, function, module or class names, no test counts,
   no CI, no refactors, no dependency bumps unless a user can see the effect.
   Tests belong in the PRs, not here.
4. **Heads up only when action is needed** — a changed default, something that
   now refuses to run until configured, a setting that moved, a removal. Say
   what changed, who it affects (macOS / Linux / Windows), and the exact fix.
5. **New features** are capabilities a user did not have. **Improvements** are
   things they already had that now work better or differently.
   An improvement that changes behaviour a user relied on also goes in
   Heads up.
6. **Bug fixes list only bugs that shipped in an earlier release.** A bug found
   and fixed inside the same release (in a feature that is new in it) never
   reached anyone — listing it is noise and makes the release look worse than
   it is. Check: does the bug's code exist in the previous tag?
7. **Every bug fix links its issue** when there is one. Describe the symptom the
   user saw ("the viewer closed after a failed pin"), not the cause.
8. **Group, don't enumerate.** Five PRs that together make the task board better
   are one or two lines, not five.
9. **Platform tags** in parentheses only when an item is platform-specific:
   "(Linux)", "(Windows preview)".
10. **Order by how many users it touches**, most first, within each section.
11. **Plain words, present tense, no hype, no emoji.** British/American mix is
    fine; consistency within a release is not optional.
12. Small releases are short. A patch with one fix is one line under Bug fixes.

## How to gather the material

```bash
PREV=vX.Y.Z-previous; NEXT=origin/develop   # or the new tag when rewriting
git log --oneline --first-parent "$PREV..$NEXT"            # merged PRs
gh pr list --repo raffaelefarinaro/ciaobot --state merged --base develop \
  --search "merged:>=<date of PREV>" --limit 200 --json number,title,body,closingIssuesReferences
gh issue list --repo raffaelefarinaro/ciaobot --state closed \
  --search "closed:>=<date of PREV>" --limit 200 --json number,title,labels
```

Read each PR's body for what changed for the user, and each issue for the
symptom. For rule 6, `git show $PREV:<path>` tells you whether the broken code
had shipped. When rewriting a past release, use that release's own range
(`vPREV..vX.Y.Z`) and its tag date, and do not mention anything that landed
after it.

## Hand-off

Write the body to a file (for a cut: `$TMPDIR/release-notes-vX.Y.Z.md`), show it
to the operator, and only then hand it to `ciao.release --notes-file`. For a past
release, the same file updates both the `## vX.Y.Z` section in `CHANGELOG.md`
(keep its heading line) and the GitHub release (`gh release edit vX.Y.Z
--notes-file <file>` — the release body *includes* the heading, as the workflow
writes it).
