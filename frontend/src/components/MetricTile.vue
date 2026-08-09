<script setup lang="ts">
import type { Component } from 'vue'

defineProps<{ label: string; value: string | number; unit?: string; note?: string; tone?: 'default' | 'warning' | 'danger'; icon: Component }>()
</script>

<template>
  <article class="metric-tile" :class="`tone-${tone || 'default'}`">
    <div class="metric-icon"><component :is="icon" :size="19" /></div>
    <div class="metric-copy"><span>{{ label }}</span><strong>{{ value }}<small v-if="unit">{{ unit }}</small></strong><p v-if="note">{{ note }}</p></div>
  </article>
</template>

