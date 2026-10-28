<script setup lang="ts">
import mpegts from 'mpegts.js'
import { Crosshair, Maximize, Pause, Play, ScanLine, ZoomIn } from 'lucide-vue-next'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { cyclePhaseLabel, type RecordingSettings, type RecordingStatus } from '../api'

interface OverlayBox { type: string; xywh: [number, number, number, number]; confidence: number; class_name?: string }
interface OverlayFrame { source_width: number; source_height: number; processing_ms: number; vessels: Array<{ ship_id: number; confidence: number; boxes: OverlayBox[] }> }
const props = defineProps<{ src?: string; frameImage?: string | null; overlay?: OverlayFrame | null; cameraId: string; configured: boolean; loading?: boolean; focusSettings?: RecordingSettings; recording?: boolean; recordingStatus?: RecordingStatus | null }>()
const emit = defineEmits<{ 'focus-center': [value: { x: number; y: number }] }>()
const root = ref<HTMLDivElement>()
const video = ref<HTMLVideoElement>()
const playing = ref(false)
const elapsed = ref(0)
const duration = ref(0)
const sourceSize = ref({ width: 16, height: 9 })
const focused = ref(false)
const mediaError = ref(false)
const optical = computed(() => props.focusSettings?.recording_mode === 'optical_cycle')
const selectedTarget = computed(() => props.recordingStatus?.locked_target || props.recordingStatus?.adaptive_selection?.selected || null)
const stationaryCount = computed(() => props.recordingStatus?.adaptive_selection?.targets.filter((target) => target.motion_state === 'stationary').length || 0)
let player: mpegts.Player | null = null
const clamp = (value: number, low: number, high: number) => Math.max(low, Math.min(value, high))
const dimensions = computed(() => props.overlay?.source_width && props.overlay?.source_height ? { width: props.overlay.source_width, height: props.overlay.source_height } : sourceSize.value)
const crop = computed(() => {
  const settings = props.focusSettings
  if (!settings || optical.value) return { x: 0, y: 0, width: 1, height: 1 }
  let size = 1 / Math.max(1, settings.zoom_factor)
  let cx = settings.center_x, cy = settings.center_y
  if (settings.focus_mode === 'auto') {
    const boxes = props.overlay?.vessels.filter((vessel) => vessel.confidence >= settings.confidence_threshold && vessel.boxes.some((box) => box.type === 'ship'))
      .flatMap((vessel) => vessel.boxes).filter((box) => ['ship', 'ship_name_area', 'waterline_area', 'draft_mark'].includes(box.type) && box.xywh.length === 4 && box.xywh.every(Number.isFinite) && box.xywh[2] > 0 && box.xywh[3] > 0) || []
    if (!boxes.length) return { x: 0, y: 0, width: 1, height: 1 }
    const left = Math.min(...boxes.map((box) => Math.max(0, box.xywh[0] / dimensions.value.width)))
    const top = Math.min(...boxes.map((box) => Math.max(0, box.xywh[1] / dimensions.value.height)))
    const right = Math.max(...boxes.map((box) => Math.min(1, (box.xywh[0] + box.xywh[2]) / dimensions.value.width)))
    const bottom = Math.max(...boxes.map((box) => Math.min(1, (box.xywh[1] + box.xywh[3]) / dimensions.value.height)))
    cx = (left + right) / 2; cy = (top + bottom) / 2
    size = Math.min(1, Math.max(size, (right - left) * (1 + settings.padding * 2), (bottom - top) * (1 + settings.padding * 2)))
  }
  return { x: clamp(cx - size / 2, 0, 1 - size), y: clamp(cy - size / 2, 0, 1 - size), width: size, height: size }
})
const stageStyle = computed(() => !optical.value && focused.value ? { transform: `translate(${-crop.value.x / crop.value.width * 100}%, ${-crop.value.y / crop.value.height * 100}%) scale(${1 / crop.value.width})` } : undefined)
const cropStyle = computed(() => ({ left: `${crop.value.x * 100}%`, top: `${crop.value.y * 100}%`, width: `${crop.value.width * 100}%`, height: `${crop.value.height * 100}%` }))
const liveBoxes = computed(() => (props.overlay?.vessels || []).flatMap((vessel) => vessel.boxes
  .filter((box) => ['ship', 'ship_name_area', 'waterline_area'].includes(box.type))
  .map((box) => {
    const [x, y, width, height] = box.xywh
    return { type: box.type, label: box.type === 'ship' ? `ID ${vessel.ship_id}` : box.type === 'ship_name_area' ? '船名区域' : '吃水区域', style: {
      left: `${x / dimensions.value.width * 100}%`, top: `${y / dimensions.value.height * 100}%`, width: `${width / dimensions.value.width * 100}%`, height: `${height / dimensions.value.height * 100}%`,
    } }
  })))
const progress = computed(() => duration.value > 0 && Number.isFinite(duration.value) ? elapsed.value / duration.value * 100 : 0)
const inferenceFps = computed(() => props.overlay?.processing_ms ? 1000 / props.overlay.processing_ms : null)

function imageLoaded(event: Event) {
  const image = event.target as HTMLImageElement
  if (image.naturalWidth && image.naturalHeight) sourceSize.value = { width: image.naturalWidth, height: image.naturalHeight }
}
function videoLoaded() { if (video.value?.videoWidth) sourceSize.value = { width: video.value.videoWidth, height: video.value.videoHeight } }
async function setupPlayer() {
  player?.destroy(); player = null; mediaError.value = false
  await nextTick()
  if (!video.value || !props.src || props.frameImage) return
  if (props.src.includes('.flv') && mpegts.getFeatureList().mseLivePlayback) {
    player = mpegts.createPlayer({ type: 'flv', isLive: true, url: props.src }, { enableStashBuffer: false, liveBufferLatencyChasing: true })
    player.on(mpegts.Events.ERROR, () => { mediaError.value = true })
    player.attachMediaElement(video.value); player.load(); player.play()?.catch(() => { mediaError.value = true })
  }
}
function chooseCenter(event: MouseEvent) {
  if (optical.value || props.focusSettings?.focus_mode !== 'manual' || !props.frameImage && !props.src) return
  const bounds = root.value!.getBoundingClientRect()
  let x = clamp((event.clientX - bounds.left) / bounds.width, 0, 1)
  let y = clamp((event.clientY - bounds.top) / bounds.height, 0, 1)
  if (focused.value) { x = crop.value.x + x * crop.value.width; y = crop.value.y + y * crop.value.height }
  emit('focus-center', { x, y })
}
function toggle() { if (video.value?.paused) video.value.play().catch(() => { mediaError.value = true }); else video.value?.pause() }
function fullscreen() { root.value?.requestFullscreen()?.catch(() => {}) }
onMounted(setupPlayer)
// JPEG updates must not rebuild the media player on every frame.
watch(() => [props.src, Boolean(props.frameImage)], setupPlayer)
watch(() => props.cameraId, () => { sourceSize.value = { width: 16, height: 9 } })
onBeforeUnmount(() => { player?.destroy() })
</script>

<template>
  <div ref="root" class="video-monitor focus-monitor" :class="{ 'manual-focus': !optical && focusSettings?.focus_mode === 'manual' }" :style="{ aspectRatio: `${dimensions.width} / ${dimensions.height}` }" @click="chooseCenter">
    <div class="video-stage" :style="stageStyle">
      <img v-if="frameImage" class="playback-frame" :src="frameImage" alt="实时推理画面" @load="imageLoaded" />
      <video v-else-if="src" ref="video" :src="src.includes('.flv') ? undefined : src" muted autoplay playsinline @loadedmetadata="videoLoaded" @error="mediaError = true" @play="playing = true" @pause="playing = false" @timeupdate="elapsed = video?.currentTime || 0" @durationchange="duration = video?.duration || 0"></video>
      <div v-for="(box, index) in liveBoxes" :key="`${box.type}-${index}`" class="detection-box" :class="`${box.type.replace(/_/g, '-')}-box`" :style="box.style"><span>{{ box.label }}</span></div>
      <div v-if="!optical && focusSettings && !focused && (frameImage || src) && crop.width < .99" class="focus-crop-outline" :style="cropStyle"><span><Crosshair :size="11" />录制特写范围</span></div>
    </div>
    <div v-if="(!src && !frameImage) || mediaError" class="monitor-loading" role="status" aria-live="polite"><ScanLine :size="28" /><strong>{{ mediaError ? '画面暂不可播放' : loading ? '正在连接并初始化模型' : '等待监测画面' }}</strong><span>{{ mediaError ? '启动摄像头采集可显示实时推理帧，请同时检查流地址' : '收到真实视频帧后显示检测与聚焦范围' }}</span></div>
    <div class="video-vignette"></div>
    <div class="camera-badge"><span class="status-dot" :class="configured ? 'online' : 'offline'"></span>{{ cameraId }}<b v-if="recording" class="recording-live-indicator">● REC</b></div>
    <div v-if="overlay" class="inference-badge"><ScanLine :size="14" /> {{ inferenceFps?.toFixed(1) }} FPS <span>{{ overlay.processing_ms.toFixed(1) }} ms</span></div>
    <div v-if="optical" class="optical-monitor-state" role="status"><ZoomIn :size="13" /><strong>{{ recordingStatus?.current_zoom_factor == null ? '镜头倍率待确认' : `光学 ${recordingStatus.current_zoom_factor}×` }}</strong><span>{{ recordingStatus?.state === 'disabled' ? '循环未开启' : cyclePhaseLabel(recordingStatus?.phase, recordingStatus?.awaiting_zoomed_target) }}</span></div>
    <div v-if="optical && (selectedTarget || stationaryCount)" class="optical-selection-badge" role="status"><template v-if="selectedTarget">{{ recordingStatus?.locked_target ? '本轮锁定' : '优先候选' }} · 目标 {{ selectedTarget.target_id }}<template v-if="selectedTarget.height_ratio"> · 1× 船高 {{ (selectedTarget.height_ratio * 100).toFixed(1) }}%</template></template><template v-if="stationaryCount">{{ selectedTarget ? ' · ' : '' }}{{ stationaryCount }} 个静止目标</template></div>
    <div v-else-if="focusSettings" class="focus-view-switch" @click.stop>
      <button :class="{ active: !focused }" @click="focused = false">全景</button><button :class="{ active: focused }" @click="focused = true"><ZoomIn :size="13" />聚焦 {{ (1 / crop.width).toFixed(1) }}×</button>
    </div>
    <div v-if="!optical && focusSettings?.focus_mode === 'manual' && (frameImage || src)" class="focus-click-hint">点击画面设置聚焦中心 · 保存后应用到录制</div>
    <div class="video-controls" @click.stop>
      <button v-if="src && !frameImage" class="icon-button" :aria-label="playing ? '暂停' : '播放'" @click="toggle"><Pause v-if="playing" :size="18" /><Play v-else :size="18" /></button>
      <div v-if="src && !frameImage" class="timeline"><span :style="{ width: `${progress}%` }"></span></div>
      <span v-else class="monitor-caption">{{ optical ? '摄像头原画面 · 实际光学取景' : focused ? '数字聚焦预览' : '原始画面 · 检测叠加' }}</span>
      <time v-if="src && !frameImage">{{ Number.isFinite(elapsed) ? `${Math.floor(elapsed / 60).toString().padStart(2, '0')}:${Math.floor(elapsed % 60).toString().padStart(2, '0')}` : 'LIVE' }}</time>
      <button class="icon-button" aria-label="全屏" @click="fullscreen"><Maximize :size="18" /></button>
    </div>
  </div>
</template>
