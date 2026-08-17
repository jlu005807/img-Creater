import test from 'node:test'
import assert from 'node:assert/strict'

import * as downloads from '../src/utils/download.js'

const {
  BatchDownloadError,
  MAX_BATCH_IMAGE_BYTES,
  MAX_BATCH_IMAGE_COUNT,
  MAX_BATCH_TOTAL_BYTES,
  downloadImagesAsZip,
  fetchImageBlob,
  triggerBlobDownload,
} = downloads

function imageResponse(bytes, type = 'image/png', init = {}) {
  const body = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes)
  return new Response(body, {
    status: init.status ?? 200,
    headers: {
      'Content-Type': type,
      ...(init.headers || {}),
    },
  })
}

function item(key, overrides = {}) {
  const [sessionId = 'session', rawIndex = '0'] = key.split(':')
  return {
    key,
    sessionId,
    imageIndex: Number(rawIndex),
    url: `https://example.test/${key}.png`,
    ...overrides,
  }
}

test('exports explicit small-batch safety limits', () => {
  assert.equal(MAX_BATCH_IMAGE_COUNT, 50)
  assert.equal(MAX_BATCH_IMAGE_BYTES, 50 * 1024 * 1024)
  assert.equal(MAX_BATCH_TOTAL_BYTES, 200 * 1024 * 1024)
})

test('fetchImageBlob accepts data image URLs without a network fallback', async () => {
  const blob = await fetchImageBlob('data:image/png;base64,AQID')

  assert.equal(blob.type, 'image/png')
  assert.deepEqual(new Uint8Array(await blob.arrayBuffer()), new Uint8Array([1, 2, 3]))
})

test('triggerBlobDownload is injectable and still releases its object URL when clicking fails', () => {
  const appended = []
  const removed = []
  const revoked = []
  const timers = []
  const anchor = {
    remove() { removed.push(this) },
    click() { throw new Error('download blocked') },
  }
  const documentImpl = {
    createElement(name) {
      assert.equal(name, 'a')
      return anchor
    },
    body: { appendChild(node) { appended.push(node) } },
  }
  const urlImpl = {
    createObjectURL() { return 'blob:test' },
    revokeObjectURL(url) { revoked.push(url) },
  }

  assert.throws(
    () => triggerBlobDownload(new Blob(['zip']), 'images.zip', {
      documentImpl,
      urlImpl,
      setTimeoutImpl(callback, delay) { timers.push({ callback, delay }) },
    }),
    /download blocked/,
  )
  assert.equal(anchor.href, 'blob:test')
  assert.equal(anchor.download, 'images.zip')
  assert.equal(appended.length, 1)
  assert.equal(removed.length, 1)
  assert.equal(timers[0].delay, 1000)
  timers[0].callback()
  assert.deepEqual(revoked, ['blob:test'])
})

test('fetchImageBlob rejects HTTP, MIME, and single-file size violations', async (t) => {
  await t.test('HTTP status', async () => {
    await assert.rejects(
      fetchImageBlob('/missing.png', { fetchImpl: async () => imageResponse([], 'image/png', { status: 404 }) }),
      (error) => error instanceof BatchDownloadError && error.code === 'HTTP_ERROR',
    )
  })

  await t.test('non-image content type', async () => {
    await assert.rejects(
      fetchImageBlob('/not-image', { fetchImpl: async () => imageResponse([1], 'text/html') }),
      (error) => error instanceof BatchDownloadError && error.code === 'INVALID_IMAGE_TYPE',
    )
    await assert.rejects(
      fetchImageBlob('/invalid-image-type', { fetchImpl: async () => imageResponse([1], 'image/') }),
      (error) => error instanceof BatchDownloadError && error.code === 'INVALID_IMAGE_TYPE',
    )
  })

  await t.test('response MIME is preserved when blob() returns a generic type', async () => {
    const response = {
      ok: true,
      status: 200,
      headers: new Headers({ 'Content-Type': 'image/jpeg' }),
      async blob() {
        return new Blob([new Uint8Array([1])], { type: 'application/octet-stream' })
      },
    }
    const blob = await fetchImageBlob('/generic-blob', { fetchImpl: async () => response })
    assert.equal(blob.type, 'image/jpeg')
  })

  await t.test('declared content length', async () => {
    let blobRead = false
    const response = {
      ok: true,
      status: 200,
      headers: new Headers({ 'Content-Type': 'image/png', 'Content-Length': '11' }),
      async blob() {
        blobRead = true
        return new Blob([new Uint8Array(11)], { type: 'image/png' })
      },
    }
    await assert.rejects(
      fetchImageBlob('/large.png', { fetchImpl: async () => response, maxBytes: 10 }),
      (error) => error.code === 'IMAGE_SIZE_LIMIT',
    )
    assert.equal(blobRead, false)
  })

  await t.test('actual blob size', async () => {
    await assert.rejects(
      fetchImageBlob('/large.png', { fetchImpl: async () => imageResponse(new Uint8Array(11)), maxBytes: 10 }),
      (error) => error.code === 'IMAGE_SIZE_LIMIT',
    )
  })
})

test('downloads several images through one ZIP blob download and reports progress', async () => {
  const triggers = []
  const zipInputs = []
  const progress = []
  const selected = [item('one:0'), item('two:1')]

  const result = await downloadImagesAsZip(selected, {
    fetchImpl: async () => imageResponse([1, 2, 3]),
    createZip(files) {
      zipInputs.push(files)
      return new Uint8Array([80, 75, 3, 4])
    },
    triggerDownload(blob, filename) {
      triggers.push({ blob, filename })
    },
    now: new Date(2026, 7, 17, 12, 34, 56),
    onProgress(value) {
      progress.push(value)
    },
  })

  assert.equal(triggers.length, 1)
  assert.equal(triggers[0].blob.type, 'application/zip')
  assert.equal(triggers[0].filename, 'img-Creater-20260817-123456.zip')
  assert.equal(zipInputs.length, 1)
  assert.deepEqual(Object.keys(zipInputs[0]), ['one-image-1.png', 'two-image-2.png'])
  assert.equal(result.downloaded, 2)
  assert.equal(result.failed, 0)
  assert.equal(progress.at(-1).completed, 2)
  assert.equal(progress.at(-1).succeeded, 2)
})

test('creates safe unique archive names using MIME before URL extension', async () => {
  let archiveNames = []
  const selected = [
    item('first:0', { sessionId: '../same:*?', imageIndex: 0, url: '/misleading.png' }),
    item('second:0', { sessionId: '../same:*?', imageIndex: 0, url: '/other.webp' }),
  ]

  await downloadImagesAsZip(selected, {
    fetchImpl: async () => imageResponse([1], 'image/jpeg'),
    createZip(files) {
      archiveNames = Object.keys(files)
      return new Uint8Array([1])
    },
    triggerDownload() {},
  })

  assert.deepEqual(archiveNames, ['same-image-1.jpg', 'same-image-1-2.jpg'])
  assert.equal(archiveNames.every((name) => !name.includes('..') && !name.includes('/') && !name.includes('\\')), true)
})

test('uses bounded concurrency while fetching images', async () => {
  let active = 0
  let peak = 0
  let release
  const gate = new Promise((resolve) => { release = resolve })
  const selected = Array.from({ length: 8 }, (_, index) => item(`session:${index}`))

  const operation = downloadImagesAsZip(selected, {
    concurrency: 3,
    fetchImpl: async () => {
      active += 1
      peak = Math.max(peak, active)
      await gate
      active -= 1
      return imageResponse([1])
    },
    createZip: () => new Uint8Array([1]),
    triggerDownload() {},
  })

  await new Promise((resolve) => setTimeout(resolve, 0))
  assert.equal(peak, 3)
  release()
  await operation
  assert.equal(peak, 3)
})

test('rejects count and total-size limit violations without triggering a download', async (t) => {
  await t.test('count limit is checked before fetching', async () => {
    let fetches = 0
    await assert.rejects(
      downloadImagesAsZip(
        Array.from({ length: MAX_BATCH_IMAGE_COUNT + 1 }, (_, index) => item(`many:${index}`)),
        {
          fetchImpl: async () => { fetches += 1; return imageResponse([1]) },
          triggerDownload() { assert.fail('must not download') },
        },
      ),
      (error) => error.code === 'IMAGE_COUNT_LIMIT',
    )
    assert.equal(fetches, 0)
  })

  await t.test('total size cancels the archive', async () => {
    let triggers = 0
    await assert.rejects(
      downloadImagesAsZip([item('one:0'), item('two:0')], {
        concurrency: 1,
        maxTotalBytes: 10,
        fetchImpl: async () => imageResponse(new Uint8Array(6)),
        triggerDownload() { triggers += 1 },
      }),
      (error) => error.code === 'TOTAL_SIZE_LIMIT',
    )
    assert.equal(triggers, 0)
  })

  await t.test('streaming responses stop reading as soon as the aggregate budget is exceeded', async () => {
    let readCount = 0
    let canceled = false
    let blobRead = false
    const chunks = [new Uint8Array(6), new Uint8Array(6)]
    const response = {
      ok: true,
      status: 200,
      headers: new Headers({ 'Content-Type': 'image/png' }),
      body: {
        getReader() {
          return {
            async read() {
              const value = chunks[readCount]
              readCount += 1
              return value ? { done: false, value } : { done: true, value: undefined }
            },
            async cancel() { canceled = true },
            releaseLock() {},
          }
        },
      },
      async blob() {
        blobRead = true
        return new Blob([new Uint8Array(12)], { type: 'image/png' })
      },
    }

    await assert.rejects(
      downloadImagesAsZip([item('stream:0')], {
        maxTotalBytes: 10,
        fetchImpl: async () => response,
        triggerDownload() { assert.fail('must not download') },
      }),
      (error) => error.code === 'TOTAL_SIZE_LIMIT',
    )
    assert.equal(readCount, 2)
    assert.equal(canceled, true)
    assert.equal(blobRead, false)
  })

  await t.test('releases bytes from a streamed item that fails before the next item', async () => {
    let calls = 0
    let canceled = false
    const failedResponse = {
      ok: true,
      status: 200,
      headers: new Headers({ 'Content-Type': 'image/png' }),
      body: {
        getReader() {
          let readOnce = false
          return {
            async read() {
              if (readOnce) throw new Error('stream disconnected')
              readOnce = true
              return { done: false, value: new Uint8Array([1, 2, 3, 4]) }
            },
            async cancel() { canceled = true },
            releaseLock() {},
          }
        },
      },
    }

    const result = await downloadImagesAsZip([item('broken:0'), item('good:0')], {
      concurrency: 1,
      maxTotalBytes: 4,
      fetchImpl: async () => calls++ === 0 ? failedResponse : imageResponse([5, 6, 7, 8]),
      createZip: () => new Uint8Array([1]),
      triggerDownload() {},
    })

    assert.equal(calls, 2)
    assert.equal(canceled, true)
    assert.equal(result.downloaded, 1)
    assert.equal(result.failed, 1)
  })
})

test('partial failures still create a ZIP containing only successful blobs', async () => {
  let archiveNames = []
  let triggers = 0
  const selected = [item('good:0'), item('bad:0')]

  const result = await downloadImagesAsZip(selected, {
    fetchImpl: async (url) => url.includes('bad')
      ? imageResponse([1], 'text/plain')
      : imageResponse([1], 'image/png'),
    createZip(files) {
      archiveNames = Object.keys(files)
      return new Uint8Array([1])
    },
    triggerDownload() { triggers += 1 },
  })

  assert.equal(triggers, 1)
  assert.deepEqual(archiveNames, ['good-image-1.png'])
  assert.equal(result.downloaded, 1)
  assert.equal(result.failed, 1)
  assert.equal(result.errors[0].key, 'bad:0')
})

test('all failures return retryable errors without creating an empty ZIP', async () => {
  let zipped = false
  let triggers = 0
  const result = await downloadImagesAsZip([item('bad:0'), item('worse:0')], {
    fetchImpl: async () => imageResponse([], 'text/plain'),
    createZip() { zipped = true; return new Uint8Array() },
    triggerDownload() { triggers += 1 },
  })

  assert.equal(result.downloaded, 0)
  assert.equal(result.failed, 2)
  assert.equal(result.errors.length, 2)
  assert.equal(zipped, false)
  assert.equal(triggers, 0)
})

test('an aborted batch never creates or downloads a ZIP', async () => {
  const controller = new AbortController()
  let zipped = false
  let triggers = 0
  const operation = downloadImagesAsZip([item('slow:0'), item('slow:1')], {
    signal: controller.signal,
    fetchImpl: async (_url, { signal }) => new Promise((_resolve, reject) => {
      signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true })
    }),
    createZip() { zipped = true; return new Uint8Array() },
    triggerDownload() { triggers += 1 },
  })

  controller.abort()
  const result = await operation

  assert.equal(result.aborted, true)
  assert.equal(zipped, false)
  assert.equal(triggers, 0)
})

test('cancelling during asynchronous compression terminates before saving the ZIP', async () => {
  const controller = new AbortController()
  let compressionStarted
  const started = new Promise((resolve) => { compressionStarted = resolve })
  let triggers = 0
  const operation = downloadImagesAsZip([item('ready:0')], {
    signal: controller.signal,
    fetchImpl: async () => imageResponse([1, 2, 3]),
    createZip(_files, { signal }) {
      compressionStarted()
      return new Promise((_resolve, reject) => {
        signal.addEventListener(
          'abort',
          () => reject(new DOMException('Aborted', 'AbortError')),
          { once: true },
        )
      })
    },
    triggerDownload() { triggers += 1 },
  })

  await started
  controller.abort()
  const result = await operation

  assert.equal(result.aborted, true)
  assert.equal(triggers, 0)
})
