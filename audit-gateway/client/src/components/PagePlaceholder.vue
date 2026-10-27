<template>
  <v-card border flat rounded="lg">
    <v-empty-state
      :headline="title"
      :icon="icon"
      :text="description"
    >
      <template #actions>
        <v-list v-if="todos?.length" class="mx-auto text-start" density="compact" max-width="480">
          <v-list-subheader>本页待实现</v-list-subheader>

          <v-list-item
            v-for="todo in todos"
            :key="todo"
            prepend-icon="mdi-checkbox-blank-circle-outline"
            :title="todo"
          />
        </v-list>
      </template>
    </v-empty-state>
  </v-card>
</template>

<script lang="ts" setup>
  /** 骨架阶段的占位页 —— 明确写出待实现项，避免菜单点进来一片空白 */
  defineProps<{
    title: string
    icon: string
    description: string
    todos?: string[]
  }>()
</script>
