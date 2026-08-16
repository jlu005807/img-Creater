const RUNNING_STATUSES = new Set(['submitting', 'queued', 'processing'])

export function historyTimeBounds(filter, now = new Date()) {
  if (!['today', 'week', 'month'].includes(filter)) {
    return { from: undefined, to: undefined }
  }

  const current = new Date(now)
  const start = new Date(current)
  start.setHours(0, 0, 0, 0)
  if (filter === 'week') {
    const day = (current.getDay() + 6) % 7 // Monday = 0
    start.setDate(current.getDate() - day)
  } else if (filter === 'month') {
    start.setDate(1)
  }

  return {
    from: start.toISOString(),
    to: current.toISOString(),
  }
}

function stringList(value) {
  return Array.isArray(value) ? value.filter((item) => typeof item === 'string') : []
}

function imageFilename(url, fallback) {
  if (typeof url !== 'string' || !url) return fallback
  const withoutQuery = url.split(/[?#]/, 1)[0]
  const name = withoutQuery.split('/').pop()
  if (!name) return fallback
  try {
    return decodeURIComponent(name)
  } catch {
    return name
  }
}

function sessionTime(session, createdAt, updatedAt) {
  const existing = Number(session?.time)
  if (Number.isFinite(existing)) return existing
  const parsed = Date.parse(updatedAt || createdAt || '')
  return Number.isFinite(parsed) ? parsed : 0
}

function sessionTask(session, apiName) {
  if (session?.task && typeof session.task === 'object') return { ...session.task }
  const taskId = session?.task_id ?? session?.taskId
  if (typeof taskId !== 'string' || !taskId) return null
  return {
    taskId,
    apiId: session?.api_id ?? session?.apiId ?? null,
    apiName,
  }
}

function uniqueImages(images) {
  const keys = new Set()
  return images.filter((image) => {
    if (keys.has(image.key)) return false
    keys.add(image.key)
    return true
  })
}

function sessionImages(session, sessionId, expiresAt) {
  const rawImages = Array.isArray(session?.images) && session.images.length
    ? session.images
    : stringList(session?.urls).map((url, index) => ({ index, url }))

  const mapped = rawImages.flatMap((rawImage, position) => {
    const image = typeof rawImage === 'string' ? { url: rawImage } : rawImage
    if (!image || typeof image.url !== 'string') return []
    const rawIndex = image.index ?? image.image_index ?? image.imageIndex
    const numericIndex = Number(rawIndex)
    const imageIndex = rawIndex !== null && rawIndex !== undefined && rawIndex !== '' && Number.isInteger(numericIndex) && numericIndex >= 0
      ? numericIndex
      : position
    const filenameValue = image.filename ?? image.file_name
    const filename = typeof filenameValue === 'string' && filenameValue
      ? filenameValue
      : imageFilename(image.url, `image-${imageIndex + 1}`)

    return [{
      key: `${sessionId}:${imageIndex}`,
      sessionId,
      imageIndex,
      url: image.url,
      filename,
      expiresAt: image.expires_at ?? image.expiresAt ?? expiresAt,
    }]
  })
  if (mapped.length || !rawImages.length) return uniqueImages(mapped)
  return uniqueImages(stringList(session?.urls).map((url, index) => ({
    key: `${sessionId}:${index}`,
    sessionId,
    imageIndex: index,
    url,
    filename: imageFilename(url, `image-${index + 1}`),
    expiresAt,
  })))
}

export function mapSessionSummary(session = {}) {
  const id = session.id == null ? '' : String(session.id)
  const createdAt = session.created_at ?? session.createdAt ?? ''
  const updatedAt = session.updated_at ?? session.updatedAt ?? createdAt
  const expiresAt = session.expires_at ?? session.expiresAt ?? null
  const apiName = session.api_name ?? session.apiName ?? ''
  const status = session.status || session._status || 'completed'
  const images = sessionImages(session, id, expiresAt)

  return {
    id,
    prompt: typeof session.prompt === 'string' ? session.prompt : '',
    mode: session.mode || 'generate',
    size: session.size || '1024x1024',
    status,
    createdAt,
    updatedAt,
    images,
    referenceImages: stringList(session.reference_images ?? session.referenceImages),
    n: session.n ?? null,
    lastTaskId: session.last_task_id ?? session.lastTaskId ?? null,
    lastStatus: session.last_status ?? session.lastStatus ?? null,
    lastError: session.last_error ?? session.lastError ?? null,

    // Compatibility fields used by Playground while task management remains
    // in the existing component.
    urls: images.map((image) => image.url),
    time: sessionTime(session, createdAt, updatedAt),
    _status: status,
    _origin: 'server',
    task: sessionTask(session, apiName),
    apiName,
    attempts: Array.isArray(session.attempts) ? session.attempts : [],
    responseMeta: session.response_meta ?? session.responseMeta ?? null,
    expiresAt,
  }
}

export function normalizeSessionPage(payload) {
  if (Array.isArray(payload)) {
    return {
      // Legacy list endpoints return complete manifests rather than summaries,
      // so callers must not request a separate detail route for these items.
      items: payload
        .map((session) => ({ ...mapSessionSummary(session), _detailsLoaded: true }))
        .filter((item) => item.id),
      nextCursor: null,
      hasMore: false,
    }
  }

  const rawItems = Array.isArray(payload?.items) ? payload.items : []
  return {
    items: rawItems.map(mapSessionSummary).filter((item) => item.id),
    nextCursor: typeof payload?.next_cursor === 'string'
      ? payload.next_cursor
      : typeof payload?.nextCursor === 'string'
        ? payload.nextCursor
        : null,
    hasMore: Boolean(payload?.has_more ?? payload?.hasMore),
  }
}

function keepLocalTaskEntry(existing, incoming) {
  if (!existing || existing._origin === 'server') return false
  const existingStatus = existing._status || existing.status
  if (existingStatus === 'completed') return false

  const existingTaskId = existing.task?.taskId
  const incomingTaskId = incoming.lastTaskId || incoming.task?.taskId

  // A completed manifest can still describe the previous successful attempt
  // while a newer local queued/failed/cancelled attempt is unresolved. Keep
  // the local state until the manifest identifies the same attempt as settled.
  if (!existingTaskId || !incomingTaskId) return true
  if (
    incoming.lastStatus &&
    incoming.lastStatus !== 'completed' &&
    incomingTaskId === existingTaskId
  ) {
    return true
  }
  return incomingTaskId !== existingTaskId
}

export function mergeSessionHistory(current = [], incoming = [], { replaceServer = false } = {}) {
  const retained = replaceServer
    ? current.filter(
      (item) => item?._origin !== 'server' && (item?._status || item?.status) !== 'completed',
    )
    : current
  const byId = new Map()

  for (const item of retained) {
    if (item?.id && !byId.has(item.id)) byId.set(item.id, item)
  }

  for (const item of incoming) {
    if (!item?.id) continue
    const existing = byId.get(item.id)
    if (keepLocalTaskEntry(existing, item)) continue

    const merged = existing ? { ...existing, ...item } : item
    if (
      existing &&
      (item._status || item.status) === 'completed' &&
      (existing._status || existing.status) !== 'completed'
    ) {
      merged.errorMessage = ''
    }
    if ((!item.attempts || !item.attempts.length) && Array.isArray(existing?.attempts)) {
      merged.attempts = existing.attempts
    }
    byId.set(item.id, merged)
  }

  // Preserve backend page order. Consumers may place live local tasks by
  // timestamp, while persisted pages retain the API's updated-desc cursor order.
  return Array.from(byId.values())
}

export function applySessionPage(
  current = [],
  payload,
  { replaceServer = false, requestedCursor = null, seenCursors = new Set() } = {},
) {
  const page = normalizeSessionPage(payload)
  const nextCursor = page.nextCursor
  const cursorSeen = seenCursors instanceof Set
    ? seenCursors.has(nextCursor)
    : Array.isArray(seenCursors) && seenCursors.includes(nextCursor)
  const repeatedCursor = Boolean(
    page.hasMore && (
      !nextCursor ||
      nextCursor === requestedCursor ||
      cursorSeen
    ),
  )

  return {
    items: mergeSessionHistory(current, page.items, { replaceServer }),
    nextCursor: page.hasMore && !repeatedCursor ? nextCursor : null,
    hasMore: page.hasMore && !repeatedCursor,
  }
}
