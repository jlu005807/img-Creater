import { reactive, watch } from 'vue'

const STORAGE_KEY = 'studio-settings'

const DEFAULTS = {
  maxPromptChars: 3000, // configurable max prompt length
  maxReferenceImages: 3, // configurable reference-image upload limit
}

const MINIMUMS = {
  maxPromptChars: 1,
  maxReferenceImages: 1,
}

// el-input-number emits null on clear; never let null/invalid numbers stick.
function sanitize(values) {
  const next = { ...DEFAULTS, ...values }
  for (const key of Object.keys(MINIMUMS)) {
    const value = Number(next[key])
    next[key] = Number.isFinite(value) && value >= MINIMUMS[key] ? value : DEFAULTS[key]
  }
  return next
}

function load() {
  try {
    const parsed = JSON.parse(window.localStorage.getItem(STORAGE_KEY))
    if (parsed && typeof parsed === 'object') {
      return sanitize(parsed)
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
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(sanitize({ ...settings })))
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
