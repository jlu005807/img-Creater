import { apiClient } from './client'

export function listConfigs() {
  return apiClient.get('/configs')
}

export function createConfig(payload) {
  return apiClient.post('/configs', payload)
}

export function updateConfig(id, payload) {
  return apiClient.put(`/configs/${id}`, payload)
}

export function getConfigSecret(id) {
  return apiClient.get(`/configs/${id}/secret`)
}

export function deleteConfig(id) {
  return apiClient.delete(`/configs/${id}`)
}

export function reorderConfigs(orderedIds) {
  return apiClient.post('/configs/reorder', { ordered_ids: orderedIds })
}
