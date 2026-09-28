/** Content sent from the OS share sheet is staged locally, never sent to the engine. */
export interface SharedContent {
  id: string
  createdAt: number
  title: string
  text: string
  url: string
  files: File[]
}

const DB_NAME = 'ciaobot-shared-content'
const STORE = 'pending'

function openDb(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, 1)
    request.onupgradeneeded = () => request.result.createObjectStore(STORE, { keyPath: 'id' })
    request.onsuccess = () => resolve(request.result)
    request.onerror = () => reject(request.error)
  })
}

export async function readSharedContent(id: string): Promise<SharedContent | undefined> {
  const db = await openDb()
  try {
    const content = await new Promise<SharedContent | undefined>((resolve, reject) => {
      const request = db.transaction(STORE).objectStore(STORE).get(id)
      request.onsuccess = () => resolve(request.result as SharedContent | undefined)
      request.onerror = () => reject(request.error)
    })
    return content && Date.now() - content.createdAt < 24 * 60 * 60 * 1000 ? content : undefined
  } finally {
    db.close()
  }
}

export async function removeSharedContent(id: string): Promise<void> {
  const db = await openDb()
  try {
    await new Promise<void>((resolve, reject) => {
      const transaction = db.transaction(STORE, 'readwrite')
      transaction.objectStore(STORE).delete(id)
      transaction.oncomplete = () => resolve()
      transaction.onerror = () => reject(transaction.error)
    })
  } finally {
    db.close()
  }
}

export async function clearSharedContent(): Promise<void> {
  const db = await openDb()
  try {
    await new Promise<void>((resolve, reject) => {
      const transaction = db.transaction(STORE, 'readwrite')
      transaction.objectStore(STORE).clear()
      transaction.oncomplete = () => resolve()
      transaction.onerror = () => reject(transaction.error)
    })
  } finally {
    db.close()
  }
}

export function sharedPrompt(content: SharedContent): string {
  return [content.title, content.text, content.url].filter(Boolean).join('\n\n')
}
