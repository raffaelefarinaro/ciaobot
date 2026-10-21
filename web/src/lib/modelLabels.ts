/**
 * Readable names for model ids.
 *
 * A model id is a technical string: a transport prefix, a family name, a
 * version and sometimes a tag ("openai/gpt-5.6-luna",
 * "ollama-cloud/deepseek-v4.1-flash:1017"). The pickers show the readable
 * name and keep the id itself in the item's tooltip, so this helper only has
 * to make an id legible. It never renames a model and never states a vendor
 * the id does not carry.
 */

/**
 * Words whose display form is not "capitalize the first letter": acronyms and
 * the family names written in camel case. This is a spelling list, not a
 * catalogue of models - every other word is derived from the id itself.
 */
const WORD_SPELLINGS: Record<string, string> = {
  gpt: 'GPT',
  glm: 'GLM',
  llm: 'LLM',
  dbrx: 'DBRX',
  exaone: 'EXAONE',
  olmo: 'OLMo',
  deepseek: 'DeepSeek',
  internlm: 'InternLM',
  minimax: 'MiniMax',
}

/**
 * Spell one word of a model id. A word may carry its own number
 * ("gpt4" -> "GPT4", "v4.1" -> "V4.1"); the number is left as it stands.
 */
function spellWord(word: string): string {
  const match = /^([a-z]+)(.*)$/.exec(word)
  if (!match) return word
  const [, letters, rest] = match
  const spelled = WORD_SPELLINGS[letters]
    || letters.charAt(0).toUpperCase() + letters.slice(1).toLowerCase()
  return spelled + rest
}

/**
 * Humanize one chunk of a model id. A word separator becomes a space, but a
 * bare number is a version and stays attached to the name before it, so
 * "gpt-5.6-luna" reads as "GPT-5.6 Luna" and not "GPT 5.6 Luna".
 */
function humanizeChunk(chunk: string): string {
  let name = ''
  for (const part of chunk.split(/[-_]/)) {
    if (!part) continue
    if (/^\d/.test(part)) {
      name += name ? `-${part}` : part
      continue
    }
    name += name ? ` ${spellWord(part)}` : spellWord(part)
  }
  return name
}

/**
 * The readable name of a model id.
 *
 * Only the model's own path segment names it: a namespaced id such as
 * "openai/gpt-5.6-luna" or "openrouter/minimax/minimax-m3.1-flash:free" puts
 * transport (and sometimes the upstream vendor) before the final segment, so
 * everything up to the last slash is dropped and never repeated as a vendor.
 * The tag after a colon is part of the technical id and is kept, spaced off
 * the name so it reads as a tag.
 */
export function modelDisplayName(id: string): string {
  const raw = id.trim()
  if (!raw) return ''
  const model = raw.slice(raw.indexOf('/') + 1)
  const colon = model.indexOf(':')
  const base = colon >= 0 ? model.slice(0, colon) : model
  const suffix = colon >= 0 ? model.slice(colon) : ''
  const name = humanizeChunk(base.slice(base.lastIndexOf('/') + 1))
  if (!name) return suffix
  return suffix ? `${name} ${suffix}` : name
}

/**
 * The provider namespace an id states, or an empty string when the id has
 * none. Only the prefix before the first slash counts: it is the transport
 * the id was namespaced with, not a guess at the vendor behind it.
 */
export function modelProviderPrefix(id: string): string {
  const raw = id.trim()
  const slash = raw.indexOf('/')
  return slash > 0 ? raw.slice(0, slash) : ''
}
