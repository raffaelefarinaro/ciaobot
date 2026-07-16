# Compact Chat Activity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make completed activity traces compact, surface turn outputs below the final answer, and spell out token usage labels.

**Architecture:** Add small pure helpers for output-file collection and token-label formatting, then use them from the existing `ChatPanel.vue` render walk. The transcript and API formats stay unchanged. Only the always-visible file chips move; expanded activity retains its chronological file cards.

**Tech Stack:** Vue 3, TypeScript, Pinia, Vitest, Vue Test Utils, scoped CSS

---

### Task 1: Add chat activity helpers test-first

**Files:**
- Create: `web/src/lib/chatActivity.ts`
- Create: `web/src/lib/chatActivity.test.ts`

- [ ] **Step 1: Write failing tests for output collection and token labels**

```ts
import { describe, expect, it } from 'vitest'
import { collectTraceOutputs, formatTokenUsage } from './chatActivity'

describe('collectTraceOutputs', () => {
  it('returns each file path once in first-seen order', () => {
    expect(collectTraceOutputs([
      { tool_name: '_activity', content: 'Edit draft.md' },
      { tool_name: '_filecard', file_path: 'draft.md', content: '' },
      { tool_name: '_filecard', file_path: 'draft.md', content: '' },
      { tool_name: '_filecard', file_path: 'brief.md', content: '' },
    ])).toEqual([{ file_path: 'draft.md' }, { file_path: 'brief.md' }])
  })
})

describe('formatTokenUsage', () => {
  it('spells out input and output token labels', () => {
    expect(formatTokenUsage({ input_tokens: '2', output_tokens: '1079' }))
      .toBe('tokens in: 2 · tokens out: 1079')
  })

  it('omits a missing side without hiding the available value', () => {
    expect(formatTokenUsage({ output_tokens: '1079' })).toBe('tokens out: 1079')
  })
})
```

- [ ] **Step 2: Run the tests and verify the missing module fails**

Run: `cd web && npm test -- src/lib/chatActivity.test.ts`
Expected: FAIL because `./chatActivity` does not exist.

- [ ] **Step 3: Add the minimal helpers**

```ts
import type { ChatMessage } from './types'

export type TraceOutput = { file_path: string }

export function collectTraceOutputs(
  steps: Pick<ChatMessage, 'tool_name' | 'file_path' | 'content'>[] | undefined,
): TraceOutput[] {
  const seen = new Set<string>()
  const outputs: TraceOutput[] = []
  for (const step of steps || []) {
    if (step.tool_name !== '_filecard') continue
    const filePath = step.file_path || step.content
    if (!filePath || seen.has(filePath)) continue
    seen.add(filePath)
    outputs.push({ file_path: filePath })
  }
  return outputs
}

export function formatTokenUsage(usage?: Record<string, string>): string {
  const parts: string[] = []
  if (usage?.input_tokens) parts.push(`tokens in: ${usage.input_tokens}`)
  if (usage?.output_tokens) parts.push(`tokens out: ${usage.output_tokens}`)
  return parts.join(' · ')
}
```

- [ ] **Step 4: Run the focused helper tests**

Run: `cd web && npm test -- src/lib/chatActivity.test.ts`
Expected: PASS.

### Task 2: Integrate compact Activity and answer outputs

**Files:**
- Modify: `web/src/components/ChatPanel.vue:147-170, 288-318, 770-776, 2011-2034, 2140-2210, 3223-3365`

- [ ] **Step 1: Import `collectTraceOutputs`, `formatTokenUsage`, and `TraceOutput`; add optional `outputs` to assistant and trace render items.**
- [ ] **Step 2: Make the completed trace summary a full-width `<button>` with `aria-expanded`, label it `Activity`, and render always-visible chips only from `item.outputs`. Keep the live trace label and behavior unchanged.**
- [ ] **Step 3: In `flushTurn`, collect file cards from the whole buffered turn. Attach them to the final assistant item when one exists; otherwise attach them to the trace item.**
- [ ] **Step 4: Render an `Outputs` group below an assistant message when `item.outputs` is non-empty, reusing the existing file-chip control and viewer action.**
- [ ] **Step 5: Replace the abbreviated footer expression with `formatTokenUsage(item.msg.usage)`, retaining the existing vertical separator before the usage group.**
- [ ] **Step 6: Update scoped styles so the summary button has a 44px touch target, inherits typography, has visible focus, and the output group aligns below the assistant bubble without adding height to collapsed Activity.**
- [ ] **Step 7: Run the focused helper tests again.**

Run: `cd web && npm test -- src/lib/chatActivity.test.ts`
Expected: PASS.

### Task 3: Refresh frontend documentation

**Files:**
- Modify: `README.md`
- Modify: `web/README.md`

- [ ] **Step 1: Update the file-touch feature description to say completed work surfaces below the final answer while detailed activity stays expandable.**
- [ ] **Step 2: Add the compact Activity/output placement to the frontend interaction guidance. Confirm `INTEGRATIONS.md` and `PWA_API.md` need no change because no runtime option or API contract changes.**

### Task 4: Verify the complete change

- [ ] **Step 1: Run the complete frontend unit suite.**

Run: `cd web && npm test`
Expected: all Vitest suites pass with zero failures.

- [ ] **Step 2: Run the production frontend build.**

Run: `cd web && npm run build`
Expected: Vue TypeScript checking and Vite production build exit successfully.

- [ ] **Step 3: Review the final diff for scope, accessibility, and generated static assets. Do not restart the running Ciaobot service; hand deployment back to the user.**
