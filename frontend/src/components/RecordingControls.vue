<script setup lang="ts">
import { Camera, Check, Crosshair, HardDrive, RefreshCw, Save, Settings2, Video, ZoomIn } from 'lucide-vue-next'
import { computed } from 'vue'
import { cyclePhaseLabel, formatBytes, selectionReasonLabel, targetMotionLabel, type AdaptiveTarget, type ProcessingSummary, type PtzAction, type PtzStatus, type RecordingSettings, type RecordingStatus } from '../api'

const props = defineProps<{ modelValue: RecordingSettings; status: RecordingStatus | null; saving: boolean; loaded: boolean; dirty: boolean; ptz: PtzStatus | null; ptzBusy: boolean; sourceMode: 'camera' | 'local'; sourceStatus?: string; processingSummary?: ProcessingSummary | null }>()
const emit = defineEmits<{ 'update:modelValue': [settings: RecordingSettings]; save: []; ptz: [action: PtzAction]; reset: []; 'refresh-ptz': [] }>()
function set<K extends keyof RecordingSettings>(field: K, value: RecordingSettings[K]) {
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
const availableMagnifications = computed(() => magnifications.value.filter((factor) => factor >= props.modelValue.min_optical_zoom && factor <= props.modelValue.max_optical_zoom))
const ready = computed(() => Boolean(props.ptz?.cycle_supported && (props.modelValue.adaptive_zoom_enabled ? availableMagnifications.value.length > 0 : magnifications.value.some((factor) => Math.abs(factor - props.modelValue.zoom_factor) < .001))))
const roiValid = computed(() => props.modelValue.target_roi_left < props.modelValue.target_roi_right && props.modelValue.target_roi_top < props.modelValue.target_roi_bottom)
// Saving a locally valid policy does not require a currently reachable lens.
// The backend validates calibration; movement validates hardware again.
const blocked = computed(() => !roiValid.value)
const sourceRunning = computed(() => props.sourceStatus === 'running')
const operationNote = computed(() => {
  if (!props.loaded) return '正在读取已保存的录制策略。'
  if (props.dirty) return '设置尚未应用，请点击“保存并应用”；开关变化不会立即开始录像。'
  if (!props.modelValue.enabled) return '录制总开关已关闭：检测、跟踪仍可运行，但不会保存过船视频。开启后需保存并启动视频采集。'
  if (props.sourceStatus === 'reconnecting') return '视频链路中断，采集仍在自动重连；已保存的录制策略保持开启。点击“停止采集”可取消重试。'
  if (props.status?.error) return `录制策略已开启，但当前异常已暂停录制：${props.status.error}`
  if (!sourceRunning.value) return props.sourceMode === 'camera'
    ? '录制策略已开启，正在等待现场视频源。恢复网络后请重新检查镜头能力，再启动摄像头采集；尚未采集时不会生成录像。'
    : '录制策略已开启，等待本地视频回放；本地验证使用数字录制，不驱动镜头。'
  return optical.value && props.sourceMode === 'camera'
    ? '现场采集与自动录制已开启：先确认 1× 全景，再对符合条件的移动船舶启动循环。'
    : '视频采集与自动录制已开启，等待符合条件的船舶触发。'
})
const manualBusy = computed(() => props.sourceMode === 'camera' && (optical.value && props.modelValue.enabled || ['initializing', 'zooming_in', 'zoomed', 'zooming_out', 'rearming'].includes(props.status?.phase || '')))
const activeStep = computed(() => ({ overview: 0, initializing: 0, zooming_in: 1, zoomed: 2, zooming_out: 3, rearming: 4, error: -1 }[props.status?.phase || 'overview']))
const stages = computed(() => ['1× 观察移动目标 / 近船优先', props.modelValue.adaptive_zoom_enabled ? '锁定目标 / 自适应放大' : '锁定目标 / 指定倍率', '目标船头至船尾经过', '目标离场 / 回 1×', '保存一段 / 重新选船'])
const stateLabel = computed(() => props.status ? props.status.state === 'disabled' ? '录制已关闭' : props.sourceStatus === 'reconnecting' ? '链路中断 · 自动重连' : props.status.state === 'error' ? '录制异常' : !sourceRunning.value ? '已开启 · 等待视频源' : optical.value && props.sourceMode === 'camera' ? cyclePhaseLabel(props.status.phase, props.status.awaiting_zoomed_target) : { armed: '待船触发', recording: '正在录制' }[props.status.state] : '正在读取状态')
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
  <article class="panel recording-controls" :aria-busy="!loaded">
    <header class="panel-header"><div><span class="section-kicker">检测 → 筛选 → 识别 / 录制</span><h2><Video :size="16" />目标筛选与循环录制</h2></div><span class="recording-state" :class="status?.state"><i></i>{{ stateLabel }}</span></header>
    <div class="recording-controls-body">
      <section class="target-filter-panel" aria-label="重点船舶筛选与识别策略">
        <div class="target-filter-heading"><div><strong>优先识别符合条件的大目标</strong><p>{{ modelValue.target_filter_enabled ? '小目标保留检测框与轨迹，跳过船名、吃水识别及录像触发。' : '筛选已关闭，恢复全部目标的完整识别流程。' }}</p></div><el-switch :model-value="modelValue.target_filter_enabled" :disabled="!loaded || saving" aria-label="启用重点船舶筛选" @update:model-value="set('target_filter_enabled', Boolean($event))" /></div>
        <p class="setting-hint">筛选独立于录制开关，适用于现场摄像头和本地回放。仍对整幅画面运行船舶检测，减少的是后续识别开销；离线分析保持原流程。</p>
        <div v-if="processingSummary" class="processing-summary" aria-label="当前画面处理分流"><span>检测 <strong>{{ processingSummary.detected }}</strong></span><span>入选 <strong>{{ processingSummary.eligible }}</strong></span><span>本帧完整识别 <strong>{{ processingSummary.detail_inferred }}</strong></span><span>轻量处理 <strong>{{ processingSummary.lightweight }}</strong></span></div>
        <el-form v-if="modelValue.target_filter_enabled" label-position="top" class="recording-settings-grid">
          <el-form-item label="入选最小船高 (1×画面 %)"><el-input-number :model-value="Number((modelValue.min_target_height_ratio * 100).toFixed(1))" :min="0" :max="80" :step="1" :precision="1" :disabled="!loaded || saving" controls-position="right" @update:model-value="set('min_target_height_ratio', Number($event) / 100)" /></el-form-item>
          <el-form-item label="入选最小船宽 (1×画面 %)"><el-input-number :model-value="Number((modelValue.min_target_width_ratio * 100).toFixed(1))" :min="0" :max="100" :step="1" :precision="1" :disabled="!loaded || saving" controls-position="right" @update:model-value="set('min_target_width_ratio', Number($event) / 100)" /></el-form-item>
          <el-form-item label="连续观测次数"><el-input-number :model-value="modelValue.target_confirm_hits" :min="1" :max="30" :disabled="!loaded || saving" controls-position="right" @update:model-value="set('target_confirm_hits', Number($event))" /></el-form-item>
          <el-form-item label="稳定观测时长 (秒)"><el-input-number :model-value="modelValue.target_confirm_seconds" :min="0" :max="10" :step=".1" :precision="1" :disabled="!loaded || saving" controls-position="right" @update:model-value="set('target_confirm_seconds', Number($event))" /></el-form-item>
        </el-form>
        <p v-if="modelValue.target_filter_enabled" class="setting-hint">尺寸统一换算到 1× 全景；船头刚入画、水平边缘截断时暂不要求船宽达标。画面尺寸仅近似船型，需按固定机位试调；“入选最小船高”与下方放大后的“目标船体画面高度”用途不同。</p>
        <el-collapse class="recording-advanced">
          <el-collapse-item name="processing-policy" title="监测区域与识别频率">
            <p class="setting-hint">区域以 1× 全景为基准。仅筛选区域内目标，适合固定机位；改变摄像头朝向后需重新检查区域。</p>
            <el-form label-position="top" class="recording-settings-grid">
              <el-form-item label="区域左边界 (%)"><el-input-number :model-value="Math.round(modelValue.target_roi_left * 100)" :min="0" :max="99" :disabled="!loaded || saving" controls-position="right" @update:model-value="set('target_roi_left', Number($event) / 100)" /></el-form-item>
              <el-form-item label="区域右边界 (%)"><el-input-number :model-value="Math.round(modelValue.target_roi_right * 100)" :min="1" :max="100" :disabled="!loaded || saving" controls-position="right" @update:model-value="set('target_roi_right', Number($event) / 100)" /></el-form-item>
              <el-form-item label="区域上边界 (%)"><el-input-number :model-value="Math.round(modelValue.target_roi_top * 100)" :min="0" :max="99" :disabled="!loaded || saving" controls-position="right" @update:model-value="set('target_roi_top', Number($event) / 100)" /></el-form-item>
              <el-form-item label="区域下边界 (%)"><el-input-number :model-value="Math.round(modelValue.target_roi_bottom * 100)" :min="1" :max="100" :disabled="!loaded || saving" controls-position="right" @update:model-value="set('target_roi_bottom', Number($event) / 100)" /></el-form-item>
              <el-form-item label="同一目标识别间隔 (秒)"><el-input-number :model-value="modelValue.detail_interval_seconds" :min="0" :max="30" :step=".1" :precision="1" :disabled="!loaded || saving" controls-position="right" @update:model-value="set('detail_interval_seconds', Number($event))" /></el-form-item>
              <el-form-item label="每帧最多完整识别目标"><el-input-number :model-value="modelValue.max_detail_targets" :min="1" :max="8" :disabled="!loaded || saving" controls-position="right" @update:model-value="set('max_detail_targets', Number($event))" /></el-form-item>
            </el-form>
            <p v-if="!roiValid" class="recording-error" role="alert">区域左边界必须小于右边界，上边界必须小于下边界。</p>
            <p class="setting-hint">多目标按优先级分配完整识别机会，间隔期间继续跟踪。停泊判断、运动证据和镜头视野限制仍参与录制选船。</p>
          </el-collapse-item>
        </el-collapse>
      </section>
      <el-radio-group :model-value="modelValue.recording_mode" :disabled="!loaded || saving" class="focus-mode-options recording-mode-options" aria-label="录制工作模式" @update:model-value="set('recording_mode', $event as 'optical_cycle' | 'passage')"><el-radio-button value="optical_cycle">光学变焦循环</el-radio-button><el-radio-button value="passage">数字取景录制</el-radio-button></el-radio-group>
      <div class="recording-enable"><div><strong>{{ optical ? '启用摄像头自动光学循环' : '仅录制有船经过的画面' }}</strong><p>{{ optical ? '观察并选择可录制的目标后开始，镜头缩回 1× 后保存为一个视频，再重新选船。' : '同步保存全景与数字聚焦视频，空闲时仅保留内存缓冲。' }}</p></div><el-switch :model-value="modelValue.enabled" :disabled="!loaded || saving" aria-label="启用船舶通行自动录制" @update:model-value="set('enabled', Boolean($event))" /></div>
      <p class="setting-hint recording-operation-note" role="status">{{ operationNote }}</p>
      <p v-if="status?.error" class="recording-error" role="alert">{{ status.error }}</p>
      <template v-if="optical">
        <ol class="optical-cycle-stages" aria-label="光学循环录制顺序"><li v-for="(stage, index) in stages" :key="stage" :class="{ active: modelValue.enabled && status?.state !== 'disabled' && status?.state !== 'error' && index === activeStep, passed: modelValue.enabled && status?.state !== 'disabled' && index < activeStep }"><span><Check v-if="modelValue.enabled && index < activeStep" :size="11" /><template v-else>{{ index + 1 }}</template></span><strong>{{ stage }}</strong></li></ol>
        <el-radio-group :model-value="modelValue.adaptive_zoom_enabled" :disabled="!loaded || saving" class="focus-mode-options adaptive-mode-options" aria-label="光学倍率策略" @update:model-value="set('adaptive_zoom_enabled', Boolean($event))"><el-radio-button :value="true">按船高自适应</el-radio-button><el-radio-button :value="false">固定倍率</el-radio-button></el-radio-group>
        <template v-if="modelValue.adaptive_zoom_enabled">
          <div class="optical-zoom-setting"><label>目标船体画面高度</label><el-input-number :model-value="Math.round(modelValue.target_height_ratio * 100)" :min="15" :max="90" :step="5" :disabled="!loaded || saving" controls-position="right" aria-label="目标船体高度占画面百分比" @update:model-value="set('target_height_ratio', Number($event) / 100)" /><span>建议倍率 = 目标高度占比 ÷ 1× 船体高度占比</span></div>
          <div class="adaptive-zoom-range"><label>最小倍率<el-input-number :model-value="modelValue.min_optical_zoom" :min="1.1" :max="40" :step=".1" :precision="1" :disabled="!loaded || saving" controls-position="right" @update:model-value="set('min_optical_zoom', Number($event))" /></label><label>最大倍率<el-input-number :model-value="modelValue.max_optical_zoom" :min="1.1" :max="40" :step=".5" :precision="1" :disabled="!loaded || saving" controls-position="right" @update:model-value="set('max_optical_zoom', Number($event))" /></label></div>
          <p class="setting-hint">按稳定船高选择已标定档位，录制中锁定目标，倍率在冷却与滞回条件下受控更新。船体已足够大或放大会裁切时保留 1×，遮挡和档位不足时可能无法达到目标高度。</p>
        </template>
        <div v-else class="optical-zoom-setting"><label>目标光学倍率</label><el-select v-if="magnifications.length" :model-value="modelValue.zoom_factor" :disabled="!loaded || saving" aria-label="目标光学倍率" @update:model-value="set('zoom_factor', Number($event))"><el-option v-for="factor in magnifications" :key="factor" :value="factor" :label="`${factor}×`" /></el-select><el-input-number v-else :model-value="modelValue.zoom_factor" :min="1.1" :max="40" :step=".5" :precision="1" :disabled="!loaded || saving" controls-position="right" aria-label="待标定目标光学倍率" @update:model-value="set('zoom_factor', Number($event))" /><span>回位固定为 1×</span></div>
        <div class="optical-capability" :class="{ ready }" role="status"><Camera :size="15" /><span>{{ capabilityNote }}</span><el-button text size="small" :loading="ptzBusy" aria-label="重新检查光学变焦能力" @click="emit('refresh-ptz')"><RefreshCw :size="13" /></el-button></div>
        <p v-if="!ready" class="setting-hint">离线时可以保存录制策略；恢复采集后仍需核验镜头能力，核验通过才会执行光学变焦。</p>
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
        <p class="setting-hint">每轮只锁定一艘目标船，不让背景停泊船延长本轮。目标离开放大视野后缩回 1×，再重新选择可录制的移动目标。</p>
        <p v-if="sourceMode === 'local'" class="optical-local-note">光学循环需现场摄像头。本地视频只验证检测与数字录制，不会驱动摄像头镜头。</p>
        <div v-if="status?.zoom_driver === 'optical'" class="optical-live-facts"><span>镜头倍率 <strong>{{ status.current_zoom_factor == null ? '运动中 / 待确认' : `${status.current_zoom_factor}×` }}</strong></span><span>已完成 <strong>{{ status.cycle_count || 0 }}</strong> 轮</span></div>
      </template>
      <template v-else>
        <p v-if="status?.state === 'recording'" class="recording-active-note" role="status">正在保存 {{ status.source_name || '当前视频源' }} · 船舶离场后自动归档</p>
        <div class="focus-setting-heading"><span><Crosshair :size="15" />聚焦方式</span><span class="small-chip">数字放大</span></div>
        <el-radio-group :model-value="modelValue.focus_mode" :disabled="!loaded" class="focus-mode-options" aria-label="聚焦方式" @update:model-value="set('focus_mode', $event as 'auto' | 'manual')"><el-radio-button value="auto">自动跟随船舶</el-radio-button><el-radio-button value="manual">自定义中心</el-radio-button></el-radio-group>
        <p class="setting-hint">{{ modelValue.focus_mode === 'auto' ? '围绕船舶及船名、吃水区域取景，留出边缘，优先保留完整船体。' : '点击左侧画面选择中心，也可调整坐标，全景同步保留。' }}</p>
        <div class="slider-heading"><label for="zoom-level">{{ modelValue.focus_mode === 'auto' ? '最大数字放大倍率' : '数字放大倍率' }}</label><strong>{{ modelValue.zoom_factor.toFixed(1) }}×</strong></div>
        <el-slider id="zoom-level" :model-value="modelValue.zoom_factor" :min="1" :max="8" :step=".1" :disabled="!loaded" :format-tooltip="(value: number) => `${value.toFixed(1)}×`" @update:model-value="set('zoom_factor', Number($event))" />
        <div v-if="modelValue.focus_mode === 'manual'" class="focus-coordinates"><label>水平中心 (%)<el-input-number :model-value="Math.round(modelValue.center_x * 100)" :min="0" :max="100" controls-position="right" @update:model-value="set('center_x', Number($event) / 100)" /></label><label>垂直中心 (%)<el-input-number :model-value="Math.round(modelValue.center_y * 100)" :min="0" :max="100" controls-position="right" @update:model-value="set('center_y', Number($event) / 100)" /></label></div>
        <div v-else class="slider-heading padding-setting"><label>船体边缘留白</label><el-input-number :model-value="Math.round(modelValue.padding * 100)" :min="0" :max="100" :step="5" controls-position="right" size="small" @update:model-value="set('padding', Number($event) / 100)" /><span>%</span></div>
        <p class="digital-focus-note"><ZoomIn :size="14" /><span>此模式只放大现有像素，不控制镜头，也不会提升远距离原始细节。</span></p>
      </template>
      <el-collapse class="recording-advanced">
        <el-collapse-item v-if="optical" name="target-policy"><template #title><Crosshair :size="14" />移动目标判断与停泊排除</template>
          <div class="stationary-policy-toggle"><span>排除长期静止目标</span><el-switch :model-value="modelValue.suppress_stationary" aria-label="排除停泊船舶" @update:model-value="set('suppress_stationary', Boolean($event))" /></div>
          <el-form label-position="top" class="recording-settings-grid">
            <el-form-item label="持续静止判定 (秒)"><el-input-number :model-value="modelValue.stationary_seconds" :min="3" :max="600" controls-position="right" @update:model-value="set('stationary_seconds', Number($event))" /></el-form-item>
            <el-form-item label="运动确认时长 (秒)"><el-input-number :model-value="modelValue.motion_confirm_seconds" :min=".2" :max="10" :step=".1" :precision="1" controls-position="right" @update:model-value="set('motion_confirm_seconds', Number($event))" /></el-form-item>
            <el-form-item label="位移阈值 (1×画面 %)"><el-input-number :model-value="Number((modelValue.stationary_motion_threshold * 100).toFixed(1))" :min=".2" :max="10" :step=".1" :precision="1" controls-position="right" @update:model-value="set('stationary_motion_threshold', Number($event) / 100)" /></el-form-item>
            <el-form-item label="选船切换余量 (%)"><el-input-number :model-value="Math.round(modelValue.target_switch_margin * 100)" :min="0" :max="100" :step="5" controls-position="right" @update:model-value="set('target_switch_margin', Number($event) / 100)" /></el-form-item>
            <el-form-item label="变倍冷却时长 (秒)"><el-input-number :model-value="modelValue.zoom_adjust_cooldown_seconds" :min="1" :max="30" controls-position="right" @update:model-value="set('zoom_adjust_cooldown_seconds', Number($event))" /></el-form-item>
            <el-form-item label="变倍滞回幅度 (%)"><el-input-number :model-value="Math.round(modelValue.zoom_adjust_hysteresis * 100)" :min="5" :max="50" :step="5" controls-position="right" @update:model-value="set('zoom_adjust_hysteresis', Number($event) / 100)" /></el-form-item>
            <el-form-item label="放大后目标重识别超时 (秒)"><el-input-number :model-value="modelValue.target_acquire_timeout_seconds" :min="3" :max="120" controls-position="right" @update:model-value="set('target_acquire_timeout_seconds', Number($event))" /></el-form-item>
          </el-form>
          <p class="setting-hint">启动时先观察运动，避免停泊船抢占。静止计时只累计有效观测，镜头移动和漏检间隔不算停泊证据。倍率调整需满足冷却与滞回；放大后无法关联锁定目标时，超时缩回 1× 重新选船，背景船不延长本轮。</p>
        </el-collapse-item>
        <el-collapse-item name="timing"><template #title><Settings2 :size="14" />{{ optical ? '循环判断与存储策略' : '触发、时长与存储策略' }}</template>
          <el-form label-position="top" class="recording-settings-grid">
            <template v-if="optical"><el-form-item label="船尾消失确认 (秒)"><el-input-number :model-value="modelValue.departure_confirm_seconds" :min=".3" :max="10" :step=".1" :precision="1" controls-position="right" @update:model-value="set('departure_confirm_seconds', Number($event))" /></el-form-item><el-form-item label="镜头到位超时 (秒)"><el-input-number :model-value="modelValue.zoom_settle_timeout_seconds" :min="3" :max="60" controls-position="right" @update:model-value="set('zoom_settle_timeout_seconds', Number($event))" /></el-form-item><el-form-item label="回 1× 后稳定等待 (秒)"><el-input-number :model-value="modelValue.overview_settle_seconds" :min="0" :max="5" :step=".1" :precision="1" controls-position="right" @update:model-value="set('overview_settle_seconds', Number($event))" /></el-form-item></template>
            <template v-else><el-form-item label="入场前预录 (秒)"><el-input-number :model-value="modelValue.pre_roll_seconds" :min="0" :max="20" controls-position="right" @update:model-value="set('pre_roll_seconds', Number($event))" /></el-form-item><el-form-item label="离场后延录 (秒)"><el-input-number :model-value="modelValue.post_roll_seconds" :min="1" :max="60" controls-position="right" @update:model-value="set('post_roll_seconds', Number($event))" /></el-form-item></template>
            <el-form-item label="连续检测触发次数"><el-input-number :model-value="modelValue.min_hits" :min="1" :max="20" controls-position="right" @update:model-value="set('min_hits', Number($event))" /></el-form-item>
            <el-form-item label="船舶置信度阈值"><el-input-number :model-value="modelValue.confidence_threshold" :min=".1" :max="1" :step=".05" :precision="2" controls-position="right" @update:model-value="set('confidence_threshold', Number($event))" /></el-form-item>
            <el-form-item :label="optical ? '单轮异常超时 (秒)' : '单段最长 (秒)'"><el-input-number :model-value="modelValue.max_clip_seconds" :min="Math.max(5, modelValue.pre_roll_seconds + 1)" :max="3600" :step="10" controls-position="right" @update:model-value="set('max_clip_seconds', Number($event))" /></el-form-item>
            <el-form-item label="视频保留 (天)"><el-input-number :model-value="modelValue.retention_days" :min="1" :max="3650" controls-position="right" @update:model-value="set('retention_days', Number($event))" /></el-form-item>
            <el-form-item label="总存储上限 (GB)"><el-input-number :model-value="modelValue.max_storage_gb" :min=".1" :max="10000" :step="1" :precision="1" controls-position="right" @update:model-value="set('max_storage_gb', Number($event))" /></el-form-item>
          </el-form>
          <p class="setting-hint">{{ optical ? '每个正常循环保存一个视频，包含放大与缩回过程。异常超时用于防止镜头或模型故障导致无限录制。' : '短暂漏检通过延录合并，长时间有船自动分段。' }} 达到容量或保留天数上限时，自动清理最早的已结束视频。</p>
        </el-collapse-item>
        <el-collapse-item name="ptz"><template #title><Crosshair :size="14" />镜头手动操作 / 对焦</template>
          <p class="setting-hint">{{ manualBusy ? '循环已开启，请先关闭循环并等待镜头回到 1× 后再进行手动操作。' : ptz?.error || '手动变焦会改变实际取景，原位由摄像头预先设置。' }}</p>
          <p v-if="ptz?.focus_error" class="setting-hint">{{ ptz.focus_error }}</p>
          <div class="ptz-buttons"><el-button :disabled="!ptz?.supported || ptzBusy || manualBusy" @click="emit('ptz', 'zoom_in')">光学放大</el-button><el-button :disabled="!ptz?.supported || ptzBusy || manualBusy" @click="emit('ptz', 'zoom_out')">光学缩小</el-button><el-button :disabled="!ptz?.focus_supported || ptzBusy || manualBusy" @click="emit('ptz', 'focus_near')">近焦</el-button><el-button :disabled="!ptz?.focus_supported || ptzBusy || manualBusy" @click="emit('ptz', 'focus_far')">远焦</el-button><el-button :disabled="!ptz?.autofocus_supported || ptzBusy || manualBusy" @click="emit('ptz', 'autofocus')">自动对焦</el-button><el-button :disabled="!ptz?.supported || ptzBusy || manualBusy" @click="emit('ptz', 'home')">回设备原位</el-button><el-button :disabled="!(ptz?.supported || ptz?.absolute_zoom_supported || status?.zoom_driver === 'optical')" type="danger" plain @click="emit('ptz', 'stop')">紧急停止运动</el-button></div>
        </el-collapse-item>
      </el-collapse>
      <div class="recording-storage"><HardDrive :size="14" /><span>{{ formatBytes(status?.storage_bytes || 0) }} / {{ modelValue.max_storage_gb }} GB</span><span v-if="!optical">缓冲 {{ (status?.buffer_seconds || 0).toFixed(1) }} s</span></div>
      <el-progress :percentage="storagePercent" :show-text="false" :stroke-width="3" />
      <div class="recording-save-row"><span :class="{ 'has-changes': dirty }">{{ !loaded ? '设置加载中' : dirty ? '尚未保存' : '设置已同步' }}</span><el-button v-if="dirty" text :disabled="saving" @click="emit('reset')">撤销</el-button><el-button type="primary" :loading="saving" :disabled="!loaded || !dirty || blocked" @click="emit('save')"><Save :size="14" />保存并应用</el-button></div>
      <RouterLink class="recording-library-link" to="/recordings"><Video :size="15" />查看已保存的每轮录像 <span>→</span></RouterLink>
    </div>
  </article>
</template>
