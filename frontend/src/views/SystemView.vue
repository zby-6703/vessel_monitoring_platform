<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus'
import { AlertCircle, CheckCircle2, CircleOff, Cpu, Database, RadioTower, RefreshCw, Server, Settings2 } from 'lucide-vue-next'
import { onMounted, ref } from 'vue'
import { getHealth, getModelSettings, getStreams, resetModelSettings, updateModelSettings, type Health, type ModelSettings, type StreamItem } from '../api'

const health = ref<Health | null>(null)
const stream = ref<StreamItem | null>(null)
const loading = ref(false)
const settingsOpen = ref(false)
const saving = ref(false)
const modelSettings = ref<ModelSettings | null>(null)
const modelForm = ref<Record<string, string>>({})
const formError = ref('')
const names: Record<string, string> = { ship_detection: '船舶/吃水区域/船名检测模型', draft_multitask: 'DraftFormer 多任务模型', ship_name_recognition: '船名识别模型' }
const modelGroups = [
  { label: '船舶/吃水区域/船名检测', config: 'ship_detector_config', weights: 'ship_detector_weights' },
  { label: 'DraftFormer 多任务', config: 'draftformer_config', weights: 'draftformer_weights' },
  { label: '船名识别', config: 'shipname_config', weights: 'shipname_weights' },
]
async function load() {
  loading.value = true
  try {
    const [healthResult, streamsResult, settingsResult] = await Promise.allSettled([getHealth(), getStreams(), getModelSettings()])
    if (healthResult.status === 'fulfilled') health.value = healthResult.value
    if (streamsResult.status === 'fulfilled') stream.value = streamsResult.value[0] || null
    if (settingsResult.status === 'fulfilled') modelSettings.value = settingsResult.value
  } finally { loading.value = false }
}
function openSettings() {
  modelForm.value = { ...(modelSettings.value?.values || {}) }
  formError.value = ''
  settingsOpen.value = true
}
async function saveSettings() {
  saving.value = true; formError.value = ''
  try {
    const result = await updateModelSettings(modelForm.value)
    modelSettings.value = result
    ElMessage.success(result.message || '模型路径已保存')
    settingsOpen.value = false
    await load()
  } catch (error: any) {
    const detail = error?.response?.data?.detail
    formError.value = typeof detail === 'object' ? Object.entries(detail).map(([key, value]) => `${key}: ${value}`).join('\n') : detail || '保存失败'
  } finally { saving.value = false }
}
async function restoreSettings() {
  await ElMessageBox.confirm('恢复 .env 中的模型路径，并释放离线模型实例？', '恢复默认路径', { type: 'warning' })
  saving.value = true
  try {
    const result = await resetModelSettings()
    modelSettings.value = result; modelForm.value = { ...result.values }
    ElMessage.success(result.message || '已恢复默认路径')
    settingsOpen.value = false; await load()
  } catch (error: any) {
    formError.value = error?.response?.data?.detail || '恢复失败'
  } finally { saving.value = false }
}
onMounted(load)
</script>

<template>
  <div class="page system-page" v-loading="loading">
    <section class="system-overview panel">
      <div class="health-hero"><div class="health-icon" :class="health?.status"><CheckCircle2 v-if="health?.status === 'healthy'" :size="30" /><AlertCircle v-else :size="30" /></div><div><span class="section-kicker">实际运行状态</span><h2>{{ health?.status === 'healthy' ? '模型文件全部就绪' : '模型配置不完整或服务未连接' }}</h2><p>状态来自后端对配置文件和权重文件的实时检查。</p></div></div>
      <dl class="node-facts"><div><dt>计算设备</dt><dd><Cpu :size="17" />{{ health?.device?.toUpperCase() || '--' }}</dd></div><div><dt>数据库</dt><dd><Database :size="17" />{{ health?.dependencies.database === 'ready' ? '可用' : '不可用' }}</dd></div><div><dt>Redis 队列（可选）</dt><dd><Server :size="17" />{{ health?.dependencies.redis === 'ready' ? '可用' : health?.dependencies.redis === 'optional' ? '未启用' : '不可用' }}</dd></div></dl>
    </section>

    <section class="system-grid">
      <article class="panel model-panel"><header class="panel-header"><div><span class="section-kicker">AI 推理链路</span><h2>模型配置与权重</h2></div><div class="header-actions"><el-button plain @click="openSettings"><Settings2 :size="16" />配置权重</el-button><el-button circle aria-label="刷新状态" @click="load"><RefreshCw :size="17" /></el-button></div></header><div class="model-list"><div v-for="model in health?.models" :key="model.name" class="model-row"><div class="model-state" :class="model.status"><CheckCircle2 v-if="model.status === 'ready'" :size="18" /><CircleOff v-else :size="18" /></div><div class="model-copy"><strong>{{ names[model.name] || model.name }}</strong><span>{{ model.status === 'ready' ? '配置与权重文件存在' : '配置或权重文件缺失' }}</span></div><code :title="model.weights || ''">{{ model.weights || '未指定权重' }}</code></div></div></article>
      <article class="panel stream-panel"><header class="panel-header"><div><span class="section-kicker">媒体流配置</span><h2>实时监测点</h2></div><RadioTower :size="19" /></header><div class="stream-card"><div class="stream-head"><span class="status-dot" :class="stream?.status === 'configured' ? 'online' : 'offline'"></span><div><strong>{{ stream?.name || '未配置监测点' }}</strong><span>{{ stream?.id || '--' }}</span></div><span class="risk-label" :class="stream?.status === 'configured' ? 'normal' : 'warning'">{{ stream?.status === 'configured' ? '已配置' : '未配置' }}</span></div><dl><div><dt>播放协议</dt><dd>{{ stream?.protocol || '--' }}</dd></div><div><dt>播放地址</dt><dd class="stream-url" :title="stream?.play_url || ''">{{ stream?.play_url || '请设置 LIVE_STREAM_URL' }}</dd></div></dl><p class="config-note">平台只报告配置状态，不虚构摄像头在线性、延迟、分辨率或推理吞吐。</p></div></article>
    </section>

    <el-dialog v-model="settingsOpen" title="模型配置与权重路径" width="min(920px, 94vw)" destroy-on-close>
      <div class="model-settings-note"><AlertCircle :size="18" /><span>路径是后端服务器本地文件路径。保存前会校验 YAML 和权重文件；离线模型立即释放并在下个任务重载，独立运行的实时 Worker 仍需手动重启。</span></div>
      <el-form label-position="top" class="model-settings-form">
        <fieldset v-for="group in modelGroups" :key="group.config">
          <legend>{{ group.label }}</legend>
          <el-form-item label="配置文件"><el-input v-model="modelForm[group.config]" /></el-form-item>
          <el-form-item label="权重文件"><el-input v-model="modelForm[group.weights]" /></el-form-item>
        </fieldset>
      </el-form>
      <pre v-if="formError" class="settings-error" role="alert">{{ formError }}</pre>
      <template #footer><el-button :disabled="saving" @click="restoreSettings">恢复 .env 路径</el-button><el-button type="primary" :loading="saving" @click="saveSettings">校验并应用</el-button></template>
    </el-dialog>
  </div>
</template>
