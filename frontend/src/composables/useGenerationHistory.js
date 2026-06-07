import { ref } from 'vue'

const STORAGE_KEY = 'studio-generation-history'
const MAX_ENTRIES = 30

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

// base64 (data:) images are far too large for the ~5MB localStorage quota, so
// only remote URLs are persisted; data: results stay in memory for this session.
function sanitizeForStorage(entries) {
  return entries.map((entry) => {
    const urls = entry.urls || []
    return {
      ...entry,
      urls: urls.filter((url) => typeof url === 'string' && !url.startsWith('data:')),
      imageCount: urls.length,
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
    const urls = entry.urls || []
    const item = {
      id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      time: Date.now(),
      imageCount: urls.length,
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

  return { history, addEntry, updateEntry, removeEntry, clearHistory }
}
