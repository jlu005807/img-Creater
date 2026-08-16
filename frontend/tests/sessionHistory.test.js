import test from 'node:test'
import assert from 'node:assert/strict'

import * as sessionHistory from '../src/utils/sessionHistory.js'

const {
  applySessionPage,
  mapSessionSummary,
  mergeSessionHistory,
  normalizeSessionPage,
} = sessionHistory

test('recomputes rolling history time bounds from the supplied current time', () => {
  assert.equal(typeof sessionHistory.historyTimeBounds, 'function')

  const firstNow = new Date(2026, 7, 16, 10, 30, 0)
  const nextDayNow = new Date(2026, 7, 17, 10, 30, 0)
  const first = sessionHistory.historyTimeBounds('today', firstNow)
  const nextDay = sessionHistory.historyTimeBounds('today', nextDayNow)

  assert.equal(first.to, firstNow.toISOString())
  assert.equal(nextDay.to, nextDayNow.toISOString())
  assert.equal(first.from, new Date(2026, 7, 16).toISOString())
  assert.equal(nextDay.from, new Date(2026, 7, 17).toISOString())
  assert.notEqual(first.from, nextDay.from)
  assert.deepEqual(sessionHistory.historyTimeBounds('all', firstNow), {
    from: undefined,
    to: undefined,
  })
})

test('normalizes legacy arrays into the shared camelCase session shape', () => {
  const page = normalizeSessionPage([
    {
      id: 'legacy-1',
      prompt: 'legacy prompt',
      mode: 'edit',
      size: '1024x1360',
      status: 'completed',
      urls: ['/api/results/legacy-1/result.png'],
      reference_images: ['/api/results/legacy-1/references/source.png'],
      api_id: 'api-1',
      api_name: 'Provider A',
      task_id: 'task-1',
      expires_at: 123,
      created_at: '2026-08-15T01:00:00Z',
      updated_at: '2026-08-15T02:00:00Z',
      attempts: [{ api_name: 'Provider A', status: 'success' }],
    },
  ])

  assert.equal(page.hasMore, false)
  assert.equal(page.nextCursor, null)
  assert.deepEqual(page.items[0].referenceImages, ['/api/results/legacy-1/references/source.png'])
  assert.deepEqual(page.items[0].urls, ['/api/results/legacy-1/result.png'])
  assert.equal(page.items[0].createdAt, '2026-08-15T01:00:00Z')
  assert.equal(page.items[0].updatedAt, '2026-08-15T02:00:00Z')
  assert.equal(page.items[0].apiName, 'Provider A')
  assert.deepEqual(page.items[0].task, {
    taskId: 'task-1',
    apiId: 'api-1',
    apiName: 'Provider A',
  })
  assert.deepEqual(page.items[0].attempts, [{ api_name: 'Provider A', status: 'success' }])
})

test('normalizes a paginated summary and gives every image a stable session-index key', () => {
  const page = normalizeSessionPage({
    items: [
      {
        id: 'session-9',
        prompt: 'page prompt',
        status: 'completed',
        images: [
          { index: 4, url: '/first.png', filename: 'first.png', expires_at: 91 },
          { url: '/second.png' },
        ],
        reference_images: ['/reference.png'],
        created_at: '2026-08-16T01:00:00Z',
        updated_at: '2026-08-16T02:00:00Z',
        expires_at: 92,
      },
    ],
    next_cursor: 'cursor-2',
    has_more: true,
  })

  assert.equal(page.nextCursor, 'cursor-2')
  assert.equal(page.hasMore, true)
  assert.deepEqual(page.items[0].images, [
    {
      key: 'session-9:4',
      sessionId: 'session-9',
      imageIndex: 4,
      url: '/first.png',
      filename: 'first.png',
      expiresAt: 91,
    },
    {
      key: 'session-9:1',
      sessionId: 'session-9',
      imageIndex: 1,
      url: '/second.png',
      filename: 'second.png',
      expiresAt: 92,
    },
  ])
})

test('maps URL-only legacy images with stable keys that do not depend on the URL', () => {
  const first = mapSessionSummary({ id: 'same', urls: ['/old-url.png'] })
  const second = mapSessionSummary({ id: 'same', urls: ['/new-url.png'] })

  assert.equal(first.images[0].key, 'same:0')
  assert.equal(second.images[0].key, 'same:0')
})

test('deduplicates malformed image metadata by its stable session-index key', () => {
  const session = mapSessionSummary({
    id: 'same-session',
    images: [
      { index: 2, url: '/first.png' },
      { index: 2, url: '/duplicate.png' },
      { index: 3, url: '/third.png' },
    ],
  })

  assert.deepEqual(
    session.images.map((image) => [image.key, image.url]),
    [
      ['same-session:2', '/first.png'],
      ['same-session:3', '/third.png'],
    ],
  )
})

test('deduplicates page entries while preserving a running local task over an older manifest task', () => {
  const running = {
    id: 'session-1',
    prompt: 'new local request',
    time: 200,
    _origin: 'local',
    _status: 'processing',
    task: { taskId: 'task-new', apiId: 'api-new' },
    attempts: [{ apiName: 'new provider' }],
  }
  const oldManifest = mapSessionSummary({
    id: 'session-1',
    prompt: 'old persisted result',
    status: 'completed',
    task_id: 'task-old',
    updated_at: '1970-01-01T00:00:00.100Z',
    urls: ['/old.png'],
  })

  const merged = mergeSessionHistory(
    [running],
    [oldManifest, oldManifest, mapSessionSummary({ id: 'session-2', urls: ['/two.png'] })],
  )

  assert.equal(merged.length, 2)
  assert.equal(merged.find((item) => item.id === 'session-1').prompt, 'new local request')
  assert.equal(merged.find((item) => item.id === 'session-1').task.taskId, 'task-new')
  assert.deepEqual(merged.find((item) => item.id === 'session-1').attempts, [{ apiName: 'new provider' }])
})

test('preserves existing attempts when the matching server summary omits them', () => {
  const existing = {
    id: 'session-1',
    _origin: 'local',
    _status: 'processing',
    task: { taskId: 'task-1' },
    attempts: [{ apiName: 'Provider A', status: 'failed' }],
  }
  const completed = mapSessionSummary({
    id: 'session-1',
    status: 'completed',
    task_id: 'task-1',
    urls: ['/done.png'],
  })

  const [merged] = mergeSessionHistory([existing], [completed])

  assert.equal(merged._status, 'completed')
  assert.deepEqual(merged.attempts, existing.attempts)
})

test('refresh replaces server entries, preserves local entries, and records the next page', () => {
  const current = [
    { id: 'server-old', _origin: 'server', _status: 'completed', time: 10 },
    { id: 'local-running', _origin: 'local', _status: 'queued', time: 30 },
  ]
  const result = applySessionPage(
    current,
    {
      items: [{ id: 'server-new', status: 'completed', updated_at: '1970-01-01T00:00:00.020Z' }],
      next_cursor: 'next-page',
      has_more: true,
    },
    { replaceServer: true },
  )

  assert.deepEqual(result.items.map((item) => item.id), ['local-running', 'server-new'])
  assert.equal(result.nextCursor, 'next-page')
  assert.equal(result.hasMore, true)
})

test('refresh drops completed local cache entries that are absent from the backend page', () => {
  const current = [
    { id: 'local-completed', _origin: 'local', _status: 'completed', urls: ['/stale.png'] },
    { id: 'local-failed', _origin: 'local', _status: 'failed', errorMessage: 'retryable' },
    { id: 'local-running', _origin: 'local', _status: 'processing', task: { taskId: 'task-1' } },
  ]

  const result = applySessionPage(current, { items: [], has_more: false }, { replaceServer: true })

  assert.deepEqual(result.items.map((item) => item.id), ['local-failed', 'local-running'])
})

test('preserves a local failed task over an older completed manifest with another task id', () => {
  const localFailure = {
    id: 'session-1',
    _origin: 'local',
    _status: 'failed',
    task: { taskId: 'task-new' },
    errorMessage: 'new attempt failed',
  }
  const oldManifest = mapSessionSummary({
    id: 'session-1',
    status: 'completed',
    task_id: 'task-old',
    urls: ['/old.png'],
  })

  const [merged] = applySessionPage(
    [localFailure],
    { items: [oldManifest], has_more: false },
    { replaceServer: true },
  ).items

  assert.equal(merged._status, 'failed')
  assert.equal(merged.task.taskId, 'task-new')
  assert.equal(merged.errorMessage, 'new attempt failed')
})

test('keeps cursor order when summaries have no timestamps', () => {
  const result = applySessionPage([], {
    items: [
      { id: 'newest-page-item', urls: ['/newest.png'] },
      { id: 'older-page-item', urls: ['/older.png'] },
    ],
    has_more: false,
    next_cursor: null,
  })

  assert.deepEqual(result.items.map((item) => item.id), ['newest-page-item', 'older-page-item'])
})

test('a repeated or missing next cursor terminates pagination instead of looping', () => {
  const repeated = applySessionPage(
    [],
    { items: [], next_cursor: 'cursor-1', has_more: true },
    { requestedCursor: 'cursor-1' },
  )
  const seen = applySessionPage(
    [],
    { items: [], next_cursor: 'cursor-2', has_more: true },
    { seenCursors: new Set(['cursor-2']) },
  )
  const missing = applySessionPage([], { items: [], next_cursor: null, has_more: true })

  for (const result of [repeated, seen, missing]) {
    assert.equal(result.nextCursor, null)
    assert.equal(result.hasMore, false)
  }
})
