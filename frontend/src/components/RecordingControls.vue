<script setup lang="ts">
import { Camera, Check, Crosshair, HardDrive, RefreshCw, Save, Settings2, Video, ZoomIn } from 'lucide-vue-next'
import { computed } from 'vue'
import { cyclePhaseLabel, formatBytes, type PtzAction, type PtzStatus, type RecordingSettings, type RecordingStatus } from '../api'

const props = defineProps<{ modelValue: RecordingSettings; status: RecordingStatus | null; saving: boolean; loaded: boolean; dirty: boolean; ptz: PtzStatus | null; ptzBusy: boolean; sourceMode: 'camera' | 'local' }>()
const emit = defineEmits<{ 'update:modelValue': [settings: RecordingSettings]; save: []; ptz: [action: PtzAction]; reset: []; 'refresh-ptz': [] }>()
function set<K extends keyof RecordingSettings>(field: K, value: RecordingSettings[K]) {
  const next = { ...props.modelValue, [field]: value }
  if (next.max_clip_seconds <= next.pre_roll_seconds) next.max_clip_seconds = next.pre_roll_seconds + 1
  if (field === 'recording_mode' && value === 'passage') next.zoom_factor = Math.min(8, next.zoom_factor)
  if (field === 'recording_mode' && value === 'optical_cycle' && magnifications.value.length && !magnifications.value.includes(next.zoom_factor)) next.zoom_factor = magnifications.value.includes(3) ? 3 : magnifications.value[0]!
  emit('update:modelValue', next)
}
const optical = computed(() => props.modelValue.recording_mode === 'optical_cycle')
const magnifications = computed(() => (props.ptz?.magnifications || []).filter((factor) => factor > 1 && factor <= 40).sort((a, b) => a - b))
const ready = computed(() => Boolean(props.ptz?.cycle_supported && magnifications.value.some((factor) => Math.abs(factor - props.modelValue.zoom_factor) < .001)))
const blocked = computed(() => optical.value && props.modelValue.enabled && !ready.value)
const manualBusy = computed(() => props.sourceMode === 'camera' && (optical.value && props.modelValue.enabled || ['initializing', 'zooming_in', 'zoomed', 'zooming_out', 'rearming'].includes(props.status?.phase || '')))
const activeStep = computed(() => ({ overview: 0, initializing: 0, zooming_in: 1, zoomed: 2, zooming_out: 3, rearming: 4, error: -1 }[props.status?.phase || 'overview']))
const stages = ['1× 检测到船', '开始录像 / 放大', '船头至船尾经过', '船尾消失 / 回 1×', '保存一段 / 再检测']
const stateLabel = computed(() => props.status ? props.status.state === 'disabled' ? '录制已关闭' : props.status.state === 'error' ? '录制异常' : optical.value ? cyclePhaseLabel(props.status.phase, props.status.awaiting_zoomed_target) : { armed: '待船触发', recording: '正在录制' }[props.status.state] : '正在读取状态')
const storagePercent = computed(() => Math.min(100, (props.status?.storage_bytes || 0) / (props.modelValue.max_storage_gb * 1024 ** 3) * 100))
const capabilityNote = computed(() => props.ptz?.zoom_error || props.ptz?.error || (!props.ptz ? '正在读取摄像头光学变焦能力' : !props.ptz.absolute_zoom_supported ? '摄像头尚未确认支持绝对光学变焦' : !props.ptz.calibrated ? '请先在服务端完成 1× 与目标倍率的镜头位置标定' : !ready.value ? '所选倍率未标定，请选择下方已标定倍率' : '绝对光学变焦与倍率标定已就绪'))
</script>

<template>
  <article class="panel recording-controls" :aria-busy="!loaded">
    <header class="panel-header"><div><span class="section-kicker">检测 → 放大 → 回位 → 归档</span><h2><Video :size="16" />船舶循环录制</h2></div><span class="recording-state" :class="status?.state"><i></i>{{ stateLabel }}</span></header>
    <div class="recording-controls-body">
      <el-radio-group :model-value="modelValue.recording_mode" :disabled="!loaded || saving" class="focus-mode-options recording-mode-options" aria-label="录制工作模式" @update:model-value="set('recording_mode', $event as 'optical_cycle' | 'passage')"><el-radio-button value="optical_cycle">光学变焦循环</el-radio-button><el-radio-button value="passage">数字取景录制</el-radio-button></el-radio-group>
      <div class="recording-enable"><div><strong>{{ optical ? '启用摄像头自动光学循环' : '仅录制有船经过的画面' }}</strong><p>{{ optical ? '每轮从检测到船开始录制，镜头缩回 1× 后保存为一个视频，再重新检测。' : '同步保存全景与数字聚焦视频，空闲时仅保留内存缓冲。' }}</p></div><el-switch :model-value="modelValue.enabled" :disabled="!loaded || saving || optical && !ready && !modelValue.enabled" aria-label="启用船舶通行自动录制" @update:model-value="set('enabled', Boolean($event))" /></div>
      <p v-if="status?.error" class="recording-error" role="alert">{{ status.error }}</p>
      <template v-if="optical">
        <ol class="optical-cycle-stages" aria-label="光学循环录制顺序"><li v-for="(stage, index) in stages" :key="stage" :class="{ active: modelValue.enabled && status?.state !== 'disabled' && status?.state !== 'error' && index === activeStep, passed: modelValue.enabled && status?.state !== 'disabled' && index < activeStep }"><span><Check v-if="modelValue.enabled && index < activeStep" :size="11" /><template v-else>{{ index + 1 }}</template></span><strong>{{ stage }}</strong></li></ol>
        <div class="optical-zoom-setting"><label>目标光学倍率</label><el-select v-if="magnifications.length" :model-value="modelValue.zoom_factor" :disabled="!loaded || saving" aria-label="目标光学倍率" @update:model-value="set('zoom_factor', Number($event))"><el-option v-for="factor in magnifications" :key="factor" :value="factor" :label="`${factor}×`" /></el-select><el-input-number v-else :model-value="modelValue.zoom_factor" :min="1.1" :max="40" :step=".5" :precision="1" :disabled="!loaded || saving" controls-position="right" aria-label="待标定目标光学倍率" @update:model-value="set('zoom_factor', Number($event))" /><span>回位固定为 1×</span></div>
        <div class="optical-capability" :class="{ ready }" role="status"><Camera :size="15" /><span>{{ capabilityNote }}</span><el-button text size="small" :loading="ptzBusy" aria-label="重新检查光学变焦能力" @click="emit('refresh-ptz')"><RefreshCw :size="13" /></el-button></div>
        <p class="setting-hint">镜头保持固定方向，不自动追随船体。先等船头进入放大视野，再确认船尾离开后缩回 1×；全景中仍有船体时，另开一个视频并再次放大。</p>
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
          <div class="ptz-buttons"><el-button :disabled="!ptz?.supported || ptzBusy || manualBusy" @click="emit('ptz', 'zoom_in')">光学放大</el-button><el-button :disabled="!ptz?.supported || ptzBusy || manualBusy" @click="emit('ptz', 'zoom_out')">光学缩小</el-button><el-button :disabled="!ptz?.focus_supported || ptzBusy || manualBusy" @click="emit('ptz', 'focus_near')">近焦</el-button><el-button :disabled="!ptz?.focus_supported || ptzBusy || manualBusy" @click="emit('ptz', 'focus_far')">远焦</el-button><el-button :disabled="!ptz?.focus_supported || ptzBusy || manualBusy" @click="emit('ptz', 'autofocus')">自动对焦</el-button><el-button :disabled="!ptz?.supported || ptzBusy || manualBusy" @click="emit('ptz', 'home')">回设备原位</el-button><el-button :disabled="!(ptz?.supported || ptz?.absolute_zoom_supported || status?.zoom_driver === 'optical')" type="danger" plain @click="emit('ptz', 'stop')">紧急停止运动</el-button></div>
        </el-collapse-item>
      </el-collapse>
      <div class="recording-storage"><HardDrive :size="14" /><span>{{ formatBytes(status?.storage_bytes || 0) }} / {{ modelValue.max_storage_gb }} GB</span><span v-if="!optical">缓冲 {{ (status?.buffer_seconds || 0).toFixed(1) }} s</span></div>
      <el-progress :percentage="storagePercent" :show-text="false" :stroke-width="3" />
      <div class="recording-save-row"><span :class="{ 'has-changes': dirty }">{{ !loaded ? '设置加载中' : dirty ? '尚未保存' : '设置已同步' }}</span><el-button v-if="dirty" text :disabled="saving" @click="emit('reset')">撤销</el-button><el-button type="primary" :loading="saving" :disabled="!loaded || !dirty || blocked" @click="emit('save')"><Save :size="14" />保存并应用</el-button></div>
      <RouterLink class="recording-library-link" to="/recordings"><Video :size="15" />查看已保存的每轮录像 <span>→</span></RouterLink>
    </div>
  </article>
</template>
