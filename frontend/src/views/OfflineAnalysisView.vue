<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { ChevronLeft, ChevronRight, Download, Play, UploadCloud } from 'lucide-vue-next'
import { cancelOfflineJob, createOfflineJob, getOfflineJob, getOfflineJobs, type OfflineJob } from '../api'

const files = ref<File[]>([])
const previewFiles = ref<File[]>([])
const previewIndex = ref(0)
const jobs = ref<OfflineJob[]>([])
const selected = ref<OfflineJob | null>(null)
const submitting = ref(false)
const form = reactive({ task: 'full_pipeline', enableTracking: false, frameStep: 1, maxFrames: undefined as number | undefined, visualize: true })
const taskOptions = [
  { value: 'full_pipeline', label: '完整流水线' },
  { value: 'region_detection', label: '区域检测' },
  { value: 'ship_name_recognition', label: '船名识别' },
  { value: 'draft_estimation', label: '吃水估计' },
]
const jobStatusLabels: Record<string, string> = { queued: '排队中', running: '处理中', completed: '已完成', failed: '失败', cancelled: '已取消', uploading: '上传中' }
const active = computed(() => selected.value && ['queued', 'running'].includes(selected.value.status))
const resultUrl = computed(() => selected.value ? `/api/jobs/${selected.value.id}/files/result.csv` : '')
const previewFile = computed(() => previewFiles.value[previewIndex.value] || null)
const previewItem = computed(() => selected.value?.items[previewIndex.value] || null)
const originalSource = ref('')
const resultSource = computed(() => selected.value?.status === 'completed' && previewItem.value?.visual_uri ? `/api/jobs/${selected.value.id}/files/${previewItem.value.visual_uri}` : '')
const previewVideo = computed(() => /\.(mp4|avi|mov|mkv|m4v|wmv|flv|ts)$/i.test(previewItem.value?.filename || previewFile.value?.name || ''))
const statusLabel = computed(() => jobStatusLabels[selected.value?.status || ''] || '未开始')

let originalObjectUrl = ''
watch(previewFile, (file) => {
  if (originalObjectUrl) URL.revokeObjectURL(originalObjectUrl)
  originalObjectUrl = file ? URL.createObjectURL(file) : ''
  originalSource.value = originalObjectUrl
}, { immediate: true })

function selectFiles(event: Event) {
  files.value = Array.from((event.target as HTMLInputElement).files || [])
  previewFiles.value = [...files.value]
  previewIndex.value = 0
}
async function refresh() {
  jobs.value = await getOfflineJobs()
  if (selected.value) selected.value = await getOfflineJob(selected.value.id)
}
async function submit() {
  if (!files.value.length) return ElMessage.warning('请选择一个或多个图片/视频文件')
  const body = new FormData()
  files.value.forEach(file => body.append('files', file))
  body.append('task', form.task)
  body.append('enable_tracking', String(form.enableTracking))
  body.append('frame_step', String(form.frameStep))
  body.append('visualize', String(form.visualize))
  if (form.maxFrames) body.append('max_frames', String(form.maxFrames))
  submitting.value = true
  try {
    selected.value = await createOfflineJob(body)
    await refresh()
  } catch (error: any) {
    ElMessage.error(error?.response?.data?.detail || '任务创建失败')
  } finally {
    submitting.value = false
  }
}
async function cancel() { if (selected.value) selected.value = await cancelOfflineJob(selected.value.id) }
async function selectJob(row: OfflineJob) { selected.value = await getOfflineJob(row.id) }
let timer: number | undefined
onMounted(() => { refresh(); timer = window.setInterval(refresh, 1500) })
onBeforeUnmount(() => {
  if (timer) clearInterval(timer)
  if (originalObjectUrl) URL.revokeObjectURL(originalObjectUrl)
})
</script>

<template>
  <div class="page offline-page">
    <section class="panel upload-panel">
      <header class="panel-header">
        <div><span class="section-kicker">V2 批量任务</span><h2>创建离线分析任务</h2></div>
        <UploadCloud :size="20" aria-hidden="true" />
      </header>

      <el-form label-position="top" class="job-form">
        <div class="primary-fields">
          <el-form-item label="推理任务">
            <el-select v-model="form.task" aria-label="推理任务"><el-option v-for="item in taskOptions" :key="item.value" :label="item.label" :value="item.value" /></el-select>
          </el-form-item>
          <el-form-item label="输入文件" class="file-field">
            <label class="file-picker" :class="{ selected: files.length }">
              <input type="file" multiple accept="image/*,video/*" @change="selectFiles" />
              <UploadCloud :size="18" aria-hidden="true" />
              <span>{{ files.length ? `已选择 ${files.length} 个文件` : '选择图片或视频' }}</span>
              <small>{{ files.length ? files[0].name : '支持多选；最近一次选择会保留' }}</small>
            </label>
          </el-form-item>
        </div>

        <fieldset class="advanced-options">
          <legend>视频与输出选项</legend>
          <div class="option-grid">
            <el-form-item label="视频跟踪" class="switch-field"><el-switch v-model="form.enableTracking" /><span>聚合同一船舶的历史预测</span></el-form-item>
            <el-form-item label="采样间隔"><el-input-number v-model="form.frameStep" :min="1" :max="1000" aria-label="视频采样间隔" /></el-form-item>
            <el-form-item label="最大处理帧"><el-input-number v-model="form.maxFrames" :min="1" placeholder="不限制" aria-label="最大处理帧" /></el-form-item>
            <el-form-item label="生成可视化" class="switch-field"><el-switch v-model="form.visualize" /><span>输出 JPG 或浏览器可播放的视频</span></el-form-item>
          </div>
        </fieldset>

        <div class="submit-row">
          <p>任务将按顺序使用 GPU；创建后仍可直接复用当前文件。</p>
          <el-button type="primary" :loading="submitting" :disabled="!files.length" @click="submit"><Play :size="16" />创建任务</el-button>
        </div>
      </el-form>
    </section>

    <section class="panel result-panel">
      <header class="panel-header result-header">
        <div><span class="section-kicker">任务结果</span><h2>{{ selected?.id || '尚未选择任务' }}</h2></div>
        <div class="result-actions"><span v-if="selected" class="status-chip" :class="selected.status">{{ statusLabel }} · {{ selected.progress }}%</span><a v-if="selected?.status === 'completed'" :href="resultUrl"><Download :size="16" />下载精简 CSV</a></div>
      </header>
      <div v-if="selected || previewFile" class="result-body">
        <div class="preview-grid">
          <section class="preview-panel"><h3>原始数据</h3><video v-if="originalSource && previewVideo" :src="originalSource" controls preload="metadata"></video><img v-else-if="originalSource" :src="originalSource" alt="原始上传数据" /><div v-else class="preview-empty">选择文件后显示原图或原视频</div></section>
          <section class="preview-panel"><h3>结果可视化</h3><video v-if="resultSource && previewVideo" :src="resultSource" controls preload="metadata"></video><img v-else-if="resultSource" :src="resultSource" alt="推理可视化结果" /><div v-else class="preview-empty">任务完成后在此显示可视化结果</div></section>
        </div>
        <div v-if="previewFiles.length > 1" class="preview-pager"><el-button circle aria-label="上一文件" :disabled="previewIndex === 0" @click="previewIndex--"><ChevronLeft :size="16" /></el-button><span>{{ previewIndex + 1 }} / {{ previewFiles.length }}</span><el-button circle aria-label="下一文件" :disabled="previewIndex >= previewFiles.length - 1" @click="previewIndex++"><ChevronRight :size="16" /></el-button></div>
        <p v-if="selected?.error" class="error-text">错误：{{ selected.error }}</p>
        <el-button v-if="active" type="danger" plain @click="cancel">取消任务</el-button>
        <el-table v-if="selected" :data="selected.items" class="result-table"><el-table-column prop="filename" label="文件" /><el-table-column prop="status" label="状态" width="100" /><el-table-column prop="progress" label="进度" width="90" /><el-table-column label="详细结果" width="120"><template #default="scope"><a v-if="scope.row.result_uri" :href="`/api/jobs/${selected?.id}/files/${scope.row.result_uri}`" target="_blank">JSON/JSONL</a></template></el-table-column></el-table>
      </div>
      <div v-else class="result-empty">选择本地文件后，可在这里并列查看原始数据与可视化结果。</div>
    </section>

    <section class="panel job-list-panel"><header class="panel-header"><div><span class="section-kicker">任务队列</span><h2>历史任务</h2></div></header><el-table :data="jobs" @row-click="selectJob"><el-table-column prop="created_at" label="提交时间" /><el-table-column prop="id" label="任务" /><el-table-column prop="status" label="状态" /><el-table-column prop="progress" label="进度" /></el-table></section>
  </div>
</template>

<style scoped>
.offline-page { display: grid; gap: 16px; }
.upload-panel { overflow: visible; }
.job-form { padding: 16px; }
.primary-fields, .option-grid { display: grid; gap: 16px; }
.primary-fields { grid-template-columns: minmax(210px, .75fr) minmax(330px, 1.25fr); align-items: start; }
.job-form :deep(.el-form-item) { margin-bottom: 0; }
.file-picker { min-height: 42px; display: grid; grid-template-columns: auto minmax(0, 1fr); align-items: center; column-gap: 9px; padding: 9px 12px; border: 1px dashed var(--el-border-color); border-radius: 8px; color: var(--el-text-color-regular); cursor: pointer; transition: border-color .2s ease, background-color .2s ease; }
.file-picker:hover, .file-picker:focus-within { border-color: var(--el-color-primary); background: color-mix(in srgb, var(--el-color-primary) 8%, transparent); outline: none; }
.file-picker.selected { border-style: solid; }
.file-picker input { position: absolute; width: 1px; height: 1px; opacity: 0; }
.file-picker span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 600; }
.file-picker small { grid-column: 2; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--el-text-color-secondary); }
.advanced-options { min-width: 0; margin: 18px 0 0; padding: 13px 14px 15px; border: 1px solid var(--el-border-color-lighter); border-radius: 8px; }
.advanced-options legend { padding: 0 7px; color: var(--el-text-color-secondary); font-size: 13px; }
.option-grid { grid-template-columns: repeat(4, minmax(140px, 1fr)); align-items: end; }
.switch-field :deep(.el-form-item__content) { min-height: 32px; gap: 8px; }
.switch-field span { color: var(--el-text-color-secondary); font-size: 12px; line-height: 1.35; }
.submit-row { display: flex; justify-content: space-between; align-items: center; gap: 16px; padding-top: 16px; }
.submit-row p { margin: 0; color: var(--el-text-color-secondary); font-size: 13px; }
.submit-row .el-button { min-width: 124px; min-height: 40px; }
.result-header { align-items: center; }
.result-actions { display: flex; align-items: center; gap: 12px; }
.status-chip { padding: 4px 8px; border-radius: 999px; background: var(--el-fill-color); color: var(--el-text-color-secondary); font-size: 12px; font-variant-numeric: tabular-nums; }
.status-chip.completed { color: var(--el-color-success); }.status-chip.failed { color: var(--el-color-danger); }.status-chip.running { color: var(--el-color-primary); }
.result-body { padding: 0 16px 16px; }
.preview-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }
.preview-panel { min-height: 300px; border: 1px solid var(--el-border-color); border-radius: 8px; overflow: hidden; background: #0b1115; }
.preview-panel h3 { margin: 0; padding: 10px 14px; font-size: 14px; border-bottom: 1px solid var(--el-border-color); }
.preview-panel img, .preview-panel video { display: block; width: 100%; height: 300px; object-fit: contain; background: #05080a; }
.preview-empty { display: grid; min-height: 300px; place-items: center; padding: 18px; text-align: center; color: var(--el-text-color-secondary); }
.preview-pager { display: flex; gap: 12px; align-items: center; justify-content: center; margin: 12px 0; font-variant-numeric: tabular-nums; }
.result-table { margin-top: 14px; }.error-text { color: var(--el-color-danger); }.result-empty { min-height: 260px; display: grid; place-items: center; padding: 24px; color: var(--el-text-color-secondary); text-align: center; }
@media (max-width: 1050px) { .option-grid { grid-template-columns: repeat(2, minmax(180px, 1fr)); } }
@media (max-width: 760px) { .primary-fields, .option-grid, .preview-grid { grid-template-columns: 1fr; } .submit-row { align-items: stretch; flex-direction: column; } .submit-row .el-button { width: 100%; } .result-header, .result-actions { align-items: flex-start; flex-direction: column; } .preview-panel img, .preview-panel video, .preview-empty { min-height: 220px; height: 220px; } }
</style>
