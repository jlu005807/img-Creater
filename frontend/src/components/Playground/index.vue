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
import { createHistoryInteractionGuard } from '../../utils/historyInteraction'
import { historyTimeBounds } from '../../utils/sessionHistory'
import { useGenerationHistory } from '../../composables/useGenerationHistory'
import { useInfiniteScrollSentinel } from '../../composables/useInfiniteScrollSentinel'
import { useSettings } from '../../composables/useSettings'
import { usePromptTemplates } from '../../composables/usePromptTemplates'
import { useTaskPolling } from '../../composables/useTaskPolling'
import RegionEditor from '../RegionEditor/index.vue'

const { settings } = useSettings()
const { templates, loadTemplates, pendingFill, requestRandomFill } = usePromptTemplates()
const promptZoomOpen = ref(false)

const DRAFT_KEY = 'studio-form-draft'
const EDIT_DRAFT_FLUSH_MS = 3000
const HISTORY_WIDTH = '276px'
const HISTORY_PAGE_SIZE = 30
const SESSION_SUMMARY_PROMPT_MAX = 4000

const form = reactive({
  prompt: '',
  size: '1024x1024',
})

const promptChars = computed(() => form.prompt.length)

// ---- reference images ----
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

function persistedReferenceImages(result) {
  return Array.isArray(result?.reference_images)
    ? result.reference_images.filter((url) => typeof url === 'string' && !url.startsWith('data:'))
    : []
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
  invalidateSessionRequests,
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
} = useGenerationHistory()
const displayHistoryId = ref(null)

const {
  pollTimers,
  pollFailures,
  clearPollTimer,
  clearAllTimers,
  schedulePoll,
  pollStatusOnce,
  stopWithError,
  stopWithCancelled,
  cancelTask: cancelTaskViaComposable,
} = useTaskPolling({
  isDisposed: () => disposed,
  findEntry: findHistoryEntry,
  updateEntry,
  elapsedForEntry,
})
const clockNow = ref(Date.now())
const historyScrollRef = ref(null)
const historySentinelRef = ref(null)

let clockTimer = null
const historyInteraction = createHistoryInteractionGuard()
// 局部编辑草稿的本地工作缓冲：画布本身持有内容，这里只记录“有未落盘改动”的脏标记
// 和一个防抖定时器；到期/切换/提交时才调用 exportDraft() 拉取重草稿并持久化。
let editDraftFlushTimer = null
let editDraftDirty = false
// 每个会话节点最近一次成功上传完整原图时的 imageRevision：原图未变化时增量保存可省略 base64 原图。
const savedDraftImageRevisions = new Map()
// Set on unmount so late-resolving awaits cannot re-arm timers in a dead instance.
let disposed = false

function beginHistoryInteraction() {
  restoringEditDraft.value = false
  return historyInteraction.begin()
}

function isCurrentHistoryInteraction(token) {
  return !disposed && historyInteraction.isCurrent(token)
}

function isRunningStatus(value) {
  return ['submitting', 'queued', 'processing'].includes(value)
}

function isStoppedStatus(value) {
  return ['failed', 'timeout', 'cancelled'].includes(value)
}

function findHistoryEntry(id) {
  return history.value.find((entry) => entry.id === id) || null
}

function isCurrentHistoryEntry(entry, token) {
  return Boolean(entry?.id) && isCurrentHistoryInteraction(token) && Boolean(findHistoryEntry(entry.id))
}

const activeEntry = computed(() => findHistoryEntry(displayHistoryId.value))
const status = computed(() => activeEntry.value?._status || 'idle')
const task = computed(() => activeEntry.value?.task || null)
const images = computed(() => activeEntry.value?.urls || [])
const currentImage = computed(() => images.value[0] || '')
const currentImageIsPersisted = computed(() => String(currentImage.value || '').startsWith('/api/results/'))
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
  if (currentImageIsPersisted.value) return ''
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

function protocolLabel(configuredType, effectiveType) {
  const configured = apiTypeText(configuredType)
  const effective = apiTypeText(effectiveType)
  if (configured && effective && configured !== effective) return `${configured} -> ${effective}`
  return effective || configured
}

function attemptProtocolLabel(attempt) {
  return protocolLabel(attempt?.configured_api_type, attempt?.effective_api_type)
}

const taskProtocolLabel = computed(() => protocolLabel(task.value?.configuredApiType, task.value?.effectiveApiType))

function compactDetail(value, limit = 320) {
  const text = String(value || '').replace(/\s+/g, ' ').trim()
  if (!text) return ''
  return text.length > limit ? `${text.slice(0, limit)}...` : text
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
    const httpStatus = details.http_status || details.status_code
    if (httpStatus) parts.push(`HTTP ${httpStatus}`)
    if (details.gateway_timeout) parts.push('网关超时：上游返回 HTML，未返回可解析的 OpenAI JSON 结果')
    if (details.gateway_hint) parts.push(details.gateway_hint)
    else if (details.cloudflare) parts.push('Cloudflare/网关返回 HTML，未返回 OpenAI JSON')
    if (details.html_title) parts.push(`HTML title: ${details.html_title}`)
    if (details.server) parts.push(`Server: ${details.server}`)
    if (details.cf_ray) parts.push(`CF-Ray: ${details.cf_ray}`)
    if (details.parse_error && !details.is_html_response) parts.push(`JSON parse error: ${details.parse_error}`)
    if (details.content_type) parts.push(`Content-Type: ${details.content_type}`)
    if (details.text_preview) parts.push(`响应片段: ${compactDetail(details.text_preview)}`)
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
const historyTimeOptions = [
  { value: 'all', label: '全部' },
  { value: 'today', label: '今天' },
  { value: 'week', label: '本周' },
  { value: 'month', label: '本月' },
]
function historyServerParams() {
  const bounds = historyTimeBounds(historyTimeFilter.value)
  return {
    q: historyQuery.value.trim(),
    from: bounds.from,
    to: bounds.to,
    limit: HISTORY_PAGE_SIZE,
  }
}

function historyServerQueryKey(params = historyServerParams()) {
  return JSON.stringify([
    historyQuery.value.trim(),
    historyTimeFilter.value,
    params.from || null,
  ])
}

let historyFilterTimer = null

async function refreshHistorySessions() {
  try {
    const params = historyServerParams()
    await refreshSessions(params, { queryKey: historyServerQueryKey(params) })
  } catch {
    // The inline retry state keeps the current local/live entries usable.
  }
}

function scheduleHistoryRefresh() {
  invalidateSessionRequests()
  clearTimeout(historyFilterTimer)
  historyFilterTimer = setTimeout(() => {
    historyFilterTimer = null
    refreshHistorySessions()
  }, 300)
}

watch([historyQuery, historyTimeFilter], scheduleHistoryRefresh)

const canLoadMoreHistory = computed(
  () => hasMore.value && !initialLoading.value && !loadingMore.value && !loadError.value,
)

async function loadNextHistoryPage() {
  try {
    await loadMoreSessions()
  } catch {
    // The shared store keeps the failed cursor for the Retry command.
  }
}

async function retryHistoryLoad() {
  try {
    await retrySessions()
  } catch {
    // Keep the current list and Retry command visible.
  }
}

useInfiniteScrollSentinel({
  rootRef: historyScrollRef,
  sentinelRef: historySentinelRef,
  enabled: canLoadMoreHistory,
  onIntersect: loadNextHistoryPage,
})

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
      // A successful server refresh already applied q/from against the full
      // manifest (including prompts longer than the summary cap). While a
      // refresh is pending, or failed, apply the local predicate so stale
      // server rows do not masquerade as results for the new query.
      if (entry._origin === 'server' && serverResultsCurrent.value) return true
      if (!withinTimeFilter(entry.time)) return false
      if (q && !(entry.prompt || '').toLowerCase().includes(q)) return false
      return true
    })
    .sort((a, b) => Number(b.time || 0) - Number(a.time || 0))
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
  clearAllTimers()
  discardEditDraftBuffer()
  if (clockTimer) window.clearInterval(clockTimer)
  clockTimer = null
}

// 丢弃本地草稿缓冲：切换到其它会话、提交成功或删除当前会话后调用。
function discardEditDraftBuffer() {
  if (editDraftFlushTimer) window.clearTimeout(editDraftFlushTimer)
  editDraftFlushTimer = null
  editDraftDirty = false
}

function startClock() {
  if (disposed || clockTimer) return
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
const isSubmitting = ref(false)

async function submitTask(reuseId = null) {
  if (isSubmitting.value) return
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
  beginHistoryInteraction()
  const currentReferenceImages = [...referenceImages.value]
  isSubmitting.value = true

  // Reuse an existing history entry on retry (overwrite), else create a new one.
  let entryId = reuseId
  if (reuseId && history.value.some((e) => e.id === reuseId)) {
    updateEntry(reuseId, {
      prompt: form.prompt.trim(),
      mode: mode.value,
      size: form.size,
      urls: [],
      apiName: '',
      time: Date.now(),
      _status: 'submitting',
      startedAt: Date.now(),
      elapsedSeconds: 0,
      task: null,
      errorMessage: '',
      attempts: [],
      expiresAt: null,
      editDraft,
      referenceImages: currentReferenceImages,
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
      referenceImages: currentReferenceImages,
    })
    entryId = entry.id
  }
  displayHistoryId.value = entryId
  // 显示焦点切到本次提交的条目后，本地草稿缓冲一律作废——即使是文生图提交，
  // 否则残留的脏缓冲会在之后被冲刷到不相关的新条目上。
  discardEditDraftBuffer()
  if (editDraft) {
    // 草稿已随本次提交挂到该记录上（对已完成记录的再编辑即 clone-on-edit 的新记录）。
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
            source_image: editPayload.source_image,
            marked_image: editPayload.marked_image,
            ...(currentReferenceImages.length ? { reference_images: currentReferenceImages } : {}),
          })
        : await generateImages({
            ...basePayload,
            ...(currentReferenceImages.length ? { reference_images: currentReferenceImages } : {}),
          })
    const nextTask = {
      apiId: result.api_id,
      taskId: result.task_id,
      apiName: result.api_name,
      operation: result.operation || mode.value,
      configuredApiType: result.configured_api_type,
      effectiveApiType: result.effective_api_type,
    }
    if (!findHistoryEntry(entryId)) {
      // 条目在提交期间被删除：任务已被后端接收，直接尽力取消，避免继续计费生成。
      cancelGenerationTask(result.task_id).catch(() => {})
      return
    }
    const acceptedReferenceImages = persistedReferenceImages(result)
    const entryFields = {
      task: nextTask,
      attempts: result.attempts || [],
      _status: result.status || 'queued',
      maxWaitSeconds: result.max_wait_seconds ?? null,
      errorMessage: '',
      referenceImages: acceptedReferenceImages.length ? acceptedReferenceImages : currentReferenceImages,
    }
    if (disposed) {
      // 组件已卸载但任务已被接收：仍要把 taskId 记到共享历史上，
      // 下次挂载由 restoreRunningTasks 恢复轮询，而不是误判失败诱导重复提交。
      updateEntry(entryId, entryFields)
      return
    }
    if (acceptedReferenceImages.length) {
      referenceImages.value = [...acceptedReferenceImages]
    }
    updateEntry(entryId, entryFields)
    schedulePoll(entryId, nextTask)
  } catch (error) {
    if (disposed) return
    stopWithError(entryId, error.message || '提交任务失败')
  } finally {
    isSubmitting.value = false
  }
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
    const cancelResult = await cancelTaskViaComposable(entry.id, entry)
    if (cancelResult?.alreadyCompleted) {
      ElMessage.info('任务已完成，无需停止')
      return
    }
    ElMessage.success('已停止本地等待')
  } catch (error) {
    if (error === 'cancel') return
    if (findHistoryEntry(entry.id)?._status === 'completed') {
      ElMessage.info('任务已完成，无需停止')
      return
    }
    if (isBackendRouteMissing(error)) {
      ElMessage.error(backendRouteMissingMessage('停止任务'))
      return
    }
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
  if (!restoringEditDraft.value && mode.value === 'edit' && nextState?.hasImage) {
    scheduleEditDraftFlush()
  }
}

async function downloadOne(url, index) {
  const ok = await downloadImage(url, `img-Creater-${index + 1}`)
  if (!ok) {
    ElMessage.info('图片为跨域链接，已在新标签页打开，可右键另存为')
  }
}

// Flush the local draft buffer: pull the heavy draft from the editor once and
// persist it onto the currently displayed NOT-yet-completed entry. Completed
// entries are clone-on-edit — their proven draft must stay intact, so the
// buffer stays dirty until submit attaches it to a new entry (or the buffer
// dies on recall of a different entry).
function persistCurrentEditDraft() {
  if (editDraftFlushTimer) window.clearTimeout(editDraftFlushTimer)
  editDraftFlushTimer = null
  if (!editDraftDirty) return
  const entry = findHistoryEntry(displayHistoryId.value)
  // 已完成的历史记录不能被工作草稿覆盖：保留缓冲，提交时挂到新记录上。
  // 非编辑模式的条目（如文生图）也不能收编辑草稿。
  if (!entry || entry._status === 'completed' || entry.mode !== 'edit') return
  const draft = regionEditorRef.value?.exportDraft?.()
  editDraftDirty = false
  if (!draft) return
  updateEntry(entry.id, { editDraft: draft })
  persistEditDraftNow(entry.id, draft)
}

// 每一笔只标脏并重置防抖：到期后统一拉取一次 exportDraft() 再持久化。
function scheduleEditDraftFlush() {
  editDraftDirty = true
  if (editDraftFlushTimer) window.clearTimeout(editDraftFlushTimer)
  editDraftFlushTimer = window.setTimeout(() => {
    editDraftFlushTimer = null
    persistCurrentEditDraft()
  }, EDIT_DRAFT_FLUSH_MS)
}

async function persistEditDraftNow(entryId, draft) {
  if (!entryId || !draft) return
  const imageRevision = Number(draft.imageRevision || 0)
  const skipImage =
    Boolean(draft.image) && imageRevision > 0 && savedDraftImageRevisions.get(entryId) === imageRevision
  const payload = { ...draft }
  if (skipImage) {
    // 原图未变化：增量保存只带 mask/元数据，后端与已存草稿合并，避免重复上传整幅 base64 原图。
    delete payload.image
  }
  try {
    await saveEditDraft(entryId, payload)
    if (!skipImage && imageRevision > 0) savedDraftImageRevisions.set(entryId, imageRevision)
  } catch {
    // Draft persistence is best-effort; generation history remains usable.
  }
}

async function restoreEditDraftForEntry(entry, token) {
  if (!isCurrentHistoryEntry(entry, token)) return false
  restoringEditDraft.value = true
  try {
    let draft = entry.editDraft || null
    if (!draft) {
      draft = await getEditDraft(entry.id)
      if (!isCurrentHistoryEntry(entry, token)) return false
      if (draft) updateEntry(entry.id, { editDraft: draft })
    }
    if (!isCurrentHistoryEntry(entry, token)) return false
    if (draft) {
      await regionEditorRef.value?.restoreDraft?.(draft)
      if (!isCurrentHistoryEntry(entry, token)) return false
    } else {
      regionEditorRef.value?.clearAll?.()
    }
    return true
  } catch {
    if (!isCurrentHistoryEntry(entry, token)) return false
    regionEditorRef.value?.clearAll?.()
    return false
  } finally {
    if (isCurrentHistoryEntry(entry, token)) restoringEditDraft.value = false
  }
}

// ---- history actions ----
async function resolveHistoryEntryForReuse(entry, token) {
  if (!isCurrentHistoryEntry(entry, token)) return null
  if (!entry || entry._origin !== 'server' || entry._detailsLoaded) return entry
  try {
    const resolved = await ensureSessionDetails(entry.id)
    return isCurrentHistoryEntry(resolved, token) ? resolved : null
  } catch (error) {
    if (!isCurrentHistoryEntry(entry, token)) return null
    if (String(entry.prompt || '').length >= SESSION_SUMMARY_PROMPT_MAX) {
      ElMessage.error('完整历史参数加载失败，为避免使用被截断的 Prompt，本次未切换，请重试')
      return null
    }
    ElMessage.warning(error.message || '完整历史参数加载失败，已使用当前摘要')
    return entry
  }
}

async function recallHistory(entry) {
  const token = beginHistoryInteraction()
  entry = await resolveHistoryEntryForReuse(entry, token)
  if (!isCurrentHistoryEntry(entry, token)) return
  if (!entry) return
  const sameEntry = entry.id === displayHistoryId.value
  // 重复点击当前会话时保留画布上的未落盘改动；切换到其它会话则丢弃本地缓冲，
  // 之后再召回原纪录时看到的仍是其原始草稿。
  const keepWorkingCanvas = sameEntry && editDraftDirty
  persistCurrentEditDraft()
  if (!sameEntry) discardEditDraftBuffer()
  displayHistoryId.value = entry.id
  form.prompt = entry.prompt || ''
  if (entry.size) parseSize(entry.size)
  referenceImages.value = Array.isArray(entry.referenceImages) ? [...entry.referenceImages] : []
  if (entry.mode) mode.value = entry.mode
  if (entry.mode === 'edit') {
    if (!keepWorkingCanvas) await restoreEditDraftForEntry(entry, token)
    if (!isCurrentHistoryEntry(entry, token)) return
  } else {
    regionEditorRef.value?.clearAll?.()
    maskState.value = { hasImage: false, hasMask: false }
  }
  ElMessage.success('已切换到该历史记录')
}

async function deleteHistory(entry) {
  try {
    await ElMessageBox.confirm('删除该会话记录？运行中的任务会先停止。', '确认删除', {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消',
    })
    // historyItems 传入的是渲染时的浅拷贝；确认框打开期间提交可能刚拿到
    // taskId，必须按当前活动条目决定是否取消。
    beginHistoryInteraction()
    const live = findHistoryEntry(entry.id) || entry
    if (isRunningStatus(live._status) && live.task?.taskId) {
      try {
        await cancelGenerationTask(live.task.taskId)
      } catch {
        /* 停止任务失败也继续删除，取消属于尽力而为 */
      }
    }
    clearPollTimer(entry.id)
    if (displayHistoryId.value === entry.id) discardEditDraftBuffer()
    savedDraftImageRevisions.delete(entry.id)
    try {
      await deleteSession(entry.id)
    } catch {
      ElMessage.warning('后端会话目录删除失败，本地记录已移除')
    }
    const removedDisplayedEntry = displayHistoryId.value === entry.id
    removeEntry(entry.id)
    if (removedDisplayedEntry) {
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
  beginHistoryInteraction()
  persistCurrentEditDraft()
  discardEditDraftBuffer()
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
    await ElMessageBox.confirm('删除全部会话历史？运行中的任务会先停止，该操作不可撤销。', '确认删除全部', {
      type: 'warning',
      confirmButtonText: '删除全部',
      cancelButtonText: '取消',
    })
    beginHistoryInteraction()
    clearTimers()
    savedDraftImageRevisions.clear()
    const runningTasks = history.value.filter((entry) => isRunningStatus(entry._status) && entry.task?.taskId)
    if (runningTasks.length) {
      await Promise.allSettled(runningTasks.map((entry) => cancelGenerationTask(entry.task.taskId)))
    }
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
  const token = beginHistoryInteraction()
  entry = await resolveHistoryEntryForReuse(entry, token)
  if (!isCurrentHistoryEntry(entry, token)) return
  if (!entry) return
  persistCurrentEditDraft()
  discardEditDraftBuffer()
  form.prompt = entry.prompt || ''
  if (entry.size) parseSize(entry.size)
  referenceImages.value = Array.isArray(entry.referenceImages) ? [...entry.referenceImages] : []
  if (entry.mode) mode.value = entry.mode
  if (entry.mode === 'edit') {
    displayHistoryId.value = entry.id
    await restoreEditDraftForEntry(entry, token)
    if (!isCurrentHistoryEntry(entry, token)) return
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
  if (disposed) return
  const now = Date.now()
  let hasRunning = false
  history.value.forEach((entry) => {
    if (!isRunningStatus(entry._status)) return
    if (!entry.task?.taskId) {
      // Persisted while POST was still in flight: no task to poll, so it
      // would spin forever without this reconciliation.
      updateEntry(entry.id, { _status: 'failed', errorMessage: '页面刷新时提交中断，请重试' })
      return
    }
    hasRunning = true
    if (!entry.startedAt) {
      updateEntry(entry.id, { startedAt: now })
    }
    schedulePoll(entry.id, entry.task, 500)
  })
  if (hasRunning) startClock()
}

// Restore the draft during setup so the immediate pendingFill watcher below
// can override it (instead of the draft clobbering the fill in onMounted).
restoreDraft()

// A template chosen in Settings fills the prompt here; immediate covers fills
// requested while this component was unmounted, null-out makes each fill
// apply exactly once.
watch(
  pendingFill,
  (val) => {
    if (val && val.text != null) {
      form.prompt = val.text
      switchMode('generate')
      pendingFill.value = null
    }
  },
  { immediate: true },
)

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
  try {
    const params = historyServerParams()
    await ensureSessions(params, { queryKey: historyServerQueryKey(params) })
  } catch (error) {
    ElMessage.warning(error.message || '后端历史会话加载失败，仅显示本地历史')
  }
  restoreRunningTasks()
})
onBeforeUnmount(() => {
  disposed = true
  historyInteraction.invalidate()
  clearTimeout(historyFilterTimer)
  // 卸载前落盘一次未保存的草稿（仅限未完成的编辑上下文，见 persistCurrentEditDraft）。
  persistCurrentEditDraft()
  clearTimers()
  window.removeEventListener('pointermove', moveMonitor)
  window.removeEventListener('pointermove', moveResult)
})

watch(
  history,
  (items) => {
    if (displayHistoryId.value && !items.some((item) => item.id === displayHistoryId.value)) {
      displayHistoryId.value = items[0]?.id || null
      return
    }
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
      <div v-if="history.length || historyQuery || historyTimeFilter !== 'all'" class="space-y-2 border-b border-[var(--studio-line)] px-3 py-2">
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
      </div>

      <div ref="historyScrollRef" class="thin-scrollbar flex-1 overflow-auto">
        <div v-if="initialLoading && !historyItems.length" class="flex min-h-[120px] items-center justify-center px-4 text-xs text-[var(--studio-muted)]">
          正在加载历史记录…
        </div>
        <div v-else-if="!history.length && !historyQuery && historyTimeFilter === 'all'" class="flex min-h-[120px] items-center justify-center px-4 text-xs text-[var(--studio-muted)]">
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
        <div ref="historySentinelRef" class="h-px" aria-hidden="true"></div>
        <div v-if="initialLoading" class="px-4 py-3 text-center text-xs text-[var(--studio-muted)]">正在刷新历史记录…</div>
        <div v-else-if="loadingMore" class="px-4 py-3 text-center text-xs text-[var(--studio-muted)]">正在加载更多记录…</div>
        <div v-else-if="loadError" class="flex items-center justify-center gap-2 px-3 py-3 text-center text-xs text-[var(--studio-coral)]">
          <span class="min-w-0 truncate" :title="loadError.message">{{ loadError.message || '历史记录加载失败' }}</span>
          <button type="button" class="shrink-0 font-bold text-[var(--studio-teal)] hover:underline" @click="retryHistoryLoad">重试</button>
        </div>
        <div v-else-if="!hasMore && history.length" class="px-4 py-3 text-center text-xs text-[var(--studio-muted)]">已加载全部记录</div>
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

          <!-- Reference images -->
          <div class="mt-4">
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
              <p class="text-xs text-[var(--studio-muted)]">标注</p>
              <p class="mt-1 font-black">{{ maskState.hasMask ? '已选择' : '未选择' }}</p>
            </div>
          </div>

          <el-button
            class="mt-5 w-full"
            type="primary"
            size="large"
            native-type="submit"
            :icon="MagicStick"
            :loading="isSubmitting"
            :disabled="isSubmitting"
          >
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
              <p v-if="taskProtocolLabel" class="mt-0.5 truncate text-xs text-[var(--studio-muted)]">
                协议 {{ taskProtocolLabel }}
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
            <div v-for="(attempt, attemptIndex) in attempts" :key="`${attemptIndex}-${attempt.api_id}-${attempt.ok}`" class="max-w-full rounded-md border border-[var(--studio-line)] bg-[var(--studio-surface)] px-3 py-2 text-sm">
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
              <p class="mt-2 max-w-md text-sm leading-6 text-[var(--studio-muted)]">文生图会直接展示生成结果；局部编辑会基于原图和白色标注区域返回修改后的图片。</p>
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
      <div v-if="referenceImages.length" class="mt-3 flex flex-wrap gap-2">
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
