<script setup lang="ts">
import { Save } from 'lucide-vue-next'

defineProps<{ loaded: boolean; dirty: boolean; saving: boolean; valid: boolean }>()
const emit = defineEmits<{ save: []; reset: []; defaults: [] }>()
</script>

<template>
  <div class="recording-save-row">
    <span :class="{ 'has-changes': dirty }">{{ !loaded ? '设置加载中' : dirty ? '尚未应用' : '已保存到本机' }}</span>
    <el-button text :disabled="!loaded || saving" @click="emit('defaults')">恢复默认配置</el-button>
    <el-button v-if="dirty" text :disabled="saving" @click="emit('reset')">恢复上次保存</el-button>
    <el-button type="primary" :loading="saving" :disabled="!loaded || !dirty || !valid" @click="emit('save')">
      <Save :size="14" />保存并应用
    </el-button>
  </div>
</template>
