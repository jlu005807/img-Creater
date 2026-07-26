function triggerAnchorDownload(href, filename) {
  const anchor = document.createElement('a')
  anchor.href = href
  anchor.download = filename
  anchor.rel = 'noreferrer'
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
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
  const objectUrl = URL.createObjectURL(blob)
  triggerAnchorDownload(objectUrl, filename)
  // Revoke after the click has been handled.
  setTimeout(() => URL.revokeObjectURL(objectUrl), 1000)
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
