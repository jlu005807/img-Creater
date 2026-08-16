import { ref } from 'vue'
import { getSession, listSessions } from '../api/generation.js'
import { backendRouteMissingMessage, isBackendRouteMissing } from '../api/client.js'
import { applySessionPage, mapSessionSummary } from '../utils/sessionHistory.js'

const STORAGE_KEY = 'studio-generation-history'
const SESSION_PAGE_SIZE = 30
const STORAGE_CACHE_LIMIT = 30
const RUNNING_STATUSES = new Set(['submitting', 'queued', 'processing'])

function persistedUrlList(value) {
  return Array.isArray(value)
    ? value.filter((url) => typeof url === 'string' && !url.startsWith('data:'))
    : []
}

function cachedEntry(value) {
  if (!value || typeof value !== 'object' || typeof value.id !== 'string') return null
  const mapped = mapSessionSummary({
    id: value.id,
    prompt: value.prompt,
    mode: value.mode,
    size: value.size,
    status: value._status || value.status,
    created_at: value.createdAt ?? value.created_at,
    updated_at: value.updatedAt ?? value.updated_at,
    images: value.images,
    urls: value.urls,
    reference_images: value.referenceImages ?? value.reference_images,
    time: value.time,
    task: value.task,
    api_name: value.apiName ?? value.api_name,
    expires_at: value.expiresAt,
  })
  return {
    ...mapped,
    _origin: value._origin || (RUNNING_STATUSES.has(mapped._status) ? 'local' : 'server'),
    attempts: [],
    responseMeta: null,
    startedAt: Number(value.startedAt || 0) || null,
    maxWaitSeconds: Number(value.maxWaitSeconds || 0) || null,
    errorMessage: typeof value.errorMessage === 'string' ? value.errorMessage : '',
  }
}

function load() {
  if (typeof window === 'undefined') return []
  try {
    const parsed = JSON.parse(window.localStorage.getItem(STORAGE_KEY))
    return Array.isArray(parsed)
      ? parsed.slice(0, STORAGE_CACHE_LIMIT).map(cachedEntry).filter(Boolean)
      : []
  } catch {
    return []
  }
}

function cachedTask(task) {
  if (!task || typeof task !== 'object' || !task.taskId) return null
  return {
    taskId: task.taskId,
    apiId: task.apiId ?? null,
    apiName: task.apiName ?? '',
    apiType: task.apiType ?? null,
    effectiveApiType: task.effectiveApiType ?? null,
    requestUrl: task.requestUrl ?? null,
    upstreamTaskId: task.upstreamTaskId ?? null,
    upstreamRequestId: task.upstreamRequestId ?? null,
    pollCount: task.pollCount ?? 0,
    lastPollStatus: task.lastPollStatus ?? null,
    waitPhase: task.waitPhase ?? null,
    lastPollError: task.lastPollError ?? null,
  }
}

function sanitizeForStorage(entries) {
  return entries.map((entry) => {
    const urls = persistedUrlList(entry.urls)
    const images = Array.isArray(entry.images)
      ? entry.images
        .filter((image) => image && urls.includes(image.url))
        .map((image) => ({
          key: image.key,
          sessionId: image.sessionId,
          imageIndex: image.imageIndex,
          url: image.url,
          filename: image.filename,
          expiresAt: image.expiresAt ?? null,
        }))
      : []
    return {
      id: entry.id,
      prompt: String(entry.prompt || '').slice(0, 4000),
      mode: entry.mode || 'generate',
      size: entry.size || '1024x1024',
      status: entry.status || entry._status || 'completed',
      createdAt: entry.createdAt || '',
      updatedAt: entry.updatedAt || '',
      images,
      referenceImages: persistedUrlList(entry.referenceImages),
      urls,
      time: Number(entry.time || 0),
      _status: entry._status || 'completed',
      _origin: entry._origin || 'server',
      task: cachedTask(entry.task),
      apiName: entry.apiName || '',
      expiresAt: entry.expiresAt ?? null,
      startedAt: Number(entry.startedAt || 0) || null,
      maxWaitSeconds: Number(entry.maxWaitSeconds || 0) || null,
      errorMessage: entry.errorMessage || '',
    }
  })
}

// Keep only a bounded, lightweight startup cache. The loaded in-memory
// history remains unbounded and the backend is the completed-session source.
function persist() {
  if (typeof window === 'undefined') return
  try {
    const cached = sanitizeForStorage(history.value.slice(0, STORAGE_CACHE_LIMIT))
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(cached))
  } catch {
    /* ignore quota / availability errors */
  }
}

function mappedImagesForEntry(entry, ignoreExistingImages = false) {
  return mapSessionSummary({
    ...entry,
    images: ignoreExistingImages ? undefined : entry.images,
    status: entry._status || entry.status,
    expires_at: entry.expiresAt,
  }).images
}

function normalizedQuery(params) {
  const query = {}
  for (const [key, value] of Object.entries(params || {})) {
    if (key === 'cursor' || key === 'limit') continue
    if (value !== undefined && value !== null && value !== '') query[key] = value
  }
  return query
}

function sessionQueryKey(params) {
  const query = normalizedQuery(params)
  return JSON.stringify(
    Object.keys(query)
      .sort()
      .map((key) => [key, query[key]]),
  )
}

function normalizedLoadError(error) {
  if (!isBackendRouteMissing(error)) return error
  const next = new Error(backendRouteMissingMessage('历史会话'))
  next.status = error.status
  return next
}

// Module-level refs intentionally survive App.vue's Playground/Gallery v-if
// remounts, so both views share the same pages and request generation.
const history = ref(load())
const initialLoading = ref(false)
const loadingMore = ref(false)
const loadError = ref(null)
const hasMore = ref(true)
const nextCursor = ref(null)
const serverResultsCurrent = ref(false)
const detailRequests = new Map()

let requestGeneration = 0
let currentQuery = {}
let currentQueryKey = sessionQueryKey(currentQuery)
let loadedQueryKey = null
let loadedSuccessfully = false
let consumedCursors = new Set()
let tombstoneIds = new Set()
let activeRefreshToken = null
let activeLoadMoreToken = null
let failedOperation = null

export function useGenerationHistory({
  loadSessionPage = listSessions,
  loadSessionDetail = getSession,
} = {}) {
  function withoutTombstonedEntries(items) {
    return items.filter((item) => !tombstoneIds.has(item.id))
  }

  function addEntry(entry) {
    const item = {
      id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      time: Date.now(),
      _status: 'queued',
      _origin: 'local',
      ...entry,
    }
    item._origin = item._origin || 'local'
    item.status = item._status || item.status || 'queued'
    item.images = mappedImagesForEntry(item)
    tombstoneIds.delete(item.id)
    history.value = [item, ...history.value.filter((existing) => existing.id !== item.id)]
    persist()
    return item
  }

  function updateEntry(id, fields) {
    const idx = history.value.findIndex((entry) => entry.id === id)
    if (idx < 0) return
    const next = { ...history.value[idx], ...fields }
    if ('_status' in fields) {
      next.status = fields._status
      if (RUNNING_STATUSES.has(fields._status)) next._origin = 'local'
    }
    if ('urls' in fields || 'images' in fields || 'expiresAt' in fields) {
      next.images = mappedImagesForEntry(next, 'urls' in fields || 'expiresAt' in fields)
    }
    history.value[idx] = next
    persist()
  }

  function removeEntry(id) {
    tombstoneIds.add(id)
    detailRequests.delete(id)
    history.value = history.value.filter((entry) => entry.id !== id)
    if (!history.value.length && !hasMore.value) nextCursor.value = null
    persist()
  }

  function clearHistory() {
    requestGeneration += 1
    activeRefreshToken = null
    activeLoadMoreToken = null
    failedOperation = null
    currentQuery = {}
    currentQueryKey = sessionQueryKey(currentQuery)
    loadedQueryKey = null
    loadedSuccessfully = false
    serverResultsCurrent.value = false
    consumedCursors = new Set()
    tombstoneIds = new Set()
    detailRequests.clear()
    history.value = []
    initialLoading.value = false
    loadingMore.value = false
    loadError.value = null
    hasMore.value = false
    nextCursor.value = null
    persist()
  }

  function invalidateSessionRequests() {
    requestGeneration += 1
    activeRefreshToken = null
    activeLoadMoreToken = null
    detailRequests.clear()
    failedOperation = null
    currentQueryKey = null
    loadedQueryKey = null
    loadedSuccessfully = false
    serverResultsCurrent.value = false
    initialLoading.value = true
    loadingMore.value = false
    loadError.value = null
    hasMore.value = false
    nextCursor.value = null
  }

  async function refreshSessions(params = {}, { queryKey = sessionQueryKey(params) } = {}) {
    const generation = ++requestGeneration
    const token = {}
    const query = normalizedQuery(params)
    currentQuery = query
    currentQueryKey = queryKey
    loadedSuccessfully = false
    serverResultsCurrent.value = false
    consumedCursors = new Set()
    tombstoneIds = new Set()
    detailRequests.clear()
    activeRefreshToken = token
    activeLoadMoreToken = null
    failedOperation = null
    initialLoading.value = true
    loadingMore.value = false
    loadError.value = null

    try {
      const payload = await loadSessionPage({ ...query, limit: SESSION_PAGE_SIZE })
      if (generation !== requestGeneration || activeRefreshToken !== token) return []

      const isLegacyFullManifestArray = Array.isArray(payload)
      const page = applySessionPage(history.value, payload, { replaceServer: true })
      const items = withoutTombstonedEntries(page.items)
      history.value = items
      nextCursor.value = page.nextCursor
      hasMore.value = page.hasMore
      loadedQueryKey = currentQueryKey
      loadedSuccessfully = true
      serverResultsCurrent.value = !isLegacyFullManifestArray
      loadError.value = null
      persist()
      return items
    } catch (error) {
      if (generation !== requestGeneration || activeRefreshToken !== token) return []
      const normalized = normalizedLoadError(error)
      loadedSuccessfully = false
      serverResultsCurrent.value = false
      loadError.value = normalized
      failedOperation = { type: 'refresh', params: query, queryKey }
      throw normalized
    } finally {
      if (generation === requestGeneration && activeRefreshToken === token) {
        activeRefreshToken = null
        initialLoading.value = false
      }
    }
  }

  // Component instances are intentionally short-lived because App.vue uses
  // v-if/v-else for page switching. Reuse the already loaded cursor chain when
  // the requested server query is unchanged; an explicit refresh still calls
  // refreshSessions directly and always starts a new first-page request.
  async function ensureSessions(params = {}, { queryKey = sessionQueryKey(params) } = {}) {
    const key = queryKey
    const requestKey = sessionQueryKey(params)
    if (
      loadedSuccessfully &&
      (loadedQueryKey === key || sessionQueryKey(currentQuery) === requestKey)
    ) {
      return history.value
    }
    if (
      initialLoading.value &&
      activeRefreshToken &&
      (currentQueryKey === key || sessionQueryKey(currentQuery) === requestKey)
    ) {
      return history.value
    }
    return refreshSessions(params, { queryKey: key })
  }

  async function ensureSessionDetails(historyId) {
    const id = typeof historyId === 'string' ? historyId : historyId?.id
    if (!id) return null

    const entry = history.value.find((item) => item.id === id)
    if (!entry) return null
    if (entry._detailsLoaded || entry._origin !== 'server' || entry._status !== 'completed') {
      return entry
    }

    const existingRequest = detailRequests.get(id)
    if (existingRequest) return existingRequest

    const requestedEntry = entry

    const request = (async () => {
      const payload = await loadSessionDetail(id)
      const details = mapSessionSummary(payload)
      if (!details.id || details.id !== id) {
        throw new Error('历史详情响应与请求的会话不匹配')
      }

      const index = history.value.findIndex((item) => item.id === id)
      if (index < 0) return null
      const current = history.value[index]
      if (current !== requestedEntry || tombstoneIds.has(id)) return null
      if (current._origin !== 'server' || current._status !== 'completed') return current

      const detailedEntry = {
        ...current,
        ...details,
        _detailsLoaded: true,
      }
      history.value[index] = detailedEntry
      persist()
      return detailedEntry
    })()
    detailRequests.set(id, request)

    try {
      return await request
    } finally {
      if (detailRequests.get(id) === request) detailRequests.delete(id)
    }
  }

  async function loadMoreSessions() {
    if (
      activeLoadMoreToken ||
      initialLoading.value ||
      loadError.value ||
      !hasMore.value ||
      !nextCursor.value
    ) {
      return []
    }

    if (consumedCursors.has(nextCursor.value)) {
      nextCursor.value = null
      hasMore.value = false
      loadError.value = null
      return []
    }

    const generation = requestGeneration
    const requestedCursor = nextCursor.value
    const token = {}
    activeLoadMoreToken = token
    failedOperation = null
    loadingMore.value = true
    loadError.value = null

    try {
      const payload = await loadSessionPage({
        ...currentQuery,
        limit: SESSION_PAGE_SIZE,
        cursor: requestedCursor,
      })
      if (generation !== requestGeneration || activeLoadMoreToken !== token) return []

      const page = applySessionPage(history.value, payload, {
        requestedCursor,
        seenCursors: consumedCursors,
      })
      consumedCursors.add(requestedCursor)
      const items = withoutTombstonedEntries(page.items)
      history.value = items
      nextCursor.value = page.nextCursor
      hasMore.value = page.hasMore
      loadError.value = null
      persist()
      return items
    } catch (error) {
      if (generation !== requestGeneration || activeLoadMoreToken !== token) return []
      const normalized = normalizedLoadError(error)
      loadError.value = normalized
      failedOperation = { type: 'loadMore' }
      throw normalized
    } finally {
      if (generation === requestGeneration && activeLoadMoreToken === token) {
        activeLoadMoreToken = null
        loadingMore.value = false
      }
    }
  }

  async function retrySessions() {
    if (failedOperation?.type === 'loadMore') {
      loadError.value = null
      return loadMoreSessions()
    }
    const params = failedOperation?.params || currentQuery
    const queryKey = failedOperation?.queryKey || sessionQueryKey(params)
    return refreshSessions(params, { queryKey })
  }

  // Backward-compatible name for existing callers.
  const loadPersistedSessions = refreshSessions

  return {
    history,
    addEntry,
    updateEntry,
    removeEntry,
    clearHistory,
    invalidateSessionRequests,
    loadPersistedSessions,
    refreshSessions,
    ensureSessions,
    ensureSessionDetails,
    loadMoreSessions,
    retrySessions,
    initialLoading,
    loadingMore,
    loadError,
    serverResultsCurrent,
    hasMore,
    nextCursor,
  }
}
