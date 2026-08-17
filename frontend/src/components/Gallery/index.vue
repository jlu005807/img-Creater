<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Check, Close, Delete, Download, Picture, Refresh } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { useGenerationHistory } from '../../composables/useGenerationHistory'
import { useInfiniteScrollSentinel } from '../../composables/useInfiniteScrollSentinel'
import {
  MAX_BATCH_IMAGE_COUNT,
  downloadImage,
  downloadImagesAsZip,
} from '../../utils/download'

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
const selectionMode = ref(false)
const selectedKeys = ref(new Set())
const selectedItems = ref(new Map())
const batchDownloading = ref(false)
const batchFailures = ref([])
const batchError = ref('')
const downloadProgress = ref({ completed: 0, total: 0, succeeded: 0, failed: 0, totalBytes: 0 })
let batchController = null

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
const selectedCount = computed(() => selectedKeys.value.size)
const selectedForDownload = computed(() => Array.from(selectedKeys.value)
  .map((key) => selectedItems.value.get(key))
  .filter(Boolean))
const selectionOverLimit = computed(() => selectedCount.value > MAX_BATCH_IMAGE_COUNT)
const downloadPercentage = computed(() => {
  if (!downloadProgress.value.total) return 0
  return Math.round((downloadProgress.value.completed / downloadProgress.value.total) * 100)
})

function replaceSelection(keys, items) {
  selectedKeys.value = keys
  selectedItems.value = items
}

function toggleSelection(item) {
  if (batchDownloading.value) return
  batchError.value = ''
  const keys = new Set(selectedKeys.value)
  const items = new Map(selectedItems.value)
  if (keys.has(item.key)) {
    keys.delete(item.key)
    items.delete(item.key)
    batchFailures.value = batchFailures.value.filter((failure) => failure.key !== item.key)
  } else {
    keys.add(item.key)
    items.set(item.key, item)
    if (keys.size === MAX_BATCH_IMAGE_COUNT + 1) {
      ElMessage.warning(`单次最多下载 ${MAX_BATCH_IMAGE_COUNT} 张图片，请减少选择`)
    }
  }
  replaceSelection(keys, items)
}

function selectLoadedImages() {
  if (batchDownloading.value) return
  batchError.value = ''
  const keys = new Set(selectedKeys.value)
  const items = new Map(selectedItems.value)
  for (const item of images.value) {
    keys.add(item.key)
    items.set(item.key, item)
  }
  replaceSelection(keys, items)
  if (keys.size > MAX_BATCH_IMAGE_COUNT) {
    ElMessage.warning(`已选 ${keys.size} 张，单次下载上限为 ${MAX_BATCH_IMAGE_COUNT} 张`)
  }
}

function clearSelection() {
  if (batchDownloading.value) return
  replaceSelection(new Set(), new Map())
  batchFailures.value = []
  batchError.value = ''
}

function removeSelectedKeys(keysToRemove) {
  const keys = new Set(selectedKeys.value)
  const items = new Map(selectedItems.value)
  for (const key of keysToRemove) {
    keys.delete(key)
    items.delete(key)
  }
  replaceSelection(keys, items)
}

function reconcileSelection() {
  const historyFullyLoaded = !hasMore.value
  if (!historyFullyLoaded || initialLoading.value || loadingMore.value || loadError.value) return
  const available = new Set(images.value.map((item) => item.key))
  const missing = Array.from(selectedKeys.value).filter((key) => !available.has(key))
  if (!missing.length) return
  removeSelectedKeys(missing)
  batchFailures.value = batchFailures.value.filter((failure) => available.has(failure.key))
  if (!batchFailures.value.length) batchError.value = ''
}

watch(
  [images, hasMore, initialLoading, loadingMore, loadError],
  () => {
    if (selectedKeys.value.size) {
      const items = new Map(selectedItems.value)
      for (const item of images.value) {
        if (selectedKeys.value.has(item.key)) items.set(item.key, item)
      }
      selectedItems.value = items
    }
    reconcileSelection()
  },
  { immediate: true },
)

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

function updateProgress(progress) {
  downloadProgress.value = progress
}

function resetBatchState(total) {
  batchFailures.value = []
  batchError.value = ''
  downloadProgress.value = { completed: 0, total, succeeded: 0, failed: 0, totalBytes: 0 }
}

async function runBatchDownload(items) {
  if (batchDownloading.value || !items.length) return
  if (items.length > MAX_BATCH_IMAGE_COUNT) {
    batchError.value = `单次最多下载 ${MAX_BATCH_IMAGE_COUNT} 张图片，当前已选 ${items.length} 张`
    ElMessage.warning(batchError.value)
    return
  }

  batchController = new AbortController()
  batchDownloading.value = true
  resetBatchState(items.length)
  try {
    const result = await downloadImagesAsZip(items, {
      signal: batchController.signal,
      onProgress: updateProgress,
    })
    if (result.aborted) {
      ElMessage.info('批量下载已取消')
      return
    }

    batchFailures.value = result.errors
    removeSelectedKeys(result.successes.map((success) => success.key))
    if (!result.downloaded) {
      batchError.value = '所选图片均下载失败，请检查图片地址或跨域权限后重试'
      ElMessage.error(batchError.value)
    } else if (result.failed) {
      batchError.value = `${result.failed} 张图片下载失败，可重试失败项`
      ElMessage.warning(`ZIP 已下载，其中 ${result.failed} 张图片失败`)
    } else {
      ElMessage.success(`已打包下载 ${result.downloaded} 张图片`)
    }
  } catch (error) {
    batchError.value = error?.message || '批量下载失败，请重试'
    ElMessage.error(batchError.value)
  } finally {
    batchController = null
    batchDownloading.value = false
  }
}

async function downloadSelectedImages() {
  await runBatchDownload(selectedForDownload.value)
}

async function retryFailedDownloads() {
  const failedItems = batchFailures.value
    .map((failure) => selectedItems.value.get(failure.key) || failure.item)
    .filter(Boolean)
  await runBatchDownload(failedItems)
}

function cancelBatchDownload() {
  batchController?.abort()
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

onMounted(async () => {
  try {
    await ensureSessions({})
  } catch {
    // The shared inline state keeps the failed request retryable.
  }
})

onBeforeUnmount(() => {
  batchController?.abort()
})
</script>

<template>
  <section class="flex h-full min-h-0 flex-col p-5">
    <div class="studio-panel mb-4 rounded-lg p-5">
      <div class="flex flex-wrap items-center justify-between gap-3">
        <div>
          <p class="text-xs font-semibold uppercase tracking-[0.16em] text-[var(--studio-teal)]">Gallery</p>
          <h2 class="mt-1 text-2xl font-black">作品集</h2>
        </div>
        <div class="flex flex-wrap items-center justify-end gap-2">
          <span
            v-if="selectionMode"
            class="text-sm font-semibold"
            :class="selectionOverLimit ? 'text-[var(--studio-coral)]' : 'text-[var(--studio-muted)]'"
            aria-live="polite"
          >
            已选 {{ selectedCount }} 张<span v-if="selectionOverLimit">（上限 {{ MAX_BATCH_IMAGE_COUNT }} 张）</span>
          </span>
          <el-button
            :icon="Check"
            :disabled="batchDownloading"
            :title="selectionMode ? '退出选择模式' : '进入选择模式'"
            :aria-pressed="selectionMode"
            @click="selectionMode = !selectionMode"
          >
            {{ selectionMode ? '退出选择' : '选择作品' }}
          </el-button>
          <template v-if="selectionMode">
            <el-button :disabled="batchDownloading || !images.length" title="选择当前已加载的全部图片" @click="selectLoadedImages">
              全选已加载
            </el-button>
            <el-button :icon="Delete" :disabled="batchDownloading || !selectedCount" title="清空选择" @click="clearSelection">
              清空
            </el-button>
            <el-button
              v-if="!batchDownloading"
              type="primary"
              :icon="Download"
              :disabled="!selectedCount || selectionOverLimit"
              title="将所选图片打包为 ZIP"
              @click="downloadSelectedImages"
            >
              下载 ZIP
            </el-button>
            <el-button v-else type="danger" :icon="Close" title="取消当前批量下载" @click="cancelBatchDownload">
              取消下载
            </el-button>
          </template>
          <el-button :icon="Refresh" :loading="initialLoading" :disabled="batchDownloading" @click="loadGallery">刷新</el-button>
        </div>
      </div>

      <div v-if="batchDownloading" class="mt-4 border-t border-[var(--studio-line)] pt-4" aria-live="polite">
        <div class="mb-2 flex flex-wrap items-center justify-between gap-x-3 gap-y-1 text-xs text-[var(--studio-muted)]">
          <span>正在获取并压缩图片</span>
          <span class="flex flex-wrap items-center gap-x-3 gap-y-1" aria-label="批量下载计数">
            <span>完成 {{ downloadProgress.completed }} / {{ downloadProgress.total }}</span>
            <span>成功 {{ downloadProgress.succeeded }}</span>
            <span :class="downloadProgress.failed ? 'text-[var(--studio-coral)]' : ''">失败 {{ downloadProgress.failed }}</span>
          </span>
        </div>
        <el-progress :percentage="downloadPercentage" :stroke-width="8" :show-text="false" />
      </div>

      <div
        v-else-if="batchError || batchFailures.length"
        class="mt-4 flex flex-wrap items-center justify-between gap-3 border-t border-[var(--studio-line)] pt-4 text-sm"
        aria-live="polite"
      >
        <div class="min-w-0">
          <p class="font-semibold text-[var(--studio-coral)]">{{ batchError || `${batchFailures.length} 张图片失败` }}</p>
          <p v-if="batchFailures.length" class="mt-1 truncate text-xs text-[var(--studio-muted)]" :title="batchFailures[0].message">
            可重试错误：{{ batchFailures[0].message }}<span v-if="batchFailures.length > 1"> 等 {{ batchFailures.length }} 项</span>
          </p>
        </div>
        <el-button v-if="batchFailures.length" :icon="Refresh" @click="retryFailedDownloads">重试失败项</el-button>
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
          class="gallery-card group overflow-hidden rounded-md border border-[var(--studio-line)] bg-[var(--studio-surface)]"
          :class="{ 'is-selected': selectedKeys.has(item.key) }"
        >
          <div class="gallery-image relative aspect-square bg-[var(--studio-canvas)]">
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
            <input
              v-if="selectionMode"
              type="checkbox"
              :checked="selectedKeys.has(item.key)"
              class="selection-checkbox absolute left-2 top-2 z-10 h-6 w-6 cursor-pointer"
              :aria-label="`${selectedKeys.has(item.key) ? '取消选择' : '选择'}作品 ${index + 1}`"
              :title="selectedKeys.has(item.key) ? '取消选择' : '选择图片'"
              @click.stop
              @change="toggleSelection(item)"
            />
            <button
              v-if="!selectionMode"
              type="button"
              class="download-button absolute right-2 top-2 flex h-9 w-9 items-center justify-center rounded-md bg-[rgba(23,33,38,0.76)] text-white opacity-0 transition hover:bg-[var(--studio-coral)] group-hover:opacity-100"
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
.gallery-card {
  transition: border-color 160ms ease, box-shadow 160ms ease;
}

.gallery-card.is-selected {
  border-color: var(--studio-teal);
  box-shadow: inset 0 0 0 2px var(--studio-teal);
}

.gallery-card.is-selected .gallery-image::after {
  position: absolute;
  inset: 0;
  z-index: 1;
  background: color-mix(in srgb, var(--studio-teal) 14%, transparent);
  content: '';
  pointer-events: none;
}

.selection-checkbox {
  accent-color: var(--studio-teal);
  filter: drop-shadow(0 1px 2px rgba(23, 33, 38, 0.45));
}

.selection-checkbox:focus-visible,
.download-button:focus-visible {
  outline: 2px solid var(--studio-teal);
  outline-offset: 2px;
}

.download-button:focus-visible {
  opacity: 1;
}

@media (hover: none) {
  .download-button {
    opacity: 1;
  }
}

.line-clamp-2 {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
</style>
