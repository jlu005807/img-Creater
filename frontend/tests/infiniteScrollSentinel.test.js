import test from 'node:test'
import assert from 'node:assert/strict'

import { createRenderer, nextTick, ref } from 'vue'

import { useInfiniteScrollSentinel } from '../src/composables/useInfiniteScrollSentinel.js'

function testRenderer() {
  return createRenderer({
    patchProp() {},
    insert() {},
    remove() {},
    createElement: () => ({}),
    createText: (text) => ({ text }),
    createComment: (text) => ({ text }),
    setText(node, text) { node.text = text },
    setElementText(node, text) { node.text = text },
    parentNode: () => null,
    nextSibling: () => null,
  })
}

async function flushScheduling() {
  for (let index = 0; index < 8; index += 1) {
    await Promise.resolve()
    await nextTick()
  }
}

test('fallback keeps filling a short container until loading is disabled', async () => {
  let loadCalls = 0
  let scrollListener = null
  const scrollRoot = {
    scrollTop: 0,
    clientHeight: 200,
    scrollHeight: 200,
    addEventListener(type, listener) {
      if (type === 'scroll') scrollListener = listener
    },
    removeEventListener(type, listener) {
      if (type === 'scroll' && scrollListener === listener) scrollListener = null
    },
  }
  const canLoad = ref(true)
  const renderer = testRenderer()
  const app = renderer.createApp({
    setup() {
      useInfiniteScrollSentinel({
        rootRef: ref(scrollRoot),
        sentinelRef: ref({}),
        enabled: canLoad,
        onIntersect: async () => {
          loadCalls += 1
          canLoad.value = false
          await nextTick()
          canLoad.value = loadCalls < 2
          await nextTick()
        },
      })
      return () => null
    },
  })

  app.mount({})
  await flushScheduling()

  assert.equal(loadCalls, 2)
  assert.equal(canLoad.value, false)
  app.unmount()
  assert.equal(scrollListener, null)
})

test('fallback does not preload while the scroll root has no layout size', async () => {
  let loadCalls = 0
  let scrollListener = null
  const scrollRoot = {
    scrollTop: 0,
    clientHeight: 0,
    scrollHeight: 0,
    addEventListener(type, listener) {
      if (type === 'scroll') scrollListener = listener
    },
    removeEventListener(type, listener) {
      if (type === 'scroll' && scrollListener === listener) scrollListener = null
    },
  }
  const renderer = testRenderer()
  const app = renderer.createApp({
    setup() {
      useInfiniteScrollSentinel({
        rootRef: ref(scrollRoot),
        sentinelRef: ref({}),
        enabled: ref(true),
        onIntersect: () => { loadCalls += 1 },
      })
      return () => null
    },
  })

  app.mount({})
  await flushScheduling()

  assert.equal(loadCalls, 0)
  assert.equal(typeof scrollListener, 'function')
  app.unmount()
  assert.equal(scrollListener, null)
})

test('intersection observer keeps filling while the sentinel remains visible', async () => {
  const previousObserver = globalThis.IntersectionObserver
  let observerInstance = null
  let loadCalls = 0
  const canLoad = ref(true)

  globalThis.IntersectionObserver = class FakeIntersectionObserver {
    constructor(callback) {
      this.callback = callback
      observerInstance = this
    }

    observe() {
      this.callback([{ isIntersecting: true }])
    }

    disconnect() {
      observerInstance = null
    }
  }

  try {
    const renderer = testRenderer()
    const app = renderer.createApp({
      setup() {
        useInfiniteScrollSentinel({
          rootRef: ref({}),
          sentinelRef: ref({}),
          enabled: canLoad,
          onIntersect: async () => {
            loadCalls += 1
            canLoad.value = false
            await nextTick()
            canLoad.value = loadCalls < 2
            await nextTick()
          },
        })
        return () => null
      },
    })

    app.mount({})
    await flushScheduling()

    assert.equal(loadCalls, 2)
    app.unmount()
  } finally {
    globalThis.IntersectionObserver = previousObserver
  }
})

test('intersection observer does not spin when the load callback starts no request', async () => {
  const previousObserver = globalThis.IntersectionObserver
  let loadCalls = 0
  const canLoad = ref(true)

  globalThis.IntersectionObserver = class FakeIntersectionObserver {
    constructor(callback) {
      this.callback = callback
    }

    observe() {
      this.callback([{ isIntersecting: true }])
    }

    disconnect() {}
  }

  try {
    const renderer = testRenderer()
    const app = renderer.createApp({
      setup() {
        useInfiniteScrollSentinel({
          rootRef: ref({}),
          sentinelRef: ref({}),
          enabled: canLoad,
          onIntersect: () => {
            loadCalls += 1
            // Stop a broken retry loop so this regression test terminates.
            if (loadCalls >= 3) canLoad.value = false
          },
        })
        return () => null
      },
    })

    app.mount({})
    await flushScheduling()

    assert.equal(loadCalls, 1)
    app.unmount()
  } finally {
    globalThis.IntersectionObserver = previousObserver
  }
})
