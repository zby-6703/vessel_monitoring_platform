<script setup lang="ts">
import { computed, reactive } from 'vue'
import { RefreshCw } from 'lucide-vue-next'
import type { FocusRegion, FocusStatus, PtzAction, PtzStatus, RecordingStatus } from '../api'

const props = defineProps<{
  cameraSource: boolean
  sourceRunning: boolean
  previewAvailable: boolean
  opticalOwned: boolean
  ptz: PtzStatus | null
  status: RecordingStatus | null
  focusStatus: FocusStatus | null
  busy: boolean
}>()
const emit = defineEmits<{
  ptz: [action: PtzAction]
  zoom: [factor: number]
  refresh: []
  'pick-focus': []
  'stop-focus': []
  'focus-region': [region: FocusRegion]
  'show-recording': []
}>()

const directions = [
  { action: 'pan_left', label: '向左' }, { action: 'pan_right', label: '向右' },
  { action: 'tilt_up', label: '向上' }, { action: 'tilt_down', label: '向下' },
] as const
const region = reactive({ left: .25, top: .25, right: .75, bottom: .75 })
const factors = computed(() => (props.ptz?.magnifications || []).filter(factor => factor >= 1 && factor <= 40).sort((a, b) => a - b))
const factor = computed(() => props.status ? props.status.current_zoom_factor : props.ptz?.measured_zoom_factor)
const smaller = computed(() => factors.value.filter(value => factor.value != null && value < factor.value).slice(-1)[0])
const larger = computed(() => factors.value.find(value => factor.value != null && value > factor.value))
const focusing = computed(() => props.focusStatus?.status === 'focusing')
const moving = computed(() => Boolean(props.status?.manual_zoom_active || props.busy))
const blocked = computed(() => !props.cameraSource || props.opticalOwned || moving.value || focusing.value)
const calibrated = computed(() => props.ptz?.absolute_zoom_supported && props.ptz.calibrated)
const canFocusRegion = computed(() => !blocked.value && props.sourceRunning && props.ptz?.region_focus_supported)
const stopAvailable = computed(() => props.cameraSource && (props.opticalOwned || props.ptz?.supported ||
  props.ptz?.pan_tilt_supported || props.ptz?.absolute_zoom_supported || props.ptz?.focus_supported))
</script>

<template>
  <section class="recording-controls-body" aria-labelledby="manual-camera-title">
    <div class="manual-control-header">
      <h3 id="manual-camera-title" class="control-heading">手动镜头</h3>
      <el-button size="small" :loading="busy" :disabled="!cameraSource" @click="emit('refresh')"><RefreshCw :size="13" />检查镜头</el-button>
    </div>
    <p v-if="!cameraSource" class="setting-hint">切换视频源到现场摄像头，即可使用手动镜头。</p>
    <div v-else-if="opticalOwned" class="manual-control-lock" role="status">
      <span>自动光学循环占用镜头。关闭循环录制并等待归位后可手动操作。</span>
      <el-button text @click="emit('show-recording')">前往循环录制</el-button>
    </div>
    <p v-else-if="ptz?.error" class="recording-error" role="alert">{{ ptz.error }}</p>

    <section class="manual-control-group" aria-label="光学变焦">
      <h4>光学变焦</h4>
      <p class="setting-hint" role="status">{{ moving ? '正在确认镜头状态' : factor == null ? '当前倍率待确认' : `当前光学 ${factor}×` }}</p>
      <template v-if="calibrated && factors.length">
        <div class="ptz-buttons" role="group" aria-label="已标定光学倍率">
          <el-button
            v-for="stop in factors"
            :key="stop"
            :type="factor === stop ? 'primary' : undefined"
            :aria-pressed="factor === stop"
            :disabled="blocked"
            @click="emit('zoom', stop)"
          >{{ stop }}×</el-button>
        </div>
        <div class="ptz-buttons">
          <el-button :disabled="blocked || smaller == null" @click="smaller != null && emit('zoom', smaller)">缩小一档</el-button>
          <el-button :disabled="blocked || larger == null" @click="larger != null && emit('zoom', larger)">放大一档</el-button>
        </div>
      </template>
      <p v-else-if="cameraSource" class="setting-hint">{{ ptz?.zoom_error || '检查镜头后可查看已标定的光学倍率。' }}</p>
      <div v-if="ptz?.supported && !calibrated" class="ptz-buttons">
        <el-button :disabled="blocked" @click="emit('ptz', 'zoom_out')">光学缩小</el-button>
        <el-button :disabled="blocked" @click="emit('ptz', 'zoom_in')">光学放大</el-button>
      </div>
    </section>

    <section class="manual-control-group" aria-label="云台与机位">
      <h4>云台与机位</h4>
      <div class="ptz-buttons">
        <el-button v-for="direction in directions" :key="direction.action" :disabled="blocked || !ptz?.pan_tilt_supported" @click="emit('ptz', direction.action)">{{ direction.label }}</el-button>
      </div>
      <div class="ptz-buttons">
        <el-button :disabled="blocked || !ptz?.absolute_pan_tilt_supported" @click="emit('ptz', 'save_view')">保存监测机位</el-button>
        <el-button :disabled="blocked || !ptz?.absolute_pan_tilt_supported" @click="emit('ptz', 'restore_view')">恢复监测机位</el-button>
        <el-button :disabled="blocked || !ptz?.supported" @click="emit('ptz', 'home')">回设备原位</el-button>
      </div>
      <p class="setting-hint">方向按钮每次小幅移动。保存监测机位前先回到已标定的 1×。</p>
    </section>

    <el-collapse class="recording-advanced">
      <el-collapse-item name="focus" title="镜头对焦">
        <div class="ptz-buttons">
          <el-button :disabled="blocked || !ptz?.focus_supported" @click="emit('ptz', 'focus_near')">近焦</el-button>
          <el-button :disabled="blocked || !ptz?.focus_supported" @click="emit('ptz', 'focus_far')">远焦</el-button>
          <el-button :disabled="blocked || !ptz?.autofocus_supported" @click="emit('ptz', 'autofocus')">自动对焦</el-button>
          <el-button :disabled="!canFocusRegion || !previewAvailable" @click="emit('pick-focus')">画面框选区域</el-button>
          <el-button :disabled="!cameraSource || !focusing" @click="emit('stop-focus')">停止对焦</el-button>
        </div>
        <p v-if="focusStatus?.error" class="recording-error" role="alert">{{ focusStatus.error }}</p>
        <p v-else-if="focusing" class="setting-hint" role="status">正在对焦</p>
        <p v-else-if="focusStatus?.status === 'completed'" class="setting-hint" role="status">选区对焦已完成</p>
        <p v-if="ptz?.region_focus_error || ptz?.focus_error" class="setting-hint">{{ ptz.region_focus_error || ptz.focus_error }}</p>
        <el-form label-position="top" class="recording-settings-grid" :disabled="!canFocusRegion">
          <el-form-item v-for="field in (['left', 'top', 'right', 'bottom'] as const)" :key="field" :label="({ left: '左边界 (0–1)', top: '上边界 (0–1)', right: '右边界 (0–1)', bottom: '下边界 (0–1)' })[field]">
            <el-input-number v-model="region[field]" :min="0" :max="1" :step=".05" :precision="2" />
          </el-form-item>
        </el-form>
        <el-button :disabled="!canFocusRegion || region.left >= region.right || region.top >= region.bottom" @click="emit('focus-region', { ...region })">按坐标对焦</el-button>
      </el-collapse-item>
    </el-collapse>
    <div class="ptz-buttons">
      <el-button type="danger" plain :disabled="!stopAvailable" @click="emit('ptz', 'stop')">停止镜头运动</el-button>
    </div>
  </section>
</template>
