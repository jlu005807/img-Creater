<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Crop, Delete, EditPen, FullScreen, RefreshLeft, Remove, Upload } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { useTheme } from '../../composables/useTheme'

const emit = defineEmits(['mask-change'])

const PANEL_WIDTH = 400 // canvas fills the panel
const PANEL_HEIGHT = 360
const ZOOM_CANVAS = 900
const ZOOM_HEIGHT = 640
const UNDO_LIMIT = 25
const OVERLAY_ALPHA = 0.4
const DEFAULT_MARK_COLOR = '#ffffff'

const markColors = [
  { label: '白色', value: '#ffffff', rgb: [255, 255, 255] },
  { label: '红色', value: '#ff4d4f', rgb: [255, 77, 79] },
  { label: '黄色', value: '#fadb14', rgb: [250, 219, 20] },
  { label: '绿色', value: '#52c41a', rgb: [82, 196, 26] },
  { label: '蓝色', value: '#4096ff', rgb: [64, 150, 255] },
]

const { theme } = useTheme()

const canvasRef = ref(null)
const fileInputRef = ref(null)
const zoomCanvasRef = ref(null)
const fileName = ref('')
const tool = ref('brush')
const brushSize = ref(42)
const markerColor = ref(DEFAULT_MARK_COLOR)
const imageDataUrl = ref('')
const hasMask = ref(false)
const canUndo = ref(false)
const isDragOver = ref(false)
const comparePosition = ref(100)
const zoomOpen = ref(false)

let imageElement = null
let maskCanvas = null
let maskContext = null
let drawing = false
let startPoint = null
let lastPoint = null
let previewRect = null
let hoverPoint = null
let undoStack = []
// Pixel scale: 1 canvas px = how many image px
let canvasScale = 1
let imageRevision = 0

const cursorStyle = computed(() => {
  if (!imageDataUrl.value) return 'default'
  return tool.value === 'rect' ? 'crosshair' : 'none'
})
const selectedMarkColor = computed(() => markColors.find((item) => item.value === markerColor.value) || markColors[0])
const markerColorLabel = computed(() => selectedMarkColor.value.label)

function markerColorRgba(alpha = OVERLAY_ALPHA) {
  const [r, g, b] = selectedMarkColor.value.rgb
  return `rgba(${r},${g},${b},${alpha})`
}

function normalizeMarkerColor(value) {
  return markColors.some((item) => item.value === value) ? value : DEFAULT_MARK_COLOR
}

function setMarkerColor(value) {
  markerColor.value = normalizeMarkerColor(value)
  drawScene()
  if (zoomOpen.value) drawZoomScene()
  emitMaskState()
}

function openFilePicker() { fileInputRef.value?.click() }

function loadImage(event) {
  readImageFile(event.target.files?.[0])
  event.target.value = ''
}

function onDrop(event) {
  isDragOver.value = false
  readImageFile(event.dataTransfer?.files?.[0])
}

function readImageFile(file) {
  if (!file) return
  if (!file.type.startsWith('image/')) { ElMessage.warning('请选择图片文件'); return }
  const reader = new FileReader()
  reader.onload = () => {
    const img = new Image()
    img.onload = async () => {
      imageDataUrl.value = reader.result
      fileName.value = file.name
      imageElement = img
      imageRevision += 1
      initMaskCanvas()
      await nextTick()
      drawScene()
      emitMaskState()
    }
    img.src = reader.result
  }
  reader.readAsDataURL(file)
}

function initMaskCanvas() {
  maskCanvas = document.createElement('canvas')
  maskCanvas.width = PANEL_WIDTH
  maskCanvas.height = PANEL_HEIGHT
  maskContext = maskCanvas.getContext('2d')
  maskContext.clearRect(0, 0, PANEL_WIDTH, PANEL_HEIGHT)
  canvasScale = 1
  comparePosition.value = 100
  hasMask.value = false
  undoStack = []
  canUndo.value = false
}

async function loadImageDataUrl(dataUrl, nextFileName = '') {
  if (!dataUrl) return false
  const img = new Image()
  await new Promise((resolve, reject) => {
    img.onload = resolve
    img.onerror = reject
    img.src = dataUrl
  })
  imageDataUrl.value = dataUrl
  fileName.value = nextFileName || fileName.value || 'restored-image'
  imageElement = img
  imageRevision += 1
  initMaskCanvas()
  await nextTick()
  drawScene()
  emitMaskState()
  return true
}

function drawScene() {
  const canvas = canvasRef.value
  if (!canvas) return
  canvas.width = PANEL_WIDTH
  canvas.height = PANEL_HEIGHT
  const ctx = canvas.getContext('2d')
  const styles = getComputedStyle(canvas)
  ctx.clearRect(0, 0, PANEL_WIDTH, PANEL_HEIGHT)

  if (!imageElement) {
    ctx.fillStyle = 'rgba(127,127,127,0.15)'
    ctx.font = '600 15px Aptos, sans-serif'
    ctx.textAlign = 'center'
    ctx.fillText('上传或拖拽一张图片到此处', PANEL_WIDTH / 2, PANEL_HEIGHT / 2)
    return
  }

  // Fit image into the panel.
  const scale = Math.min(PANEL_WIDTH / imageElement.width, PANEL_HEIGHT / imageElement.height)
  canvasScale = scale
  const dw = imageElement.width * scale
  const dh = imageElement.height * scale
  const dx = (PANEL_WIDTH - dw) / 2
  const dy = (PANEL_HEIGHT - dh) / 2

  // Image.
  ctx.drawImage(imageElement, dx, dy, dw, dh)

  // Semi-transparent colored mark overlay, clipped for before/after compare.
  drawMarkOverlay(ctx, PANEL_WIDTH, PANEL_HEIGHT, dx, dy, dw, dh, dx, dy, dw, dh, compareRatio())
  drawCompareDivider(ctx, dx, dy, dw, dh)

  // Selection preview (rect drag outline).
  if (previewRect) {
    ctx.save()
    ctx.strokeStyle = markerColorRgba(0.95)
    ctx.lineWidth = 2
    ctx.setLineDash([6, 4])
    ctx.strokeRect(previewRect.x, previewRect.y, previewRect.width, previewRect.height)
    ctx.restore()
  }

  // Brush / eraser cursor ring.
  if (hoverPoint && tool.value !== 'rect') {
    const radius = brushSize.value / 2
    ctx.save()
    ctx.beginPath()
    ctx.arc(hoverPoint.x, hoverPoint.y, radius, 0, Math.PI * 2)
    ctx.lineWidth = 2
    ctx.strokeStyle = 'rgba(23,33,38,0.65)'
    ctx.stroke()
    ctx.beginPath()
    ctx.arc(hoverPoint.x, hoverPoint.y, radius, 0, Math.PI * 2)
    ctx.lineWidth = 1
    ctx.setLineDash([4, 3])
    ctx.strokeStyle = tool.value === 'eraser' ? '#d96b4d' : markerColorRgba(0.95)
    ctx.stroke()
    ctx.restore()
  }
}

function drawZoomScene() {
  const canvas = zoomCanvasRef.value
  if (!canvas || !imageElement) return
  canvas.width = ZOOM_CANVAS
  canvas.height = ZOOM_HEIGHT
  const ctx = canvas.getContext('2d')
  const zoomRect = imageFitRect(ZOOM_CANVAS, ZOOM_HEIGHT)
  const panelRect = imageFitRect(PANEL_WIDTH, PANEL_HEIGHT)
  const { dw, dh, dx, dy } = zoomRect

  ctx.clearRect(0, 0, ZOOM_CANVAS, ZOOM_HEIGHT)
  ctx.drawImage(imageElement, dx, dy, dw, dh)

  // Draw the mark layer scaled up, clipped for before/after compare.
  drawMarkOverlay(ctx, ZOOM_CANVAS, ZOOM_HEIGHT, panelRect.dx, panelRect.dy, panelRect.dw, panelRect.dh, dx, dy, dw, dh, compareRatio())
  drawCompareDivider(ctx, dx, dy, dw, dh)

  if (previewRect) {
    const from = panelToZoomPoint({ x: previewRect.x, y: previewRect.y })
    const to = panelToZoomPoint({ x: previewRect.x + previewRect.width, y: previewRect.y + previewRect.height })
    ctx.save()
    ctx.strokeStyle = markerColorRgba(0.95)
    ctx.lineWidth = 2
    ctx.setLineDash([8, 5])
    ctx.strokeRect(
      Math.min(from.x, to.x),
      Math.min(from.y, to.y),
      Math.abs(to.x - from.x),
      Math.abs(to.y - from.y),
    )
    ctx.restore()
  }

  if (hoverPoint && tool.value !== 'rect') {
    const zoomHover = panelToZoomPoint(hoverPoint)
    const radius = (brushSize.value / 2) * (zoomRect.scale / panelRect.scale)
    ctx.save()
    ctx.beginPath()
    ctx.arc(zoomHover.x, zoomHover.y, radius, 0, Math.PI * 2)
    ctx.lineWidth = 2
    ctx.strokeStyle = 'rgba(23,33,38,0.65)'
    ctx.stroke()
    ctx.beginPath()
    ctx.arc(zoomHover.x, zoomHover.y, radius, 0, Math.PI * 2)
    ctx.lineWidth = 1
    ctx.setLineDash([5, 4])
    ctx.strokeStyle = tool.value === 'eraser' ? '#d96b4d' : markerColorRgba(0.95)
    ctx.stroke()
    ctx.restore()
  }
}

function imageFitRect(width, height) {
  if (!imageElement) return { scale: 1, dw: width, dh: height, dx: 0, dy: 0 }
  const scale = Math.min(width / imageElement.width, height / imageElement.height)
  const dw = imageElement.width * scale
  const dh = imageElement.height * scale
  return {
    scale,
    dw,
    dh,
    dx: (width - dw) / 2,
    dy: (height - dh) / 2,
  }
}

function compareRatio() {
  return clamp(Number(comparePosition.value || 0), 0, 100) / 100
}

function drawMarkOverlay(ctx, width, height, sx, sy, sw, sh, dx, dy, dw, dh, ratio = 1) {
  const clippedRatio = clamp(ratio, 0, 1)
  if (!maskCanvas || clippedRatio <= 0 || sw <= 0 || sh <= 0 || dw <= 0 || dh <= 0) return
  const layer = document.createElement('canvas')
  layer.width = width
  layer.height = height
  const layerContext = layer.getContext('2d')
  layerContext.globalAlpha = OVERLAY_ALPHA
  layerContext.drawImage(maskCanvas, sx, sy, sw, sh, dx, dy, dw, dh)
  ctx.save()
  ctx.beginPath()
  ctx.rect(dx, dy, dw * clippedRatio, dh)
  ctx.clip()
  ctx.drawImage(layer, 0, 0)
  ctx.restore()
}

function drawCompareDivider(ctx, dx, dy, dw, dh) {
  if (!imageElement || !hasMask.value) return
  const ratio = compareRatio()
  if (ratio <= 0 || ratio >= 1) return
  const x = dx + dw * ratio
  ctx.save()
  ctx.beginPath()
  ctx.moveTo(x, dy)
  ctx.lineTo(x, dy + dh)
  ctx.lineWidth = 3
  ctx.strokeStyle = 'rgba(23, 33, 38, 0.55)'
  ctx.stroke()
  ctx.lineWidth = 1
  ctx.strokeStyle = 'rgba(255,255,255,0.95)'
  ctx.stroke()
  ctx.restore()
}

function getCanvasPoint(event) {
  const rect = canvasRef.value.getBoundingClientRect()
  return {
    x: ((event.clientX - rect.left) * PANEL_WIDTH) / rect.width,
    y: ((event.clientY - rect.top) * PANEL_HEIGHT) / rect.height,
  }
}

function getZoomPoint(event) {
  const rect = zoomCanvasRef.value.getBoundingClientRect()
  return {
    x: ((event.clientX - rect.left) * ZOOM_CANVAS) / rect.width,
    y: ((event.clientY - rect.top) * ZOOM_HEIGHT) / rect.height,
  }
}

// Map zoom canvas coords to panel coords.
function zoomToPanel(pt) {
  if (!imageElement) return pt
  const zoomRect = imageFitRect(ZOOM_CANVAS, ZOOM_HEIGHT)
  const panelRect = imageFitRect(PANEL_WIDTH, PANEL_HEIGHT)
  const imageX = clamp((pt.x - zoomRect.dx) / zoomRect.scale, 0, imageElement.width)
  const imageY = clamp((pt.y - zoomRect.dy) / zoomRect.scale, 0, imageElement.height)
  return {
    x: panelRect.dx + imageX * panelRect.scale,
    y: panelRect.dy + imageY * panelRect.scale,
  }
}

function panelToZoomPoint(pt) {
  if (!imageElement) return pt
  const zoomRect = imageFitRect(ZOOM_CANVAS, ZOOM_HEIGHT)
  const panelRect = imageFitRect(PANEL_WIDTH, PANEL_HEIGHT)
  const imageX = (pt.x - panelRect.dx) / panelRect.scale
  const imageY = (pt.y - panelRect.dy) / panelRect.scale
  return {
    x: zoomRect.dx + imageX * zoomRect.scale,
    y: zoomRect.dy + imageY * zoomRect.scale,
  }
}

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value))
}

function pushUndo() {
  if (!maskContext) return
  undoStack.push(maskContext.getImageData(0, 0, PANEL_WIDTH, PANEL_HEIGHT))
  if (undoStack.length > UNDO_LIMIT) undoStack.shift()
  canUndo.value = true
}

function undo() {
  if (!maskContext || !undoStack.length) return
  const snapshot = undoStack.pop()
  maskContext.putImageData(snapshot, 0, 0)
  canUndo.value = undoStack.length > 0
  recomputeHasMask()
  previewRect = null
  drawScene()
  emitMaskState()
}

function pointerDown(event, isZoom = false) {
  if (!imageElement) return
  ;(isZoom ? zoomCanvasRef : canvasRef).value?.setPointerCapture?.(event.pointerId)
  pushUndo()
  drawing = true
  const pt = isZoom ? zoomToPanel(getZoomPoint(event)) : getCanvasPoint(event)
  startPoint = pt
  lastPoint = pt
  if (tool.value === 'brush' || tool.value === 'eraser') paintLine(pt, pt)
  drawScene()
  if (isZoom) drawZoomScene()
}

function pointerMove(event, isZoom = false) {
  if (!imageElement) return
  const raw = isZoom ? zoomToPanel(getZoomPoint(event)) : getCanvasPoint(event)
  hoverPoint = raw
  if (drawing) {
    if (tool.value === 'rect') {
      previewRect = normalizeRect(startPoint, raw)
    } else {
      paintLine(lastPoint, raw)
      lastPoint = raw
    }
  }
  drawScene()
  if (isZoom) drawZoomScene()
}

function pointerUp(isZoom = false) {
  if (drawing && imageElement && tool.value === 'rect' && previewRect) {
    if (previewRect.width > 3 && previewRect.height > 3) {
      maskContext.fillStyle = markerColorRgba(1)
      maskContext.fillRect(previewRect.x, previewRect.y, previewRect.width, previewRect.height)
      hasMask.value = true
    }
  }
  if (drawing && tool.value === 'eraser') recomputeHasMask()
  drawing = false
  startPoint = null
  lastPoint = null
  previewRect = null
  drawScene()
  if (isZoom) drawZoomScene()
  emitMaskState()
}

function pointerLeave() {
  pointerUp()
  hoverPoint = null
  drawScene()
  if (zoomOpen.value) drawZoomScene()
}

function paintLine(from, to) {
  maskContext.save()
  if (tool.value === 'eraser') maskContext.globalCompositeOperation = 'destination-out'
  maskContext.strokeStyle = markerColorRgba(1)
  maskContext.lineWidth = brushSize.value
  maskContext.lineCap = 'round'
  maskContext.lineJoin = 'round'
  maskContext.beginPath()
  maskContext.moveTo(from.x, from.y)
  maskContext.lineTo(to.x, to.y)
  maskContext.stroke()
  maskContext.restore()
  if (tool.value !== 'eraser') hasMask.value = true
}

function normalizeRect(from, to) {
  return {
    x: Math.min(from.x, to.x),
    y: Math.min(from.y, to.y),
    width: Math.abs(to.x - from.x),
    height: Math.abs(to.y - from.y),
  }
}

function clearMask() {
  if (!maskContext) return
  pushUndo()
  maskContext.clearRect(0, 0, PANEL_WIDTH, PANEL_HEIGHT)
  hasMask.value = false
  previewRect = null
  drawScene()
  emitMaskState()
}

function recomputeHasMask() { hasMask.value = Boolean(getMaskBoundingBox()) }

function exportDraft() {
  if (!imageDataUrl.value || !imageElement || !maskCanvas) return null
  return {
    version: 1,
    fileName: fileName.value,
    image: imageDataUrl.value,
    imageWidth: imageElement.width,
    imageHeight: imageElement.height,
    mask: maskCanvas.toDataURL('image/png'),
    hasMask: hasMask.value,
    tool: tool.value,
    brushSize: brushSize.value,
    markerColor: markerColor.value,
    comparePosition: comparePosition.value,
  }
}

async function restoreDraft(draft) {
  if (!draft?.image) {
    clearAll()
    return false
  }
  const restored = await loadImageDataUrl(draft.image, draft.fileName || '')
  if (!restored) return false

  if (draft.mask && maskContext) {
    const maskImage = new Image()
    await new Promise((resolve, reject) => {
      maskImage.onload = resolve
      maskImage.onerror = reject
      maskImage.src = draft.mask
    })
    maskContext.clearRect(0, 0, PANEL_WIDTH, PANEL_HEIGHT)
    maskContext.drawImage(maskImage, 0, 0, PANEL_WIDTH, PANEL_HEIGHT)
  }
  tool.value = ['brush', 'rect', 'eraser'].includes(draft.tool) ? draft.tool : 'brush'
  brushSize.value = Number.isFinite(Number(draft.brushSize)) ? Number(draft.brushSize) : 42
  markerColor.value = normalizeMarkerColor(draft.markerColor)
  comparePosition.value = Number.isFinite(Number(draft.comparePosition)) ? Number(draft.comparePosition) : 100
  recomputeHasMask()
  undoStack = []
  canUndo.value = false
  previewRect = null
  hoverPoint = null
  drawScene()
  if (zoomOpen.value) drawZoomScene()
  emitMaskState()
  return true
}

function clearAll() {
  imageDataUrl.value = ''
  fileName.value = ''
  imageElement = null
  imageRevision += 1
  initMaskCanvas()
  previewRect = null
  hoverPoint = null
  drawScene()
  emitMaskState()
}

function emitMaskState() {
  emit('mask-change', {
    hasImage: Boolean(imageDataUrl.value),
    hasMask: hasMask.value,
    imageWidth: imageElement?.width || null,
    imageHeight: imageElement?.height || null,
    imageRevision,
    markerColor: markerColor.value,
    markerColorLabel: markerColorLabel.value,
    draft: exportDraft(),
  })
}

function getMaskBoundingBox() {
  if (!maskContext) return null
  const pixels = maskContext.getImageData(0, 0, PANEL_WIDTH, PANEL_HEIGHT).data
  let minX = PANEL_WIDTH, minY = PANEL_HEIGHT, maxX = 0, maxY = 0
  for (let y = 0; y < PANEL_HEIGHT; y++) {
    for (let x = 0; x < PANEL_WIDTH; x++) {
      if (pixels[(y * PANEL_WIDTH + x) * 4 + 3] > 0) {
        minX = Math.min(minX, x); minY = Math.min(minY, y)
        maxX = Math.max(maxX, x); maxY = Math.max(maxY, y)
      }
    }
  }
  if (minX === PANEL_WIDTH) return null
  return { x: minX, y: minY, width: maxX - minX + 1, height: maxY - minY + 1 }
}

function exportPayload() {
  if (!imageDataUrl.value || !maskCanvas || !maskContext || !hasMask.value) return null
  const markedImage = buildMarkedImageDataUrl()
  return markedImage
    ? {
        source_image: imageDataUrl.value,
        marked_image: markedImage,
      }
    : null
}

function buildMarkedImageDataUrl() {
  if (!imageElement || !maskCanvas) return null
  const iw = imageElement.naturalWidth || imageElement.width
  const ih = imageElement.naturalHeight || imageElement.height
  if (!iw || !ih) return null

  const outputCanvas = document.createElement('canvas')
  outputCanvas.width = iw
  outputCanvas.height = ih
  const outputContext = outputCanvas.getContext('2d')
  outputContext.drawImage(imageElement, 0, 0, iw, ih)

  const markCanvas = document.createElement('canvas')
  markCanvas.width = iw
  markCanvas.height = ih
  const markContext = markCanvas.getContext('2d')
  const panelRect = imageFitRect(PANEL_WIDTH, PANEL_HEIGHT)
  markContext.globalAlpha = OVERLAY_ALPHA
  markContext.drawImage(
    maskCanvas,
    panelRect.dx,
    panelRect.dy,
    panelRect.dw,
    panelRect.dh,
    0,
    0,
    iw,
    ih,
  )
  outputContext.drawImage(markCanvas, 0, 0)

  return outputCanvas.toDataURL('image/png')
}

function onCanvasKeydown(event) {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'z') {
    event.preventDefault()
    event.stopPropagation()
  }
}

// Zoom popup: draw once when opened.
watch(zoomOpen, (val) => {
  if (val) nextTick(drawZoomScene)
})
watch(theme, () => { drawScene(); if (zoomOpen.value) drawZoomScene() })
watch(comparePosition, () => { drawScene(); if (zoomOpen.value) drawZoomScene() })
onMounted(drawScene)
onBeforeUnmount(() => { undoStack = [] })

defineExpose({ exportPayload, exportDraft, restoreDraft, clearMask, clearAll })
</script>

<template>
  <section class="studio-panel rounded-lg p-4">
    <div class="mb-3 flex items-center justify-between">
      <div>
        <p class="text-xs font-bold uppercase tracking-[0.16em] text-[var(--studio-teal)]">Region Edit</p>
        <h3 class="mt-1 text-lg font-black">局部修改标注</h3>
      </div>
      <div class="flex items-center gap-2">
        <input ref="fileInputRef" class="hidden" type="file" accept="image/*" @change="loadImage" />
        <el-button :icon="Upload" size="small" @click="openFilePicker">上传</el-button>
        <el-button :icon="FullScreen" size="small" :disabled="!imageDataUrl" title="放大编辑" @click="zoomOpen = true">放大</el-button>
      </div>
    </div>

    <div class="mb-3 flex flex-wrap items-center justify-between gap-3">
      <div class="min-w-0 flex-1 truncate text-sm text-[var(--studio-muted)]">{{ fileName || '未选择图片（可拖拽上传）' }}</div>
      <div class="flex items-center gap-2">
        <el-button-group>
          <el-button :type="tool === 'brush' ? 'primary' : 'default'" :icon="EditPen" size="small" title="涂抹" @click="tool = 'brush'" />
          <el-button :type="tool === 'rect' ? 'primary' : 'default'" :icon="Crop" size="small" title="框选" @click="tool = 'rect'" />
          <el-button :type="tool === 'eraser' ? 'primary' : 'default'" :icon="Remove" size="small" title="擦除" @click="tool = 'eraser'" />
        </el-button-group>
        <el-button :icon="RefreshLeft" :disabled="!canUndo" size="small" title="撤销" @click="undo" />
        <el-button :icon="Delete" size="small" title="清除标注" @click="clearMask" />
      </div>
    </div>

    <div class="mb-3 grid grid-cols-[72px_1fr_48px] items-center gap-2 text-sm">
      <span class="font-semibold">画笔</span>
      <el-slider v-model="brushSize" :min="8" :max="120" :step="2" :disabled="tool === 'rect'" />
      <span class="text-right text-[var(--studio-muted)]">{{ brushSize }}</span>
    </div>

    <div class="mb-3 grid grid-cols-[72px_1fr] items-center gap-2 text-sm">
      <span class="font-semibold">颜色</span>
      <div class="flex flex-wrap gap-2">
        <button
          v-for="color in markColors"
          :key="color.value"
          type="button"
          class="flex h-7 w-7 items-center justify-center rounded-md border transition"
          :class="markerColor === color.value ? 'border-[var(--studio-ink)] ring-2 ring-[var(--studio-teal)]' : 'border-[var(--studio-line)] hover:border-[var(--studio-teal)]'"
          :style="{ backgroundColor: color.value }"
          :title="`标注颜色：${color.label}`"
          :aria-label="`标注颜色：${color.label}`"
          @click="setMarkerColor(color.value)"
        >
          <span v-if="markerColor === color.value" class="h-2 w-2 rounded-full bg-[rgba(23,33,38,0.8)] shadow-[0_0_0_1px_rgba(255,255,255,0.9)]" />
        </button>
      </div>
    </div>

    <canvas
      ref="canvasRef"
      tabindex="0"
      class="aspect-[400/360] w-full rounded-md border bg-[var(--studio-canvas)] outline-none transition-colors"
      :class="isDragOver ? 'border-[var(--studio-coral)]' : 'border-[var(--studio-line)]'"
      :style="{ cursor: cursorStyle }"
      @pointerdown.prevent="(e) => pointerDown(e, false)"
      @pointermove.prevent="(e) => pointerMove(e, false)"
      @pointerup.prevent="() => pointerUp(false)"
      @pointerleave.prevent="pointerLeave"
      @keydown="onCanvasKeydown"
      @dragover.prevent="isDragOver = true"
      @dragleave.prevent="isDragOver = false"
      @drop.prevent="onDrop"
    />

    <div v-if="imageDataUrl" class="mt-3 grid grid-cols-[44px_1fr_44px] items-center gap-2 text-xs text-[var(--studio-muted)]">
      <span>原图</span>
      <el-slider v-model="comparePosition" :min="0" :max="100" :step="1" :show-tooltip="false" />
      <span class="text-right">标注</span>
    </div>

    <p class="mt-2 text-xs leading-5 text-[var(--studio-muted)]">
      <span v-if="!imageDataUrl">上传图片后可直接在图上涂抹或框选修改区域。</span>
      <span v-else>涂抹选中 — 半透明彩色区域为标注。当前颜色：{{ markerColorLabel }}。</span>
    </p>

    <!-- Zoom modal -->
    <el-dialog v-model="zoomOpen" title="放大编辑" width="1120px" destroy-on-close @opened="drawZoomScene">
      <div class="space-y-4">
        <div class="flex items-start justify-between gap-4">
          <div class="min-w-0">
            <p class="text-xs font-bold uppercase tracking-[0.16em] text-[var(--studio-teal)]">Region Edit</p>
            <h3 class="mt-1 truncate text-lg font-black">{{ fileName || '未选择图片' }}</h3>
          </div>
          <div class="flex shrink-0 items-center gap-2">
            <el-button :icon="Upload" size="small" @click="openFilePicker">上传</el-button>
            <el-button :icon="RefreshLeft" :disabled="!canUndo" size="small" title="撤销" @click="undo" />
            <el-button :icon="Delete" size="small" title="清除标注" @click="clearMask" />
          </div>
        </div>

        <div class="grid grid-cols-[auto_1fr] gap-4">
          <div class="w-52 space-y-4 rounded-md border border-[var(--studio-line)] bg-[var(--studio-surface-soft)] p-3">
            <div>
              <p class="mb-2 text-xs font-semibold text-[var(--studio-muted)]">工具</p>
              <el-button-group class="w-full">
                <el-button :type="tool === 'brush' ? 'primary' : 'default'" :icon="EditPen" size="small" title="涂抹" @click="tool = 'brush'" />
                <el-button :type="tool === 'rect' ? 'primary' : 'default'" :icon="Crop" size="small" title="框选" @click="tool = 'rect'" />
                <el-button :type="tool === 'eraser' ? 'primary' : 'default'" :icon="Remove" size="small" title="擦除" @click="tool = 'eraser'" />
              </el-button-group>
            </div>
            <div>
              <div class="mb-3">
                <p class="mb-2 text-xs font-semibold text-[var(--studio-muted)]">颜色</p>
                <div class="flex flex-wrap gap-2">
                  <button
                    v-for="color in markColors"
                    :key="color.value"
                    type="button"
                    class="flex h-7 w-7 items-center justify-center rounded-md border transition"
                    :class="markerColor === color.value ? 'border-[var(--studio-ink)] ring-2 ring-[var(--studio-teal)]' : 'border-[var(--studio-line)] hover:border-[var(--studio-teal)]'"
                    :style="{ backgroundColor: color.value }"
                    :title="`标注颜色：${color.label}`"
                    :aria-label="`标注颜色：${color.label}`"
                    @click="setMarkerColor(color.value)"
                  >
                    <span v-if="markerColor === color.value" class="h-2 w-2 rounded-full bg-[rgba(23,33,38,0.8)] shadow-[0_0_0_1px_rgba(255,255,255,0.9)]" />
                  </button>
                </div>
              </div>
            </div>
            <div>
              <div class="mb-1 flex items-center justify-between text-sm">
                <span class="font-semibold">画笔</span>
                <span class="text-[var(--studio-muted)]">{{ brushSize }}</span>
              </div>
              <el-slider v-model="brushSize" :min="8" :max="120" :step="2" :disabled="tool === 'rect'" />
            </div>
            <div>
              <div class="mb-1 flex items-center justify-between text-sm">
                <span class="font-semibold">对比</span>
                <span class="text-[var(--studio-muted)]">{{ comparePosition }}</span>
              </div>
              <el-slider v-model="comparePosition" :min="0" :max="100" :step="1" :show-tooltip="false" />
            </div>
            <div class="rounded-md border border-[var(--studio-line)] bg-[var(--studio-surface)] p-3 text-xs leading-5 text-[var(--studio-muted)]">
              <p>{{ hasMask ? '已标记修改区域' : '尚未标记区域' }}</p>
              <p>标注颜色：{{ markerColorLabel }}</p>
              <p>图片按比例完整显示，不裁剪、不拉伸。</p>
            </div>
          </div>

          <canvas
            ref="zoomCanvasRef"
            tabindex="0"
            class="w-full rounded-md border border-[var(--studio-line)] bg-[var(--studio-canvas)] outline-none"
            :style="{ cursor: cursorStyle }"
            @pointerdown.prevent="(e) => pointerDown(e, true)"
            @pointermove.prevent="(e) => pointerMove(e, true)"
            @pointerup.prevent="() => pointerUp(true)"
            @pointerleave.prevent="pointerLeave"
            @keydown="onCanvasKeydown"
          />
        </div>
      </div>
      <template #footer>
        <el-button @click="zoomOpen = false">关闭</el-button>
      </template>
    </el-dialog>
  </section>
</template>
