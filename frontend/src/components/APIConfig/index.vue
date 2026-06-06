<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { Delete, Edit, Plus, Rank, Refresh, SwitchButton } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { createConfig, deleteConfig, listConfigs, reorderConfigs, updateConfig } from '../../api/configs'

const configs = ref([])
const loading = ref(false)
const saving = ref(false)
const dragIndex = ref(null)
const editingId = ref(null)

const emptyForm = {
  name: '',
  base_url: '',
  api_key: '',
  model: 'gpt-image-2',
  status: true,
}

const form = reactive({ ...emptyForm })

const formTitle = computed(() => (editingId.value ? '编辑中转节点' : '新增中转节点'))
const enabledCount = computed(() => configs.value.filter((item) => item.status).length)

function resetForm() {
  Object.assign(form, emptyForm)
  editingId.value = null
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
  saving.value = true
  try {
    const payload = { ...form }
    if (editingId.value) {
      const updated = await updateConfig(editingId.value, payload)
      configs.value = configs.value.map((item) => (item.id === updated.id ? updated : item))
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

function editConfig(item) {
  editingId.value = item.id
  Object.assign(form, {
    name: item.name,
    base_url: item.base_url,
    api_key: item.api_key,
    model: item.model || 'gpt-image-2',
    status: Boolean(item.status),
  })
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
            <el-input v-model="form.api_key" show-password placeholder="sk-..." />
          </label>
          <label class="block">
            <span class="mb-1 block text-sm font-semibold">模型</span>
            <el-input v-model="form.model" placeholder="gpt-image-2" />
          </label>
          <div class="flex items-center justify-between rounded-md border border-[var(--studio-line)] bg-white/70 px-3 py-2">
            <span class="text-sm font-semibold">启用节点</span>
            <el-switch v-model="form.status" />
          </div>
        </div>

        <el-button class="mt-5 w-full" type="primary" native-type="submit" :loading="saving" :icon="editingId ? Edit : Plus">
          {{ editingId ? '保存修改' : '添加节点' }}
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

        <div v-else class="thin-scrollbar max-h-[620px] space-y-3 overflow-auto pr-1">
          <article
            v-for="(item, index) in configs"
            :key="item.id"
            draggable="true"
            class="grid grid-cols-[36px_minmax(0,1fr)_auto] gap-3 rounded-md border border-[var(--studio-line)] bg-white p-3 transition hover:border-[var(--studio-teal)]"
            @dragstart="onDragStart(index)"
            @dragover.prevent
            @drop="onDrop(index)"
          >
            <button class="flex h-9 w-9 cursor-grab items-center justify-center rounded-md border border-[var(--studio-line)] text-[var(--studio-muted)]" type="button" title="拖拽排序">
              <el-icon><Rank /></el-icon>
            </button>

            <div class="min-w-0">
              <div class="flex flex-wrap items-center gap-2">
                <h4 class="truncate text-base font-black">{{ item.name }}</h4>
                <el-tag size="small" :type="item.status ? 'success' : 'info'">{{ item.status ? '启用' : '禁用' }}</el-tag>
                <span class="text-xs font-semibold text-[var(--studio-amber)]">#{{ index + 1 }}</span>
              </div>
              <p class="mt-1 truncate text-sm text-[var(--studio-muted)]">{{ item.base_url }}</p>
              <p class="mt-1 text-xs text-[var(--studio-muted)]">{{ item.model }}</p>
            </div>

            <div class="flex items-center gap-2">
              <el-switch v-model="item.status" :active-icon="SwitchButton" @change="toggleStatus(item)" />
              <el-button :icon="Edit" circle title="编辑" @click="editConfig(item)" />
              <el-button :icon="Delete" circle title="删除" type="danger" @click="removeConfig(item)" />
            </div>
          </article>
        </div>
      </div>
    </div>
  </section>
</template>
