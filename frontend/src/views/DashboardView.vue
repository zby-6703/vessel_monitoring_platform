<script setup lang="ts">
import { AlertTriangle, Anchor, FileSearch, RadioTower, ServerCog, Ship, Square, Upload, Waves } from 'lucide-vue-next'
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { getDashboard, getHealth, getLocalPlayback, getRealtimeResults, getStreams, startLocalPlayback, stopLocalPlayback, type Health, type LocalPlaybackStatus, type RecordItem, type Statistics, type StreamItem } from '../api'
import MetricTile from '../components/MetricTile.vue'
import TrendChart from '../components/TrendChart.vue'
import VideoMonitor from '../components/VideoMonitor.vue'

interface LiveFrame {
  frame_timestamp: string
  frame_id: number
  camera_id: string
  source_width: number
  source_height: number
  processing_ms: number
  vessels: Array<any>
}

const statistics = ref<Statistics>({ generated_at: '', today_traffic: 0, overload_alerts: 0, average_draft: null, average_displacement: null, hourly: [] })
const records = ref<RecordItem[]>([])
const liveRows = ref<RecordItem[]>([])
const health = ref<Health | null>(null)
const stream = ref<StreamItem | null>(null)
const connected = ref(false)
const liveFrame = ref<LiveFrame | null>(null)
const localPlayback = ref<LocalPlaybackStatus | null>(null)
const localFrame = ref<string | null>(null)
const videoInput = ref<HTMLInputElement>()
const localUploadProgress = ref(0)
const liveFrameStep = ref(3)
const loading = ref(true)
let socket: WebSocket | null = null
let reconnectTimer: number | undefined
let refreshTimer: number | undefined
let statisticsRefreshTimer: number | undefined

const latestVessel = computed(() => liveFrame.value?.vessels?.[0] || null)
const visibleRows = computed(() => liveRows.value.length ? liveRows.value : records.value)
const hasTrendData = computed(() => statistics.value.hourly.some((item) => item.traffic > 0 || item.average_draft !== null))
const localActive = computed(() => ['starting', 'running'].includes(localPlayback.value?.status || 'idle'))
const monitorConfigured = computed(() => Boolean(localActive.value || localFrame.value || stream.value?.play_url))
const latestStages = computed(() => {
  const labels: Record<string, string> = { ship: '船舶', ship_name_area: '船名区域', waterline_area: '吃水区域', draft_mark: '刻度字符' }
  const boxes = latestVessel.value?.boxes || []
  return [...new Set(boxes.map((box: any) => labels[box.type]).filter(Boolean))]
})
const monitorName = computed(() => localPlayback.value?.filename || stream.value?.name || '尚未配置监测点')
const levelLabel = (level: string) => ({ normal: '正常', warning: '关注', critical: '超载', unknown: '待判定' }[level] || level)
const modelNames: Record<string, string> = { ship_detection: '船舶/吃水区域/船名检测', draft_multitask: '吃水多任务', ship_name_recognition: '船名识别' }
const formatTime = (value: string) => new Date(value).toLocaleTimeString('zh-CN', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' })

async function load(silent = false) {
  if (!silent) loading.value = true
  try {
    const [healthValue, streams, playback, dashboard, realtime] = await Promise.all([getHealth(), getStreams(), getLocalPlayback(), getDashboard(), getRealtimeResults()])
    statistics.value = dashboard.statistics
    records.value = dashboard.records
    health.value = healthValue
    stream.value = streams[0] || null
    localPlayback.value = playback
    if (!liveRows.value.length || !localActive.value) {
      const persisted = [...realtime.frames, ...realtime.instances].sort((a, b) => +new Date(b.captured_at) - +new Date(a.captured_at)).slice(0, 12)
      liveRows.value = persisted.map((row) => ({
        id: row.id, captured_at: row.captured_at, track_id: row.track_id || 0, camera_id: row.camera_id || row.source_name || 'realtime',
        ship_name: row.ship_name || 'UNKNOWN', mmsi: row.mmsi, draft_depth: row.draft_depth,
        displacement_tons: null, load_ratio: null, risk_level: row.status === 'confirmed' ? 'normal' : 'unknown', confidence: row.confidence || null,
        full_image_path: null, ship_name_image_path: null, water_mask_path: null, draft_image_path: null, review_status: 'pending',
      }))
    }
  } catch {
    if (!silent) ElMessage.error('真实监测数据加载失败，请检查后端服务')
  } finally { loading.value = false }
}

function scheduleRealtimeStatisticsRefresh() {
  if (statisticsRefreshTimer) return
  statisticsRefreshTimer = window.setTimeout(async () => {
    statisticsRefreshTimer = undefined
    try {
      const dashboard = await getDashboard()
      statistics.value = dashboard.statistics
      records.value = dashboard.records
    } catch { /* Keep the last valid realtime statistics until the next refresh. */ }
  }, 1000)
}

function connect() {
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
  socket = new WebSocket(`${protocol}//${location.host}/ws/realtime`)
  socket.onopen = () => { connected.value = true }
  socket.onclose = () => { connected.value = false; reconnectTimer = window.setTimeout(connect, 3000) }
  socket.onmessage = (event) => {
    let message = JSON.parse(event.data)
    if (message.type === 'local_playback_frame') {
      localFrame.value = message.data.image
      localPlayback.value = { ...(localPlayback.value || {}), status: 'running', session_id: message.data.session_id, frame_id: message.data.frame_id } as LocalPlaybackStatus
      message = { type: 'inference', data: message.data.frame }
    }
    if (message.type === 'local_playback_status') {
      localPlayback.value = message.data
      if (message.data.status === 'completed') load(true)
      return
    }
    if (message.type !== 'inference') return
    const frame = message.data as LiveFrame
    liveFrame.value = frame
    for (const vessel of frame.vessels || []) {
      liveRows.value.unshift({
        id: Number(`${Date.now()}${vessel.ship_id}`), captured_at: frame.frame_timestamp, track_id: vessel.ship_id, camera_id: frame.camera_id,
        ship_name: vessel.ship_name || 'UNKNOWN', mmsi: vessel.mmsi || null, draft_depth: vessel.draft_depth_m,
        displacement_tons: null, load_ratio: null, risk_level: vessel.current_status === 'confirmed' ? 'normal' : 'unknown', confidence: vessel.confidence,
        full_image_path: vessel.assets?.full_ship || null, ship_name_image_path: vessel.assets?.ship_name_crop || null,
        water_mask_path: vessel.assets?.water_mask || null, draft_image_path: vessel.assets?.draft_marks || null, review_status: 'pending',
      })
    }
    liveRows.value = liveRows.value.slice(0, 12)
    scheduleRealtimeStatisticsRefresh()
  }
}
async function chooseLocalVideo(event: Event) {
  const file = (event.target as HTMLInputElement).files?.[0]
  if (!file) return
  const form = new FormData()
  form.append('file', file)
  form.append('frame_step', String(liveFrameStep.value))
  form.append('camera_id', 'local-video')
  localFrame.value = null
  localUploadProgress.value = 0
  try {
    localPlayback.value = await startLocalPlayback(form, (percent) => { localUploadProgress.value = percent })
    ElMessage.success('本地视频已开始实时推理')
  } catch (error: any) {
    ElMessage.error(error?.response?.data?.detail || '本地视频启动失败')
  } finally {
    ;(event.target as HTMLInputElement).value = ''
  }
}
async function stopLocalVideo() {
  try {
    localPlayback.value = await stopLocalPlayback()
    localFrame.value = null
  } catch { ElMessage.error('停止本地视频失败') }
}
onMounted(() => { load(); connect(); refreshTimer = window.setInterval(() => load(true), 30000) })
onBeforeUnmount(() => { socket?.close(); clearTimeout(reconnectTimer); clearInterval(refreshTimer); clearTimeout(statisticsRefreshTimer) })
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
        <header class="panel-header"><div><span class="section-kicker">实时画面</span><h2>{{ stream?.name || '尚未配置监测点' }}</h2></div><div class="live-state"><span class="status-dot" :class="connected && health?.dependencies.redis === 'ready' ? 'online' : 'offline'"></span>{{ !connected ? 'WebSocket 未连接' : health?.dependencies.redis === 'ready' ? '实时结果链路就绪' : 'Redis 未连接' }}</div></header>
        <div class="local-video-actions">
          <input ref="videoInput" hidden type="file" accept="video/*" @change="chooseLocalVideo" />
          <label class="frame-step-setting">每 <el-input-number v-model="liveFrameStep" :min="1" :max="1000" :step="1" :disabled="localActive" size="small" controls-position="right" /> 帧推理一次</label>
          <el-button v-if="localActive" size="small" plain type="danger" @click="stopLocalVideo"><Square :size="14" />停止本地回放</el-button>
          <el-button v-else size="small" plain @click="videoInput?.click()"><Upload :size="14" />上传本地视频</el-button>
          <span v-if="localUploadProgress && !localActive" class="muted-text">上传 {{ localUploadProgress }}%</span>
        </div>
        <VideoMonitor v-if="monitorConfigured" :src="stream?.play_url || ''" :frame-image="localFrame" :overlay="liveFrame" :camera-id="localPlayback?.camera_id || stream?.id || 'local-video'" :configured="true" :loading="localActive && !localFrame" />
        <div v-else class="stream-empty">
          <RadioTower :size="36" />
          <strong>未配置真实视频流</strong>
          <span>在 `.env` 中设置 `LIVE_STREAM_URL`，重启后端后显示现场画面。平台不会用 demo 视频代替实时流。</span>
          <RouterLink to="/system">查看系统配置</RouterLink>
        </div>
      </article>

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
    </section>

    <section class="lower-grid">
      <article class="panel trend-panel">
        <header class="panel-header"><div><span class="section-kicker">真实归档趋势</span><h2>24 小时船舶流量与吃水</h2></div><span class="muted-text">小时粒度</span></header>
        <TrendChart v-if="hasTrendData" :data="statistics.hourly" />
        <div v-else class="chart-empty"><FileSearch :size="28" /><strong>暂无趋势数据</strong><span>实时轨迹完成归档或离线结果写入记录后生成趋势。</span></div>
      </article>
      <article class="panel model-health-panel">
        <header class="panel-header"><div><span class="section-kicker">推理链路</span><h2>模型文件状态</h2></div><ServerCog :size="19" /></header>
        <div class="pipeline-list">
          <div v-for="model in health?.models" :key="model.name"><span class="status-dot" :class="model.status === 'ready' ? 'online' : 'offline'"></span><strong>{{ modelNames[model.name] || model.name }}</strong><small>{{ model.status === 'ready' ? '权重就绪' : '配置或权重缺失' }}</small></div>
        </div>
      </article>
    </section>
  </div>
</template>
