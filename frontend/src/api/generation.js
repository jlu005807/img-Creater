import { apiClient } from './client.js'

// Submission bodies can carry base64 images; the default 30s timeout aborts
// slow uploads the backend actually accepted, causing phantom failures.
const SUBMIT_TIMEOUT_MS = 120000

export function generateImages(payload) {
  return apiClient.post('/generate', payload, { timeout: SUBMIT_TIMEOUT_MS })
}

export function editImage(payload) {
  return apiClient.post('/edit', payload, { timeout: SUBMIT_TIMEOUT_MS })
}

export function getGenerationStatus({ apiId, taskId }) {
  return apiClient.get('/status', {
    params: {
      api_id: apiId,
      task_id: taskId,
    },
  })
}

export function cancelGenerationTask(taskId) {
  return apiClient.post(`/tasks/${taskId}/cancel`)
}

export function saveEditDraft(historyId, payload) {
  return apiClient.put(editDraftResourcePath(historyId), payload)
}

export function getEditDraft(historyId) {
  return apiClient.get(editDraftResourcePath(historyId))
}

export function listSessions(params = {}) {
  return apiClient.get('/sessions', { params })
}

export function getSession(historyId) {
  return apiClient.get(sessionResourcePath(historyId))
}

export function deleteSession(historyId) {
  return apiClient.delete(sessionResourcePath(historyId))
}

export function deleteSessions() {
  return apiClient.delete('/sessions')
}

function encodedHistoryId(historyId) {
  return encodeURIComponent(String(historyId))
}

export function sessionResourcePath(historyId) {
  return `/sessions/${encodedHistoryId(historyId)}`
}

function editDraftResourcePath(historyId) {
  return `/edit-drafts/${encodedHistoryId(historyId)}`
}
