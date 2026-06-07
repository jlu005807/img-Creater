<script setup>
import { computed, ref } from 'vue'
import { Moon, Setting, Sunny } from '@element-plus/icons-vue'
import APIConfig from './components/APIConfig/index.vue'
import Playground from './components/Playground/index.vue'
import { useTheme } from './composables/useTheme'

const settingsOpen = ref(false)

const { theme, toggleTheme } = useTheme()
const isDark = computed(() => theme.value === 'dark')
</script>

<template>
  <div class="flex min-h-screen flex-col">
    <!-- Top bar -->
    <header class="flex items-center justify-between border-b border-[var(--studio-line)] bg-[var(--studio-panel)] px-6 py-3">
      <div class="flex items-center gap-3">
        <h1 class="text-lg font-black text-[var(--studio-ink)]">GPT Img2 Creater</h1>
        <span class="rounded bg-[var(--studio-coral)]/10 px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wider text-[var(--studio-coral)]">Studio</span>
      </div>
      <div class="flex items-center gap-2">
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
          class="flex h-9 w-9 shrink-0 items-center justify-center rounded-md border border-[var(--studio-line)] text-[var(--studio-muted)] transition hover:border-[var(--studio-teal)] hover:text-[var(--studio-teal)]"
          title="设置"
          aria-label="打开设置"
          @click="settingsOpen = true"
        >
          <el-icon><Setting /></el-icon>
        </button>
      </div>
    </header>

    <!-- Settings modal -->
    <el-dialog v-model="settingsOpen" title="设置" width="680px" destroy-on-close>
      <APIConfig />
      <template #footer>
        <el-button @click="settingsOpen = false">关闭</el-button>
      </template>
    </el-dialog>

    <!-- Main area: left history sidebar + right playground -->
    <div class="flex flex-1 overflow-hidden">
      <Playground class="flex-1" />
    </div>
  </div>
</template>
