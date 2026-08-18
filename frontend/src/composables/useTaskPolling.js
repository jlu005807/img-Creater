import { onBeforeUnmount } from 'vue'
import { cancelGenerationTask, getGenerationStatus } from '../api/generation.js'
import { backendRouteMissingMessage, isBackendRouteMissing } from '../api/client.js'

const DEFAULT_POLL_INTERVAL_MS = 4000
const DEFAULT_POLL_FAILURE_LIMIT = 5
const TASK_EXPIRED_MESSAGE = '任务已过期或后端已重启，请重新生成'
const CANCELLED_DEFAULT_MESSAGE = '任务已手动停止'

function isTaskExpiredError(error) {
  return (
    Number(error?.status) === 404 &&
    String(error?.message || '').includes('任务不存在')
  )
}

export function useTaskPolling({
  isDisposed,
  findEntry,
  updateEntry,
  elapsedForEntry,
  isRunningStatus = (value) => ['submitting', 'queued', 'processing'].includes(value),
  pollInterval = DEFAULT_POLL_INTERVAL_MS,
  failureLimit = DEFAULT_POLL_FAILURE_LIMIT,
  autoCleanup = true,
}) {
  const pollTimers = new Map()
  const pollFailures = new Map()

  function clearPollTimer(entryId) {
    const timer = pollTimers.get(entryId)
    if (timer) (typeof window !== 'undefined' ? window.clearTimeout : clearTimeout)(timer)
    pollTimers.delete(entryId)
  }

  function clearAllTimers() {
    pollTimers.forEach((timer) => (typeof window !== 'undefined' ? window.clearTimeout : clearTimeout)(timer))
    pollTimers.clear()
    pollFailures.clear()
  }

  if (autoCleanup) {
    onBeforeUnmount(() => clearAllTimers())
  }

  function schedulePoll(entryId, taskData = null, delay = pollInterval) {
    if (isDisposed()) return
    const entry = findEntry(entryId)
    const activeTask = taskData || entry?.task
    if (!entry || !activeTask || !isRunningStatus(entry._status)) return
    clearPollTimer(entryId)
    pollTimers.set(entryId, (typeof window !== 'undefined' ? window.setTimeout : setTimeout)(() => pollStatusOnce(entryId), delay))
  }

  async function pollStatusOnce(entryId) {
    const entry = findEntry(entryId)
    const activeTask = entry?.task
    if (!entry || !activeTask || !isRunningStatus(entry._status)) return

    try {
      const result = await getGenerationStatus({
        apiId: activeTask.apiId,
        taskId: activeTask.taskId,
      })
      if (isDisposed()) return
      pollFailures.delete(entryId)

      const nextTask = { ...activeTask }
      if (result.api_name || result.api_id) {
        nextTask.apiId = result.api_id
        nextTask.apiName = result.api_name
      }
      if (result.request_id) nextTask.requestId = result.request_id
      if (result.request_url) nextTask.requestUrl = result.request_url
      if (result.upstream_request_id) nextTask.upstreamRequestId = result.upstream_request_id
      if (result.upstream_task_id) nextTask.upstreamTaskId = result.upstream_task_id
      nextTask.pollCount = result.poll_count ?? nextTask.pollCount
      nextTask.lastPollStatus = result.last_poll_status || nextTask.lastPollStatus
      nextTask.lastPollError = result.last_poll_error || ''
      if (result.wait_phase) nextTask.waitPhase = result.wait_phase
      if (result.configured_api_type) nextTask.configuredApiType = result.configured_api_type
      if (result.effective_api_type) nextTask.effectiveApiType = result.effective_api_type
      if (result.operation) nextTask.operation = result.operation
      const nextAttempts = Array.isArray(result.attempts) ? result.attempts : entry.attempts || []

      if (result.status === 'completed') {
        const urls = result.urls || []
        clearPollTimer(entryId)
        if (urls.length) {
          updateEntry(entryId, {
            task: nextTask,
            urls,
            apiName: nextTask.apiName || result.api_name || '',
            _status: 'completed',
            attempts: nextAttempts,
            expiresAt: result.expires_at ?? null,
            maxWaitSeconds: result.max_wait_seconds ?? entry.maxWaitSeconds ?? null,
            elapsedSeconds: elapsedForEntry(entry),
          })
        } else {
          updateEntry(entryId, {
            task: nextTask,
            _status: 'failed',
            attempts: nextAttempts,
            errorMessage: '任务完成但未返回图片 URL',
            maxWaitSeconds: result.max_wait_seconds ?? entry.maxWaitSeconds ?? null,
            elapsedSeconds: elapsedForEntry(entry),
          })
        }
        return
      }

      if (result.status === 'cancelled') {
        stopWithCancelled(entryId, result.error || CANCELLED_DEFAULT_MESSAGE, { task: nextTask, attempts: nextAttempts })
        return
      }

      if (result.status === 'failed') {
        stopWithError(entryId, result.error || '任务失败', { task: nextTask, attempts: nextAttempts })
        return
      }

      updateEntry(entryId, {
        task: nextTask,
        _status: result.status || 'processing',
        attempts: nextAttempts,
        expiresAt: result.expires_at ?? entry.expiresAt ?? null,
        maxWaitSeconds: result.max_wait_seconds ?? entry.maxWaitSeconds ?? null,
      })
      schedulePoll(entryId, nextTask)
    } catch (error) {
      if (isDisposed()) return
      if (isTaskExpiredError(error)) {
        stopWithError(entryId, TASK_EXPIRED_MESSAGE)
        return
      }
      if (isBackendRouteMissing(error)) {
        stopWithError(entryId, backendRouteMissingMessage('任务状态轮询'))
        return
      }
      const statusCode = Number(error?.status)
      if (statusCode >= 400 && statusCode < 500) {
        stopWithError(entryId, error.message || '查询任务状态失败')
        return
      }
      const failures = (pollFailures.get(entryId) || 0) + 1
      if (failures >= failureLimit) {
        const failMsg = '多次轮询失败: ' + (error.message || '查询任务状态失败')
        stopWithError(entryId, failMsg)
        return
      }
      pollFailures.set(entryId, failures)
      schedulePoll(entryId, activeTask)
    }
  }

  function stopWithError(entryId, message, extra = {}) {
    const entry = findEntry(entryId)
    clearPollTimer(entryId)
    pollFailures.delete(entryId)
    updateEntry(entryId, {
      _status: 'failed',
      errorMessage: message,
      elapsedSeconds: elapsedForEntry(entry),
      ...extra,
    })
  }

  function stopWithCancelled(entryId, message = CANCELLED_DEFAULT_MESSAGE, extra = {}) {
    const entry = findEntry(entryId)
    clearPollTimer(entryId)
    pollFailures.delete(entryId)
    updateEntry(entryId, {
      _status: 'cancelled',
      errorMessage: message,
      elapsedSeconds: elapsedForEntry(entry),
      ...extra,
    })
  }

  async function cancelTask(entryId, entry) {
    const taskId = entry?.task?.taskId
    if (!entry || !taskId || !isRunningStatus(entry._status)) return null

    const result = await cancelGenerationTask(taskId)
    const nextTask = mergeTaskResult(entry.task, result)
    const nextAttempts = Array.isArray(result?.attempts) ? result.attempts : entry.attempts || []

    if (findEntry(entryId)?._status === 'completed') {
      return { alreadyCompleted: true, result }
    }
    if (result?.status === 'completed') {
      clearPollTimer(entryId)
      pollFailures.delete(entryId)
      const urls = result.urls || []
      updateEntry(entryId, {
        task: nextTask,
        urls,
        apiName: nextTask.apiName || result.api_name || '',
        _status: 'completed',
        attempts: nextAttempts,
        errorMessage: '',
        expiresAt: result.expires_at ?? null,
        maxWaitSeconds: result.max_wait_seconds ?? entry.maxWaitSeconds ?? null,
        elapsedSeconds: elapsedForEntry(entry),
      })
      return { alreadyCompleted: true, result }
    }
    stopWithCancelled(entryId, result?.error || CANCELLED_DEFAULT_MESSAGE, {
      task: nextTask,
      attempts: nextAttempts,
    })
    return { result }
  }

  return {
    pollTimers,
    pollFailures,
    clearPollTimer,
    clearAllTimers,
    schedulePoll,
    pollStatusOnce,
    stopWithError,
    stopWithCancelled,
    cancelTask,
  }
}

function mergeTaskResult(existingTask, result) {
  const nextTask = { ...existingTask }
  if (!result) return nextTask
  nextTask.apiId = result.api_id || nextTask.apiId
  nextTask.apiName = result.api_name || nextTask.apiName
  nextTask.operation = result.operation || nextTask.operation
  nextTask.requestId = result.request_id || nextTask.requestId
  nextTask.requestUrl = result.request_url || nextTask.requestUrl
  nextTask.upstreamRequestId = result.upstream_request_id || nextTask.upstreamRequestId
  nextTask.upstreamTaskId = result.upstream_task_id || nextTask.upstreamTaskId
  nextTask.pollCount = result.poll_count ?? nextTask.pollCount
  nextTask.lastPollStatus = result.last_poll_status || nextTask.lastPollStatus
  nextTask.lastPollError = result.last_poll_error || ''
  nextTask.waitPhase = result.wait_phase || nextTask.waitPhase
  nextTask.configuredApiType = result.configured_api_type || nextTask.configuredApiType
  nextTask.effectiveApiType = result.effective_api_type || nextTask.effectiveApiType
  return nextTask
}

export {
  DEFAULT_POLL_INTERVAL_MS,
  DEFAULT_POLL_FAILURE_LIMIT,
  TASK_EXPIRED_MESSAGE,
  CANCELLED_DEFAULT_MESSAGE,
  isTaskExpiredError,
}
