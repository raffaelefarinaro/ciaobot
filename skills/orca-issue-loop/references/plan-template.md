# Issue plan template

The issue body. Written for an implementer that follows instructions literally and does not explore. Every step names a file and an anchor (function/class name, plus line number at the time of writing). Delete sections that don't apply; never leave placeholders.

```markdown
## Problem
<Bug: observed vs expected, repro steps, relevant log lines. Feature: the user-facing need in 2–4 sentences.>

## Root cause / design
<Bug: the exact cause, with file:line. Feature: the chosen approach and why, and the alternative rejected.>

## Implementation plan
Base: `develop` @ <sha7>. Anchors are line numbers at that commit — search by the named symbol if they drifted.

1. **`ciao/<file>.py` — `<function>` (~L<n>)**
   - Change: <exactly what; include signatures, constant names, return shapes>.
   - Snippet (when the shape matters):
     ```python
     def foo(bar: str) -> Baz: ...
     ```
2. **`web/src/<file>.vue` — `<component/function>` (~L<n>)**
   - Change: …
3. …

## Tests
- `tests/test_<x>.py::test_<name>` — new — asserts <behavior>.
- `tests/test_<y>.py::test_<name>` — update — <why it changes>.
- `web/src/**/__tests__/<x>.test.ts` — `it("<name>")` — new — asserts <behavior>.
Do not modify other existing tests. If one breaks, stop and report it instead of editing it.

## Verify
```bash
PYTHONPATH=$PWD ~/repos/ciaobot/.venv/bin/python -m pytest tests/test_<x>.py -q
~/repos/ciaobot/.venv/bin/mypy ciao
cd web && npm test -- <file>        # if web/ changed
```

## Acceptance criteria
- [ ] <observable behavior 1>
- [ ] <observable behavior 2>
- [ ] All gates in AGENTS.md green.

## Out of scope
- <things the implementer must not touch, including tempting refactors>

## Notes for the implementer
- <gotchas: repo conventions, similar code to copy from (file:line), traps from memory>
```
