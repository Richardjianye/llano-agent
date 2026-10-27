<template>
  <div>
    <div class="flex items-center gap-2 mb-1">
      <v-chip
        :color="ROLE_COLORS[role] ?? 'grey'"
        size="x-small"
        :text="ROLE_LABELS[role] ?? role"
        variant="tonal"
      />

      <span v-if="isLong" class="text-caption text-medium-emphasis">
        {{ charCount.toLocaleString('zh-CN') }} 字
      </span>

      <v-spacer />

      <v-btn
        v-if="foldable"
        density="compact"
        size="small"
        :text="expanded ? '收起' : '展开'"
        variant="text"
        @click="expanded = !expanded"
      />
    </div>

    <!--
      折叠态用固定高度 + 渐隐遮罩，而不是 v-if 直接不渲染：
      审计员要能一眼看出「这里还有很多内容」，而不是以为消息就这么短。
    -->
    <div
      class="bubble text-body-2"
      :class="[
        `bubble--${role}`,
        { 'bubble--folded': foldable && !expanded },
      ]"
    >{{ text || '（空）' }}</div>

    <div
      v-if="foldable && !expanded"
      class="text-caption text-medium-emphasis mt-1 cursor-pointer"
      @click="expanded = true"
    >
      {{ foldHint }}
    </div>
  </div>
</template>

<script lang="ts" setup>
  import { computed, ref } from 'vue'

  const props = defineProps<{
    role: string
    text: string
    initiallyExpanded?: boolean
  }>()

  /**
   * 超过这个字数就折叠。
   *
   * 定在 600：一屏大约能看下这么多，再长就得滚动。Agent 的系统提示词
   * 动辄几千字，不折叠的话详情抽屉一打开全是提示词，真正要审的对话被推到看不见的地方。
   */
  const FOLD_CHARS = 600

  const ROLE_LABELS: Record<string, string> = {
    system: '系统提示词',
    user: '用户',
    assistant: '助手',
    tool: '工具',
    unknown: '未知',
  }

  const ROLE_COLORS: Record<string, string> = {
    system: 'grey',
    user: 'primary',
    assistant: 'teal',
    tool: 'deep-purple',
  }

  const charCount = computed(() => props.text?.length ?? 0)

  const isLong = computed(() => charCount.value > FOLD_CHARS)

  /**
   * 系统提示词**无论多短都折叠**。
   *
   * 它是配置不是对话内容：审计要看的是员工说了什么、模型答了什么，
   * 而同一份系统提示词会在每一条记录里重复出现。默认展开的话，
   * 翻十条记录就要跳过十遍同样的内容。
   */
  const foldable = computed(() => props.role === 'system' || isLong.value)

  const expanded = ref(Boolean(props.initiallyExpanded))

  const foldHint = computed(() =>
    props.role === 'system'
      ? `点击展开系统提示词（${charCount.value.toLocaleString('zh-CN')} 字）`
      : `点击展开剩余内容（共 ${charCount.value.toLocaleString('zh-CN')} 字）`,
  )
</script>

<style scoped>
/* pre-wrap 保留换行：代码片段和多行提示词是审计里最常见的内容，
   压成一行就没法读了 */
.bubble {
  background: rgba(var(--v-theme-on-surface), 0.04);
  border-radius: 8px;
  padding: 10px 12px;
  white-space: pre-wrap;
  word-break: break-word;
}

.bubble--assistant {
  background: rgba(var(--v-theme-primary), 0.06);
}

/* 系统提示词压一档存在感：它是配置，不是要审的内容 */
.bubble--system {
  background: rgba(var(--v-theme-on-surface), 0.02);
  border-left: 2px solid rgba(var(--v-theme-on-surface), 0.15);
  color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity));
}

.bubble--folded {
  max-height: 6.5rem;
  overflow: hidden;
  /* 底部渐隐，暗示「下面还有」。用 mask 而不是叠一层白色渐变，
     这样深浅主题都不用各写一遍 */
  mask-image: linear-gradient(to bottom, black 55%, transparent 100%);
}

.cursor-pointer {
  cursor: pointer;
}
</style>
