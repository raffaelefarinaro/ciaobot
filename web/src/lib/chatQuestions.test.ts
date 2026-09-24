import { describe, expect, test } from 'vitest'
import {
  parseCapabilityQuestion,
  parseQuestions,
  questionAnswerError,
  questionAnswerIsValid,
  questionEmptyAnswerAllowed,
  questionIsActive,
  questionIsVisible,
  questionsSignature,
  type ActiveQuestion,
} from './chatQuestions'

describe('parseQuestions', () => {
  test('returns [] for missing or unparseable input', () => {
    expect(parseQuestions(null)).toEqual([])
    expect(parseQuestions('')).toEqual([])
    expect(parseQuestions('{not json')).toEqual([])
    expect(parseQuestions('{"questions": "nope"}')).toEqual([])
  })

  test('parses the documented Claude shape', () => {
    const qs = parseQuestions(JSON.stringify({
      questions: [{
        id: 'q1',
        question: 'Which branch?',
        header: 'Branch',
        multiSelect: false,
        options: [{ label: 'develop', description: 'default' }, { label: 'main' }],
      }],
    }), 'req-7')
    expect(qs).toHaveLength(1)
    expect(qs[0]).toMatchObject({
      id: 'q1',
      question: 'Which branch?',
      header: 'Branch',
      multiSelect: false,
      isSecret: false,
      requestId: 'req-7',
    })
    expect(qs[0].options).toEqual([
      { label: 'develop', description: 'default' },
      { label: 'main', description: '' },
    ])
  })

  test('accepts the alternate text/type shape some providers emit', () => {
    const qs = parseQuestions(JSON.stringify({
      questions: [{ text: 'Pick some', title: 'Files', type: 'multi_select', options: [{ value: 'a.md' }] }],
    }))
    expect(qs[0].question).toBe('Pick some')
    expect(qs[0].header).toBe('Files')
    expect(qs[0].multiSelect).toBe(true)
    expect(qs[0].options).toEqual([{ label: 'a.md', description: '' }])
  })

  test('falls back to the request id carried in the payload', () => {
    const qs = parseQuestions(JSON.stringify({ request_id: 'inner', questions: [{ question: 'hi' }] }))
    expect(qs[0].requestId).toBe('inner')
  })

  test('an empty questions array becomes one free-form prompt', () => {
    const qs = parseQuestions(JSON.stringify({ questions: [], request_id: 'r1' }))
    expect(qs).toHaveLength(1)
    expect(qs[0].id).toBe('__freeform__')
    expect(qs[0].allowOther).toBe(true)
    expect(qs[0].requestId).toBe('r1')
  })

  test('allowOther defaults to true and honours an explicit isOther', () => {
    const withOptions = parseQuestions(JSON.stringify({
      questions: [{ question: 'q', isOther: false, options: [{ label: 'a' }] }],
    }))
    expect(withOptions[0].allowOther).toBe(false)
    const noOptions = parseQuestions(JSON.stringify({
      questions: [{ question: 'q', isOther: false }],
    }))
    expect(noOptions[0].allowOther).toBe(true)
    const optionalClosed = parseQuestions(JSON.stringify({
      questions: [{ question: 'q', required: false, isOther: false, options: [{ label: 'a' }] }],
    }))
    expect(optionalClosed[0].allowOther).toBe(false)
  })

  test('preserves V2 hidden, conditional, external, and optional form metadata', () => {
    const qs = parseQuestions(JSON.stringify({
      form: { id: 'frm_1', title: 'Details' },
      questions: [
        { id: 'show', type: 'string', required: true, options: [{ value: 'yes', label: 'Yes' }] },
        { id: 'detail', type: 'string', required: false, when: [{ key: 'show', op: 'eq', value: 'yes' }] },
        { id: 'link', type: 'external', url: 'https://example.test/auth' },
        { id: 'secret', type: 'string', hidden: true, required: true },
        { id: 'code', type: 'string', pattern: '^[A-Z]{3}$', minLength: 3, maxLength: 3 },
      ],
    }))
    expect(qs[1]).toMatchObject({ required: false, when: [{ key: 'show', op: 'eq', value: 'yes' }] })
    expect(qs[2]).toMatchObject({ type: 'external', url: 'https://example.test/auth' })
    expect(qs[3]).toMatchObject({ hidden: true, required: true })
    expect(qs[4]).toMatchObject({ pattern: '^[A-Z]{3}$', minLength: 3, maxLength: 3 })
    const withDefault = parseQuestions(JSON.stringify({
      questions: [{ id: 'choice', type: 'string', default: 'yes', options: [{ value: 'yes', label: 'Yes' }] }],
    }))
    expect(withDefault[0].default).toBe('yes')
    const answers = { 0: { selected: new Set(['Yes']), other: '' } }
    expect(questionIsVisible(qs[1], qs, answers)).toBe(true)
    expect(questionIsVisible(qs[3], qs, answers)).toBe(false)

    const emptyCondition = parseQuestions(JSON.stringify({
      questions: [
        { id: 'source', type: 'multiselect', options: [{ value: 'yes', label: 'Yes' }] },
        { id: 'dependent', type: 'string', when: [{ key: 'source', op: 'neq', value: 'yes' }] },
      ],
    }))
    expect(questionIsVisible(emptyCondition[1], emptyCondition, {})).toBe(false)
    expect(questionIsVisible(emptyCondition[1], emptyCondition, {
      0: { selected: new Set<string>(), other: '' },
    })).toBe(true)
    const closedCondition = parseQuestions(JSON.stringify({
      questions: [
        { id: 'source', type: 'string', options: [{ value: 'yes', label: 'Yes' }], isOther: false },
        { id: 'dependent', type: 'string', when: [{ key: 'source', op: 'neq', value: 'yes' }] },
      ],
    }))
    expect(questionIsVisible(closedCondition[1], closedCondition, {
      0: { selected: new Set<string>(), other: '' },
    })).toBe(false)
  })

  test('validates V2 scalar and item constraints before submit', () => {
    const qs = parseQuestions(JSON.stringify({ questions: [
      { id: 'code', type: 'string', pattern: '^[A-Z]{3}$', minLength: 3, maxLength: 3 },
      { id: 'amount', type: 'number', minimum: 2, maximum: 4, required: true },
      { id: 'tags', type: 'multiselect', options: [
        { value: 'a', label: 'A' }, { value: 'b', label: 'B' },
      ], minItems: 2, maxItems: 2 },
    ] }))
    expect(questionAnswerError(qs[0], { selected: new Set(), other: 'abc' })).toMatch(/format/)
    expect(questionAnswerIsValid(qs[0], { selected: new Set(), other: 'ABC' })).toBe(true)
    expect(questionAnswerIsValid(qs[1], { selected: new Set(), other: '1' })).toBe(false)
    expect(questionAnswerIsValid(qs[1], { selected: new Set(), other: '3' })).toBe(true)
    expect(questionAnswerIsValid(qs[2], { selected: new Set(['A']), other: '' })).toBe(false)
    expect(questionAnswerIsValid(qs[2], { selected: new Set(['A', 'B']), other: '' })).toBe(true)
    expect(questionAnswerIsValid(qs[2], { selected: new Set(['A', 'B']), other: 'extra' })).toBe(false)
    expect(questionEmptyAnswerAllowed(qs[0])).toBe(false)
  })

  test('preserves custom constraints and wire values', () => {
    const [closed, open] = parseQuestions(JSON.stringify({ questions: [
      { id: 'closed', type: 'string', custom: false, options: [{ value: '0', label: 'Zero' }] },
      { id: 'open', type: 'string', custom: true, options: [{ value: '0', label: 'Zero' }] },
    ] }))
    expect(closed.custom).toBe(false)
    expect(closed.options[0].value).toBe('0')
    expect(questionAnswerIsValid(closed, { selected: new Set(['other']), other: '' })).toBe(false)
    expect(questionAnswerIsValid(open, { selected: new Set(['other']), other: '' })).toBe(true)
  })

  test('uses the controlling field type for conditional values', () => {
    const qs = parseQuestions(JSON.stringify({ questions: [
      { id: 'tags', type: 'multiselect', options: [{ value: 'yes', label: 'Yes' }] },
      { id: 'detail', type: 'string', when: [{ key: 'tags', op: 'eq', value: 'yes' }] },
    ] }))
    expect(questionIsActive(qs[1], { tags: ['yes', 'also'] }, qs)).toBe(true)
    expect(questionIsActive(qs[1], { tags: ['no'] }, qs)).toBe(false)
  })

  test('allows an explicitly empty required multiselect with minItems zero', () => {
    const [q] = parseQuestions(JSON.stringify({ questions: [
      { id: 'tags', type: 'multiselect', required: true, minItems: 0, options: [] },
    ] }))
    expect(questionAnswerIsValid(q, { selected: new Set(), other: '' })).toBe(true)
  })
})

describe('questionsSignature', () => {
  const base: ActiveQuestion = {
    id: 'q1', question: 'Which?', header: 'H', multiSelect: false,
    allowOther: true, isSecret: false, requestId: '', options: [],
    type: 'string', required: true, hidden: false, when: [],
  }

  test('is empty for no picker', () => {
    expect(questionsSignature(undefined)).toBe('')
    expect(questionsSignature([])).toBe('')
  })

  test('prefers the provider request id', () => {
    expect(questionsSignature([{ ...base, requestId: 'r9' }])).toBe('rid:r9')
  })

  test('falls back to the question content when there is no request id', () => {
    const sig = questionsSignature([base])
    expect(sig.startsWith('q:')).toBe(true)
    expect(questionsSignature([base])).toBe(sig)
    expect(questionsSignature([{ ...base, question: 'Other?' }])).not.toBe(sig)
  })

  test('the live entry and a rebuilt copy share a signature', () => {
    const raw = JSON.stringify({ questions: [{ id: 'q1', question: 'Which?', header: 'H' }] })
    expect(questionsSignature(parseQuestions(raw))).toBe(questionsSignature(parseQuestions(raw)))
  })
})

describe('parseCapabilityQuestion', () => {
  test('fills defaults for a bare event', () => {
    const q = parseCapabilityQuestion({ request_id: 'c1' })
    expect(q.missing).toBe('image_input')
    expect(q.current_model).toBe('')
    expect(q.candidates).toEqual([])
    expect(q.timeout_s).toBe(30)
    expect(typeof q.opened_at).toBe('number')
  })

  test('normalises candidates', () => {
    const q = parseCapabilityQuestion({
      request_id: 'c1',
      candidates: [{ id: 'm1', supports_vision: true }, { id: 'm2', label: 'Two', disabled: true }],
      timeout_s: 12,
    })
    expect(q.candidates).toEqual([
      { id: 'm1', label: 'm1', supports_vision: true, disabled: false },
      { id: 'm2', label: 'Two', supports_vision: undefined, disabled: true },
    ])
    expect(q.timeout_s).toBe(12)
  })
})
