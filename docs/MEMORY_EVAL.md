# Memory retrieval eval

Two halves keep the memory system honest:

## 1. Deterministic suite (CI)

`tests/test_memory_eval.py` runs LongMemEval-style cases over a synthetic
fixture vault — paraphrase recall (aliases + the OR fallback), knowledge
updates (supersession keeps both versions findable, note names the current
one), abstention (an unknown topic returns nothing, never noise),
bookkeeping isolation (the proposals queue can never outrank knowledge), and
temporal aging (`[as-of:]` facts age at 90 days, learned-at stamps at 180).
It never calls a model and never touches user data. Extend it whenever a
recall failure is observed in the wild: reproduce the failure as a fixture
case first, then fix.

## 2. Manual probe runbook (live vault, sandboxed)

Spot-check a real install without touching its live FTS database or
LaunchAgents (a dev-checkout run against live state can destroy both):

```bash
export CIAO_MEMORY_DIR=$(mktemp -d)          # fresh index, isolated from ~/.ciao
export CIAO_WORKSPACE=<install workspace>           # the install root
cd <ciaobot checkout>
.venv/bin/python -m ciao.cli vault-search \
  --vault-root <workspace>/<agent-root>/memory-vault \
  --limit 3 "<probe query>"
```

Probe set (adjust entities to the vault under test):

| Class | Example probe | Pass condition |
| --- | --- | --- |
| Entity | a person's first name | their `People/` note in top 3 |
| Paraphrase | a relationship term, a paraphrased question | the aliased note found |
| Update | a fact known to have changed | note snippet names the current value |
| Abstention | a topic the vault cannot know | zero results |
| Isolation | any query | no `Memory-Proposals`/`Curation-Log` in results |

Record failures as new fixture cases in `tests/test_memory_eval.py`, and fix
recall by adding `aliases:` frontmatter to the note (curation's job) before
reaching for engine changes.
