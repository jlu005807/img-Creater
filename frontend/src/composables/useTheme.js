import { onMounted, ref } from 'vue'

const STORAGE_KEY = 'studio-theme'
const VALID = new Set(['light', 'dark'])

// Shared singleton so every component reflects the same theme.
const theme = ref('light')

function resolveInitialTheme() {
  try {
    const stored = window.localStorage.getItem(STORAGE_KEY)
    if (VALID.has(stored)) return stored
  } catch {
    /* localStorage may be unavailable (private mode); fall through. */
  }
  const prefersDark = window.matchMedia?.('(prefers-color-scheme: dark)')?.matches
  return prefersDark ? 'dark' : 'light'
}

function applyTheme(next) {
  theme.value = next
  document.documentElement.classList.toggle('dark', next === 'dark')
  try {
    window.localStorage.setItem(STORAGE_KEY, next)
  } catch {
    /* ignore persistence failures */
  }
}

let initialized = false

/**
 * Theme controller. Honors a saved preference, then the OS setting,
 * and toggles the `dark` class on <html> (the Element Plus convention).
 */
export function useTheme() {
  if (!initialized) {
    initialized = true
    applyTheme(resolveInitialTheme())
  }

  onMounted(() => {
    // Re-assert once mounted in case the class was stripped during HMR.
    document.documentElement.classList.toggle('dark', theme.value === 'dark')
  })

  function toggleTheme() {
    applyTheme(theme.value === 'dark' ? 'light' : 'dark')
  }

  function setTheme(next) {
    if (VALID.has(next)) applyTheme(next)
  }

  return { theme, toggleTheme, setTheme }
}
