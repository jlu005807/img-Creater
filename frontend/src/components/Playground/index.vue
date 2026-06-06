<script setup>
import { computed, onBeforeUnmount, reactive, ref } from 'vue'
import { Download, MagicStick, RefreshLeft } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { generateImages, getGenerationStatus } from '../../api/generation'

const MAX_WAIT_SECONDS = 300
const POLL_INTERVAL_MS = 4000

const form = reactive({
  prompt: '',
  size: '1024x1024',
  n: 1,
})

const sizeOptions = ['1024x1024', '1024x1536', '1536x1024']
const loading = ref(false)
const status = ref('idle')
const elapsedSeconds = ref(0)
const task = ref(null)
const images = ref([])
const errorMessage = ref('')
const attempts = ref([])

let elapsedTimer = null
let pollTimer = null

const statusLabel = computed(() => {
  const labels = {
    idle: '等待生成',
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
  if (!loading.value) return '生成图片'
  return `${statusLabel.value} · ${elapsedSeconds.value}s`
})

const showSkeleton = computed(() => loading.value && !images.value.length)

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
}

async function submitGeneration() {
  if (!form.prompt.trim()) {
    ElMessage.warning('请输入 Prompt')
    return
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
    const result = await generateImages({
      prompt: form.prompt.trim(),
      size: form.size,
      n: form.n,
    })
    task.value = { apiId: result.api_id, taskId: result.task_id, apiName: result.api_name }
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
    // 轮询递归核心逻辑：
    // 前端每 4 秒只查一次我们的 Flask /status，后端再代理到“提交成功的那个 api_id”。
    // queued / processing 继续排下一次 setTimeout；completed / failed 立即停止，避免无意义请求。
    const result = await getGenerationStatus({
      apiId: task.value.apiId,
      taskId: task.value.taskId,
    })
    status.value = result.status

    if (result.status === 'completed') {
      images.value = result.urls || []
      loading.value = false
      clearTimers()
      if (!images.value.length) {
        errorMessage.value = '任务完成但未返回图片 URL'
      }
      return
    }

    if (result.status === 'failed') {
      stopWithError(result.error || '中转站返回生成失败')
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
  clearTimers()
}

function stopWithTimeout() {
  loading.value = false
  status.value = 'timeout'
  errorMessage.value = '已超过 5 分钟，任务轮询已自动停止'
  clearTimers()
}

function reusePrompt() {
  form.prompt = '电影级产品摄影，一台半透明的桌面图像生成终端，暖色工作灯，清晰细节，真实材质'
}

onBeforeUnmount(clearTimers)
</script>

<template>
  <section class="grid gap-4 xl:grid-cols-[440px_minmax(0,1fr)]">
    <div class="space-y-4">
      <form class="studio-panel rounded-lg p-5" @submit.prevent="submitGeneration">
        <div class="mb-4 flex items-start justify-between gap-3">
          <div>
            <p class="text-xs font-semibold uppercase tracking-[0.16em] text-[var(--studio-coral)]">Async Image Console</p>
            <h2 class="mt-1 text-2xl font-black">生图工作区</h2>
          </div>
          <el-button :icon="RefreshLeft" @click="reusePrompt">示例</el-button>
        </div>

        <label class="block">
          <span class="mb-2 block text-sm font-semibold">Prompt</span>
          <el-input
            v-model="form.prompt"
            type="textarea"
            :rows="9"
            resize="none"
            maxlength="3000"
            show-word-limit
            placeholder="描述你要生成的画面、风格、材质、构图和限制条件..."
          />
        </label>

        <div class="mt-4 grid gap-3 sm:grid-cols-2">
          <label class="block">
            <span class="mb-2 block text-sm font-semibold">尺寸</span>
            <el-select v-model="form.size" class="w-full">
              <el-option v-for="size in sizeOptions" :key="size" :label="size" :value="size" />
            </el-select>
          </label>

          <label class="block">
            <span class="mb-2 block text-sm font-semibold">张数</span>
            <el-input-number v-model="form.n" class="w-full" :min="1" :max="4" controls-position="right" />
          </label>
        </div>

        <el-button class="mt-5 w-full" type="primary" size="large" native-type="submit" :loading="loading" :icon="MagicStick">
          {{ buttonText }}
        </el-button>
      </form>

      <div class="studio-panel rounded-lg p-5">
        <div class="flex items-center justify-between">
          <h3 class="text-lg font-black">任务状态</h3>
          <el-tag :type="status === 'completed' ? 'success' : status === 'failed' || status === 'timeout' ? 'danger' : 'warning'" effect="plain">
            {{ statusLabel }}
          </el-tag>
        </div>

        <div class="mt-4 grid grid-cols-3 gap-3 text-sm">
          <div class="rounded-md border border-[var(--studio-line)] bg-white/70 p-3">
            <p class="text-xs text-[var(--studio-muted)]">耗时</p>
            <p class="mt-1 text-xl font-black">{{ elapsedSeconds }}s</p>
          </div>
          <div class="rounded-md border border-[var(--studio-line)] bg-white/70 p-3">
            <p class="text-xs text-[var(--studio-muted)]">任务</p>
            <p class="mt-1 truncate text-sm font-bold">{{ task?.taskId || '-' }}</p>
          </div>
          <div class="rounded-md border border-[var(--studio-line)] bg-white/70 p-3">
            <p class="text-xs text-[var(--studio-muted)]">节点</p>
            <p class="mt-1 truncate text-sm font-bold">{{ task?.apiName || '-' }}</p>
          </div>
        </div>

        <el-alert v-if="errorMessage" class="mt-4" type="error" :closable="false" :title="errorMessage" />

        <div v-if="attempts.length" class="mt-4 space-y-2">
          <p class="text-xs font-semibold uppercase tracking-[0.14em] text-[var(--studio-muted)]">Fallback Trace</p>
          <div v-for="attempt in attempts" :key="`${attempt.api_id}-${attempt.ok}`" class="flex items-center justify-between rounded-md border border-[var(--studio-line)] bg-white px-3 py-2 text-sm">
            <span class="truncate">{{ attempt.api_name }}</span>
            <el-tag size="small" :type="attempt.ok ? 'success' : 'danger'">{{ attempt.ok ? '成功' : '失败' }}</el-tag>
          </div>
        </div>
      </div>
    </div>

    <div class="studio-panel min-h-[720px] rounded-lg p-5">
      <div class="mb-4 flex flex-col gap-2 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <p class="text-xs font-semibold uppercase tracking-[0.16em] text-[var(--studio-teal)]">Gallery Output</p>
          <h2 class="mt-1 text-2xl font-black">生成结果</h2>
        </div>
        <p class="text-sm text-[var(--studio-muted)]">{{ images.length }} 张图片</p>
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
        <figure v-for="(url, index) in images" :key="url" class="group relative overflow-hidden rounded-md border border-[var(--studio-line)] bg-white">
          <img :src="url" :alt="`生成图片 ${index + 1}`" class="aspect-square h-full w-full object-cover" loading="lazy" />
          <figcaption class="absolute inset-x-0 bottom-0 flex translate-y-full items-center justify-between bg-[rgba(23,33,38,0.86)] px-3 py-2 text-sm text-white transition group-hover:translate-y-0">
            <span>Image {{ index + 1 }}</span>
            <a :href="url" download target="_blank" rel="noreferrer" class="inline-flex h-8 w-8 items-center justify-center rounded-md bg-white text-[var(--studio-ink)]" title="下载图片">
              <el-icon><Download /></el-icon>
            </a>
          </figcaption>
        </figure>
      </div>

      <div v-else class="flex min-h-[560px] items-center justify-center rounded-md border border-dashed border-[var(--studio-line)] bg-white/55 px-6 text-center">
        <div>
          <p class="text-lg font-black">等待第一组生成结果</p>
          <p class="mt-2 max-w-md text-sm leading-6 text-[var(--studio-muted)]">提交后会先显示骨架屏，任务完成后图片会在这里以画廊形式展示。</p>
        </div>
      </div>
    </div>
  </section>
</template>
