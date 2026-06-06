<script setup>
import { ref } from 'vue'
import { Picture, Setting } from '@element-plus/icons-vue'
import APIConfig from './components/APIConfig/index.vue'
import Playground from './components/Playground/index.vue'

const activeTab = ref('playground')

const navItems = [
  { key: 'playground', label: '工作区', icon: Picture },
  { key: 'settings', label: '设置', icon: Setting },
]
</script>

<template>
  <div class="min-h-screen p-6">
    <div class="mx-auto grid min-h-[calc(100vh-48px)] max-w-[1680px] grid-cols-[260px_minmax(0,1fr)] gap-5">
      <aside class="studio-panel flex shrink-0 flex-col items-stretch rounded-lg p-5">
        <div>
          <p class="text-xs font-semibold uppercase tracking-[0.16em] text-[var(--studio-coral)]">Local Studio</p>
          <h1 class="mt-1 text-xl font-black leading-tight text-[var(--studio-ink)] lg:text-2xl">GPT Img2 Creater</h1>
        </div>

        <nav class="mt-8 flex flex-col gap-2">
          <button
            v-for="item in navItems"
            :key="item.key"
            type="button"
            class="flex h-11 items-center gap-2 rounded-md border px-3 text-sm font-semibold transition"
            :class="
              activeTab === item.key
                ? 'border-[var(--studio-ink)] bg-[var(--studio-ink)] text-white'
                : 'border-transparent text-[var(--studio-muted)] hover:border-[var(--studio-line)] hover:bg-white'
            "
            @click="activeTab = item.key"
          >
            <el-icon><component :is="item.icon" /></el-icon>
            <span>{{ item.label }}</span>
          </button>
        </nav>

        <div class="mt-auto rounded-md border border-[var(--studio-line)] bg-white/70 p-3 text-xs leading-5 text-[var(--studio-muted)]">
          当前后端默认代理 <span class="font-semibold text-[var(--studio-ink)]">/api</span>，Vite 开发环境会转发到 Flask。
        </div>
      </aside>

      <main class="min-w-0 flex-1">
        <Playground v-if="activeTab === 'playground'" />
        <APIConfig v-else />
      </main>
    </div>
  </div>
</template>
