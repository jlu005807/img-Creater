<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { Delete, Download, FullScreen, MagicStick, Picture, Plus, Refresh, RefreshLeft, ZoomIn } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  cancelGenerationTask,
  deleteSession,
  deleteSessions,
  editImage,
  generateImages,
  getEditDraft,
  getGenerationStatus,
  saveEditDraft,
} from '../../api/generation'
import { backendRouteMissingMessage, isBackendRouteMissing } from '../../api/client'
import { downloadImage } from '../../utils/download'
import { useGenerationHistory } from '../../composables/useGenerationHistory'
import { useSettings } from '../../composables/useSettings'
import { usePromptTemplates } from '../../composables/usePromptTemplates'
import RegionEditor from '../RegionEditor/index.vue'

const { settings } = useSettings()
const { templates, loadTemplates, pendingFill, requestRandomFill } = usePromptTemplates()
const promptZoomOpen = ref(false)

// A template chosen in Settings fills the prompt here.
watch(pendingFill, (val) => {
  if (val && val.text != null) {
    form.prompt = val.text
    switchMode('generate')
  }
})

const POLL_INTERVAL_MS = 4000
const DRAFT_KEY = 'studio-form-draft'
const HISTORY_WIDTH = '276px'

const form = reactive({
  prompt: '',
  size: '1024x1024',
})

const promptChars = computed(() => form.prompt.length)

// ---- reference images (generate mode) ----
const referenceImages = ref([]) // array of data URLs
const refInputRef = ref(null)

function openRefPicker() {
  refInputRef.value?.click()
}

function addReferenceFiles(fileList) {
  const files = Array.from(fileList || [])
  for (const file of files) {
    if (!file.type.startsWith('image/')) continue
    if (referenceImages.value.length >= settings.maxReferenceImages) {
      ElMessage.warning(`最多上传 ${settings.maxReferenceImages} 张参考图（可在设置中调整）`)
      break
    }
    const reader = new FileReader()
    reader.onload = () => {
      if (referenceImages.value.length < settings.maxReferenceImages) {
        referenceImages.value.push(reader.result)
      }
    }
    reader.readAsDataURL(file)
  }
}

function addPromptReferenceFiles(fileList) {
  const files = Array.from(fileList || []).filter((file) => file.type?.startsWith('image/'))
  if (!files.length) return false
  switchMode('generate')
  addReferenceFiles(files)
  ElMessage.success('已添加为参考图')
  return true
}

function onPromptPaste(event) {
  if (addPromptReferenceFiles(event.clipboardData?.files)) {
    event.preventDefault()
  }
}

function onPromptDrop(event) {
  if (addPromptReferenceFiles(event.dataTransfer?.files)) {
    event.preventDefault()
  }
}

function onRefInput(event) {
  addReferenceFiles(event.target.files)
  event.target.value = ''
}

function removeReference(index) {
  referenceImages.value.splice(index, 1)
}

const mode = ref('generate')
const maskState = ref({ hasImage: false, hasMask: false })
const regionEditorRef = ref(null)
const syncedEditImageRevision = ref(0)
const restoringEditDraft = ref(false)

function switchMode(nextMode) {
  if (mode.value === nextMode) return
  persistCurrentEditDraft()
  mode.value = nextMode
}

// ---- size controls ----
const ratioPresets = [
  { label: '1:1', w: 1024, h: 1024 },
  { label: '4:3', w: 1360, h: 1024 },
  { label: '3:4', w: 1024, h: 1360 },
  { label: '16:9', w: 1536, h: 864 },
  { label: '9:16', w: 864, h: 1536 },
]
const sizeW = ref(1024)
const sizeH = ref(1024)

function parseSize(str) {
  const parts = (str || '1024x1024').split('x')
  sizeW.value = parseInt(parts[0], 10) || 1024
  sizeH.value = parseInt(parts[1], 10) || 1024
  return { w: sizeW.value, h: sizeH.value }
}

function applyRatio(preset) {
  sizeW.value = preset.w
  sizeH.value = preset.h
}

watch([sizeW, sizeH], () => {
  form.size = `${sizeW.value}x${sizeH.value}`
})

// ---- rest of state ----
const {
  history,
  addEntry,
  updateEntry,
  removeEntry,
  clearHistory: clearStoredHistory,
  loadPersistedSessions,
} = useGenerationHistory()
const displayHistoryId = ref(null)
const clockNow = ref(Date.now())

const pollTimers = new Map()
const draftSaveTimers = new Map()
let clockTimer = null

function isRunningStatus(value) {
  return ['submitting', 'queued', 'processing'].includes(value)
}

function isStoppedStatus(value) {
  return ['failed', 'timeout', 'cancelled'].includes(value)
}

function findHistoryEntry(id) {
  return history.value.find((entry) => entry.id === id) || null
}

const activeEntry = computed(() => findHistoryEntry(displayHistoryId.value))
const status = computed(() => activeEntry.value?._status || 'idle')
const task = computed(() => activeEntry.value?.task || null)
const images = computed(() => activeEntry.value?.urls || [])
const currentImage = computed(() => images.value[0] || '')
const attempts = computed(() => activeEntry.value?.attempts || [])
const expiresAt = computed(() => activeEntry.value?.expiresAt ?? null)
const errorMessage = computed(() => activeEntry.value?.errorMessage || '')
const loading = computed(() => isRunningStatus(status.value))
const needsProvider = computed(() => typeof errorMessage.value === 'string' && errorMessage.value.includes('API 配置'))
const elapsedSeconds = computed(() => elapsedForEntry(activeEntry.value))
const maxWaitSeconds = computed(() => {
  const value = Number(activeEntry.value?.maxWaitSeconds)
  return Number.isFinite(value) && value > 0 ? value : null
})
const waitLimitLabel = computed(() => (maxWaitSeconds.value ? `${maxWaitSeconds.value}s` : '持续等待'))
const monitorFloating = ref(false)
const monitorPos = reactive({ x: 720, y: 96 })
const monitorDrag = reactive({ active: false, dx: 0, dy: 0 })
const resultFloating = ref(false)
const resultPos = reactive({ x: 760, y: 280 })
const resultDrag = reactive({ active: false, dx: 0, dy: 0 })
const monitorStyle = computed(() =>
  monitorFloating.value
    ? {
        position: 'fixed',
        left: `${monitorPos.x}px`,
        top: `${monitorPos.y}px`,
        width: '520px',
        zIndex: 30,
      }
    : {},
)
const resultStyle = computed(() =>
  resultFloating.value
    ? {
        position: 'fixed',
        left: `${resultPos.x}px`,
        top: `${resultPos.y}px`,
        width: 'min(760px, calc(100vw - 24px))',
        height: 'min(620px, calc(100vh - 24px))',
        zIndex: 29,
      }
    : {},
)

const expiresLabel = computed(() => {
  const raw = Number(expiresAt.value)
  if (!Number.isFinite(raw) || raw <= 0) return ''
  const ms = raw < 1e12 ? raw * 1000 : raw
  try {
    return new Date(ms).toLocaleString()
  } catch {
    return ''
  }
})

const statusLabel = computed(() => {
  const labels = {
    idle: '等待提交',
    submitting: '提交中',
    queued: '排队中',
    processing: '生成中',
    completed: '已完成',
    failed: '失败',
    timeout: '超时',
    cancelled: '已停止',
  }
  return labels[status.value] || status.value
})

const buttonText = computed(() => {
  return mode.value === 'edit' ? '提交局部修改' : '生成图片'
})

const showSkeleton = computed(() => loading.value && !images.value.length)
const operationLabel = computed(() => ((activeEntry.value?.mode || mode.value) === 'edit' ? '局部编辑' : '文生图'))

function apiTypeText(value) {
  if (value === 'auto') return '自动识别'
  if (value === 'async') return '异步中转'
  if (value === 'custom') return '自定义 URL'
  if (value === 'chat') return 'Chat'
  if (value === 'openai') return 'OpenAI'
  return value || ''
}

function attemptProtocolLabel(attempt) {
  const configured = apiTypeText(attempt?.configured_api_type)
  const effective = apiTypeText(attempt?.effective_api_type)
  if (configured && effective && configured !== effective) return `${configured} -> ${effective}`
  return effective || configured
}

function attemptErrorText(attempt) {
  if (!attempt || attempt.ok) return ''
  const parts = []
  if (attempt.error) parts.push(String(attempt.error))
  const details = attempt.details
  if (details && typeof details === 'object') {
    const upstream = details.upstream
    const upstreamError = upstream?.error || details.error
    if (upstreamError) {
      if (typeof upstreamError === 'string') {
        parts.push(upstreamError)
      } else {
        const message = upstreamError.message || upstreamError.error || upstreamError.detail || upstreamError.reason
        const code = upstreamError.code || upstreamError.type
        if (message && code) parts.push(`${message} (${code})`)
        else if (message || code) parts.push(String(message || code))
      }
    }
  }
  return [...new Set(parts.filter(Boolean))].join(' · ')
}

function floatMonitor() {
  monitorFloating.value = true
}

function resetMonitorPosition() {
  monitorFloating.value = false
  monitorDrag.active = false
}

function floatResult() {
  resultFloating.value = true
}

function resetResultPosition() {
  resultFloating.value = false
  resultDrag.active = false
}

function startMonitorDrag(event) {
  if (!monitorFloating.value) return
  monitorDrag.active = true
  monitorDrag.dx = event.clientX - monitorPos.x
  monitorDrag.dy = event.clientY - monitorPos.y
  window.addEventListener('pointermove', moveMonitor)
  window.addEventListener('pointerup', stopMonitorDrag, { once: true })
}

function startResultDrag(event) {
  if (!resultFloating.value) return
  resultDrag.active = true
  resultDrag.dx = event.clientX - resultPos.x
  resultDrag.dy = event.clientY - resultPos.y
  window.addEventListener('pointermove', moveResult)
  window.addEventListener('pointerup', stopResultDrag, { once: true })
}

function moveMonitor(event) {
  if (!monitorDrag.active) return
  const maxX = Math.max(0, window.innerWidth - 540)
  const maxY = Math.max(0, window.innerHeight - 260)
  monitorPos.x = Math.min(maxX, Math.max(0, event.clientX - monitorDrag.dx))
  monitorPos.y = Math.min(maxY, Math.max(0, event.clientY - monitorDrag.dy))
}

function stopMonitorDrag() {
  monitorDrag.active = false
  window.removeEventListener('pointermove', moveMonitor)
}

function moveResult(event) {
  if (!resultDrag.active) return
  const maxX = Math.max(0, window.innerWidth - 320)
  const maxY = Math.max(0, window.innerHeight - 220)
  resultPos.x = Math.min(maxX, Math.max(0, event.clientX - resultDrag.dx))
  resultPos.y = Math.min(maxY, Math.max(0, event.clientY - resultDrag.dy))
}

function stopResultDrag() {
  resultDrag.active = false
  window.removeEventListener('pointermove', moveResult)
}

// ---- history search + time filter ----
const historyQuery = ref('')
const historyTimeFilter = ref('all') // all | today | week | month
const historySort = ref('time_desc') // time_desc | time_asc | prompt_asc | prompt_desc
const historyTimeOptions = [
  { value: 'all', label: '全部' },
  { value: 'today', label: '今天' },
  { value: 'week', label: '本周' },
  { value: 'month', label: '本月' },
]
const historySortOptions = [
  { value: 'time_desc', label: '最新' },
  { value: 'time_asc', label: '最早' },
  { value: 'prompt_asc', label: '提示词 A-Z' },
  { value: 'prompt_desc', label: '提示词 Z-A' },
]

function withinTimeFilter(ts) {
  if (historyTimeFilter.value === 'all') return true
  const now = new Date()
  const d = new Date(ts)
  if (historyTimeFilter.value === 'today') {
    return d.toDateString() === now.toDateString()
  }
  if (historyTimeFilter.value === 'week') {
    const start = new Date(now)
    const day = (now.getDay() + 6) % 7 // Monday = 0
    start.setHours(0, 0, 0, 0)
    start.setDate(now.getDate() - day)
    return d >= start
  }
  if (historyTimeFilter.value === 'month') {
    return d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth()
  }
  return true
}

// ---- history items with live status + filtering ----
const historyItems = computed(() => {
  const q = historyQuery.value.trim().toLowerCase()
  return history.value
    .filter((entry) => {
      if (!withinTimeFilter(entry.time)) return false
      if (q && !(entry.prompt || '').toLowerCase().includes(q)) return false
      return true
    })
    .sort((a, b) => {
      if (historySort.value === 'time_asc') return Number(a.time || 0) - Number(b.time || 0)
      if (historySort.value === 'prompt_asc') return String(a.prompt || '').localeCompare(String(b.prompt || ''))
      if (historySort.value === 'prompt_desc') return String(b.prompt || '').localeCompare(String(a.prompt || ''))
      return Number(b.time || 0) - Number(a.time || 0)
    })
    .map((entry) => {
      return { ...entry, _status: entry._status || 'completed' }
    })
})

function historyStatusIcon(item) {
  if (item._status === 'completed') return '✓'
  if (isStoppedStatus(item._status)) return '✗'
  return '⟳'
}

function historyStatusClass(item) {
  if (item._status === 'completed') return 'text-[var(--studio-green)]'
  if (isStoppedStatus(item._status)) return 'text-[var(--studio-coral)]'
  return 'text-[var(--studio-teal)] animate-spin'
}

function historyPreview(prompt) {
  const text = (prompt || '').trim()
  return text.length > 20 ? text.slice(0, 20) + '…' : text
}

function historyTime(ts) {
  try {
    const d = new Date(ts)
    return `${d.getHours().toString().padStart(2, '0')}:${d.getMinutes().toString().padStart(2, '0')}`
  } catch {
    return ''
  }
}

// ---- timers ----
function clearTimers() {
  pollTimers.forEach((timer) => window.clearTimeout(timer))
  pollTimers.clear()
  draftSaveTimers.forEach((timer) => window.clearTimeout(timer))
  draftSaveTimers.clear()
  if (clockTimer) window.clearInterval(clockTimer)
  clockTimer = null
}

function clearPollTimer(entryId) {
  const timer = pollTimers.get(entryId)
  if (timer) window.clearTimeout(timer)
  pollTimers.delete(entryId)
}

function clearDraftSaveTimer(entryId) {
  const timer = draftSaveTimers.get(entryId)
  if (timer) window.clearTimeout(timer)
  draftSaveTimers.delete(entryId)
}

function startClock() {
  if (clockTimer) return
  clockTimer = window.setInterval(() => {
    clockNow.value = Date.now()
    if (!history.value.some((entry) => isRunningStatus(entry._status))) {
      window.clearInterval(clockTimer)
      clockTimer = null
    }
  }, 1000)
}

function elapsedForEntry(entry) {
  if (!entry) return 0
  if (isRunningStatus(entry._status) && entry.startedAt) {
    return Math.max(0, Math.floor((clockNow.value - Number(entry.startedAt)) / 1000))
  }
  return Number(entry.elapsedSeconds || 0)
}

// ---- submit + poll ----
async function submitTask(reuseId = null) {
  if (!form.prompt.trim()) {
    ElMessage.warning('请输入 Prompt')
    return
  }

  let editPayload = null
  let editDraft = null
  if (mode.value === 'edit') {
    editPayload = regionEditorRef.value?.exportPayload()
    if (!editPayload) {
      ElMessage.warning('请先上传原图并框选或涂抹需要修改的区域')
      return
    }
    editDraft = regionEditorRef.value?.exportDraft?.()
  }

  // Reuse an existing history entry on retry (overwrite), else create a new one.
  let entryId = reuseId
  if (reuseId && history.value.some((e) => e.id === reuseId)) {
    updateEntry(reuseId, {
      prompt: form.prompt.trim(),
      mode: mode.value,
      size: form.size,
      urls: [],
      apiName: '',
      imageCount: 0,
      time: Date.now(),
      _status: 'submitting',
      startedAt: Date.now(),
      elapsedSeconds: 0,
      task: null,
      errorMessage: '',
      attempts: [],
      expiresAt: null,
      editDraft,
    })
  } else {
    const entry = addEntry({
      prompt: form.prompt.trim(),
      mode: mode.value,
      size: form.size,
      urls: [],
      apiName: '',
      _status: 'submitting',
      startedAt: Date.now(),
      elapsedSeconds: 0,
      task: null,
      errorMessage: '',
      attempts: [],
      expiresAt: null,
      editDraft,
    })
    entryId = entry.id
  }
  displayHistoryId.value = entryId
  if (editDraft) {
    persistEditDraftNow(entryId, editDraft)
  }
  startClock()

  try {
    const basePayload = {
      prompt: form.prompt.trim(),
      size: form.size,
      n: 1,
      history_id: entryId,
    }
    const result =
      mode.value === 'edit'
        ? await editImage({
            ...basePayload,
            image: editPayload.image,
            mask: editPayload.mask,
            composite: editPayload.composite,
            edit_mode: 'mask',
            selection: editPayload.selection,
          })
        : await generateImages({
            ...basePayload,
            ...(referenceImages.value.length ? { reference_images: referenceImages.value } : {}),
          })

    const nextTask = {
      apiId: result.api_id,
      taskId: result.task_id,
      apiName: result.api_name,
      operation: result.operation || mode.value,
      configuredApiType: result.configured_api_type,
      effectiveApiType: result.effective_api_type,
    }
    updateEntry(entryId, {
      task: nextTask,
      attempts: result.attempts || [],
      _status: result.status || 'queued',
      maxWaitSeconds: result.max_wait_seconds ?? null,
      errorMessage: '',
    })
    schedulePoll(entryId, nextTask)
  } catch (error) {
    stopWithError(entryId, error.message || '提交任务失败')
  }
}

function schedulePoll(entryId, taskData = null, delay = POLL_INTERVAL_MS) {
  const entry = findHistoryEntry(entryId)
  const activeTask = taskData || entry?.task
  if (!entry || !activeTask || !isRunningStatus(entry._status)) return
  clearPollTimer(entryId)
  pollTimers.set(entryId, window.setTimeout(() => pollStatusOnce(entryId), delay))
}

async function pollStatusOnce(entryId) {
  const entry = findHistoryEntry(entryId)
  const activeTask = entry?.task
  if (!entry || !activeTask || !isRunningStatus(entry._status)) return

  try {
    const result = await getGenerationStatus({
      apiId: activeTask.apiId,
      taskId: activeTask.taskId,
    })

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
    if (result.operation) {
      nextTask.operation = result.operation
    }
    const nextAttempts = Array.isArray(result.attempts) ? result.attempts : entry.attempts || []

    if (result.status === 'completed') {
      const urls = result.urls || []
      clearPollTimer(entryId)
      if (urls.length) {
        updateEntry(entryId, {
          task: nextTask,
          urls,
          apiName: nextTask.apiName || result.api_name || '',
          imageCount: urls.length,
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
      stopWithCancelled(entryId, result.error || '任务已手动停止', { task: nextTask, attempts: nextAttempts })
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
    const message = isBackendRouteMissing(error)
      ? backendRouteMissingMessage('任务状态轮询')
      : error.message || '查询任务状态失败'
    stopWithError(entryId, message)
  }
}

function stopWithError(entryId, message, extra = {}) {
  const entry = findHistoryEntry(entryId)
  clearPollTimer(entryId)
  updateEntry(entryId, {
    _status: 'failed',
    errorMessage: message,
    elapsedSeconds: elapsedForEntry(entry),
    ...extra,
  })
}

function stopWithCancelled(entryId, message = '任务已手动停止', extra = {}) {
  const entry = findHistoryEntry(entryId)
  clearPollTimer(entryId)
  updateEntry(entryId, {
    _status: 'cancelled',
    errorMessage: message,
    elapsedSeconds: elapsedForEntry(entry),
    ...extra,
  })
}

async function stopActiveTask() {
  const entry = activeEntry.value
  const taskId = entry?.task?.taskId
  if (!entry || !taskId || !isRunningStatus(entry._status)) return
  try {
    await ElMessageBox.confirm(
      '停止后前端将不再等待该任务；如果上游接口不支持取消，已经发出的请求可能仍会在服务商侧继续执行。',
      '停止任务',
      {
        type: 'warning',
        confirmButtonText: '停止',
        cancelButtonText: '继续等待',
      },
    )
    const result = await cancelGenerationTask(taskId)
    stopWithCancelled(entry.id, result?.error || '任务已手动停止', {
      task: {
        ...entry.task,
        apiId: result?.api_id || entry.task.apiId,
        apiName: result?.api_name || entry.task.apiName,
        operation: result?.operation || entry.task.operation,
        requestId: result?.request_id || entry.task.requestId,
        requestUrl: result?.request_url || entry.task.requestUrl,
        upstreamRequestId: result?.upstream_request_id || entry.task.upstreamRequestId,
        upstreamTaskId: result?.upstream_task_id || entry.task.upstreamTaskId,
        pollCount: result?.poll_count ?? entry.task.pollCount,
        lastPollStatus: result?.last_poll_status || entry.task.lastPollStatus,
        lastPollError: result?.last_poll_error || '',
        waitPhase: result?.wait_phase || entry.task.waitPhase,
        configuredApiType: result?.configured_api_type || entry.task.configuredApiType,
        effectiveApiType: result?.effective_api_type || entry.task.effectiveApiType,
      },
      attempts: Array.isArray(result?.attempts) ? result.attempts : entry.attempts || [],
    })
    ElMessage.success('已停止本地等待')
  } catch (error) {
    if (error === 'cancel') return
    ElMessage.error(error.message || '停止任务失败')
  }
}

async function reusePrompt() {
  try {
    if (!templates.value.length) {
      await loadTemplates()
    }
    if (!templates.value.length) {
      ElMessage.warning('暂无可用模板，请先在设置中添加提示词模板')
      return
    }
    if (form.prompt.trim()) {
      await ElMessageBox.confirm('当前提示词不为空，是否用随机示例覆盖？', '覆盖提示词', {
        type: 'warning',
        confirmButtonText: '覆盖',
        cancelButtonText: '取消',
      })
    }
    const item = requestRandomFill()
    if (item) ElMessage.success(`已填入示例：${item.title}`)
  } catch (error) {
    if (error !== 'cancel') ElMessage.error(error.message || '示例加载失败')
  }
}

function onMaskChange(nextState) {
  maskState.value = nextState
  const revision = Number(nextState?.imageRevision || 0)
  const width = Number(nextState?.imageWidth)
  const height = Number(nextState?.imageHeight)
  if (
    nextState?.hasImage &&
    revision &&
    revision !== syncedEditImageRevision.value &&
    Number.isInteger(width) &&
    width > 0 &&
    Number.isInteger(height) &&
    height > 0
  ) {
    sizeW.value = width
    sizeH.value = height
    syncedEditImageRevision.value = revision
  }
  if (!nextState?.hasImage) {
    syncedEditImageRevision.value = 0
  }
  if (!restoringEditDraft.value && mode.value === 'edit' && displayHistoryId.value && nextState?.draft) {
    updateEntry(displayHistoryId.value, { editDraft: nextState.draft })
    scheduleEditDraftSave(displayHistoryId.value, nextState.draft)
  }
}

async function downloadOne(url, index) {
  const ok = await downloadImage(url, `img-Creater-${index + 1}`)
  if (!ok) {
    ElMessage.info('图片为跨域链接，已在新标签页打开，可右键另存为')
  }
}

function persistCurrentEditDraft() {
  if (mode.value !== 'edit' || !displayHistoryId.value) return
  const draft = regionEditorRef.value?.exportDraft?.()
  if (!draft) return
  updateEntry(displayHistoryId.value, { editDraft: draft })
  persistEditDraftNow(displayHistoryId.value, draft)
}

function scheduleEditDraftSave(entryId, draft) {
  if (!entryId || !draft) return
  clearDraftSaveTimer(entryId)
  draftSaveTimers.set(
    entryId,
    window.setTimeout(() => {
      draftSaveTimers.delete(entryId)
      persistEditDraftNow(entryId, draft)
    }, 500),
  )
}

async function persistEditDraftNow(entryId, draft) {
  if (!entryId || !draft) return
  try {
    await saveEditDraft(entryId, draft)
  } catch {
    // Draft persistence is best-effort; generation history remains usable.
  }
}

async function restoreEditDraftForEntry(entry) {
  restoringEditDraft.value = true
  try {
    let draft = entry.editDraft || null
    if (!draft) {
      draft = await getEditDraft(entry.id)
      if (draft) updateEntry(entry.id, { editDraft: draft })
    }
    if (draft) {
      await regionEditorRef.value?.restoreDraft?.(draft)
    } else {
      regionEditorRef.value?.clearAll?.()
    }
  } catch {
    regionEditorRef.value?.clearAll?.()
  } finally {
    restoringEditDraft.value = false
  }
}

// ---- history actions ----
async function recallHistory(entry) {
  persistCurrentEditDraft()
  displayHistoryId.value = entry.id
  form.prompt = entry.prompt || ''
  if (entry.size) parseSize(entry.size)
  if (entry.mode) mode.value = entry.mode
  if (entry.mode === 'edit') {
    await restoreEditDraftForEntry(entry)
  } else {
    regionEditorRef.value?.clearAll?.()
    maskState.value = { hasImage: false, hasMask: false }
  }
  ElMessage.success('已切换到该历史记录')
}

async function deleteHistory(entry) {
  try {
    await ElMessageBox.confirm('删除该会话记录？', '确认删除', {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消',
    })
    clearPollTimer(entry.id)
    clearDraftSaveTimer(entry.id)
    try {
      await deleteSession(entry.id)
    } catch {
      ElMessage.warning('后端会话目录删除失败，本地记录已移除')
    }
    removeEntry(entry.id)
    if (displayHistoryId.value === entry.id) {
      const nextEntry = history.value[0] || null
      displayHistoryId.value = null
      if (nextEntry) {
        await recallHistory(nextEntry)
      } else {
        regionEditorRef.value?.clearAll?.()
        maskState.value = { hasImage: false, hasMask: false }
      }
    }
  } catch (error) {
    if (error !== 'cancel') ElMessage.error(error.message || '删除失败')
  }
}

function newConversation() {
  persistCurrentEditDraft()
  displayHistoryId.value = null
  form.prompt = ''
  mode.value = 'generate'
  parseSize('1024x1024')
  referenceImages.value = []
  regionEditorRef.value?.clearAll?.()
  maskState.value = { hasImage: false, hasMask: false }
}

async function clearAllHistory() {
  try {
    await ElMessageBox.confirm('删除全部会话历史？该操作不可撤销。', '确认删除全部', {
      type: 'warning',
      confirmButtonText: '删除全部',
      cancelButtonText: '取消',
    })
    clearTimers()
    try {
      await deleteSessions()
    } catch {
      ElMessage.warning('后端会话目录清空失败，本地记录已清空')
    }
    displayHistoryId.value = null
    clearStoredHistory()
    regionEditorRef.value?.clearAll?.()
    maskState.value = { hasImage: false, hasMask: false }
  } catch (error) {
    if (error !== 'cancel') ElMessage.error(error.message || '清空失败')
  }
}

// Retry a failed entry with the same params and overwrite it (no new entry).
async function retryHistory(entry) {
  persistCurrentEditDraft()
  form.prompt = entry.prompt || ''
  if (entry.size) parseSize(entry.size)
  if (entry.mode) mode.value = entry.mode
  if (entry.mode === 'edit') {
    displayHistoryId.value = entry.id
    await restoreEditDraftForEntry(entry)
  }
  submitTask(entry.id)
}

// ---- draft ----
function restoreDraft() {
  try {
    const draft = JSON.parse(window.localStorage.getItem(DRAFT_KEY))
    if (draft && typeof draft === 'object') {
      form.prompt = draft.prompt || ''
      if (draft.size) parseSize(draft.size)
      if (draft.mode) mode.value = draft.mode
    }
  } catch {
    /* ignore malformed draft */
  }
}

function restoreRunningTasks() {
  const now = Date.now()
  let hasRunning = false
  history.value.forEach((entry) => {
    if (!isRunningStatus(entry._status) || !entry.task?.taskId) return
    hasRunning = true
    if (!entry.startedAt) {
      updateEntry(entry.id, { startedAt: now })
    }
    schedulePoll(entry.id, entry.task, 500)
  })
  if (hasRunning) startClock()
}

watch([() => form.prompt, () => form.size, mode], () => {
  try {
    window.localStorage.setItem(
      DRAFT_KEY,
      JSON.stringify({ prompt: form.prompt, size: form.size, mode: mode.value }),
    )
  } catch {
    /* ignore quota / availability errors */
  }
})

onMounted(async () => {
  restoreDraft()
  try {
    await loadPersistedSessions()
  } catch (error) {
    ElMessage.warning(error.message || '后端历史会话加载失败，仅显示本地历史')
  }
  restoreRunningTasks()
})
onBeforeUnmount(() => {
  clearTimers()
  window.removeEventListener('pointermove', moveMonitor)
  window.removeEventListener('pointermove', moveResult)
})

watch(
  history,
  (items) => {
    if (!displayHistoryId.value && items.length) {
      displayHistoryId.value = items[0].id
    }
  },
  { immediate: true },
)
</script>

<template>
  <div class="flex h-full min-h-0">
    <!-- History sidebar (permanent left panel) -->
    <aside
      class="flex shrink-0 flex-col border-r border-[var(--studio-line)] bg-[var(--studio-panel)]"
      :style="{ width: HISTORY_WIDTH }"
    >
      <div class="flex items-center justify-between border-b border-[var(--studio-line)] px-4 py-3">
        <h2 class="text-sm font-black text-[var(--studio-ink)]">会话历史</h2>
        <div class="flex items-center gap-1">
          <el-button
            text
            size="small"
            :icon="Plus"
            aria-label="新建空白对话"
            @click="newConversation"
          />
          <el-button
            v-if="history.length"
            text
            size="small"
            type="danger"
            :icon="Delete"
            aria-label="清空全部历史"
            @click="clearAllHistory"
          />
        </div>
      </div>

      <!-- Search + time filter -->
      <div v-if="history.length" class="space-y-2 border-b border-[var(--studio-line)] px-3 py-2">
        <el-input v-model="historyQuery" size="small" clearable placeholder="搜索提示词…" />
        <div class="flex gap-1">
          <button
            v-for="opt in historyTimeOptions"
            :key="opt.value"
            type="button"
            class="flex-1 rounded border px-1 py-1 text-xs font-semibold transition"
            :class="historyTimeFilter === opt.value
              ? 'border-[var(--studio-teal)] bg-[var(--studio-teal)] text-white'
              : 'border-[var(--studio-line)] text-[var(--studio-muted)] hover:border-[var(--studio-teal)]'"
            @click="historyTimeFilter = opt.value"
          >
            {{ opt.label }}
          </button>
        </div>
        <el-select v-model="historySort" size="small" class="w-full" placeholder="排序">
          <el-option v-for="opt in historySortOptions" :key="opt.value" :label="opt.label" :value="opt.value" />
        </el-select>
      </div>

      <div class="thin-scrollbar flex-1 overflow-auto">
        <div v-if="!history.length" class="flex min-h-[120px] items-center justify-center px-4 text-xs text-[var(--studio-muted)]">
          暂无历史记录，提交一次生成后出现
        </div>
        <div v-else-if="!historyItems.length" class="flex min-h-[120px] items-center justify-center px-4 text-center text-xs text-[var(--studio-muted)]">
          没有匹配的记录
        </div>
        <div v-else class="flex flex-col">
          <div
            v-for="item in historyItems"
            :key="item.id"
            class="group flex items-center gap-3 border-b border-[var(--studio-line)] px-4 py-3 transition hover:bg-[var(--studio-surface-soft)]"
          >
            <button type="button" class="flex min-w-0 flex-1 items-center gap-3 text-left" @click="recallHistory(item)">
              <span class="inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-xs font-bold" :class="historyStatusClass(item)">
                {{ historyStatusIcon(item) }}
              </span>
              <div class="min-w-0 flex-1">
                <p class="truncate text-sm font-semibold text-[var(--studio-ink)]">{{ historyPreview(item.prompt) }}</p>
                <div class="mt-0.5 flex items-center gap-2 text-xs text-[var(--studio-muted)]">
                  <span>{{ historyTime(item.time) }}</span>
                  <span>{{ item.mode === 'edit' ? '编辑' : '生成' }}</span>
                </div>
              </div>
            </button>
            <div class="flex shrink-0 items-center gap-1">
              <button
                v-if="isStoppedStatus(item._status)"
                type="button"
                class="opacity-0 transition group-hover:opacity-100"
                title="重新生成并覆盖此记录"
                aria-label="重新生成并覆盖此记录"
                @click.stop="retryHistory(item)"
              >
                <el-icon class="text-sm text-[var(--studio-muted)] hover:text-[var(--studio-teal)]"><Refresh /></el-icon>
              </button>
              <button
                type="button"
                class="opacity-0 transition group-hover:opacity-100"
                aria-label="删除该历史记录"
                title="删除"
                @click.stop="deleteHistory(item)"
              >
                <el-icon class="text-sm text-[var(--studio-muted)] hover:text-[var(--studio-coral)]"><Delete /></el-icon>
              </button>
            </div>
          </div>
        </div>
      </div>
    </aside>

    <!-- Main: control form + region editor (left column), task monitor + gallery (right column) -->
    <div class="flex min-h-0 flex-1 overflow-hidden">
      <!-- Left column: form + region editor -->
      <div class="flex w-[440px] shrink-0 flex-col gap-4 overflow-auto border-r border-[var(--studio-line)] p-5">
        <form class="studio-panel rounded-lg p-5" @submit.prevent="submitTask">
          <div class="mb-4 flex items-start justify-between gap-3">
            <div>
              <p class="text-xs font-semibold uppercase tracking-[0.16em] text-[var(--studio-coral)]">Image Playground</p>
              <h2 class="mt-1 text-2xl font-black">生成控制台</h2>
            </div>
            <el-button :icon="RefreshLeft" @click="reusePrompt">示例</el-button>
          </div>

          <div class="mb-4 grid grid-cols-2 rounded-md border border-[var(--studio-line)] bg-[var(--studio-surface)] p-1">
            <button
              type="button"
              class="h-10 rounded-[6px] text-sm font-bold transition"
              :class="mode === 'generate' ? 'bg-[var(--studio-solid)] text-[var(--studio-on-solid)]' : 'text-[var(--studio-muted)] hover:bg-[var(--studio-paper)]'"
              @click="switchMode('generate')"
            >
              文生图
            </button>
            <button
              type="button"
              class="h-10 rounded-[6px] text-sm font-bold transition"
              :class="mode === 'edit' ? 'bg-[var(--studio-solid)] text-[var(--studio-on-solid)]' : 'text-[var(--studio-muted)] hover:bg-[var(--studio-paper)]'"
              @click="switchMode('edit')"
            >
              局部编辑
            </button>
          </div>

          <div class="block" @paste.capture="onPromptPaste" @dragover.prevent @drop="onPromptDrop">
            <div class="mb-2 flex items-center justify-between">
              <span class="text-sm font-semibold">Prompt</span>
              <button
                type="button"
                class="flex items-center gap-1 text-xs text-[var(--studio-muted)] transition hover:text-[var(--studio-teal)]"
                title="放大编辑"
                @click="promptZoomOpen = true"
              >
                <el-icon><FullScreen /></el-icon>
                <span>放大</span>
              </button>
            </div>
            <el-input
              v-model="form.prompt"
              type="textarea"
              :rows="mode === 'edit' ? 7 : 10"
              resize="none"
              :maxlength="settings.maxPromptChars"
              placeholder="描述要生成或修改的画面…"
            />
            <p class="mt-1 text-right text-xs text-[var(--studio-muted)]">{{ promptChars }} / {{ settings.maxPromptChars }}</p>
          </div>

          <!-- Reference images (generate mode) -->
          <div v-if="mode === 'generate'" class="mt-4">
            <div class="mb-2 flex items-center justify-between">
              <span class="text-sm font-semibold">参考图（可选）</span>
              <span class="text-xs text-[var(--studio-muted)]">{{ referenceImages.length }} / {{ settings.maxReferenceImages }}</span>
            </div>
            <input ref="refInputRef" class="hidden" type="file" accept="image/*" multiple @change="onRefInput" />
            <div class="flex flex-wrap gap-2">
              <div
                v-for="(ref, i) in referenceImages"
                :key="i"
                class="group relative h-16 w-16 overflow-hidden rounded-md border border-[var(--studio-line)]"
              >
                <img :src="ref" alt="" class="h-full w-full object-cover" />
                <button
                  type="button"
                  class="absolute right-0.5 top-0.5 flex h-5 w-5 items-center justify-center rounded bg-[rgba(23,33,38,0.75)] text-white opacity-0 transition group-hover:opacity-100"
                  :aria-label="`移除参考图 ${i + 1}`"
                  title="移除"
                  @click="removeReference(i)"
                >
                  <el-icon class="text-xs"><Delete /></el-icon>
                </button>
              </div>
              <button
                v-if="referenceImages.length < settings.maxReferenceImages"
                type="button"
                class="flex h-16 w-16 flex-col items-center justify-center gap-0.5 rounded-md border border-dashed border-[var(--studio-line)] text-[var(--studio-muted)] transition hover:border-[var(--studio-teal)] hover:text-[var(--studio-teal)]"
                title="添加参考图"
                @click="openRefPicker"
              >
                <el-icon><Plus /></el-icon>
                <span class="text-[10px]">添加</span>
              </button>
            </div>
          </div>

          <div class="mt-4">
            <span class="mb-2 block text-sm font-semibold">尺寸</span>
            <div class="mb-3 flex flex-wrap gap-1.5">
              <button
                v-for="preset in ratioPresets"
                :key="preset.label"
                type="button"
                class="rounded-md border border-[var(--studio-line)] px-3 py-1.5 text-xs font-bold transition hover:border-[var(--studio-teal)] hover:text-[var(--studio-teal)]"
                @click="applyRatio(preset)"
              >
                {{ preset.label }}
              </button>
            </div>
            <div class="grid grid-cols-2 gap-2">
              <label class="block">
                <span class="mb-1 block text-xs text-[var(--studio-muted)]">宽 (px)</span>
                <el-input-number v-model="sizeW" :min="1" :step="1" step-strictly controls-position="right" class="w-full" />
              </label>
              <label class="block">
                <span class="mb-1 block text-xs text-[var(--studio-muted)]">高 (px)</span>
                <el-input-number v-model="sizeH" :min="1" :step="1" step-strictly controls-position="right" class="w-full" />
              </label>
            </div>
          </div>

          <div v-if="mode === 'edit'" class="mt-4 grid grid-cols-2 gap-3 text-sm">
            <div class="rounded-md border border-[var(--studio-line)] bg-[var(--studio-surface-soft)] p-3">
              <p class="text-xs text-[var(--studio-muted)]">原图</p>
              <p class="mt-1 font-black">{{ maskState.hasImage ? '已上传' : '未上传' }}</p>
            </div>
            <div class="rounded-md border border-[var(--studio-line)] bg-[var(--studio-surface-soft)] p-3">
              <p class="text-xs text-[var(--studio-muted)]">蒙版</p>
              <p class="mt-1 font-black">{{ maskState.hasMask ? '已选择' : '未选择' }}</p>
            </div>
          </div>

          <el-button class="mt-5 w-full" type="primary" size="large" native-type="submit" :icon="MagicStick">
            {{ buttonText }}
          </el-button>
        </form>

        <RegionEditor v-show="mode === 'edit'" ref="regionEditorRef" @mask-change="onMaskChange" />

        <!-- Generate-mode tip card fills the column so it aligns with the edit
             module height instead of leaving a large gap. -->
        <div v-if="mode === 'generate'" class="studio-panel rounded-lg p-4 text-sm leading-6 text-[var(--studio-muted)]">
          <p class="mb-1 text-xs font-bold uppercase tracking-[0.16em] text-[var(--studio-amber)]">Tips</p>
          <p>· 切换到「局部编辑」可在原图上直接涂抹/框选修改区域。</p>
          <p>· 生成成功的图片会保存到本地历史目录，并可在作品集中查看。</p>
        </div>
      </div>

      <!-- Right column: task monitor + gallery -->
      <div class="flex min-h-0 flex-1 flex-col gap-4 overflow-hidden p-5">
        <!-- Task monitor -->
        <div
          class="studio-panel rounded-lg p-5"
          :class="monitorFloating ? 'shadow-2xl' : ''"
          :style="monitorStyle"
        >
          <div class="grid grid-cols-[1fr_auto] items-start gap-4">
            <div
              :class="monitorFloating ? 'cursor-move select-none' : ''"
              title="拖拽移动任务状态"
              @pointerdown="startMonitorDrag"
            >
              <p class="text-xs font-semibold uppercase tracking-[0.16em] text-[var(--studio-teal)]">Task Monitor</p>
              <h2 class="mt-1 text-2xl font-black">{{ operationLabel }}任务状态</h2>
            </div>
            <div class="flex items-center gap-2">
              <el-button v-if="monitorFloating" size="small" text @click="resetMonitorPosition">回原位</el-button>
              <el-button v-else size="small" text @click="floatMonitor">悬浮</el-button>
              <el-button v-if="loading && task?.taskId" size="small" type="danger" plain @click="stopActiveTask">停止</el-button>
              <el-tag :type="status === 'completed' ? 'success' : isStoppedStatus(status) ? 'danger' : 'warning'" effect="plain">
                {{ statusLabel }}
              </el-tag>
            </div>
          </div>

          <div class="mt-4 grid grid-cols-4 gap-3 text-sm">
            <div class="rounded-md border border-[var(--studio-line)] bg-[var(--studio-surface-soft)] p-3">
              <p class="text-xs text-[var(--studio-muted)]">耗时</p>
              <p class="mt-1 text-xl font-black">{{ elapsedSeconds }}s</p>
              <p class="mt-0.5 text-xs text-[var(--studio-muted)]">{{ waitLimitLabel }}</p>
            </div>
            <div class="rounded-md border border-[var(--studio-line)] bg-[var(--studio-surface-soft)] p-3">
              <p class="text-xs text-[var(--studio-muted)]">任务</p>
              <p class="mt-1 truncate text-sm font-bold">{{ task?.taskId || '-' }}</p>
              <p v-if="task?.upstreamTaskId" class="mt-0.5 truncate text-xs text-[var(--studio-muted)]">云端 {{ task.upstreamTaskId }}</p>
              <p v-if="task?.upstreamRequestId" class="mt-0.5 truncate text-xs text-[var(--studio-muted)]">上游 {{ task.upstreamRequestId }}</p>
            </div>
            <div class="rounded-md border border-[var(--studio-line)] bg-[var(--studio-surface-soft)] p-3">
              <p class="text-xs text-[var(--studio-muted)]">节点</p>
              <p class="mt-1 truncate text-sm font-bold">{{ task?.apiName || '-' }}</p>
              <p v-if="task?.effectiveApiType" class="mt-0.5 truncate text-xs text-[var(--studio-muted)]">
                协议 {{ apiTypeText(task.configuredApiType) || '-' }}<span v-if="task.configuredApiType && task.effectiveApiType && task.configuredApiType !== task.effectiveApiType"> -> {{ apiTypeText(task.effectiveApiType) }}</span><span v-else-if="task.effectiveApiType"> {{ apiTypeText(task.effectiveApiType) }}</span>
              </p>
              <p v-if="task?.requestUrl" class="mt-0.5 truncate text-xs text-[var(--studio-muted)]" :title="task.requestUrl">{{ task.requestUrl }}</p>
            </div>
            <div class="rounded-md border border-[var(--studio-line)] bg-[var(--studio-surface-soft)] p-3">
              <p class="text-xs text-[var(--studio-muted)]">模式</p>
              <p class="mt-1 text-sm font-bold">{{ operationLabel }}</p>
              <p v-if="task?.waitPhase === 'upstream_processing'" class="mt-0.5 text-xs text-[var(--studio-muted)]">云端生成中</p>
              <p v-if="task?.pollCount" class="mt-0.5 text-xs text-[var(--studio-muted)]">
                轮询 {{ task.pollCount }} 次<span v-if="task.lastPollStatus"> · {{ task.lastPollStatus }}</span>
              </p>
            </div>
          </div>

          <el-alert v-if="errorMessage" class="mt-4" type="error" :closable="false" :title="errorMessage" />
          <el-alert
            v-else-if="task?.lastPollError"
            class="mt-4"
            type="warning"
            :closable="false"
            :title="`最近轮询错误：${task.lastPollError}`"
          />

          <div v-if="needsProvider" class="mt-4 rounded-md border border-[var(--studio-coral)] bg-[var(--studio-surface-soft)] px-4 py-3 text-sm text-[var(--studio-ink)]">
            还没有可用的 API 节点，请点击右上角齿轮「设置」添加。
          </div>

          <div v-if="attempts.length" class="mt-4 flex flex-wrap gap-2">
            <div v-for="attempt in attempts" :key="`${attempt.api_id}-${attempt.ok}`" class="max-w-full rounded-md border border-[var(--studio-line)] bg-[var(--studio-surface)] px-3 py-2 text-sm">
              <div class="flex items-center gap-2">
                <span>{{ attempt.api_name }}</span>
                <span v-if="attemptProtocolLabel(attempt)" class="text-xs text-[var(--studio-muted)]">{{ attemptProtocolLabel(attempt) }}</span>
                <span v-if="attempt.request_url" class="max-w-[280px] truncate text-xs text-[var(--studio-muted)]" :title="attempt.request_url">{{ attempt.request_url }}</span>
                <el-tag size="small" :type="attempt.ok ? 'success' : 'danger'">{{ attempt.ok ? '成功' : '失败' }}</el-tag>
              </div>
              <p v-if="attemptErrorText(attempt)" class="mt-1 max-w-[520px] break-words text-xs text-[var(--studio-coral)]">
                {{ attemptErrorText(attempt) }}
              </p>
            </div>
          </div>
        </div>

        <!-- Gallery -->
        <div
          class="studio-panel flex min-h-0 flex-1 flex-col overflow-hidden rounded-lg p-5"
          :class="resultFloating ? 'shadow-2xl' : ''"
          :style="resultStyle"
        >
          <div class="mb-4 flex items-end justify-between">
            <div
              :class="resultFloating ? 'cursor-move select-none' : ''"
              title="拖拽移动生成结果"
              @pointerdown="startResultDrag"
            >
              <p class="text-xs font-semibold uppercase tracking-[0.16em] text-[var(--studio-amber)]">Gallery Output</p>
              <h2 class="mt-1 text-2xl font-black">生成结果</h2>
            </div>
            <div class="flex items-end gap-3">
              <div class="text-right text-sm text-[var(--studio-muted)]">
                <p>{{ images.length }} 张图片</p>
                <p v-if="expiresLabel" class="mt-0.5 text-xs text-[var(--studio-amber)]">链接将于 {{ expiresLabel }} 过期</p>
              </div>
              <el-button v-if="resultFloating" size="small" text @click="resetResultPosition">回原位</el-button>
              <el-button v-else size="small" text @click="floatResult">悬浮</el-button>
            </div>
          </div>

          <div v-if="showSkeleton" class="flex min-h-0 flex-1">
            <el-skeleton animated class="h-full w-full">
              <template #template>
                <el-skeleton-item variant="image" class="!h-full !min-h-0 !rounded-md" />
                <div class="mt-3">
                  <el-skeleton-item variant="p" class="!w-2/3" />
                </div>
              </template>
            </el-skeleton>
          </div>

          <div v-else-if="currentImage" class="flex min-h-0 flex-1 items-center justify-center">
            <figure class="group relative flex h-full w-full items-center justify-center overflow-hidden rounded-md border border-[var(--studio-line)] bg-[var(--studio-canvas)]">
              <el-image
                :src="currentImage"
                alt="生成图片"
                :preview-src-list="images"
                :initial-index="0"
                preview-teleported
                hide-on-click-modal
                fit="contain"
                class="block h-full w-full cursor-zoom-in"
              >
                <template #error>
                  <div class="flex h-full w-full flex-col items-center justify-center gap-1 bg-[var(--studio-surface-soft)] text-center text-xs text-[var(--studio-muted)]">
                    <el-icon class="text-2xl"><Picture /></el-icon>
                    <span>图片无法加载</span>
                    <span>（链接可能已过期）</span>
                  </div>
                </template>
              </el-image>
              <figcaption class="pointer-events-none absolute inset-x-0 bottom-0 flex translate-y-full items-center justify-between bg-[rgba(23,33,38,0.86)] px-3 py-2 text-sm text-white transition group-hover:translate-y-0">
                <span>Image 1</span>
                <button
                  type="button"
                  class="pointer-events-auto inline-flex h-8 w-8 items-center justify-center rounded-md bg-white text-[#172126] transition hover:bg-[var(--studio-coral)] hover:text-white"
                  title="下载图片"
                  aria-label="下载当前图片"
                  @click="downloadOne(currentImage, 0)"
                >
                  <el-icon><Download /></el-icon>
                </button>
              </figcaption>
              <span class="pointer-events-none absolute right-2 top-2 flex h-7 w-7 items-center justify-center rounded-md bg-[rgba(23,33,38,0.7)] text-white opacity-0 transition group-hover:opacity-100">
                <el-icon><ZoomIn /></el-icon>
              </span>
            </figure>
          </div>

          <div v-else class="flex flex-1 items-center justify-center rounded-md border border-dashed border-[var(--studio-line)] bg-[var(--studio-surface-soft)] px-6 text-center">
            <div>
              <el-icon class="text-4xl text-[var(--studio-teal)]"><Picture /></el-icon>
              <p class="mt-3 text-lg font-black">等待结果</p>
              <p class="mt-2 max-w-md text-sm leading-6 text-[var(--studio-muted)]">文生图会直接展示生成结果；局部编辑会基于原图和蒙版区域返回修改后的图片。</p>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- Prompt zoom editor -->
    <el-dialog v-model="promptZoomOpen" title="编辑 Prompt" width="760px" top="8vh">
      <div class="rounded-md border border-transparent" @paste.capture="onPromptPaste" @dragover.prevent @drop="onPromptDrop">
        <el-input
          v-model="form.prompt"
          type="textarea"
          :rows="18"
          resize="none"
          :maxlength="settings.maxPromptChars"
          placeholder="描述要生成或修改的画面…"
        />
      </div>
      <div v-if="mode === 'generate' && referenceImages.length" class="mt-3 flex flex-wrap gap-2">
        <div
          v-for="(ref, i) in referenceImages"
          :key="i"
          class="group relative h-14 w-14 overflow-hidden rounded-md border border-[var(--studio-line)]"
        >
          <img :src="ref" alt="" class="h-full w-full object-cover" />
          <button
            type="button"
            class="absolute right-0.5 top-0.5 flex h-5 w-5 items-center justify-center rounded bg-[rgba(23,33,38,0.75)] text-white opacity-0 transition group-hover:opacity-100"
            :aria-label="`移除参考图 ${i + 1}`"
            title="移除"
            @click="removeReference(i)"
          >
            <el-icon class="text-xs"><Delete /></el-icon>
          </button>
        </div>
      </div>
      <p class="mt-1 text-right text-xs text-[var(--studio-muted)]">{{ promptChars }} / {{ settings.maxPromptChars }}</p>
      <template #footer>
        <el-button @click="promptZoomOpen = false">完成</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.animate-spin {
  animation: spin 1.5s linear infinite;
}
@keyframes spin {
  to { transform: rotate(360deg); }
}
</style>
