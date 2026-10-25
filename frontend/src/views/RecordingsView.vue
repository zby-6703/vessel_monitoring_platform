<script setup lang="ts">
import { Download, Film, Play, RefreshCw, Search, Trash2, Video, X } from 'lucide-vue-next'
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { apiError, deleteRecording, formatBytes, getRecordings, type RecordingItem } from '../api'
const filters = reactive({ query: '', camera_id: '', dates: [] as string[] })
const recordings = ref<RecordingItem[]>([])
const total = ref(0)
const page = ref(1)
const loading = ref(false)
const selected = ref<RecordingItem | null>(null)
const activeView = ref<'original' | 'focused'>('original')
const playback = ref<HTMLVideoElement>()
const playbackTime = ref(0)
const playbackWasPlaying = ref(false)
const deleting = ref<string | null>(null)
const requestError = ref('')
let refreshTimer: number | undefined
let loadingNow = false
const videoUrl = computed(() => selected.value ? activeView.value === 'focused' ? selected.value.focused_url : selected.value.original_url : null)
const pendingCount = computed(() => recordings.value.filter((item) => ['recording', 'processing'].includes(item.status)).length)
const statusLabel = (status: string) => ({ completed: '已归档', interrupted: '已中断', failed: '失败', recording: '录制中', processing: '转码中' }[status] || status)
const reasonLabel = (reason: string | null) => ({ source_completed: '视频源结束', source_stopped: '采集已停止', source_error: '视频源异常', source_changed: '切换视频源', source_discontinuity: '视频源时间中断', resolution_changed: '视频分辨率变化', max_duration: '达到分段时长', vessel_left: '船舶离开画面', disabled: '录制已关闭', settings_changed: '策略已更新', process_restarted: '服务重启恢复', server_shutdown: '服务关闭', storage_limit: '存储容量限制', writer_failed: '录像写入失败' }[reason || ''] || '自动归档')
const dateLabel = (value: string) => new Date(value).toLocaleString('zh-CN', { hour12: false })
const durationLabel = (value: number) => `${Math.floor(value / 60).toString().padStart(2, '0')}:${Math.floor(value % 60).toString().padStart(2, '0')}`
const downloadUrl = (url: string | null) => url ? `${url}${url.includes('?') ? '&' : '?'}download=true` : ''
async function load(reset = false, silent = false) {
  if (loadingNow) return
  loadingNow = true
  if (reset) page.value = 1
  if (!silent) loading.value = true
  try {
    const result = await getRecordings({ query: filters.query || undefined, camera_id: filters.camera_id || undefined, date_from: filters.dates?.[0] ? new Date(filters.dates[0]).toISOString() : undefined, date_to: filters.dates?.[1] ? new Date(filters.dates[1]).toISOString() : undefined, page: page.value, page_size: 12 })
    recordings.value = result.items; total.value = result.total; requestError.value = ''
    if (selected.value) { const latest = result.items.find((item) => item.id === selected.value?.id); if (latest) selected.value = latest }
  } catch (error) { requestError.value = apiError(error, '通行视频加载失败，请检查后端服务'); if (!silent) ElMessage.error(requestError.value) }
  finally { loading.value = false; loadingNow = false }
}
function reset() { filters.query = ''; filters.camera_id = ''; filters.dates = []; load(true) }
function open(item: RecordingItem) { activeView.value = 'original'; playbackTime.value = 0; playbackWasPlaying.value = false; selected.value = item }
function switchView(view: 'original' | 'focused') {
  if (view === activeView.value) return
  playbackTime.value = playback.value?.currentTime || 0; playbackWasPlaying.value = Boolean(playback.value && !playback.value.paused)
  activeView.value = view
}
function restorePlayback() {
  if (!playback.value) return
  playback.value.currentTime = Math.min(playbackTime.value, Math.max(0, playback.value.duration - .1))
  if (playbackWasPlaying.value) playback.value.play().catch(() => {})
}
async function remove(item: RecordingItem) {
  try { await ElMessageBox.confirm('将永久删除这次通行的全景、聚焦视频和缩略图，无法恢复。', '删除通行视频', { confirmButtonText: '删除视频', cancelButtonText: '取消', type: 'warning' }) } catch { return }
  deleting.value = item.id
  try {
    await deleteRecording(item.id); if (selected.value?.id === item.id) selected.value = null
    if (recordings.value.length === 1 && page.value > 1) page.value--
    await load(); ElMessage.success('通行视频已删除')
  } catch (error) { ElMessage.error(apiError(error, '删除失败')) } finally { deleting.value = null }
}
onMounted(() => { load(); refreshTimer = window.setInterval(() => load(false, true), 10000) })
onBeforeUnmount(() => clearInterval(refreshTimer))
</script>

<template>
  <div class="page recordings-page">
    <section class="archive-intro"><div><span class="section-kicker">PASSAGE VIDEO LIBRARY</span><h2>每次通行，一份完整记录</h2><p>从入场前预录到离场后延录，快速回看船舶全景与船名、吃水区域特写。</p></div><RouterLink class="text-link" to="/"><Video :size="16" />配置自动录制 →</RouterLink></section>
    <section class="filter-band recording-filters" aria-label="通行视频筛选">
      <el-form :inline="true" label-position="top" @submit.prevent="load(true)">
        <el-form-item label="船名 / 来源 / 轨迹 ID"><el-input v-model="filters.query" placeholder="搜索通行视频" clearable /></el-form-item>
        <el-form-item label="摄像头 ID"><el-input v-model="filters.camera_id" placeholder="全部摄像头" clearable /></el-form-item>
        <el-form-item label="通行时间"><el-date-picker v-model="filters.dates" type="datetimerange" start-placeholder="开始时间" end-placeholder="结束时间" value-format="YYYY-MM-DDTHH:mm:ss" /></el-form-item>
        <el-form-item class="form-actions"><el-button type="primary" native-type="submit"><Search :size="15" />查询</el-button><el-button @click="reset">重置</el-button></el-form-item>
      </el-form>
    </section>
    <div class="archive-results-heading"><span>共 <strong>{{ total }}</strong> 次通行<span v-if="pendingCount" class="archive-pending"> · {{ pendingCount }} 段正在处理</span></span><el-button text :loading="loading" @click="load()"><RefreshCw :size="14" />刷新</el-button></div>
    <div v-if="requestError" class="recording-error archive-error" role="alert">{{ requestError }}</div>
    <section v-loading="loading" class="recording-card-grid" aria-label="通行视频列表">
      <article v-for="item in recordings" :key="item.id" class="panel recording-card">
        <button class="recording-thumbnail" :aria-label="`查看 ${item.source_name} ${dateLabel(item.started_at)} 通行视频`" @click="open(item)">
          <img v-if="item.thumbnail_url" :src="item.thumbnail_url" alt="船舶通行关键帧" loading="lazy" /><div v-else class="recording-thumbnail-placeholder"><Film :size="34" /><span>{{ item.status === 'recording' ? '船舶通行录制中' : item.status === 'processing' ? '正在准备浏览器播放' : '暂无关键帧' }}</span></div>
          <span class="recording-card-state" :class="item.status">{{ statusLabel(item.status) }}</span><span class="recording-duration mono">{{ durationLabel(item.duration_seconds) }}</span><span v-if="item.original_url" class="recording-play"><Play :size="22" fill="currentColor" /></span>
        </button>
        <div class="recording-card-body"><time>{{ dateLabel(item.started_at) }}</time><h3 :title="item.ship_names.join(' / ')">{{ item.ship_names.length ? item.ship_names.join(' / ') : '船名待识别' }}</h3><p class="recording-source" :title="item.source_name">{{ item.source_name }} <span>· {{ item.camera_id }}</span></p><div class="recording-tags"><span v-if="item.original_url">全景原视频</span><span v-if="item.has_focus">聚焦特写</span><span v-if="item.track_ids.length">{{ item.track_ids.length }} 个轨迹</span></div><div class="recording-card-meta"><span>{{ formatBytes(item.bytes) }}</span><span>{{ reasonLabel(item.reason) }}</span></div><div class="recording-card-actions"><el-button size="small" :disabled="!item.original_url" @click="open(item)"><Play :size="13" />回看</el-button><a v-if="item.original_url" class="download-link" :href="downloadUrl(item.original_url)" download><Download :size="14" />原视频</a><el-button text type="danger" size="small" :loading="deleting === item.id" :disabled="['recording', 'processing'].includes(item.status)" aria-label="删除通行视频" @click="remove(item)"><Trash2 :size="14" /></el-button></div></div>
      </article>
    </section>
    <div v-if="!loading && !recordings.length" class="archive-empty panel"><Film :size="38" /><h3>{{ requestError ? '视频暂不可用' : filters.query || filters.camera_id || filters.dates.length ? '没有符合条件的通行视频' : '等待第一艘船经过' }}</h3><p>{{ requestError ? '请确认后端服务可用后重试。' : '在实时监测中开启“船舶通行录制”并启动采集，检测到船舶后自动生成全景与聚焦视频。' }}</p><RouterLink to="/" class="text-link">前往实时监测 →</RouterLink></div>
    <footer v-if="total" class="table-pagination"><el-pagination v-model:current-page="page" :page-size="12" :total="total" layout="prev, pager, next, jumper" @current-change="load()" /></footer>
    <el-dialog :model-value="!!selected" title="通行视频回看" width="min(1100px, 96vw)" destroy-on-close @update:model-value="(open: boolean) => { if (!open) selected = null }">
      <div v-if="selected" class="recording-playback-dialog">
        <div class="recording-playback-heading"><div><strong>{{ selected.ship_names.join(' / ') || '船名待识别' }}</strong><span>{{ dateLabel(selected.started_at) }} · {{ selected.source_name }}</span></div><div class="playback-view-tabs" role="group" aria-label="回看视频视角"><button :class="{ active: activeView === 'original' }" :disabled="!selected.original_url" @click="switchView('original')">全景原视频</button><button :class="{ active: activeView === 'focused' }" :disabled="!selected.focused_url" @click="switchView('focused')">聚焦特写</button></div></div>
        <video v-if="videoUrl" :key="videoUrl" ref="playback" class="archive-player" :src="videoUrl" :poster="selected.thumbnail_url || undefined" controls playsinline preload="metadata" @loadedmetadata="restorePlayback"></video>
        <div v-else class="archive-player-empty"><Film :size="30" /><span>{{ selected.status === 'processing' ? '视频正在转码，完成后自动更新' : selected.status === 'recording' ? '录制进行中，离场归档后可回看' : '这段视频暂不可播放' }}</span></div>
        <div class="recording-playback-facts"><div><span>通行时长</span><strong>{{ durationLabel(selected.duration_seconds) }}</strong></div><div><span>摄像头</span><strong>{{ selected.camera_id }}</strong></div><div><span>轨迹 ID</span><strong>{{ selected.track_ids.join(', ') || '--' }}</strong></div><div><span>归档状态</span><strong>{{ statusLabel(selected.status) }} · {{ reasonLabel(selected.reason) }}</strong></div></div>
        <p class="setting-hint">全景保存采集到的完整画面；聚焦视频随取景策略裁剪放大。切换视角时保留播放位置。</p>
      </div>
      <template #footer><div class="recording-dialog-footer"><a v-if="selected?.original_url" class="download-link" :href="downloadUrl(selected.original_url)" download><Download :size="15" />下载全景原视频</a><a v-if="selected?.focused_url" class="download-link" :href="downloadUrl(selected.focused_url)" download><Download :size="15" />下载聚焦特写</a><el-button @click="selected = null">关闭</el-button></div></template>
    </el-dialog>
  </div>
</template>
