import test from 'node:test'
import assert from 'node:assert/strict'

import { apiClient } from '../src/api/client.js'
import {
  CANCELLED_DEFAULT_MESSAGE,
  DEFAULT_POLL_FAILURE_LIMIT,
  DEFAULT_POLL_INTERVAL_MS,
  isTaskExpiredError,
  useTaskPolling,
} from '../src/composables/useTaskPolling.js'

function mockStatus(result) {
  const originalGet = apiClient.get
  apiClient.get = async () => result
  return () => { apiClient.get = originalGet }
}

function mockStatusError(error) {
  const originalGet = apiClient.get
  apiClient.get = async () => { throw error }
  return () => { apiClient.get = originalGet }
}

function mockCancel(result) {
  const originalPost = apiClient.post
  apiClient.post = async () => result
  return () => { apiClient.post = originalPost }
}

function makeEntry(id, overrides = {}) {
  return {
    id,
    _status: 'processing',
    task: { apiId: 'api-1', taskId: 'task-1', apiName: 'Test' },
    attempts: [],
    maxWaitSeconds: null,
    expiresAt: null,
    startedAt: Date.now(),
    elapsedSeconds: 0,
    ...overrides,
  }
}

function setup(opts = {}) {
  const entries = new Map()
  let disposed = false
  const findEntry = (id) => entries.get(id) || null
  const updateEntry = (id, patch) => {
    const entry = entries.get(id)
    if (!entry) return
    entries.set(id, { ...entry, ...patch })
  }
  const elapsedForEntry = (entry) => entry?.elapsedSeconds || 0
  const polling = useTaskPolling({
    isDisposed: () => disposed,
    findEntry,
    updateEntry,
    elapsedForEntry,
    autoCleanup: false,
    ...opts,
  })
  return { entries, findEntry, updateEntry, elapsedForEntry, setDisposed: (v) => { disposed = v }, polling }
}

test('isTaskExpiredError detects 404 with Chinese task-not-found message', () => {
  assert.ok(isTaskExpiredError({ status: 404, message: '任务不存在或已过期' }))
  assert.ok(!isTaskExpiredError({ status: 500, message: '任务不存在' }))
  assert.ok(!isTaskExpiredError({ status: 404, message: 'Not found' }))
})

test('exports sensible default constants', () => {
  assert.equal(DEFAULT_POLL_INTERVAL_MS, 4000)
  assert.equal(DEFAULT_POLL_FAILURE_LIMIT, 5)
  assert.equal(CANCELLED_DEFAULT_MESSAGE, '任务已手动停止')
})

test('schedulePoll does nothing for a non-running entry', () => {
  const { entries, polling } = setup()
  const entry = makeEntry('e1', { _status: 'completed' })
  entries.set('e1', entry)
  polling.schedulePoll('e1', null, 0)
  assert.equal(polling.pollTimers.size, 0)
})

test('pollStatusOnce handles completed status with URLs', async () => {
  const { entries, polling } = setup()
  const entry = makeEntry('e1')
  entries.set('e1', entry)
  const restore = mockStatus({
    status: 'completed',
    urls: ['/api/results/img1.png'],
    api_id: 'api-1',
    api_name: 'Test',
    attempts: [{ attempt: 1 }],
    expires_at: 1234567890,
    max_wait_seconds: 60,
  })
  try {
    await polling.pollStatusOnce('e1')
    const updated = entries.get('e1')
    assert.equal(updated._status, 'completed')
    assert.deepEqual(updated.urls, ['/api/results/img1.png'])
    assert.equal(updated.attempts.length, 1)
    assert.equal(polling.pollTimers.size, 0)
  } finally {
    restore()
  }
})

test('pollStatusOnce handles completed status without URLs as failed', async () => {
  const { entries, polling } = setup()
  const entry = makeEntry('e1')
  entries.set('e1', entry)
  const restore = mockStatus({ status: 'completed', urls: [] })
  try {
    await polling.pollStatusOnce('e1')
    const updated = entries.get('e1')
    assert.equal(updated._status, 'failed')
    assert.ok(updated.errorMessage.length > 0)
  } finally {
    restore()
  }
})

test('pollStatusOnce handles cancelled status', async () => {
  const { entries, polling } = setup()
  const entry = makeEntry('e1')
  entries.set('e1', entry)
  const restore = mockStatus({ status: 'cancelled', error: 'User stopped' })
  try {
    await polling.pollStatusOnce('e1')
    const updated = entries.get('e1')
    assert.equal(updated._status, 'cancelled')
    assert.equal(updated.errorMessage, 'User stopped')
  } finally {
    restore()
  }
})

test('pollStatusOnce handles failed status', async () => {
  const { entries, polling } = setup()
  const entry = makeEntry('e1')
  entries.set('e1', entry)
  const restore = mockStatus({ status: 'failed', error: 'Provider error' })
  try {
    await polling.pollStatusOnce('e1')
    const updated = entries.get('e1')
    assert.equal(updated._status, 'failed')
    assert.equal(updated.errorMessage, 'Provider error')
  } finally {
    restore()
  }
})

test('pollStatusOnce schedules next poll for processing status', async () => {
  const { entries, polling } = setup()
  const entry = makeEntry('e1')
  entries.set('e1', entry)
  const restore = mockStatus({ status: 'processing' })
  try {
    await polling.pollStatusOnce('e1')
    const updated = entries.get('e1')
    assert.equal(updated._status, 'processing')
    assert.ok(polling.pollTimers.has('e1'))
    polling.clearAllTimers()
  } finally {
    restore()
  }
})

test('pollStatusOnce stops on 4xx error', async () => {
  const { entries, polling } = setup()
  const entry = makeEntry('e1')
  entries.set('e1', entry)
  const restore = mockStatusError({ status: 403, message: 'Forbidden' })
  try {
    await polling.pollStatusOnce('e1')
    const updated = entries.get('e1')
    assert.equal(updated._status, 'failed')
    assert.equal(updated.errorMessage, 'Forbidden')
  } finally {
    restore()
  }
})

test('pollStatusOnce retries on 5xx error and stops after failure limit', async () => {
  const { entries, polling } = setup({ failureLimit: 3 })
  const entry = makeEntry('e1')
  entries.set('e1', entry)
  const restore = mockStatusError({ status: 502, message: 'Bad Gateway' })
  try {
    for (let i = 0; i < 2; i++) {
      await polling.pollStatusOnce('e1')
      assert.equal(entries.get('e1')._status, 'processing')
      polling.clearPollTimer('e1')
    }
    await polling.pollStatusOnce('e1')
    assert.equal(entries.get('e1')._status, 'failed')
    assert.ok(entries.get('e1').errorMessage.length > 0)
  } finally {
    restore()
  }
})

test('pollStatusOnce stops on task-expired 404', async () => {
  const { entries, polling } = setup()
  const entry = makeEntry('e1')
  entries.set('e1', entry)
  const restore = mockStatusError({ status: 404, message: '任务不存在或已过期' })
  try {
    await polling.pollStatusOnce('e1')
    const updated = entries.get('e1')
    assert.equal(updated._status, 'failed')
    assert.ok(updated.errorMessage.length > 0)
  } finally {
    restore()
  }
})

test('stopWithError sets failed status and clears timers', () => {
  const { entries, polling } = setup()
  const entry = makeEntry('e1')
  entries.set('e1', entry)
  polling.stopWithError('e1', 'Something went wrong', { custom: true })
  const updated = entries.get('e1')
  assert.equal(updated._status, 'failed')
  assert.equal(updated.errorMessage, 'Something went wrong')
  assert.equal(updated.custom, true)
})

test('stopWithCancelled sets cancelled status with default message', () => {
  const { entries, polling } = setup()
  const entry = makeEntry('e1')
  entries.set('e1', entry)
  polling.stopWithCancelled('e1')
  const updated = entries.get('e1')
  assert.equal(updated._status, 'cancelled')
  assert.equal(updated.errorMessage, CANCELLED_DEFAULT_MESSAGE)
})

test('cancelTask cancels and updates entry to cancelled', async () => {
  const { entries, polling } = setup()
  const entry = makeEntry('e1')
  entries.set('e1', entry)
  const restore = mockCancel({ status: 'cancelled', error: 'Stopped' })
  try {
    const result = await polling.cancelTask('e1', entry)
    const updated = entries.get('e1')
    assert.equal(updated._status, 'cancelled')
    assert.ok(!result.alreadyCompleted)
  } finally {
    restore()
  }
})

test('cancelTask returns alreadyCompleted when task completed in the meantime', async () => {
  const { entries, polling } = setup()
  const entry = makeEntry('e1')
  entries.set('e1', entry)
  const restore = mockCancel({
    status: 'completed',
    urls: ['/api/results/img1.png'],
    api_id: 'api-1',
    api_name: 'Test',
  })
  try {
    const result = await polling.cancelTask('e1', entry)
    const updated = entries.get('e1')
    assert.equal(updated._status, 'completed')
    assert.ok(result.alreadyCompleted)
  } finally {
    restore()
  }
})

test('clearAllTimers removes all timers and failures', () => {
  const { polling } = setup()
  polling.pollTimers.set('e1', 123)
  polling.pollFailures.set('e1', 2)
  polling.clearAllTimers()
  assert.equal(polling.pollTimers.size, 0)
  assert.equal(polling.pollFailures.size, 0)
})

test('schedulePoll does nothing when disposed', () => {
  const { entries, setDisposed, polling } = setup()
  const entry = makeEntry('e1')
  entries.set('e1', entry)
  setDisposed(true)
  polling.schedulePoll('e1', null, 0)
  assert.equal(polling.pollTimers.size, 0)
})
