import axios from 'axios'

export const apiClient = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL || '/api',
  timeout: 30000,
})

apiClient.interceptors.response.use(
  (response) => {
    const body = response.data
    if (body && typeof body === 'object' && 'success' in body) {
      if (body.success) return body.data
      const message = body.error?.message || '请求失败'
      const error = new Error(message)
      error.details = body.error?.details || {}
      throw error
    }
    return body
  },
  (error) => {
    const message = error.response?.data?.error?.message || error.message || '网络请求失败'
    const normalized = new Error(message)
    normalized.status = error.response?.status
    normalized.details = error.response?.data?.error?.details || {}
    return Promise.reject(normalized)
  },
)

export function isBackendRouteMissing(error) {
  return Number(error?.status) === 404
}

export function backendRouteMissingMessage(feature = '该功能') {
  return `${feature}接口不存在，请重启后端到当前版本后再试`
}
