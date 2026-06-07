<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Crop, Delete, EditPen, RefreshLeft, Remove, Upload } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { useTheme } from '../../composables/useTheme'

const emit = defineEmits(['mask-change'])

const CANVAS_WIDTH = 720
const CANVAS_HEIGHT = 520
const UNDO_LIMIT = 25

const { theme } = useTheme()

const canvasRef = ref(null)
const fileInputRef = ref(null)
const fileName = ref('')
const tool = ref('brush')
const brushSize = ref(42)
const imageDataUrl = ref('')
const hasMask = ref(false)
const canUndo = ref(false)
const isDragOver = ref(false)

let imageElement = null
let imageBox = null
let maskCanvas = null
let maskContext = null
let drawing = false
let startPoint = null
let lastPoint = null
let previewRect = null
let hoverPoint = null
let undoStack = []

const cursorStyle = computed(() => {
  if (!imageDataUrl.value) return 'default'
  return tool.value === 'rect' ? 'crosshair' : 'none'
})

function openFilePicker() {
  fileInputRef.value?.click()
}

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
  if (!file.type.startsWith('image/')) {
    ElMessage.warning('请选择图片文件')
    return
  }

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
  maskCanvas.width = CANVAS_WIDTH
  maskCanvas.height = CANVAS_HEIGHT
  maskContext = maskCanvas.getContext('2d')
  maskContext.clearRect(0, 0, CANVAS_WIDTH, CANVAS_HEIGHT)
  hasMask.value = false
  undoStack = []
  canUndo.value = false
}

function drawScene() {
  const canvas = canvasRef.value
  if (!canvas) return
  canvas.width = CANVAS_WIDTH
  canvas.height = CANVAS_HEIGHT
  const ctx = canvas.getContext('2d')
  const styles = getComputedStyle(canvas)
  const canvasBg = styles.getPropertyValue('--studio-canvas').trim() || '#f6f2ea'
  const mutedColor = styles.getPropertyValue('--studio-muted').trim() || '#6f777f'
  const teal = styles.getPropertyValue('--studio-teal').trim() || '#0f8f8c'
  const coral = styles.getPropertyValue('--studio-coral').trim() || '#d96b4d'

  ctx.clearRect(0, 0, CANVAS_WIDTH, CANVAS_HEIGHT)
  ctx.fillStyle = canvasBg
  ctx.fillRect(0, 0, CANVAS_WIDTH, CANVAS_HEIGHT)

  if (!imageElement) {
    ctx.fillStyle = mutedColor
    ctx.font = '600 18px Aptos, sans-serif'
    ctx.textAlign = 'center'
    ctx.fillText('上传或拖拽一张需要局部修改的图片', CANVAS_WIDTH / 2, CANVAS_HEIGHT / 2)
    return
  }

  const scale = Math.min(CANVAS_WIDTH / imageElement.width, CANVAS_HEIGHT / imageElement.height)
  const width = imageElement.width * scale
  const height = imageElement.height * scale
  const x = (CANVAS_WIDTH - width) / 2
  const y = (CANVAS_HEIGHT - height) / 2
  imageBox = { x, y, width, height }

  ctx.drawImage(imageElement, x, y, width, height)
  ctx.save()
  ctx.globalAlpha = 0.45
  ctx.drawImage(maskCanvas, 0, 0)
  ctx.globalCompositeOperation = 'source-atop'
  ctx.fillStyle = teal
  ctx.fillRect(0, 0, CANVAS_WIDTH, CANVAS_HEIGHT)
  ctx.restore()

  if (previewRect) {
    ctx.save()
    ctx.strokeStyle = coral
    ctx.lineWidth = 2
    ctx.setLineDash([8, 5])
    ctx.strokeRect(previewRect.x, previewRect.y, previewRect.width, previewRect.height)
    ctx.restore()
  }

  // Brush / eraser cursor ring (canvas hides the native cursor for these tools).
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
    ctx.strokeStyle = tool.value === 'eraser' ? coral : teal
    ctx.stroke()
    ctx.restore()
  }
}

function getCanvasPoint(event) {
  const rect = canvasRef.value.getBoundingClientRect()
  return {
    x: ((event.clientX - rect.left) * CANVAS_WIDTH) / rect.width,
    y: ((event.clientY - rect.top) * CANVAS_HEIGHT) / rect.height,
  }
}

function pushUndo() {
  if (!maskContext) return
  undoStack.push(maskContext.getImageData(0, 0, CANVAS_WIDTH, CANVAS_HEIGHT))
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

function pointerDown(event) {
  if (!imageElement) return
  canvasRef.value.setPointerCapture?.(event.pointerId)
  pushUndo()
  drawing = true
  startPoint = getCanvasPoint(event)
  lastPoint = startPoint
  hoverPoint = startPoint
  if (tool.value === 'brush' || tool.value === 'eraser') {
    paintLine(startPoint, startPoint)
  }
  drawScene()
}

function pointerMove(event) {
  if (!imageElement) return
  const point = getCanvasPoint(event)
  hoverPoint = point
  if (drawing) {
    if (tool.value === 'rect') {
      previewRect = normalizeRect(startPoint, point)
    } else {
      paintLine(lastPoint, point)
      lastPoint = point
    }
  }
  drawScene()
}

function pointerUp() {
  if (drawing && imageElement && tool.value === 'rect' && previewRect) {
    if (previewRect.width > 4 && previewRect.height > 4) {
      maskContext.fillStyle = 'rgba(255,255,255,1)'
      maskContext.fillRect(previewRect.x, previewRect.y, previewRect.width, previewRect.height)
      hasMask.value = true
    }
  }
  if (drawing && tool.value === 'eraser') {
    recomputeHasMask()
  }
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
  if (tool.value === 'eraser') {
    maskContext.globalCompositeOperation = 'destination-out'
  }
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
  const x = Math.min(from.x, to.x)
  const y = Math.min(from.y, to.y)
  return {
    x,
    y,
    width: Math.abs(to.x - from.x),
    height: Math.abs(to.y - from.y),
  }
}

function clearMask() {
  if (!maskContext) return
  pushUndo()
  maskContext.clearRect(0, 0, CANVAS_WIDTH, CANVAS_HEIGHT)
  hasMask.value = false
  previewRect = null
  drawScene()
  emitMaskState()
}

function recomputeHasMask() {
  hasMask.value = Boolean(getMaskBoundingBox())
}

function emitMaskState() {
  emit('mask-change', { hasImage: Boolean(imageDataUrl.value), hasMask: hasMask.value })
}

function getMaskBoundingBox() {
  if (!maskContext) return null
  const pixels = maskContext.getImageData(0, 0, CANVAS_WIDTH, CANVAS_HEIGHT).data
  let minX = CANVAS_WIDTH
  let minY = CANVAS_HEIGHT
  let maxX = 0
  let maxY = 0

  for (let y = 0; y < CANVAS_HEIGHT; y += 1) {
    for (let x = 0; x < CANVAS_WIDTH; x += 1) {
      const alpha = pixels[(y * CANVAS_WIDTH + x) * 4 + 3]
      if (alpha > 0) {
        minX = Math.min(minX, x)
        minY = Math.min(minY, y)
        maxX = Math.max(maxX, x)
        maxY = Math.max(maxY, y)
      }
    }
  }

  if (minX === CANVAS_WIDTH) return null
  return { x: minX, y: minY, width: maxX - minX + 1, height: maxY - minY + 1 }
}

function exportPayload() {
  if (!imageDataUrl.value || !maskCanvas || !hasMask.value) return null
  return {
    image: imageDataUrl.value,
    mask: maskCanvas.toDataURL('image/png'),
    selection: {
      type: tool.value,
      canvas: { width: CANVAS_WIDTH, height: CANVAS_HEIGHT },
      // Letterbox rect of the image inside the canvas, so the backend can crop
      // the canvas-sized mask back to the source image and align it pixel-wise.
      box: imageBox
        ? {
            x: Math.round(imageBox.x),
            y: Math.round(imageBox.y),
            width: Math.round(imageBox.width),
            height: Math.round(imageBox.height),
          }
        : null,
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

// Redraw when the theme flips so canvas colors follow the active palette.
watch(theme, () => drawScene())
onMounted(() => {
  drawScene()
  // A fresh editor mounts empty (mode toggles unmount/remount it via v-if); emit
  // the cleared state so the parent's status cards don't show stale 已上传/已选择.
  emitMaskState()
})
onBeforeUnmount(() => {
  undoStack = []
})

defineExpose({ exportPayload, clearMask })
</script>

<template>
  <section class="studio-panel rounded-lg p-4">
    <div class="mb-3 flex items-center justify-between">
      <div>
        <p class="text-xs font-bold uppercase tracking-[0.16em] text-[var(--studio-teal)]">Region Edit</p>
        <h3 class="mt-1 text-lg font-black">局部修改蒙版</h3>
      </div>
      <input ref="fileInputRef" class="hidden" type="file" accept="image/*" @change="loadImage" />
      <el-button :icon="Upload" @click="openFilePicker">上传原图</el-button>
    </div>

    <div class="mb-3 flex flex-wrap items-center justify-between gap-3">
      <div class="min-w-0 flex-1 truncate text-sm text-[var(--studio-muted)]">{{ fileName || '未选择图片' }}</div>
      <div class="flex items-center gap-2">
        <el-button-group>
          <el-button :type="tool === 'brush' ? 'primary' : 'default'" :icon="EditPen" title="涂抹" @click="tool = 'brush'">涂抹</el-button>
          <el-button :type="tool === 'rect' ? 'primary' : 'default'" :icon="Crop" title="框选" @click="tool = 'rect'">框选</el-button>
          <el-button :type="tool === 'eraser' ? 'primary' : 'default'" :icon="Remove" title="擦除" @click="tool = 'eraser'">擦除</el-button>
        </el-button-group>
        <el-button :icon="RefreshLeft" :disabled="!canUndo" title="撤销 (Ctrl+Z)" @click="undo">撤销</el-button>
        <el-button :icon="Delete" title="清除" @click="clearMask">清除</el-button>
      </div>
    </div>

    <div class="mb-3 grid grid-cols-[84px_1fr_52px] items-center gap-2 text-sm">
      <span class="font-semibold">画笔大小</span>
      <el-slider v-model="brushSize" :min="8" :max="120" :step="2" :disabled="tool === 'rect'" />
      <span class="text-right text-[var(--studio-muted)]">{{ brushSize }}px</span>
    </div>

    <canvas
      ref="canvasRef"
      tabindex="0"
      class="aspect-[720/520] w-full rounded-md border bg-[var(--studio-canvas)] outline-none transition-colors"
      :class="isDragOver ? 'border-[var(--studio-coral)]' : 'border-[var(--studio-line)]'"
      :style="{ cursor: cursorStyle }"
      @pointerdown.prevent="pointerDown"
      @pointermove.prevent="pointerMove"
      @pointerup.prevent="pointerUp"
      @pointerleave.prevent="pointerLeave"
      @keydown="onCanvasKeydown"
      @dragover.prevent="isDragOver = true"
      @dragleave.prevent="isDragOver = false"
      @drop.prevent="onDrop"
    />

    <p class="mt-3 text-xs leading-5 text-[var(--studio-muted)]">
      用涂抹/框选标记修改区域，擦除可去掉多余部分，<span class="font-semibold text-[var(--studio-ink)]">Ctrl+Z</span> 撤销。蒙版会随原图提交，OpenAI 兼容节点会自动对齐并反转透明区域。
    </p>
  </section>
</template>
