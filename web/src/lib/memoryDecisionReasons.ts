/** Quick reasons offered on a memory proposal's decision card (#1734).
 *
 * Plain text composed by the shared `DecisionReason` as comma-separated items,
 * so no label may contain a comma. */
export const MEMORY_REASON_CHIPS = [
  'already known',
  'tool quirk — belongs in a skill',
  'one-off',
  'wrong place',
  'not true',
  'yes everywhere',
] as const
