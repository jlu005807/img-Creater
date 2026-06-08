<script setup>
import { computed, onMounted, ref } from 'vue'
import { Upload } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { detectImage, detectorHealth } from '../../api/detection'

const fileInputRef = ref(null)
const imageDataUrl = ref('')
const fileName = ref('')
const loading = ref(false)
const result = ref(null)
const health = ref(null)
const isDragOver = ref(false)

const available = computed(() => health.value?.available !== false)

const verdictMeta = computed(() => {
  const v = result.value?.verdict
  return {
    ai: { label: 'AI 生成', type: 'danger' },
    suspicious: { label: '可疑', type: 'warning' },
    real: { label: '真实', type: 'success' },
    unavailable: { label: '功能未启用', type: 'info' },
  }[v] || null
})

const scorePercent = computed(() => {
  const s = result.value?.score
  return typeof s === 'number' ? Math.round(s * 100) : null
})

const stageList = computed(() => {
  const stages = result.value?.stages || {}
  const names = {
    watermark: '隐形水印', metadata: '元数据', frequency: '频域',
    noise: '噪声纹理', jpeg: 'JPEG 历史', color: '颜色一致性',
  }
  return Object.entries(stages).map(([key, val]) => ({
    key,
    name: names[key] || key,
    score: typeof val.score === 'number' ? val.score : null,
    error: val.error || null,
    signals: val.signals || {},
    evidence: val.evidence || [],
    hit: val.hit || false,
  }))
})

async function loadHealth() {
  try {
    health.value = await detectorHealth()
  } catch {
    health.value = { available: false }
  }
}

function openPicker() {
  fileInputRef.value?.click()
}

function onInput(event) {
  readFile(event.target.files?.[0])
  event.target.value = ''
}

function onDrop(event) {
  isDragOver.value = false
  readFile(event.dataTransfer?.files?.[0])
}

function readFile(file) {
  if (!file) return
  if (!file.type.startsWith('image/')) {
    ElMessage.warning('请选择图片文件')
    return
  }
  const reader = new FileReader()
  reader.onload = () => {
    imageDataUrl.value = reader.result
    fileName.value = file.name
    result.value = null
  }
  reader.readAsDataURL(file)
}

async function runDetection() {
  if (!imageDataUrl.value) {
    ElMessage.warning('请先上传图片')
    return
  }
  loading.value = true
  result.value = null
  try {
    result.value = await detectImage(imageDataUrl.value, fileName.value)
    if (result.value?.available === false) {
      ElMessage.info('检测功能未启用：请安装 detection/requirements.txt 依赖')
    }
  } catch (error) {
    ElMessage.error(error.message || '检测失败')
  } finally {
    loading.value = false
  }
}

onMounted(loadHealth)
</script>

<template>
  <div class="space-y-4">
    <div class="flex items-center gap-2">
      <h3 class="text-lg font-black">AI 生成图像检测</h3>
      <el-tag size="small" type="warning" effect="plain">Beta</el-tag>
    </div>

    <el-alert
      v-if="!available"
      type="info"
      :closable="false"
      title="检测功能未启用"
      description="该功能需要额外依赖。请在项目根目录运行：pip install -r detection/requirements.txt，然后重启后端。"
    />

    <p class="text-sm text-[var(--studio-muted)]">
      使用传统信号处理（频域 / 噪声 / JPEG / 颜色 / 水印 / 元数据）综合判断图片是否由 AI 生成，完全本地、无需 GPU。
    </p>

    <!-- Upload -->
    <input ref="fileInputRef" class="hidden" type="file" accept="image/*" @change="onInput" />
    <div
      class="flex cursor-pointer flex-col items-center justify-center gap-2 rounded-md border border-dashed p-6 text-center transition"
      :class="isDragOver ? 'border-[var(--studio-coral)] bg-[var(--studio-surface-soft)]' : 'border-[var(--studio-line)]'"
      @click="openPicker"
      @dragover.prevent="isDragOver = true"
      @dragleave.prevent="isDragOver = false"
      @drop.prevent="onDrop"
    >
      <img v-if="imageDataUrl" :src="imageDataUrl" alt="" class="max-h-48 rounded-md object-contain" />
      <template v-else>
        <el-icon class="text-3xl text-[var(--studio-muted)]"><Upload /></el-icon>
        <p class="text-sm text-[var(--studio-muted)]">点击或拖拽图片到此处</p>
      </template>
    </div>
    <p v-if="fileName" class="truncate text-xs text-[var(--studio-muted)]">{{ fileName }}</p>

    <el-button type="primary" class="w-full" :loading="loading" :disabled="!imageDataUrl" @click="runDetection">
      开始检测
    </el-button>

    <!-- Result -->
    <div v-if="result && result.available !== false" class="space-y-3 rounded-md border border-[var(--studio-line)] p-4">
      <div class="flex items-center justify-between">
        <el-tag v-if="verdictMeta" :type="verdictMeta.type" size="large" effect="dark">{{ verdictMeta.label }}</el-tag>
        <span class="text-xs text-[var(--studio-muted)]">耗时 {{ result.elapsed_ms }}ms</span>
      </div>

      <div v-if="scorePercent !== null">
        <div class="mb-1 flex items-center justify-between text-xs text-[var(--studio-muted)]">
          <span>AI 置信度</span><span>{{ scorePercent }}%</span>
        </div>
        <el-progress
          :percentage="scorePercent"
          :status="result.verdict === 'ai' ? 'exception' : result.verdict === 'real' ? 'success' : 'warning'"
          :show-text="false"
        />
      </div>

      <div v-if="result.evidence && result.evidence.length" class="space-y-1">
        <p class="text-xs font-bold text-[var(--studio-ink)]">证据</p>
        <p v-for="(e, i) in result.evidence" :key="i" class="text-xs text-[var(--studio-muted)]">· {{ e }}</p>
      </div>

      <el-collapse>
        <el-collapse-item title="各模块信号" name="stages">
          <div class="space-y-2">
            <div v-for="s in stageList" :key="s.key" class="rounded border border-[var(--studio-line)] p-2 text-xs">
              <div class="flex items-center justify-between">
                <span class="font-semibold text-[var(--studio-ink)]">{{ s.name }}</span>
                <span v-if="s.error" class="text-[var(--studio-muted)]">未运行</span>
                <span v-else-if="s.hit" class="text-[var(--studio-coral)]">命中</span>
                <span v-else-if="s.score !== null" class="text-[var(--studio-muted)]">分数 {{ s.score }}</span>
                <span v-else class="text-[var(--studio-muted)]">无信号</span>
              </div>
              <p v-for="(e, i) in s.evidence" :key="i" class="mt-0.5 text-[var(--studio-muted)]">· {{ e }}</p>
            </div>
          </div>
        </el-collapse-item>
      </el-collapse>

      <p class="text-[11px] leading-5 text-[var(--studio-muted)]">
        Beta 功能：传统方法对最新生成模型判别力有限，结果仅供参考。
      </p>
    </div>
  </div>
</template>
