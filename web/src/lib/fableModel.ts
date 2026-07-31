// The Codex "fable" tier has no model of its own yet. The picker shows it as a
// model entry, but it stores as a real model plus a thinking level, so the two
// spellings below describe the same chat state and both have to be recognised.
export const CODEX_FABLE_PSEUDO_MODEL = 'gpt-5.6-sol-ultra'
export const CODEX_FABLE_REAL_MODEL = 'gpt-5.6-sol'
export const CODEX_FABLE_LEVEL = 'ultra'

/** True when the chat is on fable, under either spelling. */
export function isFableSelection(
  model: string | undefined | null,
  level: string | undefined | null,
): boolean {
  if (model === CODEX_FABLE_PSEUDO_MODEL) return true
  return model === CODEX_FABLE_REAL_MODEL && level === CODEX_FABLE_LEVEL
}

/**
 * Which picker entry a chat currently sits on.
 *
 * Fable has no model id of its own, so a fable chat reports the real model and
 * looks identical to it. Comparing raw model ids therefore makes "switch from
 * fable to the plain model" read as a no-op. Callers pass the already-resolved
 * canonical tier for the non-fable case.
 */
export function selectedModelEntry(
  model: string | undefined | null,
  level: string | undefined | null,
  canonical: string,
): string {
  return isFableSelection(model, level) ? CODEX_FABLE_PSEUDO_MODEL : canonical
}

/**
 * Thinking levels to offer as chips for `model`.
 *
 * Fable owns real-model + `ultra`, and selecting fable hides the chips (it is a
 * model choice, not a level). Offering `ultra` as a chip on the real model would
 * let the user walk into that state from the chips and leave no chip to walk
 * back out with. Fable stays reachable as its own entry in the model list.
 */
export function selectableThinkingLevels(model: string, levels: string[]): string[] {
  if (model !== CODEX_FABLE_REAL_MODEL) return levels
  return levels.filter((level) => level !== CODEX_FABLE_LEVEL)
}
