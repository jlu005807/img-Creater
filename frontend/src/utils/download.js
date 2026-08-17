import { zip } from 'fflate'

export const MAX_BATCH_IMAGE_COUNT = 50
export const MAX_BATCH_IMAGE_BYTES = 50 * 1024 * 1024
export const MAX_BATCH_TOTAL_BYTES = 200 * 1024 * 1024
export const DEFAULT_BATCH_CONCURRENCY = 4

export class BatchDownloadError extends Error {
  constructor(code, message, details = {}) {
    super(message)
    this.name = 'BatchDownloadError'
    this.code = code
    Object.assign(this, details)
  }
}

function triggerAnchorDownload(href, filename, documentImpl = globalThis.document) {
  const anchor = documentImpl.createElement('a')
  anchor.href = href
  anchor.download = filename
  anchor.rel = 'noreferrer'
  documentImpl.body.appendChild(anchor)
  try {
    anchor.click()
  } finally {
    anchor.remove()
  }
}

export function triggerBlobDownload(
  blob,
  filename,
  {
    documentImpl = globalThis.document,
    urlImpl = globalThis.URL,
    setTimeoutImpl = globalThis.setTimeout,
  } = {},
) {
  const objectUrl = urlImpl.createObjectURL(blob)
  try {
    triggerAnchorDownload(objectUrl, filename, documentImpl)
  } finally {
    // Keep the URL lifetime bounded even when the browser rejects the click.
    setTimeoutImpl(() => urlImpl.revokeObjectURL(objectUrl), 1000)
  }
}

function guessExtension(url) {
  if (url.startsWith('data:image/')) {
    const match = /^data:image\/([a-z0-9.+-]+)/i.exec(url)
    return match ? match[1].toLowerCase().replace('jpeg', 'jpg') : 'png'
  }
  const match = /\.(png|jpe?g|webp|gif)(?:[?#]|$)/i.exec(url)
  return match ? match[1].toLowerCase().replace('jpeg', 'jpg') : 'png'
}

async function downloadViaObjectUrl(url, filename, fetchOptions) {
  const response = await fetch(url, fetchOptions)
  if (!response.ok) throw new Error(`HTTP ${response.status}`)
  const blob = await response.blob()
  triggerBlobDownload(blob, filename)
}

function responseContentType(response) {
  return String(response?.headers?.get?.('content-type') || '')
    .split(';', 1)[0]
    .trim()
    .toLowerCase()
}

function imageSizeLimitError(size, limit) {
  return new BatchDownloadError('IMAGE_SIZE_LIMIT', '单张图片超过 50 MB 上限', {
    size,
    limit,
  })
}

async function readImageResponseBlob(
  response,
  contentType,
  {
    signal,
    maxBytes,
    onChunk,
  },
) {
  const body = response?.body
  if (!body || typeof body.getReader !== 'function') {
    const blob = await response.blob()
    if (blob.size > maxBytes) throw imageSizeLimitError(blob.size, maxBytes)
    onChunk?.(blob.size)
    return blob.slice(0, blob.size, contentType)
  }

  const reader = body.getReader()
  const chunks = []
  let size = 0
  try {
    while (true) {
      if (signal?.aborted) throw new DOMException('Aborted', 'AbortError')
      const part = await reader.read()
      if (part.done) break
      const value = part.value instanceof Uint8Array
        ? part.value
        : new Uint8Array(part.value || 0)
      size += value.byteLength
      if (size > maxBytes) throw imageSizeLimitError(size, maxBytes)
      onChunk?.(value.byteLength)
      chunks.push(value)
    }
    return new Blob(chunks, { type: contentType })
  } catch (error) {
    try {
      await reader.cancel()
    } catch {
      // The stream may already be closed; preserve the original error.
    }
    throw error
  } finally {
    reader.releaseLock?.()
  }
}

export async function fetchImageBlob(
  url,
  {
    signal,
    fetchImpl = globalThis.fetch,
    maxBytes = MAX_BATCH_IMAGE_BYTES,
    onChunk,
  } = {},
) {
  if (typeof url !== 'string' || !url) {
    throw new BatchDownloadError('INVALID_URL', '图片地址无效')
  }
  if (typeof fetchImpl !== 'function') {
    throw new BatchDownloadError('FETCH_UNAVAILABLE', '当前环境不支持获取图片')
  }

  let response
  try {
    response = await fetchImpl(url, {
      signal,
      ...(url.startsWith('data:') ? {} : { mode: 'cors' }),
    })
  } catch (error) {
    if (error?.name === 'AbortError') throw error
    throw new BatchDownloadError('FETCH_FAILED', '图片获取失败，可能不允许跨域访问', { cause: error })
  }

  if (!response?.ok) {
    throw new BatchDownloadError('HTTP_ERROR', `图片请求失败（HTTP ${response?.status ?? 'unknown'}）`, {
      status: response?.status,
    })
  }

  const contentType = responseContentType(response)
  if (!/^image\/[a-z0-9.+-]+$/i.test(contentType)) {
    throw new BatchDownloadError('INVALID_IMAGE_TYPE', '响应内容不是图片')
  }

  const declaredSize = Number(response.headers?.get?.('content-length'))
  if (Number.isFinite(declaredSize) && declaredSize > maxBytes) {
    throw imageSizeLimitError(declaredSize, maxBytes)
  }

  return readImageResponseBlob(response, contentType, { signal, maxBytes, onChunk })
}

function extensionForImage(blob, url) {
  const subtype = String(blob?.type || '')
    .split(';', 1)[0]
    .toLowerCase()
    .replace(/^image\//, '')
  const mimeExtensions = {
    'svg+xml': 'svg',
    jpeg: 'jpg',
    'x-icon': 'ico',
  }
  if (mimeExtensions[subtype]) return mimeExtensions[subtype]
  if (/^[a-z0-9]{2,5}$/.test(subtype)) return subtype
  return guessExtension(url)
}

function safeSessionId(value) {
  const safe = String(value || '')
    .replace(/[^a-z0-9_-]+/gi, '-')
    .replace(/^[-_.]+|[-_.]+$/g, '')
    .slice(0, 80)
  return safe || 'session'
}

function uniqueArchiveName(item, blob, usedNames) {
  const rawIndex = Number(item?.imageIndex)
  const imageNumber = Number.isInteger(rawIndex) && rawIndex >= 0 ? rawIndex + 1 : 1
  const base = `${safeSessionId(item?.sessionId)}-image-${imageNumber}`
  const extension = extensionForImage(blob, item?.url || '')
  const candidate = `${base}.${extension}`
  const duplicateNumber = (usedNames.get(candidate) || 0) + 1
  usedNames.set(candidate, duplicateNumber)
  return duplicateNumber === 1 ? candidate : `${base}-${duplicateNumber}.${extension}`
}

function zipFilename(now) {
  const pad = (value) => String(value).padStart(2, '0')
  return `img-Creater-${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}-${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}.zip`
}

function batchResult({ aborted = false, successes = [], errors = [], totalBytes = 0, filename = null }) {
  return {
    aborted,
    downloaded: successes.length,
    failed: errors.length,
    successes: successes.map(({ item, filename: archivedAs }) => ({ key: item.key, archivedAs })),
    errors,
    totalBytes,
    filename,
  }
}

function createZipArchive(files, { signal } = {}) {
  return new Promise((resolve, reject) => {
    let terminate = null
    let settled = false
    const cleanup = () => signal?.removeEventListener('abort', abortCompression)
    const abortCompression = () => {
      if (settled) return
      settled = true
      terminate?.()
      cleanup()
      reject(new DOMException('Aborted', 'AbortError'))
    }

    if (signal?.aborted) {
      abortCompression()
      return
    }
    signal?.addEventListener('abort', abortCompression, { once: true })
    terminate = zip(files, { level: 6 }, (error, data) => {
      if (settled) return
      settled = true
      cleanup()
      if (error) reject(error)
      else resolve(data)
    })
    if (signal?.aborted) abortCompression()
  })
}

export async function downloadImagesAsZip(items, options = {}) {
  const selected = Array.isArray(items) ? items : []
  const maxCount = options.maxCount ?? MAX_BATCH_IMAGE_COUNT
  const maxFileBytes = options.maxFileBytes ?? MAX_BATCH_IMAGE_BYTES
  const maxTotalBytes = options.maxTotalBytes ?? MAX_BATCH_TOTAL_BYTES

  if (!selected.length) {
    throw new BatchDownloadError('NO_IMAGES', '请先选择要下载的图片')
  }
  if (selected.length > maxCount) {
    throw new BatchDownloadError('IMAGE_COUNT_LIMIT', `单次最多下载 ${maxCount} 张图片`, {
      count: selected.length,
      limit: maxCount,
    })
  }

  const concurrency = Math.max(1, Math.min(
    Number.isInteger(options.concurrency) ? options.concurrency : DEFAULT_BATCH_CONCURRENCY,
    selected.length,
  ))
  const controller = new AbortController()
  const externalSignal = options.signal
  const abortFromExternal = () => controller.abort(externalSignal?.reason)
  if (externalSignal?.aborted) abortFromExternal()
  else externalSignal?.addEventListener('abort', abortFromExternal, { once: true })

  const successes = new Array(selected.length)
  const errors = []
  let nextIndex = 0
  let completed = 0
  let succeeded = 0
  let totalBytes = 0
  let streamedBytes = 0
  let totalLimitError = null

  const observeChunk = (size) => {
    if (streamedBytes + size > maxTotalBytes) {
      totalLimitError = new BatchDownloadError('TOTAL_SIZE_LIMIT', '所选图片总大小超过 200 MB 上限', {
        size: streamedBytes + size,
        limit: maxTotalBytes,
      })
      controller.abort(totalLimitError)
      throw totalLimitError
    }
    streamedBytes += size
  }

  const reportProgress = () => options.onProgress?.({
    completed,
    total: selected.length,
    succeeded,
    failed: errors.length,
    totalBytes,
  })

  async function worker() {
    while (!controller.signal.aborted) {
      const index = nextIndex
      nextIndex += 1
      if (index >= selected.length) return
      const current = selected[index]
      let currentStreamBytes = 0

      try {
        const blob = await fetchImageBlob(current.url, {
          signal: controller.signal,
          fetchImpl: options.fetchImpl,
          maxBytes: maxFileBytes,
          onChunk: (size) => {
            observeChunk(size)
            currentStreamBytes += size
          },
        })
        if (controller.signal.aborted) return
        if (totalBytes + blob.size > maxTotalBytes) {
          totalLimitError = new BatchDownloadError('TOTAL_SIZE_LIMIT', '所选图片总大小超过 200 MB 上限', {
            size: totalBytes + blob.size,
            limit: maxTotalBytes,
          })
          controller.abort(totalLimitError)
          return
        }
        totalBytes += blob.size
        currentStreamBytes = 0
        successes[index] = { item: current, blob }
        succeeded += 1
        completed += 1
        reportProgress()
      } catch (error) {
        if (currentStreamBytes) {
          streamedBytes = Math.max(0, streamedBytes - currentStreamBytes)
          currentStreamBytes = 0
        }
        if (controller.signal.aborted || error?.name === 'AbortError') return
        errors.push({
          key: current.key,
          item: current,
          code: error?.code || 'FETCH_FAILED',
          message: error?.message || '图片获取失败',
        })
        completed += 1
        reportProgress()
      }
    }
  }

  try {
    await Promise.all(Array.from({ length: concurrency }, () => worker()))
  } finally {
    externalSignal?.removeEventListener('abort', abortFromExternal)
  }

  if (totalLimitError) throw totalLimitError
  const completedSuccesses = successes.filter(Boolean)
  if (externalSignal?.aborted || (controller.signal.aborted && !totalLimitError)) {
    return batchResult({ aborted: true, errors, totalBytes })
  }
  if (!completedSuccesses.length) return batchResult({ errors, totalBytes })

  const usedNames = new Map()
  const archiveFiles = {}
  await Promise.all(completedSuccesses.map(async (entry) => {
    entry.filename = uniqueArchiveName(entry.item, entry.blob, usedNames)
    archiveFiles[entry.filename] = new Uint8Array(await entry.blob.arrayBuffer())
  }))
  if (externalSignal?.aborted) return batchResult({ aborted: true, errors, totalBytes })

  const createZip = options.createZip || createZipArchive
  let zipBytes
  try {
    zipBytes = await createZip(archiveFiles, { signal: externalSignal })
  } catch (error) {
    if (externalSignal?.aborted || error?.name === 'AbortError') {
      return batchResult({ aborted: true, errors, totalBytes })
    }
    throw new BatchDownloadError('ZIP_FAILED', '压缩图片失败', { cause: error })
  }
  if (externalSignal?.aborted) return batchResult({ aborted: true, errors, totalBytes })

  const filename = zipFilename(options.now || new Date())
  const zipBlob = new Blob([zipBytes], { type: 'application/zip' })
  const triggerDownload = options.triggerDownload || triggerBlobDownload
  try {
    triggerDownload(zipBlob, filename)
  } catch (error) {
    throw new BatchDownloadError('DOWNLOAD_FAILED', '无法保存 ZIP 文件', { cause: error })
  }
  return batchResult({ successes: completedSuccesses, errors, totalBytes, filename })
}

/**
 * Save an image to disk.
 *
 * The native `<a download>` attribute is ignored for cross-origin URLs, so the
 * image is fetched as a blob first. `data:` URLs are also converted to blobs:
 * Chromium silently drops anchor downloads of data: URLs past ~2MB. On any
 * network/CORS failure we fall back to opening the image in a new tab.
 *
 * @returns {Promise<boolean>} true if a real download was triggered, false if
 *   it fell back to opening a new tab.
 */
export async function downloadImage(url, baseName = 'img-Creater') {
  const filename = `${baseName}.${guessExtension(url)}`

  if (url.startsWith('data:')) {
    try {
      await downloadViaObjectUrl(url, filename)
    } catch {
      // fetch on data: URLs works everywhere modern; keep the direct anchor as a safety net.
      triggerAnchorDownload(url, filename)
    }
    return true
  }

  try {
    await downloadViaObjectUrl(url, filename, { mode: 'cors' })
    return true
  } catch {
    window.open(url, '_blank', 'noopener')
    return false
  }
}
