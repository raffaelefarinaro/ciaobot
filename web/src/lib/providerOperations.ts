import type { ProviderConfigSettings } from './types'

// Shared by every Settings view, including one mounted while another is closing.
let mutation: Promise<void> | null = null
let generation = 0
let mutationGeneration = 0
type ReadResult = { generation: number, value: ProviderConfigSettings | null }
let reading: Promise<ReadResult> | null = null
let readingGeneration = 0
const listeners = new Set<() => void>()

export function subscribeProviderRefresh(listener: () => void): () => void {
  listeners.add(listener)
  return () => { listeners.delete(listener) }
}

export function providerReadIsCurrent(value: number): boolean {
  return value === generation
}

export function providerGeneration(): number {
  return mutationGeneration
}

export function providerMutationIsCurrent(value: number): boolean {
  return value === mutationGeneration
}

export function invalidateProviderReads(): void {
  generation++
  for (const listener of listeners) listener()
}

export function providerRead(read: () => Promise<ProviderConfigSettings>): Promise<ReadResult> {
  if (reading && readingGeneration === generation) return reading
  const current = ++generation
  readingGeneration = current
  const request = (async (): Promise<ReadResult> => {
    try {
      return { generation: current, value: await read() }
    } catch {
      return { generation: current, value: null }
    }
  })()
  reading = request
  void request.then(() => { if (reading === request) reading = null })
  return request
}

export function waitForProviderMutation(): Promise<void> | null {
  return mutation
}

export function providerMutation<T>(operation: () => Promise<T>): Promise<T> | null {
  if (mutation) return null
  let release!: () => void
  const pending = new Promise<void>(resolve => { release = resolve })
  mutation = pending
  generation++
  mutationGeneration++
  return (async () => {
    try {
      return await operation()
    } finally {
      mutation = null
      release()
      for (const listener of listeners) listener()
    }
  })()
}
