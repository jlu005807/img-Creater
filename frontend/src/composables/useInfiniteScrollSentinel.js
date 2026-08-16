import { onBeforeUnmount, watch } from 'vue'

const DEFAULT_FALLBACK_THRESHOLD = 160

function fallbackThreshold(rootMargin) {
  const parts = String(rootMargin || '').trim().split(/\s+/).filter(Boolean)
  const rawBottom = parts.length >= 3 ? parts[2] : parts[0]
  const match = /^(-?\d+(?:\.\d+)?)px$/i.exec(rawBottom || '')
  if (!match) return DEFAULT_FALLBACK_THRESHOLD
  return Math.max(0, Number(match[1]))
}

export function useInfiniteScrollSentinel({
  rootRef,
  sentinelRef,
  enabled,
  onIntersect,
  rootMargin = '0px 0px 160px 0px',
}) {
  let observer = null
  let scrollRoot = null
  let loadPending = false
  let loadDisabledDuringRequest = false
  let fallbackEnabled = false
  let observerIntersecting = false
  let disposed = false
  const threshold = fallbackThreshold(rootMargin)

  function nearBottom(root) {
    const scrollTop = Number(root?.scrollTop) || 0
    const clientHeight = Number(root?.clientHeight) || 0
    const scrollHeight = Number(root?.scrollHeight) || 0
    if (clientHeight <= 0 || scrollHeight <= 0) return false
    return scrollTop + clientHeight >= scrollHeight - threshold
  }

  function triggerLoad() {
    if (disposed || loadPending) return
    loadPending = true
    loadDisabledDuringRequest = false
    Promise.resolve()
      .then(onIntersect)
      .catch(() => {
        // The shared store exposes the error for the inline retry state.
      })
      .finally(() => {
        const shouldRecheck = loadDisabledDuringRequest
        loadPending = false
        loadDisabledDuringRequest = false
        if (!shouldRecheck) return
        if (fallbackEnabled && scrollRoot && nearBottom(scrollRoot)) {
          Promise.resolve().then(onRootScroll)
        } else if (observer && observerIntersecting) {
          Promise.resolve().then(triggerLoad)
        }
      })
  }

  function onRootScroll() {
    if (!fallbackEnabled || !scrollRoot || !nearBottom(scrollRoot)) return
    triggerLoad()
  }

  function removeScrollListener() {
    if (!scrollRoot) return
    if (typeof scrollRoot.removeEventListener === 'function') {
      scrollRoot.removeEventListener('scroll', onRootScroll)
    }
    scrollRoot = null
  }

  function disconnect() {
    if (observer) {
      observer.disconnect()
      observer = null
    }
    observerIntersecting = false
    fallbackEnabled = false
    removeScrollListener()
  }

  const stopWatching = watch(
    [rootRef, sentinelRef, enabled],
    ([root, sentinel, canLoad]) => {
      if (!canLoad && loadPending) loadDisabledDuringRequest = true
      disconnect()
      if (
        !root ||
        !sentinel ||
        !canLoad
      ) {
        return
      }

      if (typeof IntersectionObserver === 'undefined') {
        if (typeof root.addEventListener !== 'function') return
        fallbackEnabled = true
        scrollRoot = root
        root.addEventListener('scroll', onRootScroll, { passive: true })
        // Also cover short containers that have no scrollbar yet.
        onRootScroll()
        return
      }

      observer = new IntersectionObserver(
        (entries) => {
          observerIntersecting = entries.some((entry) => entry.isIntersecting)
          if (observerIntersecting) triggerLoad()
        },
        { root, rootMargin },
      )
      observer.observe(sentinel)
    },
    { immediate: true, flush: 'post' },
  )

  onBeforeUnmount(() => {
    disposed = true
    stopWatching()
    disconnect()
  })

  return { disconnect }
}
