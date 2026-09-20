import { describe, expect, test } from 'vitest'
import {
  parseCapabilityQuestion,
  parseQuestions,
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
  })
})

describe('questionsSignature', () => {
  const base: ActiveQuestion = {
    id: 'q1', question: 'Which?', header: 'H', multiSelect: false,
    allowOther: true, isSecret: false, requestId: '', options: [],
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
