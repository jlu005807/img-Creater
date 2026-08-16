import test from 'node:test'
import assert from 'node:assert/strict'

import { createHistoryInteractionGuard } from '../src/utils/historyInteraction.js'

function deferred() {
  let resolve
  const promise = new Promise((done) => { resolve = done })
  return { promise, resolve }
}

async function recall(id, detailPromise, guard, commits) {
  const token = guard.begin()
  const detail = await detailPromise
  if (!guard.isCurrent(token)) return
  commits.push({ id, detail })
}

test('stale A cannot commit after out-of-order B recall wins', async () => {
  const guard = createHistoryInteractionGuard()
  const first = deferred()
  const second = deferred()
  const commits = []

  const recallA = recall('A', first.promise, guard, commits)
  const recallB = recall('B', second.promise, guard, commits)
  second.resolve({ prompt: 'B' })
  await recallB
  first.resolve({ prompt: 'A' })
  await recallA

  assert.deepEqual(commits, [{ id: 'B', detail: { prompt: 'B' } }])
})

test('deleting the selected entry invalidates a pending detail commit', async () => {
  const guard = createHistoryInteractionGuard()
  const pending = deferred()
  const commits = []

  const recallA = recall('A', pending.promise, guard, commits)
  guard.invalidate()
  pending.resolve({ prompt: 'A' })
  await recallA

  assert.deepEqual(commits, [])
})

test('retry starts a new interaction and invalidates an older recall', async () => {
  const guard = createHistoryInteractionGuard()
  const pendingRecall = deferred()
  const commits = []

  const recallA = recall('A', pendingRecall.promise, guard, commits)
  const retryToken = guard.begin()
  commits.push({ id: 'retry', detail: { token: retryToken } })
  pendingRecall.resolve({ prompt: 'A' })
  await recallA

  assert.deepEqual(commits, [{ id: 'retry', detail: { token: retryToken } }])
})
