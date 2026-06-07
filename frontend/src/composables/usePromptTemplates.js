import { ref } from 'vue'

const STORAGE_KEY = 'studio-prompt-templates'

function load() {
  try {
    const parsed = JSON.parse(window.localStorage.getItem(STORAGE_KEY))
    if (Array.isArray(parsed)) return parsed
  } catch {
    /* ignore */
  }
  // Seed with a couple of defaults on first run.
  return [
    { id: 'seed-1', title: '电影级产品摄影', text: '电影级产品摄影，柔和工作室打光，干净背景，真实材质，高细节' },
    { id: 'seed-2', title: '局部替换背景', text: '只修改蒙版区域：将背景替换为星空夜景，保持主体光照和边缘自然融合' },
  ]
}

// Shared singleton.
const templates = ref(load())

// Bridge: Settings (modal) writes here; Playground watches and fills the prompt.
const pendingFill = ref(null)

function persist() {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(templates.value))
  } catch {
    /* ignore quota / availability errors */
  }
}

function newId() {
  return `tpl-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`
}

export function usePromptTemplates() {
  function addTemplate({ title, text }) {
    const item = { id: newId(), title: (title || '').trim() || '未命名', text: (text || '').trim() }
    templates.value = [item, ...templates.value]
    persist()
    return item
  }

  function updateTemplate(id, fields) {
    const idx = templates.value.findIndex((t) => t.id === id)
    if (idx >= 0) {
      templates.value[idx] = { ...templates.value[idx], ...fields }
      persist()
    }
  }

  function removeTemplate(id) {
    templates.value = templates.value.filter((t) => t.id !== id)
    persist()
  }

  // Request the prompt input be filled with this text (consumed by Playground).
  function requestFill(text) {
    pendingFill.value = { text, ts: Date.now() }
  }

  return { templates, addTemplate, updateTemplate, removeTemplate, pendingFill, requestFill }
}
