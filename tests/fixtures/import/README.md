# Import fixtures (synthetic)

Fixtures for `tests/test_import_sources_contract.py`,
`tests/test_import_sources_claude_code.py` and `tests/test_import_extract.py`.
**Every byte here is synthetic**: the uuids, the session ids, the paths and the
conversation text were written for these tests. There is no real conversation,
no real person's history, no real project and nothing that could be a credential
in this directory — a test must be able to read all of it without reading
anybody's data.

The adapter reads real history at `~/.claude/projects/<slug>/<sid>.jsonl`
(see `ciao.agent_paths.claude_projects_dir`), which is why these files are
JSONL in that shape: one JSON object per line, `uuid` linking to `parentUuid`.

| Fixture | What it is for |
|---|---|
| `claude_code_session_minimal.jsonl` | The straight case: four entries, one branch, roles `user`/`assistant`/`user`/`assistant`. Two of them carry content blocks that are not text — a `tool_use` and a `tool_result` — so the tool-less reading and the `non_text_content` omission are visible. The tool result arrives as a `user` entry with no prose, which is what an empty `text` beside a recorded omission means. |
| `claude_code_session_branched.jsonl` | Two leaves and a compaction. Entries `…007` and `…008` are both leaves of the same parent (`…006`), so the file has a fork; `…008` is later in the file and flagged `isSidechain`, which is the case a branch rule has to get right (a latest-indexed rule alone would import a subagent's turn). Entry `…003` is a `compact_boundary` with no `parentUuid` and a `logicalParentUuid` pointing back at `…002`, so the pre-compaction half of the conversation is only reachable through that link. |

## Extraction replies (model output, not source data)

`tests/test_import_extract.py` patches `run_oneshot`, so these three files are
the **replies** the patched model returns — hand-written arrays in the shape the
system prompt asks for, never captured from a provider. Their anchors
(`msg_0002`, `msg_9001`, …) are keys into the `NormalizedSession` each test
builds in the test file.

| Fixture | What it is for |
|---|---|
| `extraction_reply_valid.json` | Three well-formed rows: a `[memory]` fact, a `[memory]` fact whose source message carries a real date, and a `[profile]` fact. One filed row per accepted proposal. |
| `extraction_reply_injection.json` | The reply a model produced after reading `import_injection_transcript.txt`: three rows that turned the injected instructions into "facts" (a command to run, a region that must be rewritten, a fact attributed to a Ciaobot chat id with a forged `_(from: …)_` tag in its text), two rows whose `destination` is not a destination at all (a region name, a filesystem path), one row the transcript tried to date, and one ordinary fact. Every row is a proposal or nothing — see the test. |
| `extraction_reply_malformed.json` | A partially usable array: one good row, a row whose `text` is a number, a row that is a bare string, a row with no `text` at all, an unknown destination, and an anchor the session never carried. Five dropped, one filed, nothing raised. |

`import_injection_transcript.txt` is the untrusted side: a synthetic transcript
whose text tells the model to ignore its instructions, write to `AGENTS.md`,
promote to `ciao:memory`, run a command, mail somebody, delegate to another chat
and call an MCP server — and to date a fact `2026-01-01`. Nothing here is a
working command, a reachable host or a real address. `CHAT_ID` in the forged row
of `extraction_reply_injection.json` is the same synthetic chat id the test file
builds its sessions and refusals from; no Ciaobot chat was ever involved.

## The oversized case is generated, not stored

There is no `claude_code_session_oversized.jsonl`. A file that is genuinely over
`ciao.import_sources.contract.MAX_SESSION_BYTES` is a multi-megabyte file to
check into a repository, so
`test_oversize_read_stops_at_a_message_boundary` writes one into `tmp_path`
instead: a real session prefix, then one assistant entry padded past the cap,
then more entries after it. The assertions are the ones that matter — the read
stops on a whole line, `truncated` is set, a `truncated` omission is recorded,
and nothing after the cap is in the messages.

## Writing a new one

Keep an entry's `uuid` stable and unique, link it with `parentUuid` (and
`logicalParentUuid` only where a compaction is what you are testing), and give
every non-message entry a `type` of `progress`, `system` or `attachment` so it
lands in the `other_entry_type` omission rather than looking like a turn.