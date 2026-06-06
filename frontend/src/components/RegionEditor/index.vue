<script setup>
import { nextTick, ref } from 'vue'
import { Crop, Delete, EditPen, Upload } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'

const emit = defineEmits(['mask-change'])

const CANVAS_WIDTH = 720
const CANVAS_HEIGHT = 520

const canvasRef = ref(null)
const fileInputRef = ref(null)
const fileName = ref('')
const tool = ref('brush')
const brushSize = ref(42)
const imageDataUrl = ref('')
const hasMask = ref(false)

let imageElement = null
let imageBox = null
let maskCanvas = null
let maskContext = null
let drawing = false
let startPoint = null
let lastPoint = null
let previewRect = null

function openFilePicker() {
  fileInputRef.value?.click()
}

function loadImage(event) {
  const file = event.target.files?.[0]
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
  event.target.value = ''
}

function initMaskCanvas() {
  maskCanvas = document.createElement('canvas')
  maskCanvas.width = CANVAS_WIDTH
  maskCanvas.height = CANVAS_HEIGHT
  maskContext = maskCanvas.getContext('2d')
  maskContext.clearRect(0, 0, CANVAS_WIDTH, CANVAS_HEIGHT)
  hasMask.value = false
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
  ctx.clearRect(0, 0, CANVAS_WIDTH, CANVAS_HEIGHT)
  ctx.fillStyle = canvasBg
  ctx.fillRect(0, 0, CANVAS_WIDTH, CANVAS_HEIGHT)

  if (!imageElement) {
    ctx.fillStyle = mutedColor
    ctx.font = '600 18px Aptos, sans-serif'
    ctx.textAlign = 'center'
    ctx.fillText('上传一张需要局部修改的图片', CANVAS_WIDTH / 2, CANVAS_HEIGHT / 2)
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
  ctx.fillStyle = '#0f8f8c'
  ctx.fillRect(0, 0, CANVAS_WIDTH, CANVAS_HEIGHT)
  ctx.restore()

  if (previewRect) {
    ctx.save()
    ctx.strokeStyle = '#d96b4d'
    ctx.lineWidth = 2
    ctx.setLineDash([8, 5])
    ctx.strokeRect(previewRect.x, previewRect.y, previewRect.width, previewRect.height)
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

function pointerDown(event) {
  if (!imageElement) return
  canvasRef.value.setPointerCapture?.(event.pointerId)
  drawing = true
  startPoint = getCanvasPoint(event)
  lastPoint = startPoint

  if (tool.value === 'brush') {
    paintLine(startPoint, startPoint)
  }
}

function pointerMove(event) {
  if (!drawing || !imageElement) return
  const point = getCanvasPoint(event)
  if (tool.value === 'brush') {
    paintLine(lastPoint, point)
    lastPoint = point
    drawScene()
    return
  }

  previewRect = normalizeRect(startPoint, point)
  drawScene()
}

function pointerUp(event) {
  if (!drawing || !imageElement) return
  const point = getCanvasPoint(event)
  if (tool.value === 'rect') {
    const rect = normalizeRect(startPoint, point)
    if (rect.width > 4 && rect.height > 4) {
      maskContext.fillStyle = 'rgba(255,255,255,1)'
      maskContext.fillRect(rect.x, rect.y, rect.width, rect.height)
      hasMask.value = true
    }
  }
  drawing = false
  startPoint = null
  lastPoint = null
  previewRect = null
  drawScene()
  emitMaskState()
}

function paintLine(from, to) {
  maskContext.save()
  maskContext.strokeStyle = 'rgba(255,255,255,1)'
  maskContext.lineWidth = brushSize.value
  maskContext.lineCap = 'round'
  maskContext.lineJoin = 'round'
  maskContext.beginPath()
  maskContext.moveTo(from.x, from.y)
  maskContext.lineTo(to.x, to.y)
  maskContext.stroke()
  maskContext.restore()
  hasMask.value = true
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
  maskContext.clearRect(0, 0, CANVAS_WIDTH, CANVAS_HEIGHT)
  hasMask.value = false
  previewRect = null
  drawScene()
  emitMaskState()
}

function emitMaskState() {
  emit('mask-change', { hasImage: Boolean(imageDataUrl.value), hasMask: hasMask.value })
}

function getMaskBoundingBox() {
  if (!maskContext || !hasMask.value) return null
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

    <div class="mb-3 grid grid-cols-[1fr_auto_auto] items-center gap-3">
      <div class="truncate text-sm text-[var(--studio-muted)]">{{ fileName || '未选择图片' }}</div>
      <el-button-group>
        <el-button :type="tool === 'brush' ? 'primary' : 'default'" :icon="EditPen" @click="tool = 'brush'">涂抹</el-button>
        <el-button :type="tool === 'rect' ? 'primary' : 'default'" :icon="Crop" @click="tool = 'rect'">框选</el-button>
      </el-button-group>
      <el-button :icon="Delete" @click="clearMask">清除</el-button>
    </div>

    <div class="mb-3 grid grid-cols-[84px_1fr_52px] items-center gap-2 text-sm">
      <span class="font-semibold">画笔大小</span>
      <el-slider v-model="brushSize" :min="8" :max="120" :step="2" :disabled="tool !== 'brush'" />
      <span class="text-right text-[var(--studio-muted)]">{{ brushSize }}px</span>
    </div>

    <canvas
      ref="canvasRef"
      class="h-[520px] w-full rounded-md border border-[var(--studio-line)] bg-[var(--studio-canvas)]"
      @pointerdown.prevent="pointerDown"
      @pointermove.prevent="pointerMove"
      @pointerup.prevent="pointerUp"
      @pointerleave.prevent="pointerUp"
    />

    <p class="mt-3 text-xs leading-5 text-[var(--studio-muted)]">
      蒙版区域会作为 <span class="font-semibold text-[var(--studio-ink)]">mask</span> 随原图一起提交给后端，后端继续按 API 节点优先级进行容灾提交。
    </p>
  </section>
</template>
