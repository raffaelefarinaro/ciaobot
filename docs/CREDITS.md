# Credits

Ciaobot is glue around a lot of excellent open tools. Licences are the
upstream's own; code copied into this repository is listed with its licence
text in [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md).

## Agents and adapters

Ciaobot drives the agent CLIs installed on your machine; it bundles none of them.

- [Claude Code](https://github.com/anthropics/claude-code) through [claude-agent-acp](https://github.com/agentclientprotocol/claude-agent-acp) (Apache-2.0), which installs the [Claude Agent SDK for TypeScript](https://github.com/anthropics/claude-agent-sdk-typescript) with the adapter (npm licence: `SEE LICENSE IN README.md`)
- [Codex CLI](https://github.com/openai/codex) through [codex-acp](https://github.com/agentclientprotocol/codex-acp) (Apache-2.0)
- [OpenCode](https://opencode.ai) through `opencode acp`
- [Agent Client Protocol Python SDK](https://github.com/agentclientprotocol/python-sdk) (Apache-2.0)
- [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python) (MIT): the Claude session-file reader is ported from it

## Integrations and CLIs

- [NotebookLM CLI (`notebooklm-py`)](https://pypi.org/project/notebooklm-py/): optional workspace integration
- [opencli](https://www.npmjs.com/package/@jackwener/opencli): optional website CLI
- [LibreOffice](https://www.libreoffice.org): `.pptx` slide rendering
- [Tailscale](https://tailscale.com): remote access through `tailscale serve`
- [AnyDoc](https://github.com/firecrawl/anydoc) (MIT): document conversion to Markdown

## Engine (Python)

- [Starlette](https://starlette.dev) and [Uvicorn](https://uvicorn.dev) (BSD-3-Clause), [HTTPX](https://www.python-httpx.org) (BSD-3-Clause)
- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) (MIT)
- [pywebpush](https://github.com/web-push-libs/pywebpush) (MPL-2.0), [cryptography](https://cryptography.io) (Apache-2.0 or BSD-3-Clause), [itsdangerous](https://itsdangerous.palletsprojects.com) (BSD-3-Clause)
- [PyYAML](https://pyyaml.org) (MIT), [python-dotenv](https://github.com/theskumar/python-dotenv) (BSD-3-Clause), [markdown-it-py](https://github.com/executablebooks/markdown-it-py) (MIT), [tzdata](https://github.com/python/tzdata) (Apache-2.0)

## Web app

- [Vue](https://vuejs.org), [Vue Router](https://router.vuejs.org), [Pinia](https://pinia.vuejs.org), [VueUse](https://vueuse.org) and [Reka UI](https://reka-ui.com) (MIT)
- [Vite](https://vite.dev) (MIT)
- [xterm.js](https://xtermjs.org) (MIT): the browser terminal
- [VueFinder](https://github.com/n1crack/vuefinder) (MIT): the folder browser
- [marked](https://marked.js.org) (MIT) and [DOMPurify](https://github.com/cure53/DOMPurify) (MPL-2.0 or Apache-2.0), [Papa Parse](https://www.papaparse.com) (MIT), [qrcode-generator](https://github.com/kazuhikoarase/qrcode-generator) (MIT)

## Mac app and installers

- [Sparkle](https://sparkle-project.org) (MIT): Mac app updates
- [python-build-standalone](https://github.com/astral-sh/python-build-standalone): the Mac app's bundled Python
- [uv](https://github.com/astral-sh/uv) (MIT or Apache-2.0): the Linux installer

The PWA, site and Mac app use system fonts and ship no font files or
third-party icon sets.
