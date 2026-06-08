import { apiClient } from './client'

// Beta AI-image detection. Backend degrades gracefully if deps are missing.
export function detectorHealth() {
  return apiClient.get('/detect/health')
}

export function detectImage(dataUrl, filename = '') {
  return apiClient.post('/detect', { image: dataUrl, filename })
}
