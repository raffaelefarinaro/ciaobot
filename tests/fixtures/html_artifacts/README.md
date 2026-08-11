# HTML artifact fixtures (manual checks)

`tests/test_workspace_html.py` asserts the response headers. Those tests pass
even when the panel shows a blank white rectangle, which is exactly how a CSP
mistake presents. These three fixtures cover what the automated tests cannot.

Each page reports its own verdict as PASS or FAIL text, so a glance is enough.

| Fixture | Expected |
|---|---|
| `01-inline-script.html` | Green PASS. Inline script ran, inline style applied, the button counts. Anything else means the artifact CSP is too strict and every artifact is broken. |
| `02-external-requests.html` | Three green PASS lines and three CSP violations in devtools. A FAIL means a remote script, stylesheet, or image loaded. |
| `03-api-and-session.html` | Four green PASS lines. This is the one that matters: a FAIL means model-authored script can reach `/api/*`, the session cookie, the app's `localStorage`, or the embedding page. |

## How to run them

1. In a Ciaobot chat, `file_surface` the fixture (or paste its path into the panel):
   `tests/fixtures/html_artifacts/01-inline-script.html`
2. Read the verdicts in the Preview pane.
3. Open devtools and confirm the expected CSP violations for fixture 2.

Run all three in each surface, because the frame plumbing differs:

- Chrome desktop
- Safari desktop
- the iOS PWA in standalone display mode (installed to the home screen)
- the desktop app shell

## Results

- **Chromium 131 (headless, 2026-08-11): all three PASS.** Inline script and style ran; external script, stylesheet, and image were blocked; `fetch('/api/projects')` failed with `TypeError`, and `document.cookie`, `localStorage`, and `window.parent.location` all threw `SecurityError`. Run against the real route plus `SecurityHeadersMiddleware`, framed with `sandbox="allow-scripts"` exactly as the component does.
- Safari desktop, iOS standalone, and the desktop shell: not yet run.

iOS standalone and the desktop shell are the two that cannot be inferred from
the others: the PWA's viewport and layer plumbing is unusual there (see
`web/README.md`), and the shell embeds the app in its own webview.
