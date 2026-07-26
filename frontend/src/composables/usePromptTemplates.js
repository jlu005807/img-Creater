import { ref } from 'vue'
import {
  createPromptTemplate,
  deletePromptTemplate,
  listPromptTemplates,
  updatePromptTemplate,
} from '../api/promptTemplates'

const LEGACY_STORAGE_KEY = 'studio-prompt-templates'
const LEGACY_MIGRATED_KEY = 'studio-prompt-templates-migrated'

// Shared singleton.
const templates = ref([])
const templatesLoading = ref(false)
const pendingFill = ref(null)
let loadPromise = null

function readLegacyTemplates() {
  try {
    const parsed = JSON.parse(window.localStorage.getItem(LEGACY_STORAGE_KEY))
    if (Array.isArray(parsed)) {
      return parsed
        .filter((item) => item && typeof item === 'object' && String(item.text || '').trim())
        .map((item) => ({
          id: item.id,
          title: String(item.title || '').trim() || '未命名',
          text: String(item.text || '').trim(),
        }))
    }
  } catch {
    /* ignore malformed legacy data */
  }
  return []
}

function legacyMigrationDone() {
  try {
    return window.localStorage.getItem(LEGACY_MIGRATED_KEY) === '1'
  } catch {
    return true
  }
}

function markLegacyMigrated() {
  try {
    window.localStorage.setItem(LEGACY_MIGRATED_KEY, '1')
  } catch {
    /* ignore quota / availability errors */
  }
}

// Returns how many legacy items were migrated; failures are skipped so one bad
// item can never block template loading forever.
async function migrateLegacyTemplatesIfNeeded(currentTemplates) {
  if (legacyMigrationDone()) return 0
  const legacyTemplates = readLegacyTemplates()
  if (!legacyTemplates.length) {
    markLegacyMigrated()
    return 0
  }

  const existingTexts = new Set(currentTemplates.map((item) => item.text))
  let migratedCount = 0
  for (const item of legacyTemplates) {
    if (existingTexts.has(item.text)) continue
    try {
      const created = await createPromptTemplate({ title: item.title, text: item.text })
      migratedCount += 1
      existingTexts.add(created.text)
    } catch {
      /* skip legacy items that fail to migrate */
    }
  }
  markLegacyMigrated()
  return migratedCount
}

async function loadTemplates({ force = false } = {}) {
  if (loadPromise && !force) return loadPromise
  templatesLoading.value = true
  loadPromise = (async () => {
    const loaded = await listPromptTemplates()
    let next = Array.isArray(loaded) ? loaded : []
    templates.value = next
    const migratedCount = await migrateLegacyTemplatesIfNeeded(next)
    if (migratedCount > 0) {
      const refreshed = await listPromptTemplates()
      next = Array.isArray(refreshed) ? refreshed : next
      templates.value = next
    }
    return next
  })()
  try {
    return await loadPromise
  } finally {
    templatesLoading.value = false
    loadPromise = null
  }
}

export function usePromptTemplates() {
  async function addTemplate({ title, text }) {
    const item = await createPromptTemplate({ title, text })
    templates.value = [item, ...templates.value.filter((template) => template.id !== item.id)]
    return item
  }

  async function updateTemplate(id, fields) {
    const updated = await updatePromptTemplate(id, fields)
    templates.value = templates.value.map((template) => (template.id === id ? updated : template))
    return updated
  }

  async function removeTemplate(id) {
    await deletePromptTemplate(id)
    templates.value = templates.value.filter((template) => template.id !== id)
  }

  // Request the prompt input be filled with this text (consumed by Playground).
  function requestFill(text) {
    pendingFill.value = { text, ts: Date.now() }
  }

  function requestRandomFill() {
    if (!templates.value.length) return null
    const item = templates.value[Math.floor(Math.random() * templates.value.length)]
    requestFill(item.text)
    return item
  }

  return {
    templates,
    templatesLoading,
    loadTemplates,
    addTemplate,
    updateTemplate,
    removeTemplate,
    pendingFill,
    requestFill,
    requestRandomFill,
  }
}
