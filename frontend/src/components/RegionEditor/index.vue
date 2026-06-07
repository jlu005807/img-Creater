<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Crop, Delete, EditPen, FullScreen, Hide, RefreshLeft, Remove, Upload, View } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { useTheme } from '../../composables/useTheme'

const emit = defineEmits(['mask-change'])

const PANEL_WIDTH = 400 // canvas fills the panel
const PANEL_HEIGHT = 360
const ZOOM_CANVAS = 900
const ZOOM_HEIGHT = 640
const UNDO_LIMIT = 25
const OVERLAY_ALPHA = 0.4

const { theme } = useTheme()

const canvasRef = ref(null)
const fileInputRef = ref(null)
const zoomCanvasRef = ref(null)
const fileName = ref('')
const tool = ref('brush')
const brushSize = ref(42)
const imageDataUrl = ref('')
const hasMask = ref(false)
const canUndo = ref(false)
const isDragOver = ref(false)
const maskVisible = ref(true) // toggle mask overlay
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

const cursorStyle = computed(() => {
  if (!imageDataUrl.value) return 'default'
  return tool.value === 'rect' ? 'crosshair' : 'none'
})

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
  hasMask.value = false
  undoStack = []
  canUndo.value = false
}

function drawScene() {
  const canvas = canvasRef.value
  if (!canvas) return
  canvas.width = PANEL_WIDTH
  canvas.height = PANEL_HEIGHT
  const ctx = canvas.getContext('2d')
  const styles = getComputedStyle(canvas)
  const ink = styles.getPropertyValue('--studio-ink').trim() || '#172126'

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

  // Semi-transparent mask overlay (colored teal tint).
  if (maskVisible.value) {
    ctx.save()
    ctx.globalAlpha = OVERLAY_ALPHA
    ctx.drawImage(maskCanvas, 0, 0)
    ctx.globalCompositeOperation = 'source-atop'
    const teal = styles.getPropertyValue('--studio-teal').trim() || '#0f8f8c'
    ctx.fillStyle = teal
    ctx.fillRect(0, 0, PANEL_WIDTH, PANEL_HEIGHT)
    ctx.restore()
  }

  // Selection preview (rect drag outline).
  if (previewRect) {
    ctx.save()
    const coral = styles.getPropertyValue('--studio-coral').trim() || '#d96b4d'
    ctx.strokeStyle = coral
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
    ctx.strokeStyle = 'rgba(255,255,255,0.9)'
    ctx.stroke()
    ctx.beginPath()
    ctx.arc(hoverPoint.x, hoverPoint.y, radius, 0, Math.PI * 2)
    ctx.lineWidth = 1
    ctx.setLineDash([4, 3])
    ctx.strokeStyle = tool.value === 'eraser' ? '#d96b4d' : '#0f8f8c'
    ctx.stroke()
    ctx.restore()
  }
}

function drawZoomScene() {
  const canvas = zoomCanvasRef.value
  if (!canvas || !imageElement) return
  const scale = Math.min(ZOOM_CANVAS / imageElement.width, ZOOM_HEIGHT / imageElement.height)
  canvas.width = ZOOM_CANVAS
  canvas.height = ZOOM_HEIGHT
  const ctx = canvas.getContext('2d')
  const dw = imageElement.width * scale
  const dh = imageElement.height * scale
  const dx = (ZOOM_CANVAS - dw) / 2
  const dy = (ZOOM_HEIGHT - dh) / 2

  ctx.clearRect(0, 0, ZOOM_CANVAS, ZOOM_HEIGHT)
  ctx.drawImage(imageElement, dx, dy, dw, dh)

  // Draw the mask scaled up.
  if (maskVisible.value && maskCanvas) {
    ctx.save()
    ctx.globalAlpha = OVERLAY_ALPHA
    ctx.drawImage(maskCanvas, 0, 0, maskCanvas.width, maskCanvas.height, dx, dy, dw, dh)
    ctx.globalCompositeOperation = 'source-atop'
    ctx.fillStyle = '#0f8f8c'
    ctx.fillRect(dx, dy, dw, dh)
    ctx.restore()
  }
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
  if (!imageElement || !canvasScale) return pt
  const zScale = Math.min(ZOOM_CANVAS / imageElement.width, ZOOM_HEIGHT / imageElement.height)
  const scale = zScale > canvasScale ? zScale / canvasScale : 1
  return { x: pt.x / scale, y: pt.y / scale }
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
}

function pointerUp(isZoom = false) {
  if (drawing && imageElement && tool.value === 'rect' && previewRect) {
    if (previewRect.width > 3 && previewRect.height > 3) {
      maskContext.fillStyle = 'rgba(255,255,255,1)'
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
  emitMaskState()
}

function pointerLeave() {
  pointerUp()
  hoverPoint = null
  drawScene()
}

function paintLine(from, to) {
  maskContext.save()
  if (tool.value === 'eraser') maskContext.globalCompositeOperation = 'destination-out'
  maskContext.strokeStyle = 'rgba(255,255,255,1)'
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

function emitMaskState() {
  emit('mask-change', { hasImage: Boolean(imageDataUrl.value), hasMask: hasMask.value })
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

// Export mask scaled to source image dimensions.
function exportPayload() {
  if (!imageDataUrl.value || !maskCanvas || !maskContext || !hasMask.value) return null
  const iw = imageElement.width
  const ih = imageElement.height

  // Build a mask at source-image resolution.
  const offscreen = document.createElement('canvas')
  offscreen.width = iw
  offscreen.height = ih
  const octx = offscreen.getContext('2d')
  octx.drawImage(maskCanvas, 0, 0, iw, ih)

  // Auto-compose a blended image: original + a semi-transparent teal overlay
  // over the marked region — a single image the user can submit directly
  // (no manual masking step). See docs/ARCHITECTURE.md §8 for the data flow.
  const composite = document.createElement('canvas')
  composite.width = iw
  composite.height = ih
  const cctx = composite.getContext('2d')
  cctx.drawImage(imageElement, 0, 0, iw, ih)
  cctx.save()
  cctx.globalAlpha = OVERLAY_ALPHA
  cctx.drawImage(maskCanvas, 0, 0, iw, ih)
  cctx.globalCompositeOperation = 'source-atop'
  cctx.fillStyle = '#0f8f8c'
  cctx.fillRect(0, 0, iw, ih)
  cctx.restore()

  // Compute the letterbox rect of the image inside the panel.
  const scale = Math.min(PANEL_WIDTH / iw, PANEL_HEIGHT / ih)
  const dw = iw * scale
  const dh = ih * scale
  const dx = (PANEL_WIDTH - dw) / 2
  const dy = (PANEL_HEIGHT - dh) / 2

  return {
    image: imageDataUrl.value,
    mask: offscreen.toDataURL('image/png'),
    composite: composite.toDataURL('image/png'),
    selection: {
      type: tool.value,
      canvas: { width: PANEL_WIDTH, height: PANEL_HEIGHT },
      box: {
        x: Math.round(dx),
        y: Math.round(dy),
        width: Math.round(dw),
        height: Math.round(dh),
      },
      bbox: getMaskBoundingBox(),
    },
  }
}

function onCanvasKeydown(event) {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'z') {
    event.preventDefault()
    undo()
  }
}

// Zoom popup: draw once when opened.
watch(zoomOpen, (val) => {
  if (val) nextTick(drawZoomScene)
})
watch(theme, () => { drawScene(); if (zoomOpen.value) drawZoomScene() })
onMounted(drawScene)
onBeforeUnmount(() => { undoStack = [] })

defineExpose({ exportPayload, clearMask })
</script>

<template>
  <section class="studio-panel rounded-lg p-4">
    <div class="mb-3 flex items-center justify-between">
      <div>
        <p class="text-xs font-bold uppercase tracking-[0.16em] text-[var(--studio-teal)]">Region Edit</p>
        <h3 class="mt-1 text-lg font-black">局部修改蒙版</h3>
      </div>
      <div class="flex items-center gap-2">
        <input ref="fileInputRef" class="hidden" type="file" accept="image/*" @change="loadImage" />
        <el-button :icon="Upload" size="small" @click="openFilePicker">上传</el-button>
        <el-button
          :icon="maskVisible ? View : Hide"
          size="small"
          :title="maskVisible ? '隐藏蒙版' : '显示蒙版'"
          @click="maskVisible = !maskVisible"
        >
          {{ maskVisible ? '蒙版' : '隐藏' }}
        </el-button>
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
        <el-button :icon="RefreshLeft" :disabled="!canUndo" size="small" title="撤销 (Ctrl+Z)" @click="undo" />
        <el-button :icon="Delete" size="small" title="清除蒙版" @click="clearMask" />
      </div>
    </div>

    <div class="mb-3 grid grid-cols-[72px_1fr_48px] items-center gap-2 text-sm">
      <span class="font-semibold">画笔</span>
      <el-slider v-model="brushSize" :min="8" :max="120" :step="2" :disabled="tool === 'rect'" />
      <span class="text-right text-[var(--studio-muted)]">{{ brushSize }}</span>
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

    <p class="mt-2 text-xs leading-5 text-[var(--studio-muted)]">
      <span v-if="!imageDataUrl">上传图片后可直接在图上涂抹或框选修改区域。</span>
      <span v-else>涂抹选中 — 半透明<span class="text-[var(--studio-teal)] font-semibold"> 青色 </span>区域为蒙版；Ctrl+Z 撤销。</span>
    </p>

    <!-- Zoom modal -->
    <el-dialog v-model="zoomOpen" title="放大编辑" width="960px" destroy-on-close @opened="drawZoomScene">
      <canvas
        ref="zoomCanvasRef"
        tabindex="0"
        class="w-full rounded-md border border-[var(--studio-line)] bg-[var(--studio-canvas)] outline-none"
        :style="{ cursor: cursorStyle }"
        @pointerdown.prevent="(e) => pointerDown(e, true)"
        @pointermove.prevent="(e) => pointerMove(e, true)"
        @pointerup.prevent="() => pointerUp(true)"
      />
      <template #footer>
        <el-button @click="zoomOpen = false">关闭</el-button>
      </template>
    </el-dialog>
  </section>
</template>
