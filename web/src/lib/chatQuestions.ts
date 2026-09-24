// Parsing for the two question cards the chat can show: the provider's
// AskUserQuestion picker and the engine's image-capability prompt. Pure
// transforms over raw event payloads — the live state (`activeQuestions`,
// `activeCapabilityQuestions`, `resolvedQuestions`) stays in
// `stores/projects.ts`, which owns when a card appears and when it is cleared.

export type ActiveQuestionOption = { label: string; description?: string }

export type QuestionWhen = {
  key: string
  op: 'eq' | 'neq'
  value: string | number | boolean
}

export type ActiveQuestion = {
  id: string
  question: string
  header: string
  multiSelect: boolean
  allowOther: boolean
  isSecret: boolean
  requestId: string
  options: ActiveQuestionOption[]
  optionValues?: Record<string, string>
  /** V2 form semantics. Legacy AskUserQuestion payloads default required=true. */
  type?: string
  required?: boolean
  hidden?: boolean
  when?: QuestionWhen[]
  url?: string
  format?: string
  placeholder?: string
  pattern?: string
  default?: string | number | boolean | string[]
  minimum?: number
  maximum?: number
  minLength?: number
  maxLength?: number
  minItems?: number
  maxItems?: number
}

// Rendered when the engine pre-flights an image turn and the selected
// model cannot see images; the user picks a vision-capable model (switch),
// opens the full model picker, or cancels. Answered with a
// `capability_response` client message. Unlike `activeQuestions` there is
// no persisted copy on the chat — the question lives only for the
// in-flight turn, so it is never rebuilt on reload.
export type CapabilityCandidate = {
  id: string
  label: string
  supports_vision?: boolean
  disabled?: boolean
}

export type CapabilityQuestion = {
  request_id: string
  missing: string
  current_model: string
  candidates: CapabilityCandidate[]
  timeout_s: number
  opened_at: number
}

/**
 * Stable identity for a picker, computable identically from the live
 * `activeQuestions` entry (at resolve time) and from a rebuilt `pending_question`
 * (at rebuild time). Some providers carry a `requestId`; Claude's
 * picker has none, so fall back to the question content.
 */
export function questionsSignature(qs: ActiveQuestion[] | undefined): string {
  if (!qs || !qs.length) return ''
  const rid = qs[0]?.requestId
  if (rid) return `rid:${rid}`
  return `q:${qs.map(q => `${q.id}${q.question}`).join('')}`
}

/**
 * Parse the AskUserQuestion tool_input JSON (`{"questions": [...]}`) into the
 * picker's shape. Shared by the live `tool_use` handler and the reload-time
 * rebuild from a chat's persisted `pending_question`. Returns [] on anything
 * unparseable so callers can fall through to the generic trace path.
 */
export function parseQuestions(
  toolInput: string | null | undefined,
  requestId = '',
): ActiveQuestion[] {
  if (!toolInput) return []
  try {
    const parsed = JSON.parse(toolInput)
    if (!Array.isArray(parsed?.questions)) return []
    const resolvedRequestId = requestId || String(parsed?.request_id ?? '')
    if (parsed.questions.length === 0) {
      // Some provider turns emit the AskUserQuestion tool
      // with an empty questions array. Do not silently demote that event to
      // a trace row: surface a free-form response so the user can unblock
      // the turn and the provider still receives the native request id.
      return [{
        id: '__freeform__',
        question: 'The model needs your input. Enter a response to continue.',
        header: 'Response',
        multiSelect: false,
        allowOther: true,
        isSecret: false,
        requestId: resolvedRequestId,
        options: [],
        type: 'string',
        required: true,
        hidden: false,
        when: [],
      }]
    }
    // Claude Code's documented AskUserQuestion shape uses
    // `question`/`header`/`multiSelect`. Some providers (seen with
    // MiniMax via the Claude path) emit an alternate shape with
    // `text`/`type: single_select|multi_select` instead — accept both
    // so the picker prompt is never blank when the model did ask.
    return parsed.questions.map((q: Record<string, unknown>, index: number) => {
      const type = String(q.type ?? 'string').toLowerCase()
      const optionValues: Record<string, string> = {}
      const options = Array.isArray(q.options)
        ? (q.options as Array<Record<string, unknown>>).map(o => {
            const label = String(o.label ?? o.value ?? '')
            if (o.value !== undefined) optionValues[label] = String(o.value)
            return {
              label,
              description: o.description ? String(o.description) : '',
            }
          })
        : []
      // Boolean V2 fields have no option list in the wire form, but a pair of
      // explicit choices is clearer and keeps the answer mapping typed.
      if (type === 'boolean' && options.length === 0) {
        options.push(
          { label: 'Yes', description: '' },
          { label: 'No', description: '' },
        )
        optionValues.Yes = 'true'
        optionValues.No = 'false'
      }
      const required = q.required === undefined ? true : Boolean(q.required)
      const rawWhen = Array.isArray(q.when)
        ? q.when
        : q.when && typeof q.when === 'object' ? [q.when] : []
      const when = rawWhen
        .filter((condition): condition is Record<string, unknown> => Boolean(condition) && typeof condition === 'object')
        .map(condition => ({
          key: String(condition.key ?? ''),
          op: String(condition.op ?? 'eq') as 'eq' | 'neq',
          value: condition.value as string | number | boolean,
        }))
        .filter(condition => condition.key)
      const numeric = (key: string): number | undefined => {
        const value = q[key]
        return typeof value === 'number' && Number.isFinite(value) ? value : undefined
      }
      return {
        id: String(q.id ?? index),
        question: String(q.question ?? q.text ?? ''),
        header: String(q.header ?? q.title ?? ''),
        multiSelect: Boolean(q.multiSelect) || type === 'multi_select' || type === 'multiselect',
        allowOther: q.isOther === undefined
          ? true
          : Boolean(q.isOther) || !Array.isArray(q.options) || q.options.length === 0,
        isSecret: Boolean(q.isSecret),
        requestId: resolvedRequestId,
        options,
        optionValues,
        type,
        required,
        hidden: Boolean(q.hidden),
        when,
        url: q.url ? String(q.url) : undefined,
        format: q.format ? String(q.format) : undefined,
        placeholder: q.placeholder ? String(q.placeholder) : undefined,
        pattern: q.pattern ? String(q.pattern) : undefined,
        default: q.default as ActiveQuestion['default'],
        minimum: numeric('minimum'),
        maximum: numeric('maximum'),
        minLength: numeric('minLength'),
        maxLength: numeric('maxLength'),
        minItems: numeric('minItems'),
        maxItems: numeric('maxItems'),
      }
    })
  } catch {
    return []
  }
}

export type QuestionAnswerState = {
  selected: Set<string>
  other: string
  external?: boolean
}

function questionValues(question: ActiveQuestion, state: QuestionAnswerState | undefined): string[] {
  const values = [...(state?.selected ?? [])].map(
    label => question.optionValues?.[label] ?? label,
  )
  const other = state?.other.trim() ?? ''
  if (other) values.push(other)
  return values
}

/** Return a user-facing reason why a field cannot be submitted yet. */
export function questionAnswerError(
  question: ActiveQuestion,
  state: QuestionAnswerState | undefined,
): string | null {
  if (question.type === 'external') {
    return state?.external === true ? null : 'Acknowledge the external step to continue.'
  }

  const values = questionValues(question, state)
  if (!values.length) {
    return question.required === false ? null : 'This field is required.'
  }

  if (question.multiSelect || question.type === 'multiselect' || question.type === 'multi_select') {
    if (question.minItems !== undefined && values.length < question.minItems) {
      return `Choose at least ${question.minItems} item${question.minItems === 1 ? '' : 's'}.`
    }
    if (question.maxItems !== undefined && values.length > question.maxItems) {
      return `Choose at most ${question.maxItems} item${question.maxItems === 1 ? '' : 's'}.`
    }
    return null
  }

  const value = values[0] ?? ''
  if (question.type === 'number' || question.type === 'integer') {
    const number = Number(value)
    if (!Number.isFinite(number)) return 'Enter a valid number.'
    if (question.type === 'integer' && !Number.isInteger(number)) return 'Enter a whole number.'
    if (question.minimum !== undefined && number < question.minimum) {
      return `Enter a number of at least ${question.minimum}.`
    }
    if (question.maximum !== undefined && number > question.maximum) {
      return `Enter a number no greater than ${question.maximum}.`
    }
    return null
  }

  if (question.type === 'boolean') {
    const normalized = value.trim().toLowerCase()
    if (!['true', 'false', '1', '0', 'yes', 'no', 'on', 'off'].includes(normalized)) {
      return 'Choose Yes or No.'
    }
    return null
  }

  if (question.type === 'string' || question.type === undefined) {
    if (question.minLength !== undefined && value.length < question.minLength) {
      return `Use at least ${question.minLength} character${question.minLength === 1 ? '' : 's'}.`
    }
    if (question.maxLength !== undefined && value.length > question.maxLength) {
      return `Use at most ${question.maxLength} character${question.maxLength === 1 ? '' : 's'}.`
    }
    if (question.pattern) {
      try {
        if (!new RegExp(question.pattern).test(value)) return 'Enter a value in the requested format.'
      } catch {
        return 'This field has an invalid format.'
      }
    }
  }
  return null
}

export function questionAnswerIsValid(
  question: ActiveQuestion,
  state: QuestionAnswerState | undefined,
): boolean {
  return questionAnswerError(question, state) === null
}

/** Whether an empty optional value can be represented without violating the form schema. */
export function questionEmptyAnswerAllowed(question: ActiveQuestion): boolean {
  if (question.multiSelect || question.type === 'multiselect' || question.type === 'multi_select') {
    return (question.minItems ?? 0) <= 0
  }
  if (question.type === 'string' || question.type === undefined) {
    if (question.options.length > 0 && !question.allowOther) return false
    if ((question.minLength ?? 0) > 0) return false
    if (question.pattern) {
      try {
        return new RegExp(question.pattern).test('')
      } catch {
        return false
      }
    }
    return true
  }
  return false
}

/** Whether a V2 form field is active under the current answer map. */
export function questionIsVisible(
  question: ActiveQuestion,
  questions: ActiveQuestion[],
  answers: Record<number, QuestionAnswerState>,
): boolean {
  if (question.hidden) return false
  return (question.when ?? []).every(condition => {
    const index = questions.findIndex(candidate => candidate.id === condition.key)
    if (index < 0) return false
    const state = answers[index]
    // No state means the field is unanswered.  An existing state with an
    // empty string/list is an explicit empty answer, which the V2 form
    // service treats as defined for condition matching (`neq` is true).
    if (!state) return false
    const values: Array<string | number | boolean> = []
    for (const label of state.selected) {
      values.push(label)
      const optionValue = questions[index].optionValues?.[label]
      if (optionValue !== undefined) values.push(optionValue)
    }
    if (state.other.trim()) values.push(state.other.trim())
    if (!values.length) {
      const sourceQuestion = questions[index]
      const emptyIsDefined = sourceQuestion.type === 'multiselect'
        || (sourceQuestion.type === 'string' && (sourceQuestion.options.length === 0 || sourceQuestion.allowOther))
      if (!emptyIsDefined) return false
    }
    const hit = values.some(value => String(value) === String(condition.value))
    return condition.op === 'eq' ? hit : !hit
  })
}

export function parseCapabilityQuestion(event: {
  request_id: string
  missing?: string
  current_model?: string
  candidates?: Array<Record<string, unknown>>
  timeout_s?: number
}): CapabilityQuestion {
  return {
    request_id: event.request_id,
    missing: String(event.missing ?? 'image_input'),
    current_model: String(event.current_model ?? ''),
    candidates: Array.isArray(event.candidates)
      ? (event.candidates as Array<Record<string, unknown>>).map(c => ({
          id: String(c.id ?? ''),
          label: String(c.label ?? c.id ?? ''),
          supports_vision:
            c.supports_vision === undefined ? undefined : Boolean(c.supports_vision),
          disabled: Boolean(c.disabled),
        }))
      : [],
    timeout_s: Number(event.timeout_s ?? 30),
    opened_at: Date.now(),
  }
}
