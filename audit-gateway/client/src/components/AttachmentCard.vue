<template>
  <!--
    附件卡片

    三种状态要明确区分开，混在一起会让审计员得出错误结论：

    1. **已留存**：能预览/下载，点击会记一条 attachment 查阅日志
    2. **未留存**：元数据在（文件名、大小、哈希），但本体没存
       （超过体积上限或配置关掉了落盘）——「他发过一个 3MB 的 xlsx」
       这个事实本身就有价值，不能因为没存下来就不显示
    3. **远程链接**：客户端传的是 URL 而不是内容，网关**按设计从未抓取**。
       所以这里只把 URL 显示成文本，不做成可点链接——一来网关没这份内容，
       二来诱导审计员去点一个用户可控的地址不是个好主意
  -->
  <v-card
    border
    class="attachment-card"
    flat
    :ripple="false"
    rounded="lg"
  >
    <!-- 图片且已留存：直接出缩略图，审计场景下「看一眼是什么」远比文件名有用 -->
    <v-img
      v-if="isPreviewable"
      class="cursor-pointer"
      cover
      height="120"
      :src="url"
      @click="open"
    >
      <template #placeholder>
        <div class="flex items-center justify-center h-full">
          <v-progress-circular color="primary" indeterminate size="24" />
        </div>
      </template>

      <template #error>
        <div class="flex items-center justify-center h-full text-caption text-medium-emphasis">
          图片加载失败
        </div>
      </template>
    </v-img>

    <div class="p-3">
      <div class="flex items-center gap-2">
        <v-icon
          :color="attachment.stored ? 'primary' : 'grey'"
          :icon="ATTACHMENT_ICONS[attachment.kind] ?? 'mdi-file-outline'"
          size="small"
        />

        <span class="text-caption font-weight-medium truncate grow">
          {{ attachment.filename ?? '未命名文件' }}
        </span>
      </div>

      <div class="text-caption text-medium-emphasis mt-1">
        {{ attachment.mime_type ?? '未知类型' }} · {{ attachment.size_text }}
      </div>

      <!-- 未留存的原因要写清楚：审计员需要知道「看不到」是策略还是故障 -->
      <div v-if="!attachment.stored" class="text-caption text-warning mt-1">
        {{ unavailableReason }}
      </div>

      <div class="flex items-center gap-1 mt-2">
        <v-btn
          v-if="attachment.stored"
          density="comfortable"
          prepend-icon="mdi-open-in-new"
          size="small"
          text="查看原件"
          variant="text"
          @click="open"
        />

        <!-- 哈希是取证用的：要证明「审计里这张图和某处那张图是同一份」，
             靠的就是这个值。给个一键复制 -->
        <v-btn
          v-if="attachment.sha256"
          density="comfortable"
          icon="mdi-content-copy"
          size="small"
          variant="text"
          @click="copyHash"
        >
          <v-icon icon="mdi-content-copy" size="small" />

          <v-tooltip activator="parent" location="top">
            复制内容哈希 {{ attachment.sha256?.slice(0, 12) }}…
          </v-tooltip>
        </v-btn>
      </div>
    </div>
  </v-card>
</template>

<script lang="ts" setup>
  import type { AuditAttachment } from '@/types/api'
  import { computed } from 'vue'
  import { buildAttachmentUrl } from '@/api/audit'
  import { useToastStore } from '@/stores/toast'
  import { ATTACHMENT_ICONS } from '@/types/api'

  const props = defineProps<{ attachment: AuditAttachment }>()

  const toast = useToastStore()

  const url = computed(() => buildAttachmentUrl(props.attachment.id))

  const SAFE_PREVIEW_MIMES = new Set([
    'image/png',
    'image/jpeg',
    'image/jpg',
    'image/gif',
    'image/webp',
    'image/bmp',
  ])

  const isPreviewable = computed(
    () => props.attachment.stored
      && SAFE_PREVIEW_MIMES.has((props.attachment.mime_type ?? '').toLowerCase()),
  )

  const unavailableReason = computed(() => {
    if (props.attachment.store_error) {
      return `未留存：${props.attachment.store_error}`
    }
    if (props.attachment.origin_url) {
      // 不把 URL 渲染成链接：网关没有这份内容，诱导点击用户可控地址也不合适
      return `远程链接，网关按设计未抓取内容（${props.attachment.origin_url}）`
    }
    return '附件本体未留存'
  })

  /** 打开原件。这一下会在后端留下一条 attachment 查阅日志 */
  function open () {
    window.open(url.value, '_blank')
  }

  async function copyHash () {
    if (!props.attachment.sha256) {
      return
    }
    try {
      await navigator.clipboard.writeText(props.attachment.sha256)
      toast.success('已复制内容哈希')
    } catch {
      toast.error(null, '复制失败，请手动选中')
    }
  }
</script>

<style scoped>
.attachment-card {
  width: 220px;
}

.cursor-pointer {
  cursor: pointer;
}

.truncate {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
