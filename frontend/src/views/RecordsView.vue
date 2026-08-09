<script setup lang="ts">
import { Check, Download, Eye, RotateCcw, Search, X } from 'lucide-vue-next'
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { assetUrl, getRecords, updateReview, type RecordItem } from '../api'

const filters = reactive({ ship_name: '', risk_level: '', dates: [] as string[] })
const records = ref<RecordItem[]>([])
const total = ref(0)
const page = ref(1)
const loading = ref(false)
const selected = ref<RecordItem | null>(null)

async function load(reset = false) {
  if (reset) page.value = 1
  loading.value = true
  try {
    const data = await getRecords({ ship_name: filters.ship_name || undefined, risk_level: filters.risk_level || undefined, start_time: filters.dates?.[0], end_time: filters.dates?.[1], page: page.value, page_size: 15 })
    records.value = data.items; total.value = data.total
  } catch { ElMessage.error('历史记录加载失败') } finally { loading.value = false }
}
function reset() { filters.ship_name = ''; filters.risk_level = ''; filters.dates = []; load(true) }
async function review(status: RecordItem['review_status']) {
  if (!selected.value) return
  selected.value = await updateReview(selected.value.id, status)
  ElMessage.success(status === 'confirmed' ? '已确认该识别结果' : '已标记为异常结果')
  load()
}
const formatDate = (value: string) => new Date(value).toLocaleString('zh-CN', { hour12: false })
const riskLabel = (value: string) => ({ normal: '正常', warning: '关注', critical: '超载', unknown: '待判定' }[value] || value)
const reviewLabel = (value: RecordItem['review_status']) => ({ pending: '待复核', confirmed: '已确认', rejected: '已驳回' }[value])
onMounted(() => load())
</script>

<template>
  <div class="page records-page">
    <section class="filter-band" aria-label="历史数据筛选">
      <el-form :inline="true" label-position="top" @submit.prevent="load(true)">
        <el-form-item label="船名 / MMSI"><el-input v-model="filters.ship_name" clearable placeholder="输入船名或 MMSI" /></el-form-item>
        <el-form-item label="风险等级"><el-select v-model="filters.risk_level" clearable placeholder="全部状态"><el-option label="正常" value="normal" /><el-option label="关注" value="warning" /><el-option label="超载" value="critical" /></el-select></el-form-item>
        <el-form-item label="监测时间"><el-date-picker v-model="filters.dates" type="datetimerange" value-format="YYYY-MM-DDTHH:mm:ss" start-placeholder="开始时间" end-placeholder="结束时间" /></el-form-item>
        <el-form-item class="form-actions"><el-button type="primary" native-type="submit"><Search :size="16" />查询</el-button><el-button @click="reset"><RotateCcw :size="16" />重置</el-button></el-form-item>
      </el-form>
    </section>

    <section class="panel records-table-panel">
      <header class="panel-header"><div><span class="section-kicker">历史归档</span><h2>船舶监测记录</h2></div><div class="table-tools"><span>共 {{ total }} 条</span><el-button plain><Download :size="16" />导出 CSV</el-button></div></header>
      <el-table v-loading="loading" :data="records" table-layout="fixed" class="ops-table records-table">
        <el-table-column prop="captured_at" label="监测时间" width="178"><template #default="scope"><span class="mono">{{ formatDate(scope.row.captured_at) }}</span></template></el-table-column>
        <el-table-column prop="ship_name" label="船名" min-width="130" show-overflow-tooltip />
        <el-table-column prop="mmsi" label="MMSI" width="112"><template #default="scope"><span class="mono muted-text">{{ scope.row.mmsi || '--' }}</span></template></el-table-column>
        <el-table-column label="吃水深度" width="100" align="right"><template #default="scope"><strong class="mono">{{ scope.row.draft_depth?.toFixed(2) || '--' }} m</strong></template></el-table-column>
        <el-table-column label="排水量" width="112" align="right"><template #default="scope"><span class="mono">{{ scope.row.displacement_tons?.toLocaleString() || '--' }} t</span></template></el-table-column>
        <el-table-column label="风险" width="82"><template #default="scope"><span class="risk-label" :class="scope.row.risk_level">{{ riskLabel(scope.row.risk_level) }}</span></template></el-table-column>
        <el-table-column label="复核" width="82"><template #default="scope"><span class="review-label" :class="scope.row.review_status">{{ reviewLabel(scope.row.review_status) }}</span></template></el-table-column>
        <el-table-column label="操作" width="78" fixed="right"><template #default="scope"><el-button link type="primary" @click="selected = scope.row"><Eye :size="16" />复核</el-button></template></el-table-column>
      </el-table>
      <footer class="table-pagination"><el-pagination v-model:current-page="page" :page-size="15" :total="total" layout="prev, pager, next, jumper" @current-change="load()" /></footer>
    </section>

    <el-dialog :model-value="!!selected" title="识别结果人工复核" width="min(980px, 94vw)" destroy-on-close @update:model-value="(open: boolean) => { if (!open) selected = null }">
      <div v-if="selected" class="review-dialog">
        <div class="review-summary"><div><span>船名</span><strong>{{ selected.ship_name }}</strong></div><div><span>视觉吃水</span><strong>{{ selected.draft_depth?.toFixed(2) || '--' }} m</strong></div><div><span>估算排水量</span><strong>{{ selected.displacement_tons?.toLocaleString() || '--' }} t</strong></div><div><span>模型置信度</span><strong>{{ selected.confidence ? `${(selected.confidence * 100).toFixed(1)}%` : '--' }}</strong></div></div>
        <div class="evidence-grid"><figure class="primary-evidence"><img :src="assetUrl(selected.full_image_path)" alt="完整船舶关键帧" /><figcaption>完整船舶关键帧 · Track {{ selected.track_id }}</figcaption></figure><figure><img :src="assetUrl(selected.draft_image_path)" alt="吃水刻度检测特写" /><figcaption>吃水刻度检测</figcaption></figure><figure><img :src="assetUrl(selected.water_mask_path)" alt="水体分割结果" /><figcaption>水体分割结果</figcaption></figure><figure><img :src="assetUrl(selected.ship_name_image_path)" alt="船名识别区域" /><figcaption>船名识别区域</figcaption></figure></div>
      </div>
      <template #footer><el-button type="danger" plain @click="review('rejected')"><X :size="16" />驳回结果</el-button><el-button type="primary" @click="review('confirmed')"><Check :size="16" />确认结果</el-button></template>
    </el-dialog>
  </div>
</template>
