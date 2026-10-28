<script setup lang="ts">
import { AlertTriangle, Anchor, Camera, FileSearch, RadioTower, ServerCog, Ship, Square, Upload, Waves } from 'lucide-vue-next'
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { apiError, controlPtz, defaultRecordingSettings, getCameraStatus, getDashboard, getHealth, getLocalPlayback, getPtzStatus, getRealtimeResults, getRecordingSettings, getRecordingStatus, getStreams, startCamera, startLocalPlayback, stopCamera, stopLocalPlayback, updateRecordingSettings, type CameraStatus, type Health, type LocalPlaybackStatus, type PtzAction, type PtzStatus, type RecordItem, type RecordingSettings, type RecordingStatus, type Statistics, type StreamItem } from '../api'
import MetricTile from '../components/MetricTile.vue'
import RecordingControls from '../components/RecordingControls.vue'
import TrendChart from '../components/TrendChart.vue'
import VideoMonitor from '../components/VideoMonitor.vue'

interface LiveFrame { frame_timestamp: string; frame_id: number; camera_id: string; source_width: number; source_height: number; processing_ms: number; vessels: Array<any> }
const statistics = ref<Statistics>({ generated_at: '', today_traffic: 0, overload_alerts: 0, average_draft: null, average_displacement: null, hourly: [] })
const records = ref<RecordItem[]>([])
const liveRows = ref<RecordItem[]>([])
const health = ref<Health | null>(null)
const stream = ref<StreamItem | null>(null)
const connected = ref(false)
const sourceMode = ref<'camera' | 'local'>('camera')
const camera = ref<CameraStatus | null>(null)
const cameraFrame = ref<string | null>(null)
const cameraOverlay = ref<LiveFrame | null>(null)
const localOverlay = ref<LiveFrame | null>(null)
const localPlayback = ref<LocalPlaybackStatus | null>(null)
const localFrame = ref<string | null>(null)
const videoInput = ref<HTMLInputElement>()
const localUploadProgress = ref(0)
const sourceBusy = ref(false)
const liveFrameStep = ref(3)
const loading = ref(true)
const settings = ref<RecordingSettings>(defaultRecordingSettings())
const savedSettings = ref<RecordingSettings | null>(null)
const recordingStatus = ref<RecordingStatus | null>(null)
const ptz = ref<PtzStatus | null>(null)
const ptzBusy = ref(false)
const savingSettings = ref(false)
let socket: WebSocket | null = null
let reconnectTimer: number | undefined
let refreshTimer: number | undefined
let captureTimer: number | undefined
let statisticsRefreshTimer: number | undefined
let disposed = false
let statusRefreshing = false
const settingsDirty = computed(() => Boolean(savedSettings.value && JSON.stringify(settings.value) !== JSON.stringify(savedSettings.value)))
const localActive = computed(() => ['starting', 'running'].includes(localPlayback.value?.status || 'idle'))
const cameraActive = computed(() => ['starting', 'running'].includes(camera.value?.status || 'idle'))
const monitorCameraId = computed(() => sourceMode.value === 'local' ? localPlayback.value?.camera_id || 'local-video' : camera.value?.camera_id || stream.value?.id || 'camera')
const liveFrame = computed(() => sourceMode.value === 'local' ? localOverlay.value : cameraOverlay.value)
const frameImage = computed(() => sourceMode.value === 'local' ? localFrame.value : cameraFrame.value)
const monitorSrc = computed(() => sourceMode.value === 'camera' ? stream.value?.play_url || '' : '')
const monitorConfigured = computed(() => Boolean(frameImage.value || monitorSrc.value || (sourceMode.value === 'local' ? localActive.value : cameraActive.value)))
const latestVessel = computed(() => liveFrame.value?.vessels?.[0] || null)
const visibleRows = computed(() => {
  const matching = liveRows.value.filter((row) => row.camera_id === monitorCameraId.value)
  return matching.length ? matching.slice(0, 12) : records.value.filter((row) => row.camera_id === monitorCameraId.value).slice(0, 12)
})
const hasTrendData = computed(() => statistics.value.hourly.some((item) => item.traffic > 0 || item.average_draft !== null))
const latestStages = computed(() => {
  const labels: Record<string, string> = { ship: '船舶', ship_name_area: '船名区域', waterline_area: '吃水区域', draft_mark: '刻度字符' }
  return [...new Set((latestVessel.value?.boxes || []).map((box: any) => labels[box.type]).filter(Boolean))]
})
const monitorName = computed(() => sourceMode.value === 'local' ? localPlayback.value?.filename || '本地视频验证' : stream.value?.name || '现场摄像头')
const opticalMode = computed(() => settings.value.recording_mode === 'optical_cycle')
const opticalReady = computed(() => Boolean(ptz.value?.cycle_supported && ptz.value.magnifications?.some((factor) => Math.abs(factor - settings.value.zoom_factor) < .001)))
const monitorFocusSettings = computed(() => !savedSettings.value ? undefined : sourceMode.value === 'local' && opticalMode.value ? { ...settings.value, recording_mode: 'passage' as const, zoom_factor: Math.min(8, settings.value.zoom_factor) } : settings.value)
const sourceError = computed(() => sourceMode.value === 'local' ? localPlayback.value?.error : camera.value?.error)
const levelLabel = (level: string) => ({ normal: '正常', warning: '关注', critical: '超载', unknown: '待判定' }[level] || level)
const modelNames: Record<string, string> = { ship_detection: '船舶/吃水区域/船名检测', draft_multitask: '吃水多任务', ship_name_recognition: '船名识别' }
const formatTime = (value: string) => new Date(value).toLocaleTimeString('zh-CN', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' })

async function load(silent = false) {
  if (!silent) loading.value = true
  try {
    const [healthResult, streamsResult, playbackResult, dashboardResult, realtimeResult] = await Promise.allSettled([getHealth(), getStreams(), getLocalPlayback(), getDashboard(), getRealtimeResults()])
    if (dashboardResult.status === 'rejected') throw dashboardResult.reason
    statistics.value = dashboardResult.value.statistics; records.value = dashboardResult.value.records
    if (healthResult.status === 'fulfilled') health.value = healthResult.value
    if (streamsResult.status === 'fulfilled') stream.value = streamsResult.value[0] || null
    if (playbackResult.status === 'fulfilled') {
      localPlayback.value = playbackResult.value
      if (!silent && localActive.value) sourceMode.value = 'local'
    }
    if (realtimeResult.status === 'fulfilled' && !liveRows.value.length) {
      liveRows.value = [...realtimeResult.value.frames, ...realtimeResult.value.instances].sort((a, b) => +new Date(b.captured_at) - +new Date(a.captured_at)).slice(0, 60).map((row) => ({
        id: row.id, captured_at: row.captured_at, track_id: row.track_id || 0, camera_id: row.camera_id || row.source_name || 'realtime',
        ship_name: row.ship_name || 'UNKNOWN', mmsi: row.mmsi, draft_depth: row.draft_depth, displacement_tons: null, load_ratio: null,
        risk_level: row.status === 'confirmed' ? 'normal' : 'unknown', confidence: row.confidence || null,
        full_image_path: null, ship_name_image_path: null, water_mask_path: null, draft_image_path: null, review_status: 'pending',
      }))
    }
  } catch { if (!silent) ElMessage.error('监测数据加载失败，请检查后端服务') } finally { loading.value = false }
}
async function refreshCapture() {
  if (statusRefreshing) return
  statusRefreshing = true
  try {
    const [cameraResult, recordingResult] = await Promise.allSettled([getCameraStatus(), getRecordingStatus()])
    if (cameraResult.status === 'fulfilled') camera.value = cameraResult.value
    if (recordingResult.status === 'fulfilled') recordingStatus.value = recordingResult.value
  } finally { statusRefreshing = false }
}
async function loadSettings() {
  const [result, ptzResult] = await Promise.allSettled([getRecordingSettings(), getPtzStatus()])
  if (result.status === 'fulfilled') { settings.value = { ...defaultRecordingSettings(), ...result.value }; savedSettings.value = { ...settings.value } }
  else ElMessage.error('自动录制设置加载失败，请刷新重试')
  if (ptzResult.status === 'fulfilled') ptz.value = ptzResult.value
}
async function refreshPtz() {
  ptzBusy.value = true
  try { ptz.value = await getPtzStatus() }
  catch (error) { ElMessage.error(apiError(error, '镜头能力检查失败')) }
  finally { ptzBusy.value = false }
}
async function saveSettings() {
  if (opticalMode.value && settings.value.enabled && !opticalReady.value) { ElMessage.error('请先确认绝对光学变焦能力，并选择已标定的目标倍率'); return }
  savingSettings.value = true
  try {
    const result = await updateRecordingSettings(settings.value)
    settings.value = { ...result }; savedSettings.value = { ...result }
    await refreshCapture(); ElMessage.success(result.enabled ? result.recording_mode === 'optical_cycle' ? '光学变焦循环策略已生效' : '数字取景录制设置已生效' : '自动录制已关闭，设置已保存')
  } catch (error) { ElMessage.error(apiError(error, '录制设置保存失败')) } finally { savingSettings.value = false }
}
function resetSettings() { if (savedSettings.value) settings.value = { ...savedSettings.value } }
function setFocusCenter(center: { x: number; y: number }) { settings.value = { ...settings.value, center_x: center.x, center_y: center.y } }
async function sendPtz(action: PtzAction) {
  ptzBusy.value = true
  try { await controlPtz(action); ElMessage.success(action === 'stop' ? '已停止摄像头运动' : '摄像头控制指令已发送') }
  catch (error) { ElMessage.error(apiError(error, '摄像头控制失败')) } finally { ptzBusy.value = false }
}
function scheduleRealtimeStatisticsRefresh() {
  if (statisticsRefreshTimer) return
  statisticsRefreshTimer = window.setTimeout(async () => {
    statisticsRefreshTimer = undefined
    try { const dashboard = await getDashboard(); statistics.value = dashboard.statistics; records.value = dashboard.records } catch { /* Preserve the last valid data. */ }
  }, 1000)
}
function acceptInference(frame: LiveFrame, source: 'camera' | 'local') {
  if (source === 'local') localOverlay.value = frame
  else cameraOverlay.value = frame
  for (const vessel of frame.vessels || []) {
    liveRows.value.unshift({
      id: Number(`${Date.now()}${vessel.ship_id}`), captured_at: frame.frame_timestamp, track_id: vessel.ship_id, camera_id: frame.camera_id,
      ship_name: vessel.ship_name || 'UNKNOWN', mmsi: vessel.mmsi || null, draft_depth: vessel.draft_depth_m,
      displacement_tons: null, load_ratio: null, risk_level: vessel.current_status === 'confirmed' ? 'normal' : 'unknown', confidence: vessel.confidence,
      full_image_path: vessel.assets?.full_ship || null, ship_name_image_path: vessel.assets?.ship_name_crop || null,
      water_mask_path: vessel.assets?.water_mask || null, draft_image_path: vessel.assets?.draft_marks || null, review_status: 'pending',
    })
  }
  liveRows.value = liveRows.value.slice(0, 60); scheduleRealtimeStatisticsRefresh()
}
function connect() {
  if (disposed) return
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
  socket = new WebSocket(`${protocol}//${location.host}/ws/realtime`)
  socket.onopen = () => { connected.value = true }
  socket.onclose = () => { connected.value = false; if (!disposed) reconnectTimer = window.setTimeout(connect, 3000) }
  socket.onmessage = (event) => {
    let message: any
    try { message = JSON.parse(event.data) } catch { return }
    if (message.type === 'local_playback_frame') {
      if (localPlayback.value?.session_id && message.data.session_id !== localPlayback.value.session_id) return
      localFrame.value = message.data.image
      localPlayback.value = { ...(localPlayback.value || {}), status: 'running', session_id: message.data.session_id, camera_id: message.data.frame.camera_id, frame_id: message.data.frame_id } as LocalPlaybackStatus
      acceptInference(message.data.frame, 'local'); return
    }
    if (message.type === 'camera_frame') {
      if (camera.value?.session_id && message.data.session_id !== camera.value.session_id) return
      cameraFrame.value = message.data.image
      camera.value = { ...(camera.value || {}), configured: true, status: 'running', session_id: message.data.session_id, camera_id: message.data.frame.camera_id, frame_id: message.data.frame_id } as CameraStatus
      acceptInference(message.data.frame, 'camera'); return
    }
    if (message.type === 'local_playback_status') { localPlayback.value = message.data; if (['completed', 'stopped', 'failed'].includes(message.data.status)) { refreshCapture(); load(true) }; return }
    if (message.type === 'camera_status') { camera.value = message.data; if (['stopped', 'failed'].includes(message.data.status)) refreshCapture(); return }
    if (message.type !== 'inference') return
    const frame = message.data as LiveFrame
    // Capture JPEGs carry their own matching overlay. Independent worker results are
    // only drawn on the corresponding camera stream, never on local video frames.
    if (!cameraFrame.value && frame.camera_id === (camera.value?.camera_id || stream.value?.id)) acceptInference(frame, 'camera')
  }
}
async function chooseLocalVideo(event: Event) {
  const file = (event.target as HTMLInputElement).files?.[0]
  if (!file) return
  const form = new FormData(); form.append('file', file); form.append('frame_step', String(liveFrameStep.value)); form.append('camera_id', 'local-video')
  sourceBusy.value = true; localFrame.value = null; localOverlay.value = null; localUploadProgress.value = 0; sourceMode.value = 'local'
  try {
    localPlayback.value = await startLocalPlayback(form, (percent) => { localUploadProgress.value = percent })
    cameraFrame.value = null; cameraOverlay.value = null; await refreshCapture(); ElMessage.success(opticalMode.value ? '本地视频已开始检测验证，不会驱动摄像头光学变焦' : '本地视频已开始实时推理，可验证数字录制')
  } catch (error) { ElMessage.error(apiError(error, '本地视频启动失败')) }
  finally { sourceBusy.value = false; (event.target as HTMLInputElement).value = '' }
}
async function toggleCamera() {
  sourceBusy.value = true
  try {
    if (cameraActive.value) camera.value = await stopCamera()
    else {
      cameraFrame.value = null; cameraOverlay.value = null
      camera.value = await startCamera(liveFrameStep.value)
      localFrame.value = null; localOverlay.value = null; localPlayback.value = await getLocalPlayback()
    }
    await refreshCapture()
  } catch (error) { ElMessage.error(apiError(error, '摄像头采集操作失败')) } finally { sourceBusy.value = false }
}
async function stopLocalVideo() {
  sourceBusy.value = true
  try { localPlayback.value = await stopLocalPlayback(); await refreshCapture() }
  catch (error) { ElMessage.error(apiError(error, '停止本地视频失败')) } finally { sourceBusy.value = false }
}
onMounted(() => { load(); loadSettings(); refreshCapture(); connect(); refreshTimer = window.setInterval(() => load(true), 30000); captureTimer = window.setInterval(refreshCapture, 3000) })
onBeforeUnmount(() => { disposed = true; socket?.close(); clearTimeout(reconnectTimer); clearInterval(refreshTimer); clearInterval(captureTimer); clearTimeout(statisticsRefreshTimer) })
</script>

<template>
  <div class="page dashboard-page" v-loading="loading">
    <section class="metric-grid" aria-label="真实监测指标">
      <MetricTile label="今日已归档船舶" :value="statistics.today_traffic" unit="艘次" note="来自数据库真实记录" :icon="Ship" />
      <MetricTile label="平均视觉吃水" :value="statistics.average_draft?.toFixed(2) ?? '--'" unit="m" note="无有效读数时显示 --" :icon="Waves" />
      <MetricTile label="平均估算排水量" :value="statistics.average_displacement ? Math.round(statistics.average_displacement).toLocaleString() : '--'" unit="t" note="仅统计已完成 AIS 融合记录" :icon="Anchor" />
      <MetricTile label="超载预警" :value="statistics.overload_alerts" unit="次" note="来自真实风险判定" :icon="AlertTriangle" :tone="statistics.overload_alerts ? 'warning' : 'default'" />
    </section>

    <section class="monitor-grid">
      <article class="panel video-panel">
        <header class="panel-header"><div><span class="section-kicker">实时画面 / 智能取景</span><h2>{{ monitorName }}</h2></div><div class="live-state"><span class="status-dot" :class="connected ? 'online' : 'offline'"></span>{{ connected ? '实时链路已连接' : '实时链路未连接' }}</div></header>
        <div class="monitor-source-bar"><el-radio-group v-model="sourceMode" size="small" aria-label="监测视频源"><el-radio-button value="camera"><Camera :size="13" />现场摄像头</el-radio-button><el-radio-button value="local"><Upload :size="13" />本地视频验证</el-radio-button></el-radio-group><span class="muted-text">{{ sourceMode === 'camera' ? cameraActive ? '采集中' : '待启动采集' : localActive ? '实时回放中' : '用于接入前验证' }}</span></div>
        <div class="local-video-actions source-actions">
          <input ref="videoInput" hidden type="file" accept="video/*" @change="chooseLocalVideo" />
          <label class="frame-step-setting">每 <el-input-number v-model="liveFrameStep" :min="1" :max="1000" :step="1" :disabled="localActive || cameraActive || sourceBusy" size="small" controls-position="right" /> 帧推理一次</label>
          <template v-if="sourceMode === 'local'"><el-button v-if="localActive" size="small" plain type="danger" :loading="sourceBusy" @click="stopLocalVideo"><Square :size="14" />停止回放</el-button><el-button v-else size="small" plain :loading="sourceBusy" @click="videoInput?.click()"><Upload :size="14" />上传视频</el-button><span v-if="sourceBusy && localUploadProgress" class="muted-text">上传 {{ localUploadProgress }}%</span></template>
          <el-button v-else size="small" :type="cameraActive ? 'danger' : 'primary'" :plain="cameraActive" :disabled="!cameraActive && (!camera?.configured || !!savedSettings?.enabled && savedSettings.recording_mode === 'optical_cycle' && !opticalReady)" :loading="sourceBusy" @click="toggleCamera"><Square v-if="cameraActive" :size="14" /><RadioTower v-else :size="14" />{{ cameraActive ? '停止采集' : '启动摄像头采集' }}</el-button>
        </div>
        <div v-if="sourceError" class="source-error" role="alert">{{ sourceError }}</div>
        <VideoMonitor v-if="monitorConfigured" :src="monitorSrc" :frame-image="frameImage" :overlay="liveFrame" :camera-id="monitorCameraId" :configured="Boolean(frameImage || monitorSrc)" :loading="sourceMode === 'local' ? localActive && !localFrame : cameraActive && !cameraFrame" :focus-settings="monitorFocusSettings" :recording-status="sourceMode === 'camera' ? recordingStatus : null" :recording="recordingStatus?.state === 'recording' && recordingStatus?.source_name === (sourceMode === 'local' ? localPlayback?.filename : camera?.filename)" @focus-center="setFocusCenter" />
        <div v-else class="stream-empty">
          <RadioTower :size="36" />
          <strong>{{ sourceMode === 'local' ? '上传一段视频，验证船来即录' : camera?.configured ? '摄像头已配置，等待启动采集' : '等待接入现场摄像头' }}</strong>
          <span>{{ sourceMode === 'local' ? '本地视频用于检测与数字录制验证；真实光学变焦循环需要接入摄像头。' : camera?.configured ? '启动采集后进行实时识别；光学循环需先确认镜头绝对变焦能力与倍率标定。' : '在服务端配置 LIVE_CAPTURE_URL 接入摄像头，并配置 ONVIF 光学倍率标定。' }}</span>
          <RouterLink v-if="sourceMode === 'camera'" to="/system">查看系统配置</RouterLink>
        </div>
        <div class="monitor-footnote"><template v-if="opticalMode && sourceMode === 'camera'"><span>检测后录像并光学放大</span><span>船尾消失后回到 1×</span><span>每轮独立保存，继续检测</span></template><template v-else><span>全景与数字特写同步保存</span><span>本地验证不会驱动镜头</span><span>数字放大不增加原始细节</span></template></div>
      </article>

      <RecordingControls v-model="settings" :status="recordingStatus" :saving="savingSettings" :loaded="!!savedSettings" :dirty="settingsDirty" :ptz="ptz" :ptz-busy="ptzBusy" :source-mode="sourceMode" @save="saveSettings" @reset="resetSettings" @ptz="sendPtz" @refresh-ptz="refreshPtz" />
    </section>

    <section class="lower-grid monitoring-results-grid">
      <article class="panel live-records-panel">
        <header class="panel-header"><div><span class="section-kicker">真实识别结果</span><h2>实时船舶记录</h2></div><RouterLink class="text-link" to="/offline">离线分析</RouterLink></header>
        <div class="current-vessel" v-if="latestVessel">
          <div><span>当前目标</span><strong>{{ latestVessel.ship_name || 'UNKNOWN' }}</strong><small>ID {{ latestVessel.ship_id }} · {{ latestStages.join(' · ') || '船舶检测' }}</small></div>
          <div class="draft-reading"><span>视觉吃水</span><strong>{{ latestVessel.draft_depth_m?.toFixed(2) || '--' }}<small>m</small></strong></div>
        </div>
        <div v-else class="compact-empty"><Ship :size="24" /><span>尚未收到实时推理结果</span></div>
        <el-table :data="visibleRows" height="337" size="small" table-layout="fixed" class="ops-table" empty-text="暂无真实识别记录">
          <el-table-column label="时间" width="74"><template #default="scope"><span class="mono muted-text">{{ formatTime(scope.row.captured_at) }}</span></template></el-table-column>
          <el-table-column prop="ship_name" label="船名" min-width="112" show-overflow-tooltip />
          <el-table-column label="吃水" width="70" align="right"><template #default="scope"><span class="mono">{{ scope.row.draft_depth?.toFixed(2) || '--' }}</span></template></el-table-column>
          <el-table-column label="状态" width="67" align="right"><template #default="scope"><span class="risk-label" :class="scope.row.risk_level">{{ levelLabel(scope.row.risk_level) }}</span></template></el-table-column>
        </el-table>
      </article>
      <article class="panel trend-panel">
        <header class="panel-header"><div><span class="section-kicker">真实归档趋势</span><h2>24 小时船舶流量与吃水</h2></div><span class="muted-text">小时粒度</span></header>
        <TrendChart v-if="hasTrendData" :data="statistics.hourly" />
        <div v-else class="chart-empty"><FileSearch :size="28" /><strong>暂无趋势数据</strong><span>实时轨迹完成归档或离线结果写入记录后生成趋势。</span></div>
      </article>
    </section>
    <section class="model-readiness-bar panel">
      <article class="model-health-panel">
        <header class="panel-header"><div><span class="section-kicker">推理链路</span><h2>模型文件状态</h2></div><ServerCog :size="19" /></header>
        <div class="pipeline-list">
          <div v-for="model in health?.models" :key="model.name"><span class="status-dot" :class="model.status === 'ready' ? 'online' : 'offline'"></span><strong>{{ modelNames[model.name] || model.name }}</strong><small>{{ model.status === 'ready' ? '权重就绪' : '配置或权重缺失' }}</small></div>
        </div>
      </article>
    </section>
  </div>
</template>
