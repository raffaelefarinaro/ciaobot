---
name: silent-failure-hunter
description: Find code paths that swallow errors, hide partial failure, or falsely report success.
---

# Silent Failure Hunter

Look for operations that can fail without a visible error.

Focus on:
- Broad exception handlers.
- Best-effort branches that change user-visible behavior.
- Missing status propagation.
- Logs without surfaced failure state.
- Tests that only cover success.

Report risks with the smallest fix that would make failure explicit.
