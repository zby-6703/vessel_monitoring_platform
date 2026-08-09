<script setup lang="ts">
import * as echarts from 'echarts/core'
import { BarChart, LineChart } from 'echarts/charts'
import { GridComponent, LegendComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type { HourlyMetric } from '../api'

echarts.use([BarChart, LineChart, GridComponent, LegendComponent, TooltipComponent, CanvasRenderer])
const props = defineProps<{ data: HourlyMetric[] }>()
const root = ref<HTMLElement>()
let chart: echarts.ECharts | null = null

function render() {
  if (!root.value) return
  chart ||= echarts.init(root.value)
  chart.setOption({
    animationDuration: matchMedia('(prefers-reduced-motion: reduce)').matches ? 0 : 220,
    color: ['#27b7c7', '#e88842'],
    tooltip: { trigger: 'axis', backgroundColor: '#18222c', borderColor: '#34424e', textStyle: { color: '#eff5f7' } },
    legend: { right: 4, top: 0, textStyle: { color: '#93a4af', fontSize: 12 }, itemWidth: 14, itemHeight: 8 },
    grid: { top: 38, right: 42, bottom: 28, left: 38 },
    xAxis: { type: 'category', data: props.data.map((item) => item.time), axisLine: { lineStyle: { color: '#34424e' } }, axisLabel: { color: '#81929d', interval: 3 }, axisTick: { show: false } },
    yAxis: [
      { type: 'value', minInterval: 1, name: '艘次', nameTextStyle: { color: '#71838e' }, splitLine: { lineStyle: { color: '#27333d' } }, axisLabel: { color: '#81929d' } },
      { type: 'value', name: '米', min: 0, max: 8, nameTextStyle: { color: '#71838e' }, splitLine: { show: false }, axisLabel: { color: '#81929d' } },
    ],
    series: [
      { name: '船舶流量', type: 'bar', barMaxWidth: 12, data: props.data.map((item) => item.traffic), itemStyle: { borderRadius: [2, 2, 0, 0] } },
      { name: '平均吃水', type: 'line', yAxisIndex: 1, smooth: 0.25, symbol: 'circle', symbolSize: 5, data: props.data.map((item) => item.average_draft), lineStyle: { width: 2 } },
    ],
  })
}
function resize() { chart?.resize() }
onMounted(() => { render(); window.addEventListener('resize', resize) })
watch(() => props.data, render, { deep: true })
onBeforeUnmount(() => { window.removeEventListener('resize', resize); chart?.dispose() })
</script>

<template><div ref="root" class="trend-chart" role="img" aria-label="24 小时船舶流量与平均吃水趋势图"></div></template>

