import { apiClient } from './client'

export function generateImages(payload) {
  return apiClient.post('/generate', payload)
}

export function editImage(payload) {
  return apiClient.post('/edit', payload)
}

export function getGenerationStatus({ apiId, taskId }) {
  return apiClient.get('/status', {
    params: {
      api_id: apiId,
      task_id: taskId,
    },
  })
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
