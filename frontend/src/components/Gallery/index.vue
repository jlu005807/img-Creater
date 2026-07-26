<script setup>
import { computed, onMounted, ref } from 'vue'
import { Download, Picture, Refresh } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { listSessions } from '../../api/generation'
import { backendRouteMissingMessage, isBackendRouteMissing } from '../../api/client'
import { downloadImage } from '../../utils/download'

const loading = ref(false)
const sessions = ref([])

const images = computed(() => {
  return sessions.value.flatMap((session) => {
    const list = Array.isArray(session.images) ? session.images : []
    return list.map((image, index) => ({
      ...image,
      key: `${session.id}-${index}-${image.url}`,
      prompt: session.prompt || '',
      mode: session.mode || 'generate',
      size: session.size || '',
      updatedAt: session.updated_at || session.created_at || '',
      sessionId: session.id,
    }))
  })
})

const previewUrls = computed(() => images.value.map((item) => item.url))

async function loadGallery() {
  loading.value = true
  try {
    const data = await listSessions()
    sessions.value = Array.isArray(data) ? data : []
  } catch (error) {
    ElMessage.error(isBackendRouteMissing(error) ? backendRouteMissingMessage('作品集') : error.message || '作品集加载失败')
  } finally {
    loading.value = false
  }
}

async function downloadOne(item, index) {
  const ok = await downloadImage(item.url, `img-Creater-${item.sessionId}-${index + 1}`)
  if (!ok) {
    ElMessage.info('图片已在新标签页打开，可右键另存为')
  }
}

function formatTime(value) {
  if (!value) return ''
  try {
    return new Date(value).toLocaleString()
  } catch {
    return ''
  }
}

onMounted(loadGallery)
</script>

<template>
  <section class="flex h-full min-h-0 flex-col p-5">
    <div class="studio-panel mb-4 rounded-lg p-5">
      <div class="flex items-center justify-between gap-3">
        <div>
          <p class="text-xs font-semibold uppercase tracking-[0.16em] text-[var(--studio-teal)]">Gallery</p>
          <h2 class="mt-1 text-2xl font-black">作品集</h2>
        </div>
        <el-button :icon="Refresh" :loading="loading" @click="loadGallery">刷新</el-button>
      </div>
    </div>

    <div v-loading="loading" class="studio-panel thin-scrollbar min-h-0 flex-1 overflow-auto rounded-lg p-5">
      <div v-if="!images.length && !loading" class="flex h-full min-h-[360px] items-center justify-center rounded-md border border-dashed border-[var(--studio-line)] bg-[var(--studio-surface-soft)] text-center">
        <div>
          <el-icon class="text-4xl text-[var(--studio-teal)]"><Picture /></el-icon>
          <p class="mt-3 text-lg font-black">暂无作品</p>
          <p class="mt-2 text-sm text-[var(--studio-muted)]">生成完成并保存到本地的图片会出现在这里。</p>
        </div>
      </div>

      <div v-else class="grid grid-cols-[repeat(auto-fill,minmax(220px,1fr))] gap-4">
        <article
          v-for="(item, index) in images"
          :key="item.key"
          class="group overflow-hidden rounded-md border border-[var(--studio-line)] bg-[var(--studio-surface)]"
        >
          <div class="relative aspect-square bg-[var(--studio-canvas)]">
            <el-image
              :src="item.url"
              fit="cover"
              lazy
              :preview-src-list="previewUrls"
              :initial-index="index"
              preview-teleported
              hide-on-click-modal
              class="h-full w-full cursor-zoom-in"
            />
            <button
              type="button"
              class="absolute right-2 top-2 flex h-9 w-9 items-center justify-center rounded-md bg-[rgba(23,33,38,0.76)] text-white opacity-0 transition hover:bg-[var(--studio-coral)] group-hover:opacity-100"
              title="下载图片"
              :aria-label="`下载作品 ${index + 1}`"
              @click.stop="downloadOne(item, index)"
            >
              <el-icon><Download /></el-icon>
            </button>
          </div>
          <div class="p-3">
            <p class="line-clamp-2 min-h-[2.5rem] text-sm font-semibold leading-5 text-[var(--studio-ink)]">{{ item.prompt || '未命名作品' }}</p>
            <div class="mt-2 flex items-center justify-between gap-2 text-xs text-[var(--studio-muted)]">
              <span>{{ item.mode === 'edit' ? '局部编辑' : '文生图' }}</span>
              <span>{{ item.size }}</span>
            </div>
            <p class="mt-1 truncate text-xs text-[var(--studio-muted)]">{{ formatTime(item.updatedAt) }}</p>
          </div>
        </article>
      </div>
    </div>
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
