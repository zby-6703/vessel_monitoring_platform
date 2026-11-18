<script setup lang="ts">
import { computed } from 'vue'
import type { ProcessingSummary, RecordingSettings } from '../api'

const props = defineProps<{ modelValue: RecordingSettings; loaded: boolean; saving: boolean; processingSummary?: ProcessingSummary | null }>()
const emit = defineEmits<{ 'update:modelValue': [settings: RecordingSettings] }>()
const roiValid = computed(() => props.modelValue.target_roi_left < props.modelValue.target_roi_right && props.modelValue.target_roi_top < props.modelValue.target_roi_bottom)
function set<K extends keyof RecordingSettings>(field: K, value: RecordingSettings[K]) {
  if (!props.loaded || props.saving) return
  emit('update:modelValue', { ...props.modelValue, [field]: value })
}
</script>

<template>
  <div class="recording-controls-body" :aria-busy="!loaded || saving">
    <h3 class="control-heading">识别与目标筛选</h3>
    <section class="realtime-recognition" aria-label="实时识别功能开关">
      <strong>识别项目</strong>
      <div class="recognition-toggle"><span>船名识别</span><el-switch :model-value="modelValue.enable_ship_name" :disabled="!loaded || saving" aria-label="开启实时船名识别" @update:model-value="set('enable_ship_name', Boolean($event))" /></div>
      <div class="recognition-toggle"><span>吃水测量</span><el-switch :model-value="modelValue.enable_draft" :disabled="!loaded || saving" aria-label="开启实时吃水测量" @update:model-value="set('enable_draft', Boolean($event))" /></div>
      <p class="setting-hint">可按需开启船名识别与吃水测量。应用后，关闭的项目可以通过离线分析补做。</p>
      <el-tag v-if="!modelValue.enable_ship_name && !modelValue.enable_draft" type="success" size="small">检测与跟踪优先</el-tag>
    </section>
    <section class="target-filter-panel" aria-label="重点船舶筛选与识别策略">
      <div class="target-filter-heading"><div><strong>重点目标筛选</strong><p>{{ modelValue.target_filter_enabled ? '小目标保留检测框与轨迹，跳过船名、吃水识别及录像触发。' : '筛选已关闭，按上方已开启的功能处理全部目标。' }}</p></div><el-switch :model-value="modelValue.target_filter_enabled" :disabled="!loaded || saving" aria-label="启用重点船舶筛选" @update:model-value="set('target_filter_enabled', Boolean($event))" /></div>
      <p class="setting-hint">尺寸、稳定性和区域条件会影响详细识别与录像选船。基础检测与跟踪覆盖整幅画面。</p>
      <div v-if="processingSummary" class="processing-summary" aria-label="当前画面处理分流"><span>检测 <strong>{{ processingSummary.detected }}</strong></span><span>入选 <strong>{{ processingSummary.eligible }}</strong></span><span>本帧详细识别 <strong>{{ processingSummary.detail_inferred }}</strong></span><span>轻量处理 <strong>{{ processingSummary.lightweight }}</strong></span></div>
      <el-form v-if="modelValue.target_filter_enabled" label-position="top" class="recording-settings-grid">
        <el-form-item label="入选最小船高 (1×画面 %)"><el-input-number :model-value="Number((modelValue.min_target_height_ratio * 100).toFixed(1))" :min="0" :max="80" :step="1" :precision="1" :disabled="!loaded || saving" controls-position="right" @update:model-value="set('min_target_height_ratio', Number($event) / 100)" /></el-form-item>
        <el-form-item label="入选最小船宽 (1×画面 %)"><el-input-number :model-value="Number((modelValue.min_target_width_ratio * 100).toFixed(1))" :min="0" :max="100" :step="1" :precision="1" :disabled="!loaded || saving" controls-position="right" @update:model-value="set('min_target_width_ratio', Number($event) / 100)" /></el-form-item>
        <el-form-item label="连续观测次数"><el-input-number :model-value="modelValue.target_confirm_hits" :min="1" :max="30" :disabled="!loaded || saving" controls-position="right" @update:model-value="set('target_confirm_hits', Number($event))" /></el-form-item>
        <el-form-item label="稳定观测时长 (秒)"><el-input-number :model-value="modelValue.target_confirm_seconds" :min="0" :max="10" :step=".1" :precision="1" :disabled="!loaded || saving" controls-position="right" @update:model-value="set('target_confirm_seconds', Number($event))" /></el-form-item>
      </el-form>
      <p v-if="modelValue.target_filter_enabled" class="setting-hint">尺寸统一换算到 1× 全景；船头刚入画、水平边缘截断时暂不要求船宽达标。画面尺寸仅近似船型，需按固定机位试调；录制时的目标放大高度在“循环录制”中设置。</p>
      <el-collapse class="recording-advanced">
        <el-collapse-item name="processing-policy" title="监测区域与识别频率">
          <p class="setting-hint">区域以 1× 全景为基准。仅筛选区域内目标，适合固定机位；改变摄像头朝向后需重新检查区域。</p>
          <el-form label-position="top" class="recording-settings-grid">
            <el-form-item label="区域左边界 (%)"><el-input-number :model-value="Math.round(modelValue.target_roi_left * 100)" :min="0" :max="99" :disabled="!loaded || saving" controls-position="right" @update:model-value="set('target_roi_left', Number($event) / 100)" /></el-form-item>
            <el-form-item label="区域右边界 (%)"><el-input-number :model-value="Math.round(modelValue.target_roi_right * 100)" :min="1" :max="100" :disabled="!loaded || saving" controls-position="right" @update:model-value="set('target_roi_right', Number($event) / 100)" /></el-form-item>
            <el-form-item label="区域上边界 (%)"><el-input-number :model-value="Math.round(modelValue.target_roi_top * 100)" :min="0" :max="99" :disabled="!loaded || saving" controls-position="right" @update:model-value="set('target_roi_top', Number($event) / 100)" /></el-form-item>
            <el-form-item label="区域下边界 (%)"><el-input-number :model-value="Math.round(modelValue.target_roi_bottom * 100)" :min="1" :max="100" :disabled="!loaded || saving" controls-position="right" @update:model-value="set('target_roi_bottom', Number($event) / 100)" /></el-form-item>
            <el-form-item label="同一目标识别间隔 (秒)"><el-input-number :model-value="modelValue.detail_interval_seconds" :min="0" :max="30" :step=".1" :precision="1" :disabled="!loaded || saving" controls-position="right" @update:model-value="set('detail_interval_seconds', Number($event))" /></el-form-item>
            <el-form-item label="每帧最多详细识别目标"><el-input-number :model-value="modelValue.max_detail_targets" :min="1" :max="8" :disabled="!loaded || saving" controls-position="right" @update:model-value="set('max_detail_targets', Number($event))" /></el-form-item>
          </el-form>
          <p v-if="!roiValid" class="recording-error" role="alert">区域左边界必须小于右边界，上边界必须小于下边界。</p>
          <p class="setting-hint">识别频率限制详细识别的开销，目标在间隔期间继续跟踪。</p>
        </el-collapse-item>
      </el-collapse>
    </section>
    <slot />
  </div>
</template>
