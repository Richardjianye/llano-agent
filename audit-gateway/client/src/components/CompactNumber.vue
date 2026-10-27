<template>
  <span>
    {{ compactNumber(value) }}
    <v-tooltip activator="parent" location="top">
      {{ exactNumber(value) }}
    </v-tooltip>
  </span>
</template>

<script lang="ts" setup>
  import { compactNumber, exactNumber } from '@/lib/numbers'

  defineProps<{ value: number }>()
</script>
