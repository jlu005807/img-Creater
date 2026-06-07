<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { Clock, Delete, Download, MagicStick, Picture, RefreshLeft, Setting, ZoomIn } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { editImage, generateImages, getGenerationStatus } from '../../api/generation'
import { downloadImage } from '../../utils/download'
import { useGenerationHistory } from '../../composables/useGenerationHistory'
import RegionEditor from '../RegionEditor/index.vue'

const emit = defineEmits(['go-settings'])

const MAX_WAIT_SECONDS = 300
const POLL_INTERVAL_MS = 4000
const DRAFT_KEY = 'studio-form-draft'

const form = reactive({
  prompt: '',
  size: '1024x1024',
  n: 1,
})

const mode = ref('generate')
const maskState = ref({ hasImage: false, hasMask: false })
const regionEditorRef = ref(null)
const sizeOptions = [
  { value: 'auto', label: '自动 (auto)' },
  { value: '1024x1024', label: '1024 × 1024 · 方形' },
  { value: '1536x1024', label: '1536 × 1024 · 横向' },
  { value: '1024x1536', label: '1024 × 1536 · 纵向' },
]

const loading = ref(false)
const status = ref('idle')
const elapsedSeconds = ref(0)
const task = ref(null)
const images = ref([])
const errorMessage = ref('')
const attempts = ref([])
const expiresAt = ref(null)
const needsProvider = ref(false)
const historyOpen = ref(false)
const { history, addEntry, removeEntry, clearHistory } = useGenerationHistory()

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
}

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
      n: form.n,
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
    // 提交后只拿 task_id；后端在 worker 线程里完成"调用上游/容灾切换/轮询"的整个生命周期，
    // 结果存入内存任务表。前端固定每 4 秒查询 Flask /status，读取最新状态、命中的节点与尝试记录。
    // queued/processing 继续排队；completed/failed/timeout 立即停止，避免重复请求。
    const result = await getGenerationStatus({
      apiId: task.value.apiId,
      taskId: task.value.taskId,
    })
    // The elapsed timer may have fired stopWithTimeout() during the await;
    // don't clobber that terminal state with a late response.
    if (!loading.value) return
    status.value = result.status

    // 节点与尝试记录在 worker 完成后才确定，这里随状态一起回填到任务监视器。
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
        addEntry({
          prompt: form.prompt.trim(),
          mode: mode.value,
          size: form.size,
          n: form.n,
          urls: images.value,
          apiName: task.value?.apiName || result.api_name || '',
        })
      } else {
        errorMessage.value = '任务完成但未返回图片 URL'
      }
      return
    }

    if (result.status === 'failed') {
      stopWithError(result.error || '任务失败')
      return
    }

    schedulePoll()
  } catch (error) {
    stopWithError(error.message || '查询任务状态失败')
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

function formatHistoryTime(ts) {
  try {
    return new Date(ts).toLocaleString()
  } catch {
    return ''
  }
}

function reuseHistory(entry) {
  form.prompt = entry.prompt || ''
  if (entry.size) form.size = entry.size
  if (entry.n) form.n = entry.n
  if (entry.mode) mode.value = entry.mode
  historyOpen.value = false
  ElMessage.success('已回填该记录的参数')
}

function viewHistory(entry) {
  if (!entry.urls || !entry.urls.length) {
    ElMessage.info('该记录的图片未在本地保存（base64 结果仅当前会话可见），可复用参数重新生成')
    return
  }
  // Stop any in-flight run and clear unrelated run state before showing history.
  clearTimers()
  loading.value = false
  attempts.value = []
  expiresAt.value = null
  images.value = entry.urls
  status.value = 'completed'
  errorMessage.value = ''
  historyOpen.value = false
  ElMessage.info('正在查看历史结果；保存的远程链接可能已过期，如无法显示请复用参数重新生成')
}

function goSettings() {
  emit('go-settings')
}

function restoreDraft() {
  try {
    const draft = JSON.parse(window.localStorage.getItem(DRAFT_KEY))
    if (draft && typeof draft === 'object') {
      form.prompt = draft.prompt || ''
      if (draft.size) form.size = draft.size
      if (draft.n) form.n = draft.n
      if (draft.mode) mode.value = draft.mode
    }
  } catch {
    /* ignore malformed draft */
  }
}

// Persist the form so input survives a reload.
watch([() => form.prompt, () => form.size, () => form.n, mode], () => {
  try {
    window.localStorage.setItem(
      DRAFT_KEY,
      JSON.stringify({ prompt: form.prompt, size: form.size, n: form.n, mode: mode.value }),
    )
  } catch {
    /* ignore quota / availability errors */
  }
})

onMounted(restoreDraft)
onBeforeUnmount(clearTimers)
</script>

<template>
  <section class="grid min-h-[calc(100vh-48px)] grid-cols-[470px_minmax(0,1fr)] gap-5">
    <aside class="space-y-4">
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
            placeholder="描述要生成或修改的画面；局部编辑时只会把蒙版区域交给 AI 修改…（Ctrl/⌘ + Enter 提交）"
            @keydown.ctrl.enter.prevent="submitTask"
            @keydown.meta.enter.prevent="submitTask"
          />
        </label>

        <div class="mt-4 grid grid-cols-2 gap-3">
          <label class="block">
            <span class="mb-2 block text-sm font-semibold">尺寸</span>
            <el-select v-model="form.size" class="w-full">
              <el-option v-for="size in sizeOptions" :key="size.value" :label="size.label" :value="size.value" />
            </el-select>
          </label>

          <label class="block">
            <span class="mb-2 block text-sm font-semibold">张数</span>
            <el-input-number v-model="form.n" class="w-full" :min="1" :max="4" controls-position="right" />
          </label>
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

      <RegionEditor v-if="mode === 'edit'" ref="regionEditorRef" @mask-change="onMaskChange" />
    </aside>

    <main class="grid min-w-0 grid-rows-[auto_minmax(0,1fr)] gap-4">
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

        <div v-if="needsProvider" class="mt-4 flex items-center justify-between gap-3 rounded-md border border-[var(--studio-coral)] bg-[var(--studio-surface-soft)] px-4 py-3">
          <p class="text-sm text-[var(--studio-ink)]">还没有可用的 API 节点，先去「设置」添加并启用一个节点。</p>
          <el-button type="primary" :icon="Setting" @click="goSettings">前往设置</el-button>
        </div>

        <div v-if="attempts.length" class="mt-4 flex flex-wrap gap-2">
          <div v-for="attempt in attempts" :key="`${attempt.api_id}-${attempt.ok}`" class="flex items-center gap-2 rounded-md border border-[var(--studio-line)] bg-[var(--studio-surface)] px-3 py-2 text-sm">
            <span>{{ attempt.api_name }}</span>
            <el-tag size="small" :type="attempt.ok ? 'success' : 'danger'">{{ attempt.ok ? '成功' : '失败' }}</el-tag>
          </div>
        </div>
      </div>

      <div class="studio-panel min-h-0 rounded-lg p-5">
        <div class="mb-4 flex items-end justify-between">
          <div>
            <p class="text-xs font-semibold uppercase tracking-[0.16em] text-[var(--studio-amber)]">Gallery Output</p>
            <h2 class="mt-1 text-2xl font-black">生成结果</h2>
          </div>
          <div class="flex items-end gap-3">
            <div class="text-right text-sm text-[var(--studio-muted)]">
              <p>{{ images.length }} 张图片</p>
              <p v-if="expiresLabel" class="mt-0.5 text-xs text-[var(--studio-amber)]">链接将于 {{ expiresLabel }} 过期</p>
            </div>
            <el-badge :value="history.length" :hidden="!history.length" :max="99">
              <el-button :icon="Clock" @click="historyOpen = true">历史</el-button>
            </el-badge>
          </div>
        </div>

        <div v-if="showSkeleton" class="gallery-grid">
          <el-skeleton v-for="item in form.n" :key="item" animated>
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

        <div v-else class="flex h-full min-h-[520px] items-center justify-center rounded-md border border-dashed border-[var(--studio-line)] bg-[var(--studio-surface-soft)] px-6 text-center">
          <div>
            <el-icon class="text-4xl text-[var(--studio-teal)]"><Picture /></el-icon>
            <p class="mt-3 text-lg font-black">等待结果</p>
            <p class="mt-2 max-w-md text-sm leading-6 text-[var(--studio-muted)]">文生图会直接展示生成结果；局部编辑会基于原图和蒙版区域返回修改后的图片。</p>
          </div>
        </div>
      </div>
    </main>

    <el-drawer v-model="historyOpen" title="生成历史" direction="rtl" size="420px">
      <div class="mb-3 flex items-center justify-between">
        <span class="text-sm text-[var(--studio-muted)]">共 {{ history.length }} 条记录</span>
        <el-button v-if="history.length" text type="danger" :icon="Delete" @click="clearHistory">清空</el-button>
      </div>

      <div v-if="!history.length" class="flex min-h-[200px] items-center justify-center text-sm text-[var(--studio-muted)]">
        暂无历史记录
      </div>

      <div v-else class="thin-scrollbar space-y-3 overflow-auto pr-1">
        <article
          v-for="entry in history"
          :key="entry.id"
          class="rounded-md border border-[var(--studio-line)] bg-[var(--studio-surface)] p-3"
        >
          <div class="mb-2 flex items-center justify-between gap-2">
            <el-tag size="small" :type="entry.mode === 'edit' ? 'warning' : 'info'">
              {{ entry.mode === 'edit' ? '局部编辑' : '文生图' }}
            </el-tag>
            <span class="text-xs text-[var(--studio-muted)]">{{ formatHistoryTime(entry.time) }}</span>
          </div>
          <p class="line-clamp-2 text-sm text-[var(--studio-ink)]">{{ entry.prompt || '（无 Prompt）' }}</p>
          <p class="mt-1 text-xs text-[var(--studio-muted)]">{{ entry.size }} · {{ entry.imageCount }} 张 · {{ entry.apiName || '—' }}</p>

          <div v-if="entry.urls && entry.urls.length" class="mt-2 flex gap-2 overflow-hidden">
            <img
              v-for="(url, i) in entry.urls.slice(0, 4)"
              :key="i"
              :src="url"
              alt=""
              class="h-12 w-12 rounded border border-[var(--studio-line)] object-cover"
              loading="lazy"
            />
          </div>

          <div class="mt-3 flex items-center gap-2">
            <el-button size="small" :icon="RefreshLeft" @click="reuseHistory(entry)">复用参数</el-button>
            <el-button size="small" type="primary" :icon="Picture" @click="viewHistory(entry)">查看结果</el-button>
            <el-button size="small" text type="danger" :icon="Delete" aria-label="删除该历史记录" @click="removeEntry(entry.id)" />
          </div>
        </article>
      </div>
    </el-drawer>
  </section>
</template>

<style scoped>
.line-clamp-2 {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
</style>
