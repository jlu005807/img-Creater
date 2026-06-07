<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { Delete, Download, MagicStick, Picture, RefreshLeft, ZoomIn } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { editImage, generateImages, getGenerationStatus } from '../../api/generation'
import { downloadImage } from '../../utils/download'
import { useGenerationHistory } from '../../composables/useGenerationHistory'
import RegionEditor from '../RegionEditor/index.vue'

const MAX_WAIT_SECONDS = 300
const POLL_INTERVAL_MS = 4000
const DRAFT_KEY = 'studio-form-draft'
const HISTORY_WIDTH = '276px'

const form = reactive({
  prompt: '',
  size: '1024x1024',
})

const mode = ref('generate')
const maskState = ref({ hasImage: false, hasMask: false })
const regionEditorRef = ref(null)

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

const loading = ref(false)
const status = ref('idle')
const elapsedSeconds = ref(0)
const task = ref(null)
const images = ref([])
const errorMessage = ref('')
const attempts = ref([])
const expiresAt = ref(null)
const needsProvider = ref(false)
const { history, addEntry, updateEntry, removeEntry, clearHistory } = useGenerationHistory()
const activeHistoryId = ref(null)

let elapsedTimer = null
let pollTimer = null

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
  }
  return labels[status.value] || status.value
})

const buttonText = computed(() => {
  if (!loading.value) return mode.value === 'edit' ? '提交局部修改' : '生成图片'
  return `${statusLabel.value} · ${elapsedSeconds.value}s`
})

const showSkeleton = computed(() => loading.value && !images.value.length)
const operationLabel = computed(() => (mode.value === 'edit' ? '局部编辑' : '文生图'))

// ---- history items with live status ----
const historyItems = computed(() =>
  history.value.map((entry) => {
    let ist = entry._status || 'completed'
    if (entry.id === activeHistoryId.value && loading.value) {
      ist = status.value === 'idle' || status.value === 'submitting' ? 'queued' : status.value
    }
    return { ...entry, _status: ist }
  }),
)

function historyStatusIcon(item) {
  if (item._status === 'completed') return '✓'
  if (item._status === 'failed' || item._status === 'timeout') return '✗'
  return '⟳'
}

function historyStatusClass(item) {
  if (item._status === 'completed') return 'text-[var(--studio-green)]'
  if (item._status === 'failed' || item._status === 'timeout') return 'text-[var(--studio-coral)]'
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
  if (elapsedTimer) window.clearInterval(elapsedTimer)
  if (pollTimer) window.clearTimeout(pollTimer)
  elapsedTimer = null
  pollTimer = null
}

function resetRun() {
  clearTimers()
  loading.value = false
  status.value = 'idle'
  elapsedSeconds.value = 0
  task.value = null
  errorMessage.value = ''
  attempts.value = []
  expiresAt.value = null
  needsProvider.value = false
  activeHistoryId.value = null
}

// ---- submit + poll ----
async function submitTask() {
  if (loading.value) return
  if (!form.prompt.trim()) {
    ElMessage.warning('请输入 Prompt')
    return
  }

  let editPayload = null
  if (mode.value === 'edit') {
    editPayload = regionEditorRef.value?.exportPayload()
    if (!editPayload) {
      ElMessage.warning('请先上传原图并框选或涂抹需要修改的区域')
      return
    }
  }

  resetRun()
  images.value = []
  loading.value = true
  status.value = 'submitting'

  // Create a history entry immediately.
  const entry = addEntry({
    prompt: form.prompt.trim(),
    mode: mode.value,
    size: form.size,
    urls: [],
    apiName: '',
    _status: 'queued',
  })
  activeHistoryId.value = entry.id

  elapsedTimer = window.setInterval(() => {
    elapsedSeconds.value += 1
    if (elapsedSeconds.value >= MAX_WAIT_SECONDS) {
      stopWithTimeout()
    }
  }, 1000)

  try {
    const basePayload = {
      prompt: form.prompt.trim(),
      size: form.size,
      n: 1,
    }
    const result =
      mode.value === 'edit'
        ? await editImage({
            ...basePayload,
            image: editPayload.image,
            mask: editPayload.mask,
            edit_mode: 'mask',
            selection: editPayload.selection,
          })
        : await generateImages(basePayload)

    task.value = {
      apiId: result.api_id,
      taskId: result.task_id,
      apiName: result.api_name,
      operation: result.operation || mode.value,
    }
    attempts.value = result.attempts || []
    status.value = result.status || 'queued'
    schedulePoll()
  } catch (error) {
    stopWithError(error.message || '提交任务失败')
  }
}

function schedulePoll() {
  if (!loading.value) return
  pollTimer = window.setTimeout(pollStatusOnce, POLL_INTERVAL_MS)
}

async function pollStatusOnce() {
  if (!task.value || !loading.value) return
  if (elapsedSeconds.value >= MAX_WAIT_SECONDS) {
    stopWithTimeout()
    return
  }

  try {
    const result = await getGenerationStatus({
      apiId: task.value.apiId,
      taskId: task.value.taskId,
    })
    if (!loading.value) return
    status.value = result.status

    if (result.api_name || result.api_id) {
      task.value = { ...task.value, apiId: result.api_id, apiName: result.api_name }
    }
    if (result.operation) {
      task.value = { ...task.value, operation: result.operation }
    }
    if (Array.isArray(result.attempts) && result.attempts.length) {
      attempts.value = result.attempts
    }

    if (result.status === 'completed') {
      images.value = result.urls || []
      expiresAt.value = result.expires_at ?? null
      loading.value = false
      clearTimers()
      if (images.value.length) {
        updateEntry(activeHistoryId.value, {
          urls: images.value,
          apiName: task.value?.apiName || result.api_name || '',
          imageCount: images.value.length,
          _status: 'completed',
        })
      } else {
        errorMessage.value = '任务完成但未返回图片 URL'
      }
      activeHistoryId.value = null
      return
    }

    if (result.status === 'failed') {
      stopWithError(result.error || '任务失败')
      updateEntry(activeHistoryId.value, { _status: 'failed' })
      activeHistoryId.value = null
      return
    }

    schedulePoll()
  } catch (error) {
    stopWithError(error.message || '查询任务状态失败')
    updateEntry(activeHistoryId.value, { _status: 'failed' })
    activeHistoryId.value = null
  }
}

function stopWithError(message) {
  loading.value = false
  status.value = 'failed'
  errorMessage.value = message
  needsProvider.value = typeof message === 'string' && message.includes('API 配置')
  clearTimers()
}

function stopWithTimeout() {
  loading.value = false
  status.value = 'timeout'
  errorMessage.value = '已超过 5 分钟，任务轮询已自动停止'
  clearTimers()
  updateEntry(activeHistoryId.value, { _status: 'timeout' })
  activeHistoryId.value = null
}

function reusePrompt() {
  form.prompt =
    mode.value === 'edit'
      ? '只修改蒙版区域：替换为透明玻璃控制面板，保持原图光照、视角和边缘自然融合'
      : '电影级产品摄影，一台半透明的桌面图像生成终端，暖色工作灯，清晰细节，真实材质'
}

function onMaskChange(nextState) {
  maskState.value = nextState
}

async function downloadOne(url, index) {
  const ok = await downloadImage(url, `gpt-img2-${index + 1}`)
  if (!ok) {
    ElMessage.info('图片为跨域链接，已在新标签页打开，可右键另存为')
  }
}

// ---- history actions ----
function recallHistory(entry) {
  form.prompt = entry.prompt || ''
  if (entry.size) parseSize(entry.size)
  if (entry.mode) mode.value = entry.mode
  images.value = entry.urls || []
  status.value = entry._status === 'completed' ? 'completed' : 'idle'
  errorMessage.value = ''
  ElMessage.success('已切换到该历史记录')
}

function deleteHistory(entry) {
  removeEntry(entry.id)
  if (activeHistoryId.value === entry.id) activeHistoryId.value = null
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

onMounted(restoreDraft)
onBeforeUnmount(clearTimers)
</script>

<template>
  <div class="flex h-full">
    <!-- History sidebar (permanent left panel) -->
    <aside
      class="flex shrink-0 flex-col border-r border-[var(--studio-line)] bg-[var(--studio-panel)]"
      :style="{ width: HISTORY_WIDTH }"
    >
      <div class="flex items-center justify-between border-b border-[var(--studio-line)] px-4 py-3">
        <h2 class="text-sm font-black text-[var(--studio-ink)]">会话历史</h2>
        <el-button
          v-if="history.length"
          text
          size="small"
          type="danger"
          :icon="Delete"
          aria-label="清空全部历史"
          @click="clearHistory"
        />
      </div>
      <div class="thin-scrollbar flex-1 overflow-auto">
        <div v-if="!history.length" class="flex min-h-[120px] items-center justify-center px-4 text-xs text-[var(--studio-muted)]">
          暂无历史记录，提交一次生成后出现
        </div>
        <div v-else class="flex flex-col">
          <button
            v-for="item in historyItems"
            :key="item.id"
            type="button"
            class="group flex items-center gap-3 border-b border-[var(--studio-line)] px-4 py-3 text-left transition hover:bg-[var(--studio-surface-soft)]"
            @click="recallHistory(item)"
          >
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
            <button
              type="button"
              class="shrink-0 opacity-0 transition group-hover:opacity-100"
              :aria-label="`删除该历史记录`"
              title="删除"
              @click.stop="deleteHistory(item)"
            >
              <el-icon class="text-sm text-[var(--studio-muted)] hover:text-[var(--studio-coral)]"><Delete /></el-icon>
            </button>
          </button>
        </div>
      </div>
    </aside>

    <!-- Main: control form + region editor (left column), task monitor + gallery (right column) -->
    <div class="flex flex-1 overflow-hidden">
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
              @click="mode = 'generate'"
            >
              文生图
            </button>
            <button
              type="button"
              class="h-10 rounded-[6px] text-sm font-bold transition"
              :class="mode === 'edit' ? 'bg-[var(--studio-solid)] text-[var(--studio-on-solid)]' : 'text-[var(--studio-muted)] hover:bg-[var(--studio-paper)]'"
              @click="mode = 'edit'"
            >
              局部编辑
            </button>
          </div>

          <label class="block">
            <span class="mb-2 block text-sm font-semibold">Prompt</span>
            <el-input
              v-model="form.prompt"
              type="textarea"
              :rows="mode === 'edit' ? 7 : 10"
              resize="none"
              maxlength="3000"
              show-word-limit
              placeholder="描述要生成或修改的画面…（Ctrl/⌘ + Enter 提交）"
              @keydown.ctrl.enter.prevent="submitTask"
              @keydown.meta.enter.prevent="submitTask"
            />
          </label>

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
                <el-input-number v-model="sizeW" :min="64" :max="4096" :step="64" controls-position="right" class="w-full" />
              </label>
              <label class="block">
                <span class="mb-1 block text-xs text-[var(--studio-muted)]">高 (px)</span>
                <el-input-number v-model="sizeH" :min="64" :max="4096" :step="64" controls-position="right" class="w-full" />
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

          <el-button class="mt-5 w-full" type="primary" size="large" native-type="submit" :loading="loading" :icon="MagicStick">
            {{ buttonText }}
          </el-button>
        </form>

        <RegionEditor v-show="mode === 'edit'" ref="regionEditorRef" @mask-change="onMaskChange" />
      </div>

      <!-- Right column: task monitor + gallery -->
      <div class="flex flex-1 flex-col gap-4 overflow-auto p-5">
        <!-- Task monitor -->
        <div class="studio-panel rounded-lg p-5">
          <div class="grid grid-cols-[1fr_auto] items-start gap-4">
            <div>
              <p class="text-xs font-semibold uppercase tracking-[0.16em] text-[var(--studio-teal)]">Task Monitor</p>
              <h2 class="mt-1 text-2xl font-black">{{ operationLabel }}任务状态</h2>
            </div>
            <el-tag :type="status === 'completed' ? 'success' : status === 'failed' || status === 'timeout' ? 'danger' : 'warning'" effect="plain">
              {{ statusLabel }}
            </el-tag>
          </div>

          <div class="mt-4 grid grid-cols-4 gap-3 text-sm">
            <div class="rounded-md border border-[var(--studio-line)] bg-[var(--studio-surface-soft)] p-3">
              <p class="text-xs text-[var(--studio-muted)]">耗时</p>
              <p class="mt-1 text-xl font-black">{{ elapsedSeconds }}s</p>
            </div>
            <div class="rounded-md border border-[var(--studio-line)] bg-[var(--studio-surface-soft)] p-3">
              <p class="text-xs text-[var(--studio-muted)]">任务</p>
              <p class="mt-1 truncate text-sm font-bold">{{ task?.taskId || '-' }}</p>
            </div>
            <div class="rounded-md border border-[var(--studio-line)] bg-[var(--studio-surface-soft)] p-3">
              <p class="text-xs text-[var(--studio-muted)]">节点</p>
              <p class="mt-1 truncate text-sm font-bold">{{ task?.apiName || '-' }}</p>
            </div>
            <div class="rounded-md border border-[var(--studio-line)] bg-[var(--studio-surface-soft)] p-3">
              <p class="text-xs text-[var(--studio-muted)]">模式</p>
              <p class="mt-1 text-sm font-bold">{{ operationLabel }}</p>
            </div>
          </div>

          <el-alert v-if="errorMessage" class="mt-4" type="error" :closable="false" :title="errorMessage" />

          <div v-if="needsProvider" class="mt-4 rounded-md border border-[var(--studio-coral)] bg-[var(--studio-surface-soft)] px-4 py-3 text-sm text-[var(--studio-ink)]">
            还没有可用的 API 节点，请点击右上角齿轮「设置」添加。
          </div>

          <div v-if="attempts.length" class="mt-4 flex flex-wrap gap-2">
            <div v-for="attempt in attempts" :key="`${attempt.api_id}-${attempt.ok}`" class="flex items-center gap-2 rounded-md border border-[var(--studio-line)] bg-[var(--studio-surface)] px-3 py-2 text-sm">
              <span>{{ attempt.api_name }}</span>
              <el-tag size="small" :type="attempt.ok ? 'success' : 'danger'">{{ attempt.ok ? '成功' : '失败' }}</el-tag>
            </div>
          </div>
        </div>

        <!-- Gallery -->
        <div class="studio-panel flex min-h-0 flex-1 flex-col rounded-lg p-5">
          <div class="mb-4 flex items-end justify-between">
            <div>
              <p class="text-xs font-semibold uppercase tracking-[0.16em] text-[var(--studio-amber)]">Gallery Output</p>
              <h2 class="mt-1 text-2xl font-black">生成结果</h2>
            </div>
            <div class="text-right text-sm text-[var(--studio-muted)]">
              <p>{{ images.length }} 张图片</p>
              <p v-if="expiresLabel" class="mt-0.5 text-xs text-[var(--studio-amber)]">链接将于 {{ expiresLabel }} 过期</p>
            </div>
          </div>

          <div v-if="showSkeleton" class="gallery-grid">
            <el-skeleton animated>
              <template #template>
                <el-skeleton-item variant="image" class="!h-[280px] !rounded-md" />
                <div class="mt-3">
                  <el-skeleton-item variant="p" class="!w-2/3" />
                </div>
              </template>
            </el-skeleton>
          </div>

          <div v-else-if="images.length" class="gallery-grid">
            <figure v-for="(url, index) in images" :key="url" class="group relative overflow-hidden rounded-md border border-[var(--studio-line)] bg-[var(--studio-surface)]">
              <el-image
                :src="url"
                :alt="`生成图片 ${index + 1}`"
                :preview-src-list="images"
                :initial-index="index"
                preview-teleported
                hide-on-click-modal
                fit="cover"
                loading="lazy"
                class="block aspect-square h-full w-full cursor-zoom-in"
              >
                <template #error>
                  <div class="flex aspect-square h-full w-full flex-col items-center justify-center gap-1 bg-[var(--studio-surface-soft)] text-center text-xs text-[var(--studio-muted)]">
                    <el-icon class="text-2xl"><Picture /></el-icon>
                    <span>图片无法加载</span>
                    <span>（链接可能已过期）</span>
                  </div>
                </template>
              </el-image>
              <figcaption class="pointer-events-none absolute inset-x-0 bottom-0 flex translate-y-full items-center justify-between bg-[rgba(23,33,38,0.86)] px-3 py-2 text-sm text-white transition group-hover:translate-y-0">
                <span>Image {{ index + 1 }}</span>
                <button
                  type="button"
                  class="pointer-events-auto inline-flex h-8 w-8 items-center justify-center rounded-md bg-white text-[#172126] transition hover:bg-[var(--studio-coral)] hover:text-white"
                  title="下载图片"
                  :aria-label="`下载第 ${index + 1} 张图片`"
                  @click="downloadOne(url, index)"
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
