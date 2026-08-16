import test from 'node:test'
import assert from 'node:assert/strict'

import { apiClient } from '../src/api/client.js'
import { deleteSession, getSession } from '../src/api/generation.js'

test('encodes reserved history ID characters in detail and delete paths', async () => {
  const historyId = 'public?id#fragment/label with space'
  const calls = []
  const originalGet = apiClient.get
  const originalDelete = apiClient.delete
  apiClient.get = async (...args) => {
    calls.push({ method: 'get', args })
    return { id: historyId }
  }
  apiClient.delete = async (...args) => {
    calls.push({ method: 'delete', args })
    return { deleted: true }
  }

  try {
    await getSession(historyId)
    await deleteSession(historyId)
  } finally {
    apiClient.get = originalGet
    apiClient.delete = originalDelete
  }

  const expectedPath = `/sessions/${encodeURIComponent(historyId)}`
  assert.deepEqual(calls, [
    { method: 'get', args: [expectedPath] },
    { method: 'delete', args: [expectedPath] },
  ])
})
