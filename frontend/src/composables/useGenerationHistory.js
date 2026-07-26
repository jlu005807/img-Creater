import { ref } from 'vue'
import { listSessions } from '../api/generation'
import { backendRouteMissingMessage, isBackendRouteMissing } from '../api/client'

const STORAGE_KEY = 'studio-generation-history'
const MAX_ENTRIES = 30
const RUNNING_STATUSES = new Set(['submitting', 'queued', 'processing'])

// Shared singleton so the drawer and the gallery see the same list.
const history = ref(load())

function load() {
  try {
    const parsed = JSON.parse(window.localStorage.getItem(STORAGE_KEY))
    return Array.isArray(parsed) ? parsed : []
  } catch {
    return []
  }
}

function persistedUrlList(value) {
  return Array.isArray(value) ? value.filter((url) => typeof url === 'string' && !url.startsWith('data:')) : []
}

function referenceImagesForSession(session) {
  return persistedUrlList(session?.reference_images)
}

// base64 (data:) images are far too large for the ~5MB localStorage quota, so
// only remote URLs are persisted; data: results stay in memory for this session.
function sanitizeForStorage(entries) {
  return entries.map((entry) => {
    return {
      ...entry,
      urls: persistedUrlList(entry.urls),
      referenceImages: persistedUrlList(entry.referenceImages),
      editDraft: undefined,
    }
  })
}

function persist() {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(sanitizeForStorage(history.value)))
  } catch {
    /* ignore quota / availability errors */
  }
}

export function useGenerationHistory() {
  function addEntry(entry) {
    const item = {
      id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      time: Date.now(),
      _status: 'queued',
      ...entry,
    }
    history.value = [item, ...history.value].slice(0, MAX_ENTRIES)
    persist()
    return item
  }

  function updateEntry(id, fields) {
    const idx = history.value.findIndex((e) => e.id === id)
    if (idx >= 0) {
      history.value[idx] = { ...history.value[idx], ...fields }
      persist()
    }
  }

  function removeEntry(id) {
    history.value = history.value.filter((entry) => entry.id !== id)
    persist()
  }

  function clearHistory() {
    history.value = []
    persist()
  }

  async function loadPersistedSessions() {
    let sessions
    try {
      sessions = await listSessions()
    } catch (error) {
      if (isBackendRouteMissing(error)) {
        const next = new Error(backendRouteMissingMessage('历史会话'))
        next.status = error.status
        throw next
      }
      throw error
    }
    if (!Array.isArray(sessions) || !sessions.length) return []
    const existingById = new Map(history.value.map((entry) => [entry.id, entry]))
    for (const session of sessions) {
      // 正在跑新任务的本地条目不能被后端 manifest 覆盖：重新生成期间
      // manifest 仍保留上一次完成的内容（旧 task_id / 旧图），直接合并会
      // 把运行态改写成 completed 并中断轮询恢复。
      const existing = existingById.get(session.id)
      if (
        existing &&
        RUNNING_STATUSES.has(existing._status) &&
        existing.task?.taskId &&
        existing.task.taskId !== session.task_id
      ) {
        continue
      }
      const urls = Array.isArray(session.urls) ? session.urls : []
      const entry = {
        id: session.id,
        prompt: session.prompt || '',
        mode: session.mode || 'generate',
        size: session.size || '1024x1024',
        urls,
        referenceImages: referenceImagesForSession(session),
        apiName: session.api_name || '',
        time: Date.parse(session.updated_at || session.created_at || '') || Date.now(),
        _status: 'completed',
        task: session.task_id ? { taskId: session.task_id, apiId: session.api_id, apiName: session.api_name } : null,
        attempts: Array.isArray(session.attempts) ? session.attempts : [],
        expiresAt: session.expires_at ?? null,
      }
      existingById.set(entry.id, { ...(existingById.get(entry.id) || {}), ...entry })
    }
    history.value = Array.from(existingById.values())
      .sort((a, b) => Number(b.time || 0) - Number(a.time || 0))
      .slice(0, MAX_ENTRIES)
    persist()
    return sessions
  }

  return { history, addEntry, updateEntry, removeEntry, clearHistory, loadPersistedSessions }
}
