import { apiClient } from './client'

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
  return apiClient.put(`/edit-drafts/${historyId}`, payload)
}

export function getEditDraft(historyId) {
  return apiClient.get(`/edit-drafts/${historyId}`)
}

export function listSessions() {
  return apiClient.get('/sessions')
}

export function deleteSession(historyId) {
  return apiClient.delete(`/sessions/${historyId}`)
}

export function deleteSessions() {
  return apiClient.delete('/sessions')
}
