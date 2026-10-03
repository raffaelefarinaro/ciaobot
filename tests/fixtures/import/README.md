# Import fixtures (synthetic)

Fixtures for `tests/test_import_sources_contract.py` and
`tests/test_import_sources_claude_code.py`. **Every byte here is synthetic**:
the uuids, the session ids, the paths and the conversation text were written
for these tests. There is no real conversation, no real person's history, no
real project and nothing that could be a credential in this directory — a test
must be able to read all of it without reading anybody's data.

The adapter reads real history at `~/.claude/projects/<slug>/<sid>.jsonl`
(see `ciao.agent_paths.claude_projects_dir`), which is why these files are
JSONL in that shape: one JSON object per line, `uuid` linking to `parentUuid`.

| Fixture | What it is for |
|---|---|
| `claude_code_session_minimal.jsonl` | The straight case: four entries, one branch, roles `user`/`assistant`/`user`/`assistant`. Two of them carry content blocks that are not text — a `tool_use` and a `tool_result` — so the tool-less reading and the `non_text_content` omission are visible. The tool result arrives as a `user` entry with no prose, which is what an empty `text` beside a recorded omission means. |
| `claude_code_session_branched.jsonl` | Two leaves and a compaction. Entries `…007` and `…008` are both leaves of the same parent (`…006`), so the file has a fork; `…008` is later in the file and flagged `isSidechain`, which is the case a branch rule has to get right (a latest-indexed rule alone would import a subagent's turn). Entry `…003` is a `compact_boundary` with no `parentUuid` and a `logicalParentUuid` pointing back at `…002`, so the pre-compaction half of the conversation is only reachable through that link. |

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