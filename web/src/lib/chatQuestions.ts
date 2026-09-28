// Parsing for the two question cards the chat can show: the provider's
// AskUserQuestion picker and the engine's image-capability prompt. Pure
// transforms over raw event payloads — the live state (`activeQuestions`,
// `activeCapabilityQuestions`, `resolvedQuestions`) stays in
// `stores/projects.ts`, which owns when a card appears and when it is cleared.

export type QuestionWhen = {
  key: string
  op: 'eq' | 'neq'
  value: unknown
}

export type ActiveQuestionOption = {
  label: string
  value?: string
  description?: string
}

export type ActiveQuestion = {
  id: string
  question: string
  header: string
  multiSelect: boolean
  allowOther: boolean
  isSecret: boolean
  requestId: string
  sessionId?: string
  type?: string
  required?: boolean
  when?: QuestionWhen[]
  format?: string
  pattern?: string
  minLength?: number
  maxLength?: number
  minimum?: number
  maximum?: number
  minItems?: number
  maxItems?: number
  custom?: boolean
  options: ActiveQuestionOption[]
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
  if (rid) {
    const sessionId = qs[0]?.sessionId
    return `rid:${sessionId ? `${sessionId}:` : ''}${rid}`
  }
  return `q:${qs.map(q => `${q.id}${q.question}`).join('')}`
}

/**
 * Parse the AskUserQuestion tool_input JSON (`{"questions": [...]}`) into the
 * picker's shape. Shared by the live `tool_use` handler and the reload-time
 * rebuild from a chat's persisted `pending_question`. Returns [] on anything
 * unparseable so callers can fall through to the generic trace path.
 */
export function questionIsActive(
  question: ActiveQuestion,
  answers: Record<string, string[]>,
  allQuestions: ActiveQuestion[] = [],
): boolean {
  for (const condition of question.when || []) {
    const values = answers[condition.key]
    if (!values) return false
    const expected = condition.value
    const expectedText = typeof expected === 'string' ? expected : String(expected)
    const includes = values.includes(expectedText)
    const controller = allQuestions.find(candidate => candidate.id === condition.key)
    const controllerIsMulti = controller
      ? controller.multiSelect || controller.type === 'multiselect' || controller.type === 'multi_select'
      : question.type === 'multiselect' || question.type === 'multi_select'
    const equal = controllerIsMulti
      ? includes
      : values.length === 1 && values[0] === expectedText
    if ((condition.op === 'eq' && !equal) || (condition.op === 'neq' && equal)) {
      return false
    }
  }
  return true
}

export type QuestionAnswerState = { selected: Set<string>; other: string }

function isMultiQuestion(question: ActiveQuestion): boolean {
  return question.multiSelect
    || question.type === 'multiselect'
    || question.type === 'multi_select'
}

function questionOptionValue(question: ActiveQuestion, selected: string): string {
  const option = question.options.find(candidate => (
    candidate.value === selected || candidate.label === selected
  ))
  return option?.value ?? selected
}

function questionValues(question: ActiveQuestion, state?: QuestionAnswerState): string[] {
  const values = [...(state?.selected ?? [])].map(value => questionOptionValue(question, value))
  const other = state?.other.trim() ?? ''
  if (other) values.push(other)
  return values
}

function allowedOptionValues(question: ActiveQuestion): Set<string> {
  return new Set(question.options.map(option => option.value ?? option.label))
}

/** Return a user-facing reason why a V2 form field cannot be submitted yet. */
export function questionAnswerError(
  question: ActiveQuestion,
  state?: QuestionAnswerState,
): string | null {
  const values = questionValues(question, state)
  const multi = isMultiQuestion(question)
  if (!values.length) {
    if (multi) {
      if ((question.minItems ?? 0) > 0) {
        return `Choose at least ${question.minItems} item${question.minItems === 1 ? '' : 's'}.`
      }
      if (question.required === false || question.minItems === 0) return null
      return 'This field is required.'
    }
    return question.required === false ? null : 'This field is required.'
  }
  if (!multi && values.length > 1) return 'Choose one answer.'

  if (multi) {
    if (question.minItems !== undefined && values.length < question.minItems) {
      return `Choose at least ${question.minItems} item${question.minItems === 1 ? '' : 's'}.`
    }
    if (question.maxItems !== undefined && values.length > question.maxItems) {
      return `Choose at most ${question.maxItems} item${question.maxItems === 1 ? '' : 's'}.`
    }
    if (question.custom === false || !question.allowOther) {
      const allowed = allowedOptionValues(question)
      if (values.some(value => !allowed.has(value))) return 'Choose one of the available options.'
    }
    return null
  }

  const value = values[0] ?? ''
  if (question.custom === false || !question.allowOther) {
    const allowed = allowedOptionValues(question)
    if (allowed.size && !allowed.has(value)) return 'Choose one of the available options.'
  }
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
  if (question.format === 'email' && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value)) {
    return 'Enter a valid email address.'
  }
  if (question.format === 'uri') {
    try {
      if (!new URL(value).protocol) return 'Enter a valid absolute URI.'
    } catch {
      return 'Enter a valid absolute URI.'
    }
  }
  if (question.format === 'date' && !/^\d{4}-\d{2}-\d{2}$/.test(value)) {
    return 'Enter a valid date.'
  }
  if (question.format === 'date-time' && Number.isNaN(Date.parse(value))) {
    return 'Enter a valid date and time.'
  }
  return null
}

export function questionAnswerIsValid(
  question: ActiveQuestion,
  state?: QuestionAnswerState,
): boolean {
  return questionAnswerError(question, state) === null
}

export function parseQuestions(
  toolInput: string | null | undefined,
  requestId = '',
  sessionId = '',
): ActiveQuestion[] {
  if (!toolInput) return []
  try {
    const parsed = JSON.parse(toolInput)
    if (!Array.isArray(parsed?.questions)) return []
    const resolvedRequestId = requestId || String(parsed?.request_id ?? '')
    const resolvedSessionId = sessionId || String(parsed?.session_id ?? '')
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
        sessionId: resolvedSessionId || undefined,
        type: 'string',
        required: true,
        when: [],
        custom: true,
        options: [],
      }]
    }
    // Claude Code's documented AskUserQuestion shape uses
    // `question`/`header`/`multiSelect`. Some providers (seen with
    // MiniMax via the Claude path) emit an alternate shape with
    // `text`/`type: single_select|multi_select` instead — accept both
    // so the picker prompt is never blank when the model did ask.
    return parsed.questions.map((q: Record<string, unknown>, index: number) => {
      const type = String(q.type ?? '').toLowerCase()
      const multiSelect = Boolean(q.multiSelect) || type === 'multi_select' || type === 'multiselect'
      return {
        id: String(q.id ?? index),
        question: String(q.question ?? q.text ?? ''),
        header: String(q.header ?? q.title ?? ''),
        multiSelect,
        allowOther: q.isOther === undefined
          ? true
          : Boolean(q.isOther) || !Array.isArray(q.options) || q.options.length === 0,
        isSecret: Boolean(q.isSecret),
        requestId: resolvedRequestId,
        sessionId: resolvedSessionId || undefined,
        type: type || (q.multiSelect ? 'multiselect' : 'string'),
        required: q.required === undefined ? true : Boolean(q.required),
        when: Array.isArray(q.when)
          ? (q.when as QuestionWhen[]).filter(c => c && typeof c.key === 'string')
          : [],
        format: q.format ? String(q.format) : undefined,
        pattern: q.pattern ? String(q.pattern) : undefined,
        minLength: typeof q.minLength === 'number' ? q.minLength : undefined,
        maxLength: typeof q.maxLength === 'number' ? q.maxLength : undefined,
        minimum: typeof q.minimum === 'number' ? q.minimum : undefined,
        maximum: typeof q.maximum === 'number' ? q.maximum : undefined,
        minItems: typeof q.minItems === 'number' ? q.minItems : undefined,
        maxItems: typeof q.maxItems === 'number' ? q.maxItems : undefined,
        custom: q.custom === undefined ? undefined : Boolean(q.custom),
        options: Array.isArray(q.options)
          ? (q.options as Array<Record<string, unknown>>).map(o => ({
              label: String(o.label ?? o.value ?? ''),
              value: String(o.value ?? o.label ?? ''),
              description: o.description ? String(o.description) : '',
            }))
          : [],
      }
    })
  } catch {
    return []
  }
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
