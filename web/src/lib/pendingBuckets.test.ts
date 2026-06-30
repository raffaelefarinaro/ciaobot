import { describe, expect, test } from 'vitest'
import { getPendingBucket, normalizePendingBuckets, setPendingBucket } from './pendingBuckets'

describe('pending attachment buckets', () => {
  test('normalizes legacy global arrays into the active chat bucket', () => {
    const buckets = normalizePendingBuckets(['img-1'], 'chat-a')
    expect(buckets).toEqual({ 'chat-a': ['img-1'] })
  })

  test('preserves per-chat buckets and reads only the requested chat', () => {
    const buckets = normalizePendingBuckets({ 'chat-a': ['img-a'], 'chat-b': ['img-b'] }, 'chat-a')
    expect(getPendingBucket(buckets, 'chat-a')).toEqual(['img-a'])
    expect(getPendingBucket(buckets, 'chat-b')).toEqual(['img-b'])
  })

  test('removes empty chat buckets when setting values', () => {
    const buckets = { 'chat-a': ['img-a'], 'chat-b': ['img-b'] }
    setPendingBucket(buckets, 'chat-a', [])
    expect(buckets).toEqual({ 'chat-b': ['img-b'] })
  })
})
