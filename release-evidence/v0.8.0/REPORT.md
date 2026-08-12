# Release evidence: 0.8.0

This is a sanitized public scorecard. It contains aggregate behavior and structural changes, not prompts, answers, vault contents, or raw tool payloads.

## Why

See CHANGELOG.md and the release PR for the release rationale.

## What changed

- **skills:** +1, -0, ~1
- **agents:** +0, -0, ~0
- **commands:** +0, -0, ~0
- **mcp_tools:** +0, -0, ~0
- **memory:** +4, -0, ~0

## Measured behavior

| Scenario | Provider | Mode | Pass | Median ms | Median context % |
| --- | --- | --- | ---: | ---: | ---: |
| capabilities-hierarchy | claude | cold | 3/3 | 18422.0 | n/a |
| capabilities-hierarchy | claude | restart | 3/3 | 18122.0 | n/a |
| capabilities-hierarchy | claude | warm | 3/3 | 12557.0 | n/a |
| capabilities-hierarchy | codex | cold | 3/3 | 30939.0 | 39.6 |
| capabilities-hierarchy | codex | restart | 3/3 | 22393.0 | 39.1 |
| capabilities-hierarchy | codex | warm | 3/3 | 15428.0 | 75.1 |
| researcher-delegation | claude | cold | 3/3 | 16399.0 | n/a |
| researcher-delegation | claude | restart | 2/3 | 14377.0 | n/a |
| researcher-delegation | claude | warm | 3/3 | 13754.0 | n/a |
| researcher-delegation | codex | cold | 3/3 | 34747.0 | 44.2 |
| researcher-delegation | codex | restart | 3/3 | 32278.0 | 44.2 |
| researcher-delegation | codex | warm | 3/3 | 13258.0 | 70.9 |
| vault-recall-and-persistence | claude | cold | 0/3 | 8166.0 | n/a |
| vault-recall-and-persistence | claude | restart | 0/3 | 8681.0 | n/a |
| vault-recall-and-persistence | claude | warm | 3/3 | 8154.0 | n/a |
| vault-recall-and-persistence | codex | cold | 2/3 | 8187.0 | 61.6 |
| vault-recall-and-persistence | codex | restart | 2/3 | 10745.0 | 66.6 |
| vault-recall-and-persistence | codex | warm | 2/3 | 7957.0 | 100.0 |
