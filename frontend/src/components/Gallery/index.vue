<script setup>
import { computed, onMounted, ref } from 'vue'
import { Download, Picture, Refresh } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { useGenerationHistory } from '../../composables/useGenerationHistory'
import { useInfiniteScrollSentinel } from '../../composables/useInfiniteScrollSentinel'
import { downloadImage } from '../../utils/download'

const {
  history,
  refreshSessions,
  ensureSessions,
  loadMoreSessions,
  retrySessions,
  initialLoading,
  loadingMore,
  loadError,
  hasMore,
} = useGenerationHistory()

const galleryScrollRef = ref(null)
const gallerySentinelRef = ref(null)
const canLoadMore = computed(
  () => hasMore.value && !initialLoading.value && !loadingMore.value && !loadError.value,
)

const images = computed(() => {
  return history.value
    .filter((session) => (session.status || session._status) === 'completed')
    .flatMap((session) => {
      const list = Array.isArray(session.images) ? session.images : []
      return list.map((image) => ({
        ...image,
        prompt: session.prompt || '',
        mode: session.mode || 'generate',
        size: session.size || '',
        updatedAt: session.updatedAt || session.createdAt || '',
      }))
    })
})

const previewUrls = computed(() => images.value.map((item) => item.url))

async function loadGallery() {
  try {
    await refreshSessions()
  } catch {
    // The shared inline state keeps the failed request retryable.
  }
}

async function loadNextGalleryPage() {
  try {
    await loadMoreSessions()
  } catch {
    // The shared inline state keeps the failed cursor retryable.
  }
}

async function retryGalleryLoad() {
  try {
    await retrySessions()
  } catch {
    // Keep the current cards and retry command visible.
  }
}

useInfiniteScrollSentinel({
  rootRef: galleryScrollRef,
  sentinelRef: gallerySentinelRef,
  enabled: canLoadMore,
  onIntersect: loadNextGalleryPage,
})

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

onMounted(async () => {
  try {
    await ensureSessions({})
  } catch {
    // The shared inline state keeps the failed request retryable.
  }
})
</script>

<template>
  <section class="flex h-full min-h-0 flex-col p-5">
    <div class="studio-panel mb-4 rounded-lg p-5">
      <div class="flex items-center justify-between gap-3">
        <div>
          <p class="text-xs font-semibold uppercase tracking-[0.16em] text-[var(--studio-teal)]">Gallery</p>
          <h2 class="mt-1 text-2xl font-black">作品集</h2>
        </div>
        <el-button :icon="Refresh" :loading="initialLoading" @click="loadGallery">刷新</el-button>
      </div>
    </div>

    <div
      ref="galleryScrollRef"
      v-loading="initialLoading && !images.length"
      class="studio-panel thin-scrollbar min-h-0 flex-1 overflow-auto rounded-lg p-5"
    >
      <div v-if="!images.length && !initialLoading && !loadError" class="flex h-full min-h-[360px] items-center justify-center rounded-md border border-dashed border-[var(--studio-line)] bg-[var(--studio-surface-soft)] text-center">
        <div>
          <el-icon class="text-4xl text-[var(--studio-teal)]"><Picture /></el-icon>
          <p class="mt-3 text-lg font-black">暂无作品</p>
          <p class="mt-2 text-sm text-[var(--studio-muted)]">生成完成并保存到本地的图片会出现在这里。</p>
        </div>
      </div>

      <div v-else-if="!images.length && !initialLoading && loadError" class="flex min-h-[220px] items-center justify-center rounded-md border border-dashed border-[var(--studio-line)] bg-[var(--studio-surface-soft)] px-5 text-center">
        <div>
          <el-icon class="text-4xl text-[var(--studio-coral)]"><Picture /></el-icon>
          <p class="mt-3 text-sm font-semibold text-[var(--studio-coral)]">{{ loadError.message || '作品加载失败' }}</p>
          <button type="button" class="mt-3 text-xs font-bold text-[var(--studio-teal)] hover:underline" @click="retryGalleryLoad">重试</button>
        </div>
      </div>

      <div v-else-if="images.length" class="grid grid-cols-[repeat(auto-fill,minmax(220px,1fr))] gap-4">
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

      <div ref="gallerySentinelRef" class="h-px" aria-hidden="true"></div>
      <div v-if="initialLoading" class="py-4 text-center text-xs text-[var(--studio-muted)]">正在刷新作品…</div>
      <div v-else-if="loadingMore" class="py-4 text-center text-xs text-[var(--studio-muted)]">正在加载更多作品…</div>
      <div v-else-if="loadError && images.length" class="flex items-center justify-center gap-2 py-4 text-xs text-[var(--studio-coral)]">
        <span>{{ loadError.message || '作品加载失败' }}</span>
        <button type="button" class="font-bold text-[var(--studio-teal)] hover:underline" @click="retryGalleryLoad">重试</button>
      </div>
      <div v-else-if="!hasMore && !initialLoading" class="py-4 text-center text-xs text-[var(--studio-muted)]">已加载全部作品</div>
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
