---
name: web-research
description: Reliable web lookup workflow using provider-native web search plus defuddle (with optional Scrapling fallback) for source verification.
---

# Web Research

Use this skill for web questions and URL verification.

## Tool sequence

1. Discover
- Start with provider-native web search (OpenRouter web plugin when available) for candidate sources.

2. Read
- For **GitHub URLs** (`github.com/...`, `gist.github.com/...`), use `gh` CLI instead of defuddle. It hits the API directly: cleaner output, fewer tokens, and it works on private repos. See the GitHub URL mapping below.
- For all other URLs, use `defuddle parse <url> --md`. This is the default for the rest of the web; it strips clutter and reduces token usage.
- If defuddle returns empty or stub content (JS-rendered pages, SPAs, login-gated content, dashboards), fall back to Scrapling when it is installed (`command -v scrapling`):
  1. `scrapling extract get '<url>' /tmp/page.md --impersonate chrome` — fast HTTP fetch with browser impersonation, then read the output file.
  2. If still empty or stub, `scrapling extract fetch '<url>' /tmp/page.md` — renders the page in a headless browser, so it handles JS-only content. Slower; use it only after step 1 fails.
- If Scrapling is not installed or both attempts fail, say so clearly and ask the user to paste the content or try from a machine where they can access it. (Scrapling is an optional install: `pip install 'scrapling[fetchers]' && scrapling install`.)
- Only fall back to WebFetch for non-HTML targets (API endpoints, raw files, binary content).

### GitHub URL mapping

| URL pattern | Command |
|---|---|
| `github.com/owner/repo` | `gh repo view owner/repo` |
| `…/issues/N` | `gh issue view N --repo owner/repo --comments` |
| `…/pull/N` | `gh pr view N --repo owner/repo --comments` |
| `…/pull/N/files` | `gh pr diff N --repo owner/repo` |
| `…/blob/<ref>/<path>` | `gh api "repos/owner/repo/contents/<path>?ref=<ref>" -H "Accept: application/vnd.github.raw"` |
| `…/commit/<sha>` | `gh api repos/owner/repo/commits/<sha>` |
| `…/releases/tag/<tag>` | `gh release view <tag> --repo owner/repo` |
| `…/actions/runs/<id>` | `gh run view <id> --repo owner/repo --log` |
| `gist.github.com/<user>/<id>` | `gh gist view <id>` |

For search across GitHub (code, issues, PRs, repos), use `gh search code|issues|prs|repos ...` instead of defuddling the search UI.

3. Verify
- Prefer official docs and primary sources.
- If source is weak or unavailable, say so clearly.

4. Cite
- Include source URLs in the answer.

## Quality bar

- Do not answer from memory when a source is required.
- Avoid over-fetching; use focused queries.

## Bash command usage

This skill is guidance-only — it has no bash-executable component.
- **Do NOT** pass a raw search query as the bash `command` argument.
- Web search is handled automatically by the provider (OpenRouter web plugin) — just reason about what to search and results will appear.
- For reading specific URLs, use `defuddle parse <url> --md` via Bash, not `fetch_url`.
- **Never use `curl`, `wget`, or raw Python HTTP requests for HTML pages in Bash.** These frequently return gzip-encoded bytes that cause `UnicodeDecodeError` when decoded as UTF-8. Use `defuddle` instead; it handles compression and encoding correctly.
- **Do not use WebFetch for HTML pages.** It is reserved for non-HTML targets (API endpoints, raw files, binary content).
