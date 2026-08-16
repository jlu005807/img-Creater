import test from 'node:test'
import assert from 'node:assert/strict'

import { useGenerationHistory } from '../src/composables/useGenerationHistory.js'

function deferred() {
  let resolve
  const promise = new Promise((done) => { resolve = done })
  return { promise, resolve }
}

test('coalesces load-more calls and rejects a stale page immediately after query invalidation', async () => {
  const stalePage = deferred()
  const requests = []
  let firstPageCalls = 0
  const loadSessionPage = async (params) => {
    requests.push({ ...params })
    if (!params.cursor) {
      firstPageCalls += 1
    }
    if (firstPageCalls > 1 && !params.cursor) {
      return {
        items: [{ id: 'new-result', urls: ['/new.png'] }],
        next_cursor: null,
        has_more: false,
      }
    }
    if (params.cursor) return stalePage.promise
    return {
      items: [{ id: 'first-page', urls: ['/first.png'] }],
      next_cursor: 'cursor-2',
      has_more: true,
    }
  }
  const store = useGenerationHistory({ loadSessionPage })

  assert.equal(typeof store.invalidateSessionRequests, 'function')
  await store.refreshSessions({})
  const firstLoadMore = store.loadMoreSessions()
  const duplicateLoadMore = store.loadMoreSessions()

  assert.equal(requests.length, 2)
  assert.deepEqual(await duplicateLoadMore, [])

  store.invalidateSessionRequests()
  assert.equal(store.initialLoading.value, true)
  stalePage.resolve({
    items: [{ id: 'stale-result', urls: ['/stale.png'] }],
    next_cursor: null,
    has_more: false,
  })
  assert.deepEqual(await firstLoadMore, [])
  assert.deepEqual(store.history.value.map((item) => item.id), ['first-page'])
  assert.equal(store.loadingMore.value, false)

  await store.ensureSessions({})
  assert.equal(requests.length, 3)
  assert.deepEqual(store.history.value.map((item) => item.id), ['new-result'])

  await store.refreshSessions({ to: '2026-01-01T00:00:00Z' })
  const fixedRangeRequestCount = requests.length
  await store.ensureSessions({ to: '2026-01-01T00:00:00Z' })
  assert.equal(requests.length, fixedRangeRequestCount)
  await store.ensureSessions({ to: '2026-01-02T00:00:00Z' })
  assert.equal(requests.length, fixedRangeRequestCount + 1)
})

test('loads a full server session once before parameter reuse', async () => {
  const fullPrompt = `${'x'.repeat(4000)} complete detail suffix`
  let detailCalls = 0
  const store = useGenerationHistory({
    loadSessionPage: async () => ({
      items: [{
        id: 'detail-1',
        prompt: fullPrompt.slice(0, 4000),
        status: 'completed',
        urls: ['/api/results/detail-1/image.png'],
      }],
      next_cursor: null,
      has_more: false,
    }),
    loadSessionDetail: async (historyId) => {
      detailCalls += 1
      return {
        id: historyId,
        prompt: fullPrompt,
        status: 'completed',
        urls: ['/api/results/detail-1/image.png'],
        attempts: [{ api_id: 'api-1', ok: true }],
        response_meta: { request_id: 'request-1' },
      }
    },
  })

  await store.refreshSessions({})
  assert.equal(store.history.value[0].prompt.length, 4000)

  const first = await store.ensureSessionDetails('detail-1')
  const second = await store.ensureSessionDetails('detail-1')

  assert.equal(first.prompt, fullPrompt)
  assert.deepEqual(first.attempts, [{ api_id: 'api-1', ok: true }])
  assert.deepEqual(first.responseMeta, { request_id: 'request-1' })
  assert.equal(first._detailsLoaded, true)
  assert.equal(second.prompt, fullPrompt)
  assert.equal(detailCalls, 1)
})

test('does not request details again for a legacy full-manifest array', async () => {
  let detailCalls = 0
  const store = useGenerationHistory({
    loadSessionPage: async () => ([{
      id: 'legacy-full',
      prompt: 'complete legacy prompt',
      status: 'completed',
      urls: ['/api/results/legacy-full/image.png'],
      attempts: [{ api_id: 'api-legacy', ok: true }],
      response_meta: { request_id: 'legacy-request' },
    }]),
    loadSessionDetail: async () => {
      detailCalls += 1
      throw new Error('legacy backend has no detail endpoint')
    },
  })

  await store.refreshSessions({})
  const entry = await store.ensureSessionDetails('legacy-full')

  assert.equal(entry._detailsLoaded, true)
  assert.deepEqual(entry.attempts, [{ api_id: 'api-legacy', ok: true }])
  assert.deepEqual(entry.responseMeta, { request_id: 'legacy-request' })
  assert.equal(detailCalls, 0)
})

test('marks a legacy full-manifest array as requiring local query filtering', async () => {
  const store = useGenerationHistory({
    loadSessionPage: async () => ([
      { id: 'legacy-a', prompt: 'first complete manifest', urls: ['/legacy-a.png'] },
      { id: 'legacy-b', prompt: 'second complete manifest', urls: ['/legacy-b.png'] },
    ]),
  })

  const entries = await store.refreshSessions({ q: 'only a newer backend would filter' })

  assert.equal(store.serverResultsCurrent.value, false)
  assert.deepEqual(entries.map((entry) => entry.id), ['legacy-a', 'legacy-b'])
})

test('keeps an in-flight first page after removal while filtering its tombstoned row', async () => {
  const firstPage = deferred()
  const store = useGenerationHistory({
    loadSessionPage: async () => firstPage.promise,
  })
  store.clearHistory()

  const refresh = store.refreshSessions({})
  store.removeEntry('deleted-row')
  firstPage.resolve({
    items: [
      { id: 'other-row', urls: ['/other.png'] },
      { id: 'deleted-row', urls: ['/deleted.png'] },
    ],
    next_cursor: 'cursor-2',
    has_more: true,
  })

  const entries = await refresh

  assert.deepEqual(entries.map((entry) => entry.id), ['other-row'])
  assert.deepEqual(store.history.value.map((entry) => entry.id), ['other-row'])
  assert.equal(store.initialLoading.value, false)
  assert.equal(store.nextCursor.value, 'cursor-2')
  assert.equal(store.hasMore.value, true)
})

test('does not merge an old detail response into a refreshed entry with the same ID', async () => {
  const detail = deferred()
  let pageCalls = 0
  const store = useGenerationHistory({
    loadSessionPage: async () => {
      pageCalls += 1
      return {
        items: [{
          id: 'same-id',
          prompt: pageCalls === 1 ? 'original summary' : 'fresh summary',
          urls: [pageCalls === 1 ? '/original.png' : '/fresh.png'],
        }],
        next_cursor: null,
        has_more: false,
      }
    },
    loadSessionDetail: async () => detail.promise,
  })
  store.clearHistory()

  await store.refreshSessions({})
  const pendingDetails = store.ensureSessionDetails('same-id')
  await store.refreshSessions({})
  detail.resolve({
    id: 'same-id',
    prompt: 'stale full detail',
    status: 'completed',
    urls: ['/stale.png'],
  })
  await pendingDetails

  const current = store.history.value.find((entry) => entry.id === 'same-id')
  assert.equal(current.prompt, 'fresh summary')
  assert.deepEqual(current.urls, ['/fresh.png'])
  assert.notEqual(current._detailsLoaded, true)
})

test('starts a new detail request immediately after refreshing the same ID', async () => {
  const firstDetail = deferred()
  const secondDetail = deferred()
  let pageCalls = 0
  let detailCalls = 0
  const store = useGenerationHistory({
    loadSessionPage: async () => {
      pageCalls += 1
      return {
        items: [{ id: 'same-id', prompt: `summary-${pageCalls}`, urls: [`/${pageCalls}.png`] }],
        next_cursor: null,
        has_more: false,
      }
    },
    loadSessionDetail: async () => {
      detailCalls += 1
      return detailCalls === 1 ? firstDetail.promise : secondDetail.promise
    },
  })
  store.clearHistory()

  await store.refreshSessions({})
  const firstRequest = store.ensureSessionDetails('same-id')
  await store.refreshSessions({})
  const secondRequest = store.ensureSessionDetails('same-id')

  assert.equal(detailCalls, 2)
  secondDetail.resolve({
    id: 'same-id',
    prompt: 'current detail',
    status: 'completed',
    urls: ['/current.png'],
  })
  const secondResult = await secondRequest
  firstDetail.resolve({
    id: 'same-id',
    prompt: 'stale detail',
    status: 'completed',
    urls: ['/stale.png'],
  })
  await firstRequest

  assert.equal(secondResult.prompt, 'current detail')
  assert.equal(store.history.value[0].prompt, 'current detail')
})

test('keeps accepted server rows current when only the next page fails', async () => {
  let calls = 0
  const store = useGenerationHistory({
    loadSessionPage: async (params) => {
      calls += 1
      if (params.cursor) throw new Error('next page unavailable')
      return {
        items: [{ id: 'matching-row', prompt: 'summary', urls: ['/matching.png'] }],
        next_cursor: 'cursor-2',
        has_more: true,
      }
    },
  })

  await store.refreshSessions({ q: 'needle beyond summary' })
  assert.equal(store.serverResultsCurrent.value, true)
  await assert.rejects(store.loadMoreSessions(), /next page unavailable/)
  assert.equal(calls, 2)
  assert.equal(store.serverResultsCurrent.value, true)

  store.invalidateSessionRequests()
  assert.equal(store.serverResultsCurrent.value, false)
})

test('reuses an unfiltered cursor chain when callers use equivalent query keys', async () => {
  const requests = []
  const store = useGenerationHistory({
    loadSessionPage: async (params) => {
      requests.push({ ...params })
      if (params.cursor) {
        return {
          items: [{ id: 'second-page', urls: ['/second.png'] }],
          next_cursor: null,
          has_more: false,
        }
      }
      return {
        items: [{ id: 'first-page', urls: ['/first.png'] }],
        next_cursor: 'cursor-2',
        has_more: true,
      }
    },
  })

  await store.refreshSessions(
    { q: '', from: undefined, to: undefined, limit: 30 },
    { queryKey: '["","all",null]' },
  )
  await store.loadMoreSessions()

  await store.ensureSessions({})

  assert.equal(requests.length, 2)
  assert.deepEqual(store.history.value.map((item) => item.id), ['first-page', 'second-page'])
})

test('coalesces an unfiltered first-page request when callers use equivalent query keys', async () => {
  const pendingPage = deferred()
  const requests = []
  const store = useGenerationHistory({
    loadSessionPage: async (params) => {
      requests.push({ ...params })
      return pendingPage.promise
    },
  })

  const firstRequest = store.refreshSessions(
    { q: '', from: undefined, to: undefined, limit: 30 },
    { queryKey: '["","all",null]' },
  )
  const equivalentRequest = store.ensureSessions({})

  assert.equal(requests.length, 1)
  pendingPage.resolve({ items: [], next_cursor: null, has_more: false })
  await Promise.all([firstRequest, equivalentRequest])
})

test('limits an oversized legacy startup cache before rendering it', async () => {
  const previousWindow = globalThis.window
  const cached = Array.from({ length: 35 }, (_, index) => ({
    id: `cached-${index}`,
    prompt: `prompt-${index}`,
    _status: 'completed',
    urls: [`/cached-${index}.png`],
    ...(index === 0
      ? {
          editDraft: { image: 'data:image/png;base64,heavy' },
          attempts: [{ details: { text_preview: 'legacy-heavy-value' } }],
          responseMeta: { raw: 'legacy-heavy-value' },
          unexpectedLegacyField: 'drop-me',
        }
      : {}),
  }))
  globalThis.window = {
    localStorage: {
      getItem: () => JSON.stringify(cached),
      setItem() {},
    },
  }

  try {
    const module = await import(`../src/composables/useGenerationHistory.js?cache-limit=${Date.now()}`)
    const store = module.useGenerationHistory({ loadSessionPage: async () => [] })
    assert.equal(store.history.value.length, 30)
    assert.deepEqual(
      store.history.value.map((entry) => entry.id),
      cached.slice(0, 30).map((entry) => entry.id),
    )
    const first = store.history.value[0]
    assert.equal('editDraft' in first, false)
    assert.equal('unexpectedLegacyField' in first, false)
    assert.deepEqual(first.attempts, [])
    assert.equal(first.responseMeta, null)
  } finally {
    globalThis.window = previousWindow
  }
})
