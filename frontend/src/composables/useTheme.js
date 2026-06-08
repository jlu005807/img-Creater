import { onMounted, ref } from 'vue'

const STORAGE_KEY = 'studio-theme'
const VALID = new Set(['system', 'light', 'dark'])

// Shared singletons so every component reflects the same preference/resolved theme.
const themeMode = ref('system')
const theme = ref('light')

function systemTheme() {
  const prefersDark = window.matchMedia?.('(prefers-color-scheme: dark)')?.matches
  return prefersDark ? 'dark' : 'light'
}

function resolveInitialMode() {
  try {
    const stored = window.localStorage.getItem(STORAGE_KEY)
    if (VALID.has(stored)) return stored
  } catch {
    /* localStorage may be unavailable (private mode); fall through. */
  }
  return 'system'
}

function applyThemeMode(next) {
  const mode = VALID.has(next) ? next : 'system'
  themeMode.value = mode
  theme.value = mode === 'system' ? systemTheme() : mode
  document.documentElement.classList.toggle('dark', theme.value === 'dark')
  try {
    window.localStorage.setItem(STORAGE_KEY, mode)
  } catch {
    /* ignore persistence failures */
  }
}

let initialized = false
let listening = false

/**
 * Theme controller. Honors a saved preference, supports system mode,
 * and toggles the `dark` class on <html> (the Element Plus convention).
 */
export function useTheme() {
  if (!initialized) {
    initialized = true
    applyThemeMode(resolveInitialMode())
  }

  onMounted(() => {
    // Re-assert once mounted in case the class was stripped during HMR.
    document.documentElement.classList.toggle('dark', theme.value === 'dark')
    if (!listening) {
      listening = true
      const media = window.matchMedia?.('(prefers-color-scheme: dark)')
      media?.addEventListener?.('change', () => {
        if (themeMode.value === 'system') applyThemeMode('system')
      })
    }
  })

  function toggleTheme() {
    applyThemeMode(theme.value === 'dark' ? 'light' : 'dark')
  }

  function setThemeMode(next) {
    if (VALID.has(next)) applyThemeMode(next)
  }

  return { theme, themeMode, toggleTheme, setThemeMode }
}
