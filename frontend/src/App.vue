<script setup>
import { computed, ref } from 'vue'
import { Moon, Picture, Setting, Sunny } from '@element-plus/icons-vue'
import APIConfig from './components/APIConfig/index.vue'
import Playground from './components/Playground/index.vue'
import { useTheme } from './composables/useTheme'

const activeTab = ref('playground')

const { theme, toggleTheme } = useTheme()
const isDark = computed(() => theme.value === 'dark')

const navItems = [
  { key: 'playground', label: '工作区', icon: Picture },
  { key: 'settings', label: '设置', icon: Setting },
]
</script>

<template>
  <div class="min-h-screen p-6">
    <div class="mx-auto grid min-h-[calc(100vh-48px)] max-w-[1680px] grid-cols-[260px_minmax(0,1fr)] gap-5">
      <aside class="studio-panel flex shrink-0 flex-col items-stretch rounded-lg p-5">
        <div class="flex items-start justify-between gap-2">
          <div>
            <p class="text-xs font-semibold uppercase tracking-[0.16em] text-[var(--studio-coral)]">Local Studio</p>
            <h1 class="mt-1 text-xl font-black leading-tight text-[var(--studio-ink)] lg:text-2xl">GPT Img2 Creater</h1>
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
        </div>

        <nav class="mt-8 flex flex-col gap-2">
          <button
            v-for="item in navItems"
            :key="item.key"
            type="button"
            class="flex h-11 items-center gap-2 rounded-md border px-3 text-sm font-semibold transition"
            :class="
              activeTab === item.key
                ? 'border-[var(--studio-solid)] bg-[var(--studio-solid)] text-[var(--studio-on-solid)]'
                : 'border-transparent text-[var(--studio-muted)] hover:border-[var(--studio-line)] hover:bg-[var(--studio-surface)]'
            "
            @click="activeTab = item.key"
          >
            <el-icon><component :is="item.icon" /></el-icon>
            <span>{{ item.label }}</span>
          </button>
        </nav>

        <div class="mt-auto rounded-md border border-[var(--studio-line)] bg-[var(--studio-surface-soft)] p-3 text-xs leading-5 text-[var(--studio-muted)]">
          当前后端默认代理 <span class="font-semibold text-[var(--studio-ink)]">/api</span>，Vite 开发环境会转发到 Flask。
        </div>
      </aside>

      <main class="min-w-0 flex-1">
        <!-- v-show (not v-if) so an in-flight generation keeps polling when the
             user visits 设置 and comes back; both stay mounted. -->
        <Playground v-show="activeTab === 'playground'" @go-settings="activeTab = 'settings'" />
        <APIConfig v-show="activeTab === 'settings'" />
      </main>
    </div>
  </div>
</template>
