<template>
  <v-card border flat height="100%" rounded="lg">
    <v-card-text>
      <div class="flex items-center justify-between">
        <div class="text-caption text-medium-emphasis">{{ title }}</div>
        <v-icon color="primary" :icon="icon" size="20" />
      </div>

      <div class="mt-2 text-h5 font-weight-bold">
        <CompactNumber v-if="compact && typeof value === 'number'" :value="value" />
        <template v-else>{{ value }}</template>
      </div>

      <div class="mt-1 text-caption text-medium-emphasis">{{ hint }}</div>
    </v-card-text>
  </v-card>
</template>

<script lang="ts" setup>
  import CompactNumber from '@/components/CompactNumber.vue'

  defineProps<{
    title: string
    value: string | number
    hint: string
    icon: string
    compact?: boolean
  }>()
</script>
