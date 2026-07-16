# Compact chat activity design

## Goal

Reduce the visual weight of completed reasoning traces while keeping exact activity details and generated files accessible. Make token usage labels understandable without relying on abbreviations.

## Approved behavior

- Completed traces use the label `Activity`, remain collapsed by default, and fit on one compact row.
- The collapsed metadata keeps exact categories such as `2 tool calls · 2 files`. It does not invent a combined step count.
- Files touched during the turn appear as an `Outputs` row below the final assistant answer, not inside the collapsed activity container.
- Expanding `Activity` preserves the existing chronological notes, tool calls, file cards, and subagent information.
- The trace header is a real button with keyboard operation and an expanded-state announcement.
- Assistant footer usage changes from `in:2 out:1079` to `tokens in: 2 · tokens out: 1079`.
- Live `Working…` behavior remains unchanged.

## Data flow

The frontend already receives `_filecard` messages with file paths and action metadata. During `renderItems` construction, files collected from the trace preceding a final assistant message are attached to that assistant render item. No API or persisted transcript format changes are required.

## Error and edge cases

- A turn without a final answer keeps its files in the trace so interrupted work does not disappear.
- Duplicate paths within one turn appear once in the output row.
- Trailing bookkeeping activity after the final answer remains a trace and is not treated as an answer output unless it contains a file card.
- Missing input or output token values are omitted individually.

## Testing

- Unit-test pure formatting and file-deduplication helpers before integrating them into `ChatPanel.vue`.
- Verify the focused frontend tests.
- Run the production frontend build for TypeScript and template validation.
