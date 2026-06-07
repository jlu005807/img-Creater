import { reactive, watch } from 'vue'

const STORAGE_KEY = 'studio-settings'

const DEFAULTS = {
  maxPromptChars: 3000, // configurable max prompt length
  maxReferenceImages: 3, // configurable reference-image upload limit
}

function load() {
  try {
    const parsed = JSON.parse(window.localStorage.getItem(STORAGE_KEY))
    if (parsed && typeof parsed === 'object') {
      return { ...DEFAULTS, ...parsed }
    }
  } catch {
    /* ignore */
  }
  return { ...DEFAULTS }
}

// Shared singleton across all components.
const settings = reactive(load())

watch(
  settings,
  () => {
    try {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify({ ...settings }))
    } catch {
      /* ignore quota / availability errors */
    }
  },
  { deep: true },
)

export function useSettings() {
  function resetSettings() {
    Object.assign(settings, DEFAULTS)
  }
  return { settings, resetSettings, DEFAULTS }
}
