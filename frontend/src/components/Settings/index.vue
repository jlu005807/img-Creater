<script setup>
import { onMounted, reactive, ref } from 'vue'
import { Delete, Edit, Plus } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import APIConfig from '../APIConfig/index.vue'
import { useSettings } from '../../composables/useSettings'
import { usePromptTemplates } from '../../composables/usePromptTemplates'
import { useTheme } from '../../composables/useTheme'

const emit = defineEmits(['close'])

const activeTab = ref('api')
const { settings, resetSettings, DEFAULTS } = useSettings()
const { themeMode, setThemeMode } = useTheme()
const {
  templates,
  templatesLoading,
  loadTemplates,
  addTemplate,
  updateTemplate,
  removeTemplate,
  requestFill,
} = usePromptTemplates()

const tplForm = reactive({ id: null, title: '', text: '' })
const tplSaving = ref(false)

function editTpl(t) {
  tplForm.id = t.id
  tplForm.title = t.title
  tplForm.text = t.text
}

function resetTpl() {
  tplForm.id = null
  tplForm.title = ''
  tplForm.text = ''
}

async function saveTpl() {
  if (!tplForm.text.trim()) {
    ElMessage.warning('请输入模板内容')
    return
  }
  tplSaving.value = true
  try {
    if (tplForm.id) {
      await updateTemplate(tplForm.id, { title: tplForm.title.trim() || '未命名', text: tplForm.text.trim() })
      ElMessage.success('模板已更新')
    } else {
      await addTemplate({ title: tplForm.title, text: tplForm.text })
      ElMessage.success('模板已添加')
    }
    resetTpl()
  } catch (error) {
    ElMessage.error(error.message || '模板保存失败')
  } finally {
    tplSaving.value = false
  }
}

async function confirmRemoveTemplate(template) {
  try {
    await ElMessageBox.confirm(`删除模板「${template.title}」？`, '确认删除', {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消',
    })
    await removeTemplate(template.id)
    if (tplForm.id === template.id) resetTpl()
    ElMessage.success('模板已删除')
  } catch (error) {
    if (error !== 'cancel') ElMessage.error(error.message || '模板删除失败')
  }
}

onMounted(async () => {
  try {
    await loadTemplates()
  } catch (error) {
    ElMessage.error(error.message || '模板加载失败')
  }
})

function fillTpl(t) {
  requestFill(t.text)
  ElMessage.success('已填入提示词输入框')
  emit('close')
}
</script>

<template>
  <el-tabs v-model="activeTab">
    <!-- API 设置 -->
    <el-tab-pane label="API 设置" name="api">
      <APIConfig />
    </el-tab-pane>

    <!-- 偏好 -->
    <el-tab-pane label="偏好" name="prefs">
      <div class="max-w-lg space-y-5">
        <label class="block">
          <span class="mb-1 block text-sm font-semibold">提示词最大字数</span>
          <el-input-number v-model="settings.maxPromptChars" :min="1" :step="100" :value-on-clear="DEFAULTS.maxPromptChars" controls-position="right" />
          <p class="mt-1 text-xs text-[var(--studio-muted)]">默认 3000，超出后输入框不再接受更多字符。</p>
        </label>
        <label class="block">
          <span class="mb-1 block text-sm font-semibold">参考图上传上限</span>
          <el-input-number v-model="settings.maxReferenceImages" :min="1" :step="1" :value-on-clear="DEFAULTS.maxReferenceImages" controls-position="right" />
          <p class="mt-1 text-xs text-[var(--studio-muted)]">文生图时可上传的参考图数量上限，默认 3。</p>
        </label>
        <div class="block">
          <span class="mb-2 block text-sm font-semibold">主题</span>
          <div class="inline-grid grid-cols-3 rounded-md border border-[var(--studio-line)] bg-[var(--studio-surface)] p-1">
            <button
              v-for="opt in [
                { value: 'system', label: '跟随系统' },
                { value: 'light', label: '浅色' },
                { value: 'dark', label: '深色' },
              ]"
              :key="opt.value"
              type="button"
              class="h-9 rounded-[6px] px-3 text-sm font-semibold transition"
              :class="themeMode === opt.value ? 'bg-[var(--studio-solid)] text-[var(--studio-on-solid)]' : 'text-[var(--studio-muted)] hover:bg-[var(--studio-paper)]'"
              @click="setThemeMode(opt.value)"
            >
              {{ opt.label }}
            </button>
          </div>
        </div>
        <el-button text type="info" @click="resetSettings">恢复默认</el-button>
      </div>
    </el-tab-pane>

    <!-- 提示词模板 -->
    <el-tab-pane label="提示词模板" name="templates">
      <div class="grid grid-cols-[320px_minmax(0,1fr)] gap-4">
        <form class="space-y-3 rounded-md border border-[var(--studio-line)] p-4" @submit.prevent="saveTpl">
          <h4 class="text-sm font-black">{{ tplForm.id ? '编辑模板' : '新增模板' }}</h4>
          <el-input v-model="tplForm.title" placeholder="模板名称（可选）" />
          <el-input v-model="tplForm.text" type="textarea" :rows="6" resize="none" placeholder="模板内容…" />
          <div class="flex gap-2">
            <el-button type="primary" native-type="submit" :loading="tplSaving" :icon="tplForm.id ? Edit : Plus">
              {{ tplForm.id ? '保存' : '添加' }}
            </el-button>
            <el-button v-if="tplForm.id" text @click="resetTpl">取消</el-button>
          </div>
        </form>

        <div v-loading="templatesLoading" class="thin-scrollbar max-h-[420px] space-y-2 overflow-auto pr-1">
          <div v-if="!templates.length && !templatesLoading" class="flex min-h-[120px] items-center justify-center text-sm text-[var(--studio-muted)]">
            暂无模板
          </div>
          <article
            v-for="t in templates"
            :key="t.id"
            class="rounded-md border border-[var(--studio-line)] bg-[var(--studio-surface)] p-3"
          >
            <div class="mb-1 flex items-center justify-between gap-2">
              <h5 class="truncate text-sm font-bold text-[var(--studio-ink)]">{{ t.title }}</h5>
              <div class="flex shrink-0 items-center gap-1">
                <el-button size="small" text type="primary" @click="fillTpl(t)">填入</el-button>
                <el-button size="small" text :icon="Edit" :aria-label="`编辑模板 ${t.title}`" @click="editTpl(t)" />
                <el-button size="small" text type="danger" :icon="Delete" :aria-label="`删除模板 ${t.title}`" @click="confirmRemoveTemplate(t)" />
              </div>
            </div>
            <p class="line-clamp-2 text-xs leading-5 text-[var(--studio-muted)]">{{ t.text }}</p>
          </article>
        </div>
      </div>
    </el-tab-pane>
  </el-tabs>
</template>

<style scoped>
.line-clamp-2 {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
</style>
