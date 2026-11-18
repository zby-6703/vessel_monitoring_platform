<script setup lang="ts">
import { Camera, Check, Crosshair, HardDrive, RefreshCw, Settings2, Video, ZoomIn } from 'lucide-vue-next'
import { computed } from 'vue'
import { cyclePhaseLabel, formatBytes, patrolStageLabel, selectionReasonLabel, targetMotionLabel, type AdaptiveTarget, type PtzStatus, type RecordingSettings, type RecordingStatus } from '../api'

const props = defineProps<{ modelValue: RecordingSettings; enabled: boolean; status: RecordingStatus | null; saving: boolean; loaded: boolean; dirty: boolean; ptz: PtzStatus | null; ptzBusy: boolean; sourceMode: 'camera' | 'local'; sourceStatus?: string }>()
const emit = defineEmits<{ 'update:modelValue': [settings: RecordingSettings]; 'toggle-recording': [enabled: boolean]; 'refresh-ptz': [] }>()
function set<K extends keyof RecordingSettings>(field: K, value: RecordingSettings[K]) {
  if (!props.loaded || props.saving) return
  const next = { ...props.modelValue, [field]: value }
  if (next.max_clip_seconds <= next.pre_roll_seconds) next.max_clip_seconds = next.pre_roll_seconds + 1
  if (field === 'recording_mode' && value === 'passage') next.zoom_factor = Math.min(8, next.zoom_factor)
  if (field === 'recording_mode' && value === 'optical_cycle' && magnifications.value.length && !magnifications.value.includes(next.zoom_factor)) next.zoom_factor = magnifications.value.includes(3) ? 3 : magnifications.value[0]!
  if (field === 'min_optical_zoom' && next.min_optical_zoom > next.max_optical_zoom) next.max_optical_zoom = next.min_optical_zoom
  if (field === 'max_optical_zoom' && next.max_optical_zoom < next.min_optical_zoom) next.min_optical_zoom = next.max_optical_zoom
  emit('update:modelValue', next)
}
const optical = computed(() => props.modelValue.recording_mode === 'optical_cycle')
const magnifications = computed(() => (props.ptz?.magnifications || []).filter((factor) => factor > 1 && factor <= 40).sort((a, b) => a - b))
const availableMagnifications = computed(() => magnifications.value.filter((factor) => factor <= props.modelValue.max_optical_zoom))
const ready = computed(() => Boolean(props.ptz?.cycle_supported && (!props.modelValue.auto_horizontal_follow || props.ptz.horizontal_follow_supported) && (props.modelValue.adaptive_zoom_enabled ? availableMagnifications.value.length > 0 : magnifications.value.some((factor) => Math.abs(factor - props.modelValue.zoom_factor) < .001))))
const linesValid = computed(() => props.modelValue.overview_line_left < props.modelValue.overview_line_right && props.modelValue.detail_line_left < props.modelValue.detail_line_right)
const sourceRunning = computed(() => props.sourceStatus === 'running')
const opticalRunning = computed(() => props.sourceMode === 'camera' && props.enabled && props.status?.zoom_driver === 'optical')
const linePatrolRunning = computed(() => props.status?.line_patrol?.enabled === true)
const operationNote = computed(() => {
  if (!props.loaded) return '正在读取已保存的录制策略。'
  if (props.status?.active_id && props.status.media_recording === false) return '录像正在收尾，等待镜头归位。'
  if (!props.enabled) return '开启后按已保存的模式和参数录制。关闭时采集与识别可继续。'
  if (props.sourceStatus === 'reconnecting') return '视频链路中断，等待自动重连。'
  if (props.status?.zoom_recovery?.fallback_recording) return props.status.current_zoom_factor === 1 ? '已确认回到 1×，本轮继续保底录像至目标离场，本轮不再重复放大；保存后重新选船。' : props.status.zoom_recovery.active ? '镜头通信失败，本轮仍在保存实际画面；正在有限重试恢复 1×，不会把未确认的位置当作已到位。' : '镜头位置仍未确认，自动变焦已暂停；本轮继续保底录像至时长上限，等待后续位置复核。'
  if (props.status?.zoom_recovery?.active) return '正在有限重试恢复 1×；确认镜头位置后恢复自动选船。可停止采集取消恢复。'
  if (props.status?.identity_fallback) return '放大后目标关联未确认，正在回到 1× 保留同一段原始视频并重新确认目标；本轮不再放大。全景仍无法确认时会保存视频并标记为中断。'
  if (props.status?.error) return '本轮录制已暂停。'
  if (!sourceRunning.value) return props.sourceMode === 'camera'
    ? '录制策略已开启，正在等待现场视频源。恢复网络后请重新检查镜头能力，再启动摄像头采集；尚未采集时不会生成录像。'
    : '录制策略已开启，等待本地视频回放；本地验证使用数字录制，不驱动镜头。'
  return props.status?.zoom_driver === 'optical' && props.sourceMode === 'camera'
    ? '现场采集与自动录制已开启：先确认 1× 全景，再对符合条件的移动船舶启动循环。'
    : '视频采集与自动录制已开启，等待符合条件的船舶触发。'
})
const activeStep = computed(() => {
  if (linePatrolRunning.value) {
    if (['overview_exit', 'overview_fallback'].includes(props.status?.line_patrol?.stage || '')) return 4
    if (props.status?.phase === 'waiting_body') return 0
    if (props.status?.awaiting_zoomed_target) return 1
  }
  return { overview: 0, initializing: 0, waiting_body: 1, panning: 2, zooming_in: 1, zoomed: 2, zooming_out: 3, rearming: 4, recovering: 3, error: -1 }[props.status?.phase || 'overview']
})
const stages = computed(() => linePatrolRunning.value ? ['原倍率录制 / 船头越线', '放大 / 等待船只进入', '船尾触线 / 仅水平跟随', '放大离场 / 归位检查再放大', '原倍率船尾离场 / 保存整段'] : ['1× 观察移动目标 / 近船优先', '主体入画 / 水平跟随 / 放大', '目标船头至船尾经过', '目标离场 / 回原机位', '保存一段 / 重新选船'])
const stateLabel = computed(() => {
  if (props.status?.error) return '录制异常'
  if (props.status?.active_id && props.status.media_recording === false) return '录像收尾中'
  if (!props.loaded) return '读取中'
  if (!props.enabled) return '已关闭'
  if (props.sourceStatus === 'reconnecting') return '等待重连'
  if (!sourceRunning.value) return '等待视频源'
  if (props.sourceMode === 'camera' && props.status?.zoom_driver === 'optical') {
    return cyclePhaseLabel(props.status.phase, props.status.awaiting_zoomed_target)
  }
  return props.status?.media_recording ? '正在录制' : '等待目标'
})
const storagePercent = computed(() => Math.min(100, (props.status?.storage_bytes || 0) / (props.modelValue.max_storage_gb * 1024 ** 3) * 100))
const capabilityNote = computed(() => props.ptz?.zoom_error || props.ptz?.error || (!props.ptz ? '正在读取摄像头光学变焦能力' : !props.ptz.absolute_zoom_supported ? '摄像头尚未确认支持绝对光学变焦' : !props.ptz.calibrated ? '请先在服务端完成 1× 与目标倍率的镜头位置标定' : !ready.value ? props.modelValue.adaptive_zoom_enabled ? '设置范围内暂无已标定的放大档位' : '所选倍率未标定，请选择已标定倍率' : props.modelValue.adaptive_zoom_enabled ? `自适应可用档位：${availableMagnifications.value.map((factor) => `${factor}×`).join(' / ')}` : '绝对光学变焦与倍率标定已就绪'))
const selection = computed(() => props.sourceMode === 'camera' ? props.status?.adaptive_selection : null)
const lockedTarget = computed(() => props.sourceMode === 'camera' ? props.status?.locked_target || null : null)
const currentTarget = computed(() => lockedTarget.value || selection.value?.selected || selection.value?.targets.find((target) => target.target_id === selection.value?.selected_target_id) || null)
const targets = computed(() => [...(selection.value?.targets || [])].sort((a, b) => Number(b.target_id === currentTarget.value?.target_id) - Number(a.target_id === currentTarget.value?.target_id)).slice(0, 6))
const parkedCount = computed(() => selection.value?.targets.filter((target) => target.motion_state === 'stationary').length || 0)
const heightLabel = (target: AdaptiveTarget) => typeof target.height_ratio === 'number' ? `${(target.height_ratio * 100).toFixed(1)}%` : '--'
const targetLabel = (target: AdaptiveTarget) => `目标 ${target.target_id}${target.ship_id != null ? ` · 轨迹 ${target.ship_id}` : ''}`
</script>

<template>
  <section class="recording-controls recording-controls-body" :aria-busy="!loaded || saving">
    <div class="recording-enable">
      <div>
        <h3 class="control-heading">循环录制</h3>
        <span class="recording-state" :class="status?.state" role="status"><i></i>{{ stateLabel }}</span>
      </div>
      <el-switch
        :model-value="enabled"
        :disabled="!loaded || saving"
        aria-label="开启循环录制"
        @update:model-value="emit('toggle-recording', Boolean($event))"
      />
    </div>
    <p class="setting-hint" role="status">{{ operationNote }}</p>
    <p v-if="status?.error" class="recording-error" role="alert">{{ status.error }}</p>
    <el-radio-group :model-value="modelValue.recording_mode" :disabled="!loaded || saving" class="focus-mode-options recording-mode-options" aria-label="录制模式" @update:model-value="set('recording_mode', $event as 'optical_cycle' | 'passage')">
      <el-radio-button value="optical_cycle">光学循环</el-radio-button>
      <el-radio-button value="passage">数字取景</el-radio-button>
    </el-radio-group>
    <p v-if="dirty" class="setting-hint">开关即时生效。录制模式和参数修改需保存并应用。</p>
    <p v-if="sourceMode === 'local' && optical" class="setting-hint">本地视频使用数字取景录制。光学循环适用于现场摄像头。</p>
    <template v-if="opticalRunning && sourceRunning">
      <ol class="optical-cycle-stages" aria-label="光学循环录制顺序"><li v-for="(stage, index) in stages" :key="stage" :class="{ active: enabled && status?.state !== 'disabled' && status?.state !== 'error' && index === activeStep, passed: enabled && status?.state !== 'disabled' && index < activeStep }"><span><Check v-if="enabled && index < activeStep" :size="11" /><template v-else>{{ index + 1 }}</template></span><strong>{{ stage }}</strong></li></ol>
    </template>
    <el-collapse class="recording-advanced">
      <el-collapse-item v-if="optical" name="optical-magnification" title="自动光学倍率">
        <el-radio-group :model-value="modelValue.adaptive_zoom_enabled" :disabled="!loaded || saving" class="focus-mode-options adaptive-mode-options" aria-label="光学倍率策略" @update:model-value="set('adaptive_zoom_enabled', Boolean($event))"><el-radio-button :value="true">按船高自适应</el-radio-button><el-radio-button :value="false">固定倍率</el-radio-button></el-radio-group>
        <template v-if="modelValue.adaptive_zoom_enabled">
          <div class="optical-zoom-setting"><label>目标船体画面高度</label><el-input-number :model-value="Math.round(modelValue.target_height_ratio * 100)" :min="15" :max="100" :step="5" :disabled="!loaded || saving" controls-position="right" aria-label="目标船体高度占画面百分比" @update:model-value="set('target_height_ratio', Number($event) / 100)" /><span>建议倍率 = 目标高度占比 ÷ 1× 船体高度占比；{{ modelValue.zoom_view_limits_enabled ? '当前开启完整船体竖直视野限制' : '已解除竖直位置与视野边界限制，允许局部取景' }}</span></div>
          <div class="adaptive-zoom-range"><label>优先最低倍率<el-input-number :model-value="modelValue.min_optical_zoom" :min="1.1" :max="40" :step=".1" :precision="1" :disabled="!loaded || saving" controls-position="right" @update:model-value="set('min_optical_zoom', Number($event))" /></label><label>最大倍率<el-input-number :model-value="modelValue.max_optical_zoom" :min="1.1" :max="40" :step=".5" :precision="1" :disabled="!loaded || saving" controls-position="right" @update:model-value="set('max_optical_zoom', Number($event))" /></label></div>
          <p class="setting-hint">按稳定船高选择已标定档位，例如计算建议 6.15× 时可选择已标定的 6×，不插值猜测镜头位置。{{ modelValue.line_patrol_enabled || modelValue.detail_capture_priority ? '每轮放大后保持倍率到目标离开该放大画面。' : '录制中锁定目标，倍率在冷却与滞回条件下受控更新。' }}没有可用档位时保留 1×。</p>
        </template>
        <div v-else class="optical-zoom-setting"><label>目标光学倍率</label><el-select v-if="magnifications.length" :model-value="modelValue.zoom_factor" :disabled="!loaded || saving" aria-label="目标光学倍率" @update:model-value="set('zoom_factor', Number($event))"><el-option v-for="factor in magnifications" :key="factor" :value="factor" :label="`${factor}×`" /></el-select><el-input-number v-else :model-value="modelValue.zoom_factor" :min="1.1" :max="40" :step=".5" :precision="1" :disabled="!loaded || saving" controls-position="right" aria-label="待标定目标光学倍率" @update:model-value="set('zoom_factor', Number($event))" /><span>回位固定为 1×；{{ modelValue.zoom_view_limits_enabled ? '开启完整船体竖直取景限制' : '按已标定目标倍率放大，允许局部船体取景' }}</span></div>
        <el-form v-if="modelValue.adaptive_zoom_enabled && !modelValue.line_patrol_enabled && !modelValue.detail_capture_priority" label-position="top" class="recording-settings-grid" :disabled="!loaded || saving">
          <el-form-item label="变倍冷却时长 (秒)"><el-input-number :model-value="modelValue.zoom_adjust_cooldown_seconds" :min="1" :max="30" controls-position="right" @update:model-value="set('zoom_adjust_cooldown_seconds', Number($event))" /></el-form-item>
          <el-form-item label="变倍滞回幅度 (%)"><el-input-number :model-value="Math.round(modelValue.zoom_adjust_hysteresis * 100)" :min="5" :max="50" :step="5" controls-position="right" @update:model-value="set('zoom_adjust_hysteresis', Number($event) / 100)" /></el-form-item>
        </el-form>
        <div class="recognition-toggle"><strong>按完整船体竖直视野限制倍率</strong><el-switch :model-value="modelValue.zoom_view_limits_enabled" aria-label="按完整船体竖直视野限制倍率" @update:model-value="set('zoom_view_limits_enabled', Boolean($event))" /></div>
        <p class="setting-hint">关闭后，船舶竖直位置和竖直视野边界不再压低倍率；倍率由船高、已标定档位和设置上限决定，允许局部船体取景。分界线模式在每轮放大后保持倍率，不通过上下移动补偿。</p>
        <div class="optical-capability" :class="{ ready }" role="status"><Camera :size="15" /><span>{{ capabilityNote }}</span><el-button text size="small" :loading="ptzBusy" aria-label="重新检查光学变焦能力" @click="emit('refresh-ptz')"><RefreshCw :size="13" /></el-button></div>
        <p v-if="!ready" class="setting-hint">离线时可以保存录制策略；恢复采集后仍需核验镜头能力，核验通过才会执行光学变焦。</p>
      </el-collapse-item>
      <el-collapse-item v-if="optical" name="optical-follow" title="自动跟随与分界线">
        <section class="realtime-recognition" aria-label="自动左右跟随与入画等待">
          <div class="recognition-toggle"><strong>自动左右跟随船舶</strong><el-switch :model-value="modelValue.auto_horizontal_follow" :disabled="!loaded || saving" aria-label="自动左右跟随船舶" @update:model-value="set('auto_horizontal_follow', Boolean($event))" /></div>
          <p class="setting-hint">锁定同一艘船，仅做水平跟随，不主动调整上下取景；缩回原倍率时恢复原水平机位。分界线模式由放大画面中的船尾触线触发旋转。</p>
          <p v-if="modelValue.auto_horizontal_follow && ptz && !ptz.horizontal_follow_supported" class="recording-error">尚未确认设备支持 FOV 水平跟随与绝对位置反馈，请重新检查镜头能力。</p>
          <div class="recognition-toggle"><strong>分界线触发 · 同船多轮放大</strong><el-switch :model-value="modelValue.line_patrol_enabled" :disabled="!loaded || saving" aria-label="分界线触发多轮放大" @update:model-value="set('line_patrol_enabled', Boolean($event))" /></div>
          <template v-if="modelValue.line_patrol_enabled">
            <p class="setting-hint">船头越过原倍率入口线后放大。船尾越过放大画面的第一条线后开始跟随，达到旋转上限后保持倍率至船尾离场。缩回后，根据船尾是否越过原倍率出口线决定是否继续放大。</p>
            <el-form label-position="top" class="recording-settings-grid" :disabled="!loaded || saving">
              <el-form-item label="原倍率左分界线 (画面 x %)"><el-input-number :model-value="Number((modelValue.overview_line_left * 100).toFixed(2))" :min="1" :max="99" :step="1" :precision="2" controls-position="right" @update:model-value="set('overview_line_left', Number($event) / 100)" /></el-form-item>
              <el-form-item label="原倍率右分界线 (画面 x %)"><el-input-number :model-value="Number((modelValue.overview_line_right * 100).toFixed(2))" :min="1" :max="99" :step="1" :precision="2" controls-position="right" @update:model-value="set('overview_line_right', Number($event) / 100)" /></el-form-item>
              <el-form-item label="放大画面左分界线 (画面 x %)"><el-input-number :model-value="Number((modelValue.detail_line_left * 100).toFixed(2))" :min="1" :max="99" :step="1" :precision="2" controls-position="right" @update:model-value="set('detail_line_left', Number($event) / 100)" /></el-form-item>
              <el-form-item label="放大画面右分界线 (画面 x %)"><el-input-number :model-value="Number((modelValue.detail_line_right * 100).toFixed(2))" :min="1" :max="99" :step="1" :precision="2" controls-position="right" @update:model-value="set('detail_line_right', Number($event) / 100)" /></el-form-item>
              <el-form-item v-if="modelValue.auto_horizontal_follow" label="最大左旋转 (云台全行程 %)"><el-input-number :model-value="modelValue.pan_max_left_travel_ratio * 100" :min="0" :max="40" controls-position="right" @update:model-value="set('pan_max_left_travel_ratio', Number($event) / 100)" /></el-form-item>
              <el-form-item v-if="modelValue.auto_horizontal_follow" label="最大右旋转 (云台全行程 %)"><el-input-number :model-value="modelValue.pan_max_right_travel_ratio * 100" :min="0" :max="40" controls-position="right" @update:model-value="set('pan_max_right_travel_ratio', Number($event) / 100)" /></el-form-item>
            </el-form>
            <p v-if="!linesValid" class="recording-error">每个画面空间的左分界线必须小于右分界线。</p>
            <p v-if="status?.line_patrol?.enabled && status.active_id" class="setting-hint" role="status">{{ patrolStageLabel(status.line_patrol.stage) }} · 已放大 {{ status.line_patrol.pass_count }} 轮 · {{ status.line_patrol.direction === 'left_to_right' ? '从左向右' : status.line_patrol.direction === 'right_to_left' ? '从右向左' : '方向待确认' }}</p>
          </template>
          <template v-if="!modelValue.line_patrol_enabled">
          <div class="recognition-toggle"><strong>细节采集优先：放大后跟随至离场</strong><el-switch :model-value="modelValue.detail_capture_priority" :disabled="!loaded || saving" aria-label="细节采集优先" @update:model-value="set('detail_capture_priority', Boolean($event))" /></div>
          <p class="setting-hint">开启后先等待合适位置光学放大，再水平跟船；必要时在放大前做一次水平对准。本轮保持已选光学倍率，达到水平范围后停转并保持放大，目标离开画面并确认离场后才回到 1× 和原水平机位。适合采集船名、吃水刻度视频；停止、故障和最长录制保护仍生效。关闭后恢复录制中的自适应变倍。切换模式会结束当前轮并重新初始化。</p>
          <p v-if="status?.zoom_locked_until_departure" class="setting-hint" role="status">{{ status?.horizontal_follow?.limited ? '已达到水平范围，停转并保持放大录像，等待船尾离场。' : '已锁定本轮光学倍率，正在水平跟随采集细节。' }}</p>
          <p v-else-if="status?.horizontal_follow?.limited" class="setting-hint">已达到水平跟随范围，本轮继续录像并等待目标离场。</p>
          <div class="recognition-toggle"><strong>等待船舶主体入画后放大</strong><el-switch :model-value="modelValue.wait_full_body" :disabled="!loaded || saving" aria-label="等待船舶主体入画后放大" @update:model-value="set('wait_full_body', Boolean($event))" /></div>
          <p class="setting-hint">先以 1× 录制，主体入画经两次观测短暂确认即可放大；等待 {{ modelValue.body_entry_max_wait_seconds }} 秒后，已有较大船体进入时可先尝试不超过 2×，无需一直等待完整船尾。船头刚入画、船体两端均被截断或没有合适档位时继续全景录像。放大后至少观察 {{ modelValue.zoom_min_hold_seconds }} 秒，连续 {{ modelValue.departure_confirm_seconds }} 秒且至少 {{ modelValue.departure_min_observations }} 次新观测未见目标后才确认离场；目标关联不明最多等待 {{ modelValue.target_acquire_timeout_seconds }} 秒。</p>
          <p v-if="status?.waiting_full_body && status.zoom_wait_reason" class="setting-hint" role="status">当前等待原因：{{ selectionReasonLabel(status.zoom_wait_reason) }}</p>
          </template>
          <el-form label-position="top" class="recording-settings-grid" :disabled="!loaded || saving">
            <el-form-item label="放大后最短观察 (秒)"><el-input-number :model-value="modelValue.zoom_min_hold_seconds" :min="0" :max="120" controls-position="right" @update:model-value="set('zoom_min_hold_seconds', Number($event))" /></el-form-item>
            <el-form-item label="放大后目标等待 (秒)"><el-input-number :model-value="modelValue.target_acquire_timeout_seconds" :min="3" :max="180" controls-position="right" @update:model-value="set('target_acquire_timeout_seconds', Number($event))" /></el-form-item>
          </el-form>
          <el-collapse class="recording-advanced"><el-collapse-item name="horizontal-policy" title="左右跟随与入画参数">
            <el-form label-position="top" class="recording-settings-grid" :disabled="!loaded || saving">
              <el-form-item v-if="!modelValue.line_patrol_enabled" label="中心不动作区 (左右各 %)"><el-input-number :model-value="modelValue.pan_dead_zone * 100" :min="5" :max="35" controls-position="right" @update:model-value="set('pan_dead_zone', Number($event) / 100)" /></el-form-item>
              <el-form-item label="每次最多水平修正 (画面宽 %)"><el-input-number :model-value="modelValue.pan_step_limit * 50" :min="2.5" :max="30" :step="2.5" controls-position="right" @update:model-value="set('pan_step_limit', Number($event) / 50)" /></el-form-item>
              <el-form-item label="水平动作间隔 (秒)"><el-input-number :model-value="modelValue.pan_cooldown_seconds" :min=".5" :max="15" :step=".5" controls-position="right" @update:model-value="set('pan_cooldown_seconds', Number($event))" /></el-form-item>
              <el-form-item v-if="!modelValue.line_patrol_enabled" label="最大偏离原位 (云台全行程 %)"><el-input-number :model-value="modelValue.pan_max_travel_ratio * 100" :min="1" :max="40" controls-position="right" @update:model-value="set('pan_max_travel_ratio', Number($event) / 100)" /></el-form-item>
              <el-form-item v-if="!modelValue.line_patrol_enabled" label="船体入画边缘余量 (%)"><el-input-number :model-value="modelValue.body_entry_margin * 100" :min="1" :max="20" controls-position="right" @update:model-value="set('body_entry_margin', Number($event) / 100)" /></el-form-item>
              <el-form-item v-if="!modelValue.line_patrol_enabled" label="主体入画确认时长 (秒)"><el-input-number :model-value="modelValue.body_entry_confirm_seconds" :min=".2" :max="10" :step=".2" controls-position="right" @update:model-value="set('body_entry_confirm_seconds', Number($event))" /></el-form-item>
              <el-form-item v-if="!modelValue.line_patrol_enabled" label="尝试温和放大前等待 (秒)"><el-input-number :model-value="modelValue.body_entry_max_wait_seconds" :min="1" :max="30" controls-position="right" @update:model-value="set('body_entry_max_wait_seconds', Number($event))" /></el-form-item>
              <el-form-item label="离场最少新观测次数"><el-input-number :model-value="modelValue.departure_min_observations" :min="2" :max="30" controls-position="right" @update:model-value="set('departure_min_observations', Number($event))" /></el-form-item>
            </el-form>
          </el-collapse-item></el-collapse>
        </section>
      </el-collapse-item>
      <el-collapse-item v-if="optical" name="detail-focus" title="录制时自动对焦">
        <div class="recognition-toggle">
          <span>锁定目标后自动选区对焦</span>
          <el-switch :model-value="modelValue.auto_region_focus" :disabled="!loaded || saving" aria-label="自动区域对焦" @update:model-value="set('auto_region_focus', Boolean($event))" />
        </div>
        <p class="setting-hint">镜头到位后按船名区域、吃水区域、船体的顺序选区对焦。</p>
      </el-collapse-item>
      <el-collapse-item v-else name="digital-framing" title="数字取景范围">
        <div class="focus-setting-heading"><span><Crosshair :size="15" />聚焦方式</span><span class="small-chip">数字放大</span></div>
        <el-radio-group :model-value="modelValue.focus_mode" :disabled="!loaded" class="focus-mode-options" aria-label="聚焦方式" @update:model-value="set('focus_mode', $event as 'auto' | 'manual')"><el-radio-button value="auto">自动跟随船舶</el-radio-button><el-radio-button value="manual">自定义中心</el-radio-button></el-radio-group>
        <p class="setting-hint">{{ modelValue.focus_mode === 'auto' ? '围绕船舶及船名、吃水区域取景，留出边缘，优先保留完整船体。' : '点击左侧画面选择中心，也可调整坐标，全景同步保留。' }}</p>
        <div class="slider-heading"><label for="zoom-level">{{ modelValue.focus_mode === 'auto' ? '最大数字放大倍率' : '数字放大倍率' }}</label><strong>{{ modelValue.zoom_factor.toFixed(1) }}×</strong></div>
        <el-slider id="zoom-level" :model-value="modelValue.zoom_factor" :min="1" :max="8" :step=".1" :disabled="!loaded" :format-tooltip="(value: number) => `${value.toFixed(1)}×`" @update:model-value="set('zoom_factor', Number($event))" />
        <div v-if="modelValue.focus_mode === 'manual'" class="focus-coordinates"><label>水平中心 (%)<el-input-number :model-value="Math.round(modelValue.center_x * 100)" :min="0" :max="100" controls-position="right" @update:model-value="set('center_x', Number($event) / 100)" /></label><label>垂直中心 (%)<el-input-number :model-value="Math.round(modelValue.center_y * 100)" :min="0" :max="100" controls-position="right" @update:model-value="set('center_y', Number($event) / 100)" /></label></div>
        <div v-else class="slider-heading padding-setting"><label>船体边缘留白</label><el-input-number :model-value="Math.round(modelValue.padding * 100)" :min="0" :max="100" :step="5" controls-position="right" size="small" @update:model-value="set('padding', Number($event) / 100)" /><span>%</span></div>
        <p class="digital-focus-note"><ZoomIn :size="14" /><span>此模式只放大现有像素，不控制镜头，也不会提升远距离原始细节。</span></p>
      </el-collapse-item>
      <el-collapse-item v-if="opticalRunning" name="target-status" title="录制目标与镜头状态">
        <section class="adaptive-target-panel" aria-label="目标优先级与停泊排除">
          <div class="adaptive-target-heading"><strong>近船优先 · {{ modelValue.suppress_stationary ? '停泊排除' : '停泊排除已关闭' }}</strong><span v-if="parkedCount">{{ parkedCount }} 个静止目标</span></div>
          <p class="setting-hint">以船框高度估计近远，同类船型较可靠；不同船型、遮挡和透视会影响结果，不代表实测距离。</p>
          <div v-if="currentTarget" class="selected-target-facts" role="status"><strong>{{ targetLabel(currentTarget) }}</strong><span>{{ lockedTarget ? '本轮锁定录制' : '当前优先目标' }} · 1× 船高 {{ heightLabel(currentTarget) }}</span><span v-if="modelValue.adaptive_zoom_enabled && (lockedTarget ? status?.target_zoom_factor : selection?.zoom_factor)">计划倍率 {{ lockedTarget ? status?.target_zoom_factor : selection?.zoom_factor }}×<template v-if="selection?.requested_zoom_factor"> · 计算值 {{ selection.requested_zoom_factor.toFixed(2) }}×</template></span></div>
          <p v-else class="target-observing-note">{{ sourceMode === 'local' ? '本地回放不驱动光学镜头，现场采集后显示目标观测状态。' : selectionReasonLabel(selection?.selection_reason) }}</p>
          <ul v-if="targets.length" class="adaptive-target-list">
            <li v-for="target in targets" :key="target.target_id" :class="{ selected: target.target_id === currentTarget?.target_id, parked: target.motion_state === 'stationary' }">
              <div><strong>{{ targetLabel(target) }}</strong><span>船高 {{ heightLabel(target) }}<template v-if="target.motion_state === 'stationary' && target.stationary_seconds"> · 静止 {{ target.stationary_seconds.toFixed(1) }} 秒</template></span><small v-if="target.eligible === false">{{ selectionReasonLabel(target.reason) }}</small></div>
              <span class="target-motion-state" :class="target.motion_state">{{ target.motion_state === 'stationary' && !modelValue.suppress_stationary ? '静止目标' : targetMotionLabel(target.motion_state) }}</span>
            </li>
          </ul>
          <p v-if="currentTarget && selection?.target_selection_reason" class="setting-hint">{{ selectionReasonLabel(selection.target_selection_reason) }}</p>
          <p v-if="currentTarget && selection?.selection_reason" class="setting-hint">{{ selectionReasonLabel(selection.selection_reason) }}</p>
        </section>
        <div v-if="status?.zoom_driver === 'optical'" class="optical-live-facts"><span>镜头倍率 <strong>{{ status.current_zoom_factor == null ? '运动中 / 待确认' : `${status.current_zoom_factor}×` }}</strong></span><span>已完成 <strong>{{ status.cycle_count || 0 }}</strong> 轮</span></div>
      </el-collapse-item>
      <el-collapse-item v-if="optical" name="target-policy"><template #title><Crosshair :size="14" />移动目标判断与停泊排除</template>
        <div class="stationary-policy-toggle"><span>排除长期静止目标</span><el-switch :model-value="modelValue.suppress_stationary" aria-label="排除停泊船舶" @update:model-value="set('suppress_stationary', Boolean($event))" /></div>
        <el-form label-position="top" class="recording-settings-grid">
          <el-form-item label="持续静止判定 (秒)"><el-input-number :model-value="modelValue.stationary_seconds" :min="3" :max="600" controls-position="right" @update:model-value="set('stationary_seconds', Number($event))" /></el-form-item>
          <el-form-item label="运动确认时长 (秒)"><el-input-number :model-value="modelValue.motion_confirm_seconds" :min=".2" :max="10" :step=".1" :precision="1" controls-position="right" @update:model-value="set('motion_confirm_seconds', Number($event))" /></el-form-item>
          <el-form-item label="位移阈值 (1×画面 %)"><el-input-number :model-value="Number((modelValue.stationary_motion_threshold * 100).toFixed(1))" :min=".2" :max="10" :step=".1" :precision="1" controls-position="right" @update:model-value="set('stationary_motion_threshold', Number($event) / 100)" /></el-form-item>
          <el-form-item label="选船切换余量 (%)"><el-input-number :model-value="Math.round(modelValue.target_switch_margin * 100)" :min="0" :max="100" :step="5" controls-position="right" @update:model-value="set('target_switch_margin', Number($event) / 100)" /></el-form-item>
        </el-form>
        <p class="setting-hint">目标移动达到条件后参与选船。静止计时只累计有效观测。</p>
      </el-collapse-item>
      <el-collapse-item name="timing"><template #title><Settings2 :size="14" />录制触发与时长</template>
        <el-form label-position="top" class="recording-settings-grid">
          <template v-if="optical"><el-form-item label="船尾消失确认 (秒)"><el-input-number :model-value="modelValue.departure_confirm_seconds" :min=".3" :max="30" :step=".1" :precision="1" controls-position="right" @update:model-value="set('departure_confirm_seconds', Number($event))" /></el-form-item><el-form-item label="镜头到位超时 (秒)"><el-input-number :model-value="modelValue.zoom_settle_timeout_seconds" :min="3" :max="60" controls-position="right" @update:model-value="set('zoom_settle_timeout_seconds', Number($event))" /></el-form-item><el-form-item label="回 1× 后稳定等待 (秒)"><el-input-number :model-value="modelValue.overview_settle_seconds" :min="0" :max="5" :step=".1" :precision="1" controls-position="right" @update:model-value="set('overview_settle_seconds', Number($event))" /></el-form-item></template>
          <template v-else><el-form-item label="入场前预录 (秒)"><el-input-number :model-value="modelValue.pre_roll_seconds" :min="0" :max="20" controls-position="right" @update:model-value="set('pre_roll_seconds', Number($event))" /></el-form-item><el-form-item label="离场后延录 (秒)"><el-input-number :model-value="modelValue.post_roll_seconds" :min="1" :max="60" controls-position="right" @update:model-value="set('post_roll_seconds', Number($event))" /></el-form-item></template>
          <el-form-item label="连续检测触发次数"><el-input-number :model-value="modelValue.min_hits" :min="1" :max="20" controls-position="right" @update:model-value="set('min_hits', Number($event))" /></el-form-item>
          <el-form-item label="船舶置信度阈值"><el-input-number :model-value="modelValue.confidence_threshold" :min=".1" :max="1" :step=".05" :precision="2" controls-position="right" @update:model-value="set('confidence_threshold', Number($event))" /></el-form-item>
          <el-form-item :label="optical ? '单轮异常超时 (秒)' : '单段最长 (秒)'"><el-input-number :model-value="modelValue.max_clip_seconds" :min="Math.max(5, modelValue.pre_roll_seconds + 1)" :max="3600" :step="10" controls-position="right" @update:model-value="set('max_clip_seconds', Number($event))" /></el-form-item>
        </el-form>
      </el-collapse-item>
      <el-collapse-item name="storage" title="录像保留与存储">
        <el-form label-position="top" class="recording-settings-grid" :disabled="!loaded || saving">
          <el-form-item label="视频保留 (天)"><el-input-number :model-value="modelValue.retention_days" :min="1" :max="3650" controls-position="right" @update:model-value="set('retention_days', Number($event))" /></el-form-item>
          <el-form-item label="总存储上限 (GB)"><el-input-number :model-value="modelValue.max_storage_gb" :min=".1" :max="10000" :step="1" :precision="1" controls-position="right" @update:model-value="set('max_storage_gb', Number($event))" /></el-form-item>
        </el-form>
        <p class="setting-hint">{{ optical ? '每个正常循环保存一个视频，包含放大与缩回过程。异常超时用于防止镜头或模型故障导致无限录制。' : '短暂漏检通过延录合并，长时间有船自动分段。' }} 达到容量或保留天数上限时，自动清理最早的已结束视频。</p>
      </el-collapse-item>
    </el-collapse>
    <div class="recording-storage"><HardDrive :size="14" /><span>{{ formatBytes(status?.storage_bytes || 0) }} / {{ modelValue.max_storage_gb }} GB</span><span v-if="!optical">缓冲 {{ (status?.buffer_seconds || 0).toFixed(1) }} s</span></div>
    <el-progress :percentage="storagePercent" :show-text="false" :stroke-width="3" />
    <slot />
    <RouterLink class="recording-library-link" to="/recordings"><Video :size="15" />查看通行视频 <span>→</span></RouterLink>
  </section>
</template>
