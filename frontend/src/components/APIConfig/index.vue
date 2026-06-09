<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { Delete, Edit, Hide, Plus, Rank, Refresh, SwitchButton, View } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { createConfig, deleteConfig, getConfigSecret, listConfigs, reorderConfigs, updateConfig } from '../../api/configs'
import { backendRouteMissingMessage, isBackendRouteMissing } from '../../api/client'

const configs = ref([])
const loading = ref(false)
const saving = ref(false)
const dragIndex = ref(null)
const editingId = ref(null)
const secretLoadingId = ref(null)
const editingSecretVisible = ref(false)
const revealedKeys = reactive({})

function newEmptyForm() {
  return {
    name: '',
    base_url: '',
    api_key: '',
    model: 'gpt-image-2',
    api_type: 'auto',
    status: true,
    auto_mode: true,
    timeout_seconds: 30,
    retry_count: 0,
  }
}

const form = reactive(newEmptyForm())
const editingKeyPreview = ref('')

const apiTypeOptions = [
  { value: 'auto', label: '自动尝试协议', hint: '在当前节点内依次尝试 Images / async / Chat' },
  { value: 'openai', label: 'OpenAI 兼容', hint: '标准 /v1/images 接口（推荐）' },
  { value: 'chat', label: 'Chat Completions', hint: 'POST /v1/chat/completions（部分代理用此端点）' },
  { value: 'custom', label: '自定义 URL', hint: '直接 POST 到填写的完整地址，不拼接任何路径' },
  { value: 'async', label: '异步中转', hint: '自定义 /async/images 提交 + 轮询协议' },
]

const formTitle = computed(() => (editingId.value ? '编辑 API 节点' : '新增 API 节点'))
const enabledCount = computed(() => configs.value.filter((item) => item.status).length)
const apiTypeLabel = (value) => {
  if (value === 'auto') return '自动尝试协议'
  if (value === 'async') return '异步中转'
  if (value === 'custom') return '自定义 URL'
  if (value === 'chat') return 'Chat 补全'
  return 'OpenAI 兼容'
}
const endpointHint = computed(() => {
  const base = (form.base_url || '').trim().replace(/\/+$/, '')
  if (!base) return ''
  if (form.api_type === 'custom') return `直接请求：${base}`
  if (form.api_type === 'async') return `提交：${base}/async/images`
  if (form.api_type === 'chat') return `请求：${base}/v1/chat/completions`
  const root = base.endsWith('/v1') ? base : `${base}/v1`
  if (form.api_type === 'auto') return `自动：当前节点内尝试 ${root}/images/generations、${base}/async/images、${root}/chat/completions`
  return `请求：${root}/images/generations`
})

function resetForm() {
  Object.assign(form, newEmptyForm())
  editingId.value = null
  editingKeyPreview.value = ''
  editingSecretVisible.value = false
}

async function loadConfigs() {
  loading.value = true
  try {
    configs.value = await listConfigs()
  } catch (error) {
    ElMessage.error(error.message || '加载配置失败')
  } finally {
    loading.value = false
  }
}

async function submitForm() {
  if (!editingId.value && !form.api_key.trim()) {
    ElMessage.warning('请填写 API Key')
    return
  }
  saving.value = true
  try {
    const payload = buildPayload()
    // The real key is never sent back to the browser, so on edit a blank key
    // means "keep the existing one" — omit it instead of sending an empty value.
    if (editingId.value && !payload.api_key.trim()) {
      delete payload.api_key
    }
    if (editingId.value) {
      const updated = await updateConfig(editingId.value, payload)
      configs.value = configs.value.map((item) => (item.id === updated.id ? updated : item))
      delete revealedKeys[updated.id]
      ElMessage.success('配置已更新')
    } else {
      const created = await createConfig(payload)
      configs.value.push(created)
      ElMessage.success('配置已创建')
    }
    resetForm()
  } catch (error) {
    ElMessage.error(error.message || '保存配置失败')
  } finally {
    saving.value = false
  }
}

function buildPayload() {
  return {
    name: form.name,
    base_url: form.base_url,
    api_key: form.api_key,
    model: form.model,
    api_type: form.api_type,
    status: form.status,
    auto_mode: true,
    internal_auto_mode: false,
    timeout_seconds: form.timeout_seconds,
    retry_count: form.retry_count,
    modes: [],
  }
}

function editConfig(item) {
  editingId.value = item.id
  editingKeyPreview.value = item.api_key_preview || ''
  editingSecretVisible.value = false
  Object.assign(form, {
    name: item.name,
    base_url: item.base_url,
    api_key: '', // never prefilled — the backend only returns a masked preview
    model: item.model || 'gpt-image-2',
    api_type: item.api_type || 'auto',
    status: Boolean(item.status),
    auto_mode: item.auto_mode !== false,
    timeout_seconds: item.timeout_seconds || 30,
    retry_count: item.retry_count || 0,
  })
}

async function selectConfig(item) {
  if (editingId.value === item.id) {
    resetForm()
    return
  }
  editConfig(item)
  await loadEditingKey({ visible: false })
}

async function recoverMissingConfig(itemId) {
  delete revealedKeys[itemId]
  if (editingId.value === itemId) resetForm()
  ElMessage.warning('该 API 节点不存在或服务未刷新，已重新加载节点列表')
  await loadConfigs()
}

async function handleSecretNotFound(itemId) {
  delete revealedKeys[itemId]
  let latest = []
  try {
    latest = await listConfigs()
    configs.value = latest
  } catch {
    latest = configs.value
  }
  if (latest.some((item) => item.id === itemId)) {
    ElMessage.error(backendRouteMissingMessage('API Key 查看'))
    return
  }
  await recoverMissingConfig(itemId)
}

async function toggleSecret(item) {
  if (revealedKeys[item.id]) {
    delete revealedKeys[item.id]
    return
  }
  secretLoadingId.value = item.id
  try {
    const secret = await getConfigSecret(item.id)
    revealedKeys[item.id] = secret.api_key || ''
  } catch (error) {
    if (isBackendRouteMissing(error)) {
      await handleSecretNotFound(item.id)
      return
    }
    ElMessage.error(error.message || '读取 API Key 失败')
  } finally {
    secretLoadingId.value = null
  }
}

async function loadEditingKey({ visible }) {
  if (!editingId.value) return
  secretLoadingId.value = editingId.value
  try {
    const secret = await getConfigSecret(editingId.value)
    form.api_key = secret.api_key || ''
    editingSecretVisible.value = visible
  } catch (error) {
    if (isBackendRouteMissing(error)) {
      await handleSecretNotFound(editingId.value)
      return
    }
    ElMessage.error(error.message || '读取 API Key 失败')
  } finally {
    secretLoadingId.value = null
  }
}

async function revealEditingKey() {
  if (form.api_key) {
    editingSecretVisible.value = true
    return
  }
  await loadEditingKey({ visible: true })
}

function hideEditingKey() {
  editingSecretVisible.value = false
}

async function removeConfig(item) {
  try {
    await ElMessageBox.confirm(`删除节点「${item.name}」？`, '确认删除', {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消',
    })
    await deleteConfig(item.id)
    configs.value = configs.value.filter((config) => config.id !== item.id)
    ElMessage.success('配置已删除')
  } catch (error) {
    if (error !== 'cancel') ElMessage.error(error.message || '删除失败')
  }
}

async function toggleStatus(item) {
  try {
    const updated = await updateConfig(item.id, { status: item.status })
    configs.value = configs.value.map((config) => (config.id === updated.id ? updated : config))
  } catch (error) {
    item.status = !item.status
    ElMessage.error(error.message || '状态更新失败')
  }
}

function onDragStart(index) {
  dragIndex.value = index
}

async function onDrop(targetIndex) {
  if (dragIndex.value === null || dragIndex.value === targetIndex) {
    dragIndex.value = null
    return
  }

  const next = [...configs.value]
  const [moved] = next.splice(dragIndex.value, 1)
  next.splice(targetIndex, 0, moved)
  configs.value = next
  dragIndex.value = null

  try {
    configs.value = await reorderConfigs(configs.value.map((item) => item.id))
    ElMessage.success('优先级已保存')
  } catch (error) {
    ElMessage.error(error.message || '排序保存失败')
    await loadConfigs()
  }
}

onMounted(loadConfigs)
</script>

<template>
  <section class="space-y-4">
    <div class="studio-panel rounded-lg p-5">
      <div class="flex items-start justify-between gap-3">
        <div>
          <p class="text-xs font-semibold uppercase tracking-[0.16em] text-[var(--studio-teal)]">Provider Routing</p>
          <h2 class="mt-1 text-2xl font-black">API 节点管理</h2>
          <p class="mt-2 text-sm text-[var(--studio-muted)]">拖拽列表调整优先级；后端会按从上到下的顺序执行容灾切换。</p>
        </div>
        <el-button :icon="Refresh" @click="loadConfigs">刷新</el-button>
      </div>
    </div>

    <div class="grid grid-cols-[430px_minmax(0,1fr)] gap-4">
      <form class="studio-panel rounded-lg p-5" @submit.prevent="submitForm">
        <div class="mb-4 flex items-center justify-between">
          <h3 class="text-lg font-black">{{ formTitle }}</h3>
          <el-button v-if="editingId" text @click="resetForm">取消编辑</el-button>
        </div>

        <div class="space-y-4">
          <label class="block">
            <span class="mb-1 block text-sm font-semibold">名称</span>
            <el-input v-model="form.name" placeholder="例如 fnuu 主线路" />
          </label>
          <label class="block">
            <span class="mb-1 block text-sm font-semibold">Base URL</span>
            <el-input v-model="form.base_url" placeholder="https://fnuu.net" />
          </label>
          <label class="block">
            <span class="mb-1 block text-sm font-semibold">API Key</span>
            <el-input
              v-if="editingId"
              v-model="form.api_key"
              :type="editingSecretVisible ? 'text' : 'password'"
              :placeholder="editingKeyPreview || '输入新的 API Key；留空保存则不修改'"
            >
              <template #append>
                <el-button
                  :icon="editingSecretVisible ? Hide : View"
                  :loading="secretLoadingId === editingId"
                  @click="editingSecretVisible ? hideEditingKey() : revealEditingKey()"
                  :title="editingSecretVisible ? '隐藏 Key' : '显示 Key'"
                  :aria-label="editingSecretVisible ? '隐藏 API Key' : '显示 API Key'"
                />
              </template>
            </el-input>
            <el-input
              v-else
              v-model="form.api_key"
              show-password
              placeholder="sk-..."
            />
          </label>
          <label class="block">
            <span class="mb-1 block text-sm font-semibold">模型</span>
            <el-input v-model="form.model" placeholder="gpt-image-2" />
          </label>
          <label class="block">
            <span class="mb-1 block text-sm font-semibold">接入协议</span>
            <el-select v-model="form.api_type" class="w-full">
              <el-option v-for="opt in apiTypeOptions" :key="opt.value" :label="opt.label" :value="opt.value">
                <span class="font-semibold">{{ opt.label }}</span>
                <span class="ml-2 text-xs text-[var(--studio-muted)]">{{ opt.hint }}</span>
              </el-option>
            </el-select>
            <p v-if="endpointHint" class="mt-1 break-all text-xs text-[var(--studio-muted)]">实际请求：{{ endpointHint }}</p>
          </label>
          <div class="flex items-center justify-between rounded-md border border-[var(--studio-line)] bg-[var(--studio-surface-soft)] px-3 py-2">
            <span class="text-sm font-semibold">启用节点</span>
            <el-switch v-model="form.status" />
          </div>
          <div class="grid grid-cols-2 gap-3">
            <label class="block">
              <span class="mb-1 block text-sm font-semibold">超时时间（秒）</span>
              <el-input-number v-model="form.timeout_seconds" :min="1" :step="1" step-strictly controls-position="right" class="w-full" />
            </label>
            <label class="block">
              <span class="mb-1 block text-sm font-semibold">重试次数</span>
              <el-input-number v-model="form.retry_count" :min="0" :step="1" controls-position="right" class="w-full" />
            </label>
          </div>
        </div>

        <el-button class="mt-5 w-full" type="primary" native-type="submit" :loading="saving" :icon="editingId ? Edit : Plus">
          保存节点
        </el-button>
      </form>

      <div class="studio-panel min-h-[420px] rounded-lg p-5">
        <div class="mb-4 flex items-center justify-between gap-2">
          <div>
            <h3 class="text-lg font-black">调用优先级</h3>
            <p class="text-sm text-[var(--studio-muted)]">{{ enabledCount }} 个启用节点</p>
          </div>
          <el-tag type="warning" effect="plain">拖拽排序</el-tag>
        </div>

        <el-skeleton v-if="loading" animated :rows="5" />

        <div v-else-if="!configs.length" class="flex min-h-[280px] items-center justify-center rounded-md border border-dashed border-[var(--studio-line)] text-sm text-[var(--studio-muted)]">
          暂无 API 节点
        </div>

        <div v-else class="thin-scrollbar flex max-h-[620px] flex-col gap-2 overflow-auto pr-1">
          <article
            v-for="(item, index) in configs"
            :key="item.id"
            draggable="true"
            class="grid cursor-pointer grid-cols-[36px_minmax(0,1fr)_auto] gap-3 rounded-md border bg-[var(--studio-surface)] p-3 transition hover:border-[var(--studio-teal)]"
            :class="editingId === item.id ? 'border-[var(--studio-teal)] ring-1 ring-[var(--studio-teal)]' : 'border-[var(--studio-line)]'"
            @click="selectConfig(item)"
            @dragstart="onDragStart(index)"
            @dragover.prevent
            @drop="onDrop(index)"
          >
            <button class="flex h-9 w-9 cursor-grab items-center justify-center rounded-md border border-[var(--studio-line)] text-[var(--studio-muted)]" type="button" title="拖拽排序" aria-label="拖拽排序">
              <el-icon><Rank /></el-icon>
            </button>

            <div class="min-w-0">
              <div class="flex flex-wrap items-center gap-2">
                <h4 class="truncate text-base font-black">{{ item.name }}</h4>
                <el-tag size="small" :type="item.status ? 'success' : 'info'">{{ item.status ? '启用' : '禁用' }}</el-tag>
                <span class="text-xs font-semibold text-[var(--studio-amber)]">#{{ index + 1 }}</span>
              </div>
              <p class="mt-1 truncate text-sm text-[var(--studio-muted)]">{{ item.base_url }}</p>
              <p class="mt-1 flex items-center gap-2 text-xs text-[var(--studio-muted)]">
                <span>Key {{ revealedKeys[item.id] || item.api_key_preview || '********' }}</span>
              </p>
              <p class="mt-1 text-xs text-[var(--studio-muted)]">{{ item.model }} · {{ apiTypeLabel(item.api_type) }}</p>
              <p class="mt-1 text-xs text-[var(--studio-muted)]">
                失败继续后续节点 · {{ item.timeout_seconds || 30 }}s · 重试 {{ item.retry_count || 0 }}
              </p>
            </div>

            <div class="flex items-center gap-2" @click.stop>
              <el-switch v-model="item.status" :active-icon="SwitchButton" @change="toggleStatus(item)" />
              <el-button
                :icon="revealedKeys[item.id] ? Hide : View"
                circle
                title="显示或隐藏 Key"
                :loading="secretLoadingId === item.id"
                @click="toggleSecret(item)"
              />
              <el-button :icon="Edit" circle title="编辑" :aria-label="`编辑节点 ${item.name}`" @click="selectConfig(item)" />
              <el-button :icon="Delete" circle title="删除" type="danger" :aria-label="`删除节点 ${item.name}`" @click="removeConfig(item)" />
            </div>
          </article>
        </div>
      </div>
    </div>
  </section>
</template>
