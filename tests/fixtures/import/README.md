# Import fixtures (synthetic)

Fixtures for `tests/test_import_sources_contract.py`,
`tests/test_import_sources_claude_code.py` and
`tests/test_import_sources_opencode.py`. **Every byte here is synthetic**: the
uuids, the session ids, the paths and the conversation text were written for
these tests. There is no real conversation, no real person's history, no real
project and nothing that could be a credential in this directory — a test must be
able to read all of it without reading anybody's data.

The Claude Code adapter reads real history at
`~/.claude/projects/<slug>/<sid>.jsonl` (see
`ciao.agent_paths.claude_projects_dir`), which is why these files are JSONL in
that shape: one JSON object per line, `uuid` linking to `parentUuid`.

The OpenCode adapter reads no file at all. An OpenCode session lives in a
running server's database, and the only supported way in is the V2 CLI, so its
fixtures are **what the CLI writes to stdout**: `opencode session export <id>`
prints one `{info, messages}` object, and `opencode session list --format json`
prints a flat array of `{id, title, updated, created, projectId, directory}`.
The shape is `packages/schema/src/session-transfer.ts` at `sst/opencode` tag
`v2.0.16` — the enforced floor — and `time.created` is **epoch milliseconds**,
because `DateTimeUtcFromMillis` encodes to a number. Message ids are `msg_…` and
session ids are `ses_…`, as OpenCode mints them.

| Fixture | What it is for |
|---|---|
| `claude_code_session_minimal.jsonl` | The straight case: four entries, one branch, roles `user`/`assistant`/`user`/`assistant`. Two of them carry content blocks that are not text — a `tool_use` and a `tool_result` — so the tool-less reading and the `non_text_content` omission are visible. The tool result arrives as a `user` entry with no prose, which is what an empty `text` beside a recorded omission means. |
| `claude_code_session_branched.jsonl` | Two leaves and a compaction. Entries `…007` and `…008` are both leaves of the same parent (`…006`), so the file has a fork; `…008` is later in the file and flagged `isSidechain`, which is the case a branch rule has to get right (a latest-indexed rule alone would import a subagent's turn). Entry `…003` is a `compact_boundary` with no `parentUuid` and a `logicalParentUuid` pointing back at `…002`, so the pre-compaction half of the conversation is only reachable through that link. |
| `opencode_export_minimal.json` | The smallest payload `Data = {info, messages}` admits: one `user` message and one `assistant` message whose `content[]` holds one `text` part. Nothing is omitted, which is the baseline the other two are read against. |
| `opencode_export_tool_and_compaction.json` | An assistant turn carrying a `text` part, a `reasoning` part and a `tool` part whose `state` holds both an input and an output; a `shell` record with its output; a `compaction` record with a summary; and a `user` turn with a `files` attachment. Five things that are not prose and must each be counted — three `non_text_content`, two `other_entry_type`. |
| `opencode_export_unsettled.json` | Two settled turns and one assistant turn with no `time.completed`, which `isSettled` in `packages/core/src/session/transfer.ts` would have filtered. The CLI normally drops it entirely, so this fixture is how the adapter's own application of the rule is pinned: no message, one counted omission. |
| `opencode_list_page.json` | What `opencode session list --format json` prints: three rows, one project holding two of them. Metadata only, so discovery has nothing to leak into a test. |

## The oversized cases are generated, not stored

There is no `claude_code_session_oversized.jsonl` and no
`opencode_export_oversized.json`. A payload genuinely over
`ciao.import_sources.contract.MAX_SESSION_BYTES` is a multi-megabyte file to
check into a repository, so the tests that need one build it in place instead:
`test_oversize_read_stops_at_a_message_boundary` writes a JSONL file into
`tmp_path` (a real session prefix, then one assistant entry padded past the cap,
then more entries after it), and
`test_an_export_over_the_cap_is_truncated_and_never_parsed` hands the bounded
runner a JSON document padded past the same cap. The assertions are the ones that
matter — the read stops on a whole line (or, for the export, not at all, because
one JSON object has no whole-message boundary), `truncated` is set, a `truncated`
omission is recorded, and nothing past the cap is in the messages.

## Writing a new one

For Claude Code: keep an entry's `uuid` stable and unique, link it with
`parentUuid` (and `logicalParentUuid` only where a compaction is what you are
testing), and give every non-message entry a `type` of `progress`, `system` or
`attachment` so it lands in the `other_entry_type` omission rather than looking
like a turn.

For OpenCode: keep message ids `msg_`-prefixed and unique and session ids
`ses_`-prefixed, write `time.created` as **epoch milliseconds** rather than an ISO
string (that is what the CLI writes), give an assistant turn its prose in
`content[]` parts tagged `text` and its tool traffic in parts tagged `tool`, and
add `time.completed` to an assistant turn only if it really finished. Never paste
a real export here — they carry paths, titles and prose that belong to whoever was
using the machine.