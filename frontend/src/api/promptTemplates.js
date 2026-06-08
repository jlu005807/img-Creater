import { apiClient } from './client'

export function listPromptTemplates() {
  return apiClient.get('/prompt-templates')
}

export function createPromptTemplate(payload) {
  return apiClient.post('/prompt-templates', payload)
}

export function updatePromptTemplate(id, payload) {
  return apiClient.put(`/prompt-templates/${id}`, payload)
}

export function deletePromptTemplate(id) {
  return apiClient.delete(`/prompt-templates/${id}`)
}
