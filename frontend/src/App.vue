<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { Aim, MagicStick, Moon, Picture, Setting, Sunny } from '@element-plus/icons-vue'
import Settings from './components/Settings/index.vue'
import Playground from './components/Playground/index.vue'
import Detector from './components/Detector/index.vue'
import Gallery from './components/Gallery/index.vue'
import { useTheme } from './composables/useTheme'

const settingsOpen = ref(false)
const detectorOpen = ref(false)
const activePage = ref('playground')

const { theme, toggleTheme } = useTheme()
const isDark = computed(() => theme.value === 'dark')

function setPage(page) {
  activePage.value = page
  const nextPath = page === 'gallery' ? '/gallery' : '/'
  if (window.location.pathname !== nextPath) {
    window.history.pushState({}, '', nextPath)
  }
}

function syncPageFromLocation() {
  activePage.value = window.location.pathname === '/gallery' || window.location.hash === '#gallery'
    ? 'gallery'
    : 'playground'
}

onMounted(() => {
  syncPageFromLocation()
  window.addEventListener('popstate', syncPageFromLocation)
  window.addEventListener('hashchange', syncPageFromLocation)
})

onBeforeUnmount(() => {
  window.removeEventListener('popstate', syncPageFromLocation)
  window.removeEventListener('hashchange', syncPageFromLocation)
})
</script>

<template>
  <div class="flex h-screen min-h-0 flex-col overflow-hidden">
    <!-- Top bar -->
    <header class="flex items-center justify-between border-b border-[var(--studio-line)] bg-[var(--studio-panel)] px-6 py-3">
      <div class="flex items-center gap-3">
        <h1 class="text-lg font-black text-[var(--studio-ink)]">img-Creater</h1>
        <span class="rounded bg-[var(--studio-coral)]/10 px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wider text-[var(--studio-coral)]">Studio</span>
      </div>
      <div class="flex items-center gap-2">
        <div class="mr-2 inline-grid grid-cols-2 rounded-md border border-[var(--studio-line)] bg-[var(--studio-surface)] p-1">
          <button
            type="button"
            class="flex h-8 items-center gap-1.5 rounded-[6px] px-3 text-sm font-semibold transition"
            :class="activePage === 'playground' ? 'bg-[var(--studio-solid)] text-[var(--studio-on-solid)]' : 'text-[var(--studio-muted)] hover:bg-[var(--studio-paper)]'"
            @click="setPage('playground')"
          >
            <el-icon><MagicStick /></el-icon>
            <span>生成</span>
          </button>
          <button
            type="button"
            class="flex h-8 items-center gap-1.5 rounded-[6px] px-3 text-sm font-semibold transition"
            :class="activePage === 'gallery' ? 'bg-[var(--studio-solid)] text-[var(--studio-on-solid)]' : 'text-[var(--studio-muted)] hover:bg-[var(--studio-paper)]'"
            @click="setPage('gallery')"
          >
            <el-icon><Picture /></el-icon>
            <span>作品集</span>
          </button>
        </div>
        <button
          type="button"
          class="flex h-9 w-9 shrink-0 items-center justify-center rounded-md border border-[var(--studio-line)] text-[var(--studio-muted)] transition hover:border-[var(--studio-coral)] hover:text-[var(--studio-coral)]"
          :title="isDark ? '切换到浅色模式' : '切换到深色模式'"
          :aria-label="isDark ? '切换到浅色模式' : '切换到深色模式'"
          @click="toggleTheme"
        >
          <el-icon><component :is="isDark ? Sunny : Moon" /></el-icon>
        </button>
        <button
          type="button"
          class="flex h-9 items-center gap-1.5 shrink-0 rounded-md border border-[var(--studio-line)] px-2.5 text-sm font-semibold text-[var(--studio-muted)] transition hover:border-[var(--studio-amber)] hover:text-[var(--studio-amber)]"
          title="AI 生成检测 (Beta)"
          aria-label="打开 AI 生成检测"
          @click="detectorOpen = true"
        >
          <el-icon><Aim /></el-icon>
          <span>检测</span>
          <span class="rounded bg-[var(--studio-amber)]/15 px-1 text-[10px] uppercase">beta</span>
        </button>
        <button
          type="button"
          class="flex h-9 w-9 shrink-0 items-center justify-center rounded-md border border-[var(--studio-line)] text-[var(--studio-muted)] transition hover:border-[var(--studio-teal)] hover:text-[var(--studio-teal)]"
          title="设置"
          aria-label="打开设置"
          @click="settingsOpen = true"
        >
          <el-icon><Setting /></el-icon>
        </button>
      </div>
    </header>

    <!-- Detection modal (beta) -->
    <el-dialog v-model="detectorOpen" title="AI 生成检测 (Beta)" width="560px" top="6vh" destroy-on-close>
      <Detector />
    </el-dialog>

    <!-- Settings modal -->
    <el-dialog v-model="settingsOpen" title="设置" width="1180px" top="6vh" destroy-on-close>
      <Settings @close="settingsOpen = false" />
      <template #footer>
        <el-button @click="settingsOpen = false">关闭</el-button>
      </template>
    </el-dialog>

    <!-- Main area: left history sidebar + right playground -->
    <div class="flex min-h-0 flex-1 overflow-hidden">
      <Playground v-if="activePage === 'playground'" class="flex-1" />
      <Gallery v-else class="flex-1" />
    </div>
  </div>
</template>
