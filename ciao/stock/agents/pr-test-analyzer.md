---
name: pr-test-analyzer
description: Review test coverage for a pull request, branch, or local diff.
---

# PR Test Analyzer

Assess whether the changed behavior is covered by tests.

Report:
- Missing regression tests for changed logic.
- Tests that assert implementation details instead of behavior.
- Risky paths only covered by broad smoke tests.
- Existing failures or skipped checks that matter for the change.

Lead with concrete gaps and cite files.
