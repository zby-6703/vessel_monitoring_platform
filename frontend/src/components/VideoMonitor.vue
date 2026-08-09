<script setup lang="ts">
import mpegts from 'mpegts.js'
import { Maximize, Pause, Play, ScanLine } from 'lucide-vue-next'
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'

interface OverlayBox { type: string; xywh: [number, number, number, number]; confidence: number; class_name?: string }
interface OverlayFrame { source_width: number; source_height: number; processing_ms: number; vessels: Array<{ ship_id: number; boxes: OverlayBox[] }> }
const props = defineProps<{ src?: string; frameImage?: string | null; overlay?: OverlayFrame | null; cameraId: string; configured: boolean; loading?: boolean }>()
const video = ref<HTMLVideoElement>()
const playing = ref(false)
const elapsed = ref(0)
const duration = ref(0)
let player: mpegts.Player | null = null

const liveBoxes = computed(() => {
  if (!props.overlay?.source_width || !props.overlay?.source_height) return []
  const visibleTypes = new Set(['ship', 'ship_name_area', 'waterline_area'])
  return props.overlay.vessels.flatMap((vessel) => vessel.boxes
    .filter((box) => visibleTypes.has(box.type))
    .map((box) => {
    const [x, y, width, height] = box.xywh
    const label = box.type === 'ship'
      ? `ID ${vessel.ship_id}`
      : box.type === 'ship_name_area' ? '船名区域' : '吃水区域'
    return {
      type: box.type,
      label,
      style: {
        left: `${x / props.overlay!.source_width * 100}%`, top: `${y / props.overlay!.source_height * 100}%`,
        width: `${width / props.overlay!.source_width * 100}%`, height: `${height / props.overlay!.source_height * 100}%`,
      },
    }
  }))
})
const progress = computed(() => duration.value > 0 && Number.isFinite(duration.value) ? elapsed.value / duration.value * 100 : 0)
const inferenceFps = computed(() => props.overlay?.processing_ms ? 1000 / props.overlay.processing_ms : null)
const monitorStyle = computed(() => {
  if (!props.overlay?.source_width || !props.overlay?.source_height) return undefined
  return { aspectRatio: `${props.overlay.source_width} / ${props.overlay.source_height}` }
})

function setupPlayer() {
  player?.destroy(); player = null
  if (!video.value || !props.src || props.frameImage) return
  if (props.src.includes('.flv') && mpegts.getFeatureList().mseLivePlayback) {
    player = mpegts.createPlayer({ type: 'flv', isLive: true, url: props.src }, { enableStashBuffer: false, liveBufferLatencyChasing: true })
    player.attachMediaElement(video.value); player.load(); player.play()
  }
}
function toggle() { if (video.value?.paused) video.value.play(); else video.value?.pause() }
function fullscreen() { video.value?.parentElement?.requestFullscreen() }
onMounted(setupPlayer)
watch(() => [props.src, props.frameImage], setupPlayer)
onBeforeUnmount(() => { player?.destroy() })
</script>

<template>
  <div class="video-monitor" :style="monitorStyle">
    <img v-if="frameImage" class="playback-frame" :src="frameImage" alt="本地视频实时推理帧" />
    <video v-else-if="src" ref="video" :src="src.includes('.flv') ? undefined : src" muted autoplay playsinline @play="playing = true" @pause="playing = false" @timeupdate="elapsed = video?.currentTime || 0" @durationchange="duration = video?.duration || 0"></video>
    <div v-else class="monitor-loading" role="status" aria-live="polite"><ScanLine :size="28" /><strong>正在加载本地视频推理</strong><span>正在初始化模型并等待首帧真实结果</span></div>
    <div class="video-vignette"></div>
    <div v-for="(box, index) in liveBoxes" :key="`${box.type}-${index}`" class="detection-box" :class="`${box.type.replace(/_/g, '-')}-box`" :style="box.style"><span>{{ box.label }}</span></div>
    <div class="camera-badge"><span class="status-dot" :class="configured ? 'online' : 'offline'"></span>{{ cameraId }} · {{ configured ? '已配置' : '未配置' }}</div>
    <div v-if="overlay" class="inference-badge"><ScanLine :size="14" /> {{ inferenceFps?.toFixed(1) }} FPS <span>{{ overlay.processing_ms.toFixed(1) }} ms</span></div>
    <div v-if="src && !frameImage" class="video-controls">
      <button class="icon-button" :aria-label="playing ? '暂停' : '播放'" @click="toggle"><Pause v-if="playing" :size="18" /><Play v-else :size="18" /></button>
      <div class="timeline"><span :style="{ width: `${progress}%` }"></span></div>
      <time>{{ Number.isFinite(elapsed) ? `${Math.floor(elapsed / 60).toString().padStart(2, '0')}:${Math.floor(elapsed % 60).toString().padStart(2, '0')}` : 'LIVE' }}</time>
      <button class="icon-button" aria-label="全屏" @click="fullscreen"><Maximize :size="18" /></button>
    </div>
  </div>
</template>
