<script setup lang="ts">
import { Crosshair, HardDrive, Save, Settings2, Video, ZoomIn } from 'lucide-vue-next'
import { computed } from 'vue'
import { formatBytes, type PtzAction, type PtzStatus, type RecordingSettings, type RecordingStatus } from '../api'
const props = defineProps<{ modelValue: RecordingSettings; status: RecordingStatus | null; saving: boolean; loaded: boolean; dirty: boolean; ptz: PtzStatus | null; ptzBusy: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [settings: RecordingSettings]; save: []; ptz: [action: PtzAction]; reset: [] }>()
function set<K extends keyof RecordingSettings>(field: K, value: RecordingSettings[K]) {
  const next = { ...props.modelValue, [field]: value }
  if (next.max_clip_seconds <= next.pre_roll_seconds) next.max_clip_seconds = next.pre_roll_seconds + 1
  emit('update:modelValue', next)
}
const stateLabel = computed(() => props.status ? { disabled: '录制已关闭', armed: '待船触发', recording: '正在录制', error: '录制异常' }[props.status.state] : '正在读取状态')
const storagePercent = computed(() => Math.min(100, (props.status?.storage_bytes || 0) / (props.modelValue.max_storage_gb * 1024 ** 3) * 100))
</script>

<template>
  <article class="panel recording-controls" :aria-busy="!loaded">
    <header class="panel-header"><div><span class="section-kicker">船来即录 · 自动归档</span><h2><Video :size="16" />船舶通行录制</h2></div><span class="recording-state" :class="status?.state"><i></i>{{ stateLabel }}</span></header>
    <div class="recording-controls-body">
      <div class="recording-enable"><div><strong>仅录制有船经过的画面</strong><p>保留完整全景与聚焦特写，空闲时只缓冲，不保存视频。</p></div><el-switch :model-value="modelValue.enabled" :disabled="!loaded || saving" aria-label="启用船舶通行自动录制" @update:model-value="set('enabled', Boolean($event))" /></div>
      <p v-if="status?.state === 'recording'" class="recording-active-note" role="status">正在保存 {{ status.source_name || '当前视频源' }} · 船舶离场后自动归档</p>
      <p v-if="status?.error" class="recording-error" role="alert">{{ status.error }}</p>
      <div class="focus-setting-heading"><span><Crosshair :size="15" />聚焦方式</span><span class="small-chip">数字放大</span></div>
      <el-radio-group :model-value="modelValue.focus_mode" :disabled="!loaded" class="focus-mode-options" aria-label="聚焦方式" @update:model-value="set('focus_mode', $event as 'auto' | 'manual')"><el-radio-button value="auto">自动跟随船舶</el-radio-button><el-radio-button value="manual">自定义中心</el-radio-button></el-radio-group>
      <p class="setting-hint">{{ modelValue.focus_mode === 'auto' ? '围绕全部船舶及船名、吃水区域取景，留出边缘，优先保留完整船体。' : '点击左侧画面选择中心，也可调整下方坐标。全景原视频始终同步保留。' }}</p>
      <div class="slider-heading"><label for="zoom-level">{{ modelValue.focus_mode === 'auto' ? '最大放大倍率' : '放大倍率' }}</label><strong>{{ modelValue.zoom_factor.toFixed(1) }}×</strong></div>
      <el-slider id="zoom-level" :model-value="modelValue.zoom_factor" :min="1" :max="8" :step=".1" :disabled="!loaded" :format-tooltip="(value: number) => `${value.toFixed(1)}×`" @update:model-value="set('zoom_factor', Number($event))" />
      <div v-if="modelValue.focus_mode === 'manual'" class="focus-coordinates">
        <label>水平中心 (%)<el-input-number :model-value="Math.round(modelValue.center_x * 100)" :min="0" :max="100" controls-position="right" @update:model-value="set('center_x', Number($event) / 100)" /></label>
        <label>垂直中心 (%)<el-input-number :model-value="Math.round(modelValue.center_y * 100)" :min="0" :max="100" controls-position="right" @update:model-value="set('center_y', Number($event) / 100)" /></label>
      </div>
      <div v-else class="slider-heading padding-setting"><label>船体边缘留白</label><el-input-number :model-value="Math.round(modelValue.padding * 100)" :min="0" :max="100" :step="5" controls-position="right" size="small" @update:model-value="set('padding', Number($event) / 100)" /><span>%</span></div>
      <p class="digital-focus-note"><ZoomIn :size="14" /><span>数字放大用于取景，细节清晰度取决于原始分辨率。需要远距离清晰特写时，可接入支持光学变倍的摄像头。</span></p>
      <el-collapse class="recording-advanced">
        <el-collapse-item name="timing"><template #title><Settings2 :size="14" />触发、时长与存储策略</template>
          <el-form label-position="top" class="recording-settings-grid">
            <el-form-item label="入场前预录 (秒)"><el-input-number :model-value="modelValue.pre_roll_seconds" :min="0" :max="20" controls-position="right" @update:model-value="set('pre_roll_seconds', Number($event))" /></el-form-item>
            <el-form-item label="离场后延录 (秒)"><el-input-number :model-value="modelValue.post_roll_seconds" :min="1" :max="60" controls-position="right" @update:model-value="set('post_roll_seconds', Number($event))" /></el-form-item>
            <el-form-item label="连续检测触发次数"><el-input-number :model-value="modelValue.min_hits" :min="1" :max="20" controls-position="right" @update:model-value="set('min_hits', Number($event))" /></el-form-item>
            <el-form-item label="船舶置信度阈值"><el-input-number :model-value="modelValue.confidence_threshold" :min=".1" :max="1" :step=".05" :precision="2" controls-position="right" @update:model-value="set('confidence_threshold', Number($event))" /></el-form-item>
            <el-form-item label="单段最长 (秒)"><el-input-number :model-value="modelValue.max_clip_seconds" :min="Math.max(5, modelValue.pre_roll_seconds + 1)" :max="3600" :step="10" controls-position="right" @update:model-value="set('max_clip_seconds', Number($event))" /></el-form-item>
            <el-form-item label="视频保留 (天)"><el-input-number :model-value="modelValue.retention_days" :min="1" :max="3650" controls-position="right" @update:model-value="set('retention_days', Number($event))" /></el-form-item>
            <el-form-item label="总存储上限 (GB)"><el-input-number :model-value="modelValue.max_storage_gb" :min=".1" :max="10000" :step="1" :precision="1" controls-position="right" @update:model-value="set('max_storage_gb', Number($event))" /></el-form-item>
          </el-form>
          <p class="setting-hint">短暂漏检通过延录合并；长时间有船自动分段。达到容量或保留天数上限时，自动清理最早的已结束视频。</p>
        </el-collapse-item>
        <el-collapse-item name="ptz"><template #title><Crosshair :size="14" />摄像头光学变倍 / 对焦</template>
          <p v-if="!ptz?.supported" class="setting-hint">{{ ptz?.error || '尚未接入摄像头 PTZ 控制地址。配置后可手动光学变倍、近焦/远焦和自动对焦。' }}</p>
          <div class="ptz-buttons"><el-button :disabled="!ptz?.supported || ptzBusy" @click="emit('ptz', 'zoom_in')">光学放大</el-button><el-button :disabled="!ptz?.supported || ptzBusy" @click="emit('ptz', 'zoom_out')">光学缩小</el-button><el-button :disabled="!ptz?.supported || !ptz?.focus_supported || ptzBusy" @click="emit('ptz', 'focus_near')">近焦</el-button><el-button :disabled="!ptz?.supported || !ptz?.focus_supported || ptzBusy" @click="emit('ptz', 'focus_far')">远焦</el-button><el-button :disabled="!ptz?.supported || !ptz?.focus_supported || ptzBusy" @click="emit('ptz', 'autofocus')">自动对焦</el-button><el-button :disabled="!ptz?.supported || ptzBusy" @click="emit('ptz', 'home')">恢复全景</el-button><el-button :disabled="!ptz?.supported" type="danger" plain @click="emit('ptz', 'stop')">停止运动</el-button></div>
          <p v-if="ptz?.supported" class="setting-hint">光学控制会改变摄像头实际取景。请随时确认船体完整，使用“停止运动”结束当前动作。</p>
        </el-collapse-item>
      </el-collapse>
      <div class="recording-storage"><HardDrive :size="14" /><span>{{ formatBytes(status?.storage_bytes || 0) }} / {{ modelValue.max_storage_gb }} GB</span><span>缓冲 {{ (status?.buffer_seconds || 0).toFixed(1) }} s</span></div>
      <el-progress :percentage="storagePercent" :show-text="false" :stroke-width="3" />
      <div class="recording-save-row"><span :class="{ 'has-changes': dirty }">{{ !loaded ? '设置加载中' : dirty ? '预览中 · 尚未保存' : '设置已同步' }}</span><el-button v-if="dirty" text :disabled="saving" @click="emit('reset')">撤销</el-button><el-button type="primary" :loading="saving" :disabled="!loaded || !dirty" @click="emit('save')"><Save :size="14" />保存并应用</el-button></div>
      <RouterLink class="recording-library-link" to="/recordings"><Video :size="15" />查看过往船舶原视频与特写 <span>→</span></RouterLink>
    </div>
  </article>
</template>
