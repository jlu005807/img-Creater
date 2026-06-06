import { apiClient } from './client'

export function generateImages(payload) {
  return apiClient.post('/generate', payload)
}

export function getGenerationStatus({ apiId, taskId }) {
  return apiClient.get('/status', {
    params: {
      api_id: apiId,
      task_id: taskId,
    },
  })
}

