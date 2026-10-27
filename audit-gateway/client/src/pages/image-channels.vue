<template>
  <div class="channel-page">
    <div class="flex flex-wrap items-center gap-3 mb-4">
      <div class="grow"><h1 class="text-h6">生图渠道</h1><div class="text-body-2 text-medium-emphasis">配置供应商、登记生图模型；授权与配额沿用现有规则</div></div>
      <v-btn :disabled="loading || generating" text="刷新目录" @click="load()" />
      <v-btn v-if="can('model:edit')" color="primary" text="新增渠道" @click="edit(null)" />
    </div>

    <v-alert
      v-if="error"
      class="mb-4"
      :text="error"
      type="error"
      variant="tonal"
    />

    <v-progress-linear v-if="loading" class="mb-4" indeterminate />

    <v-card border class="mb-4" flat>
      <v-card-text>
        <v-select
          v-model="selectedId"
          :disabled="generating"
          item-title="name"
          item-value="id"
          :items="providers"
          label="选择生图供应商渠道（支持共用普通渠道）"
          no-data-text="尚未配置渠道"
        />

        <template v-if="selected">
          <div class="flex flex-wrap items-center gap-2 mb-3">
            <v-chip>{{ selected.status === 1 ? '已启用' : '已停用' }}</v-chip><span>{{ selected.api_key_masked }}</span>
            <span>已登记 {{ channelModels.length }} 个生图模型</span>
            <span class="text-caption text-medium-emphasis">{{ selected.code }} · 超时 {{ selected.timeout_seconds }} 秒</span>
          </div>

          <div class="flex flex-wrap gap-2">
            <v-btn v-if="can('model:edit')" :disabled="generating" text="编辑渠道 / 更换 Key" @click="edit(selected)" />
            <v-btn v-if="can('model:edit')" :disabled="generating || loading" :text="selected.status === 1 ? '停用渠道' : '启用渠道'" @click="toggle()" />

            <v-btn
              v-if="can('model:edit')"
              :disabled="selected.status !== 1 || generating"
              :loading="probing"
              text="探测可用模型（不生图）"
              @click="probe()"
            />
          </div>

          <div class="text-caption mt-3">{{ checkedAt ? `最近目录探测：${checkedAt}` : '尚未探测目录' }}。保存 Key 或刷新页面不会触发生图。</div>
        </template>

        <v-empty-state v-else text="新增独立渠道，或选择已有供应商。按次与按 token 的 Key 请分别配置。" title="选择一个渠道开始" />
      </v-card-text>
    </v-card>

    <template v-if="selectedId">
      <v-card border class="mb-4" flat title="生图模型">
        <v-card-text><ChannelModels
          :key="selectedId"
          :discovered="discovered"
          image
          :models="channelModels"
          :provider-id="selectedId"
          @changed="load()"
        /></v-card-text>
      </v-card>

      <v-card v-if="can('model:edit')" border flat title="手动试生成">
        <v-card-text>
          <v-alert class="mb-4" text="试生成会产生供应商费用，并记入当前管理员的用量与审计；仍受适用配额限制。一次生成一张，不自动重试。" type="warning" variant="tonal" />

          <v-select
            v-model="testModelId"
            :disabled="generating"
            item-title="display_name"
            item-value="id"
            :items="channelModels.filter(m => m.status === 1)"
            label="已登记的生图模型"
            no-data-text="请先探测并登记模型"
          />

          <v-textarea
            v-model="prompt"
            :disabled="generating"
            label="生图提示词"
            maxlength="16000"
            rows="3"
          />

          <v-select v-model="size" :disabled="generating" :items="sizes" label="图片比例（具体分辨率由模型决定）" />

          <v-file-input
            v-model="files"
            accept="image/png,image/jpeg,image/webp"
            :disabled="generating"
            hint="最多 3 张 PNG/JPEG/WebP，每张不超过 8 MiB"
            label="本机参考图（可选）"
            multiple
            persistent-hint
          />

          <v-btn
            color="primary"
            :disabled="!testModelId || !prompt.trim() || selected?.status !== 1"
            :loading="generating"
            text="付费生成一张"
            @click="generate()"
          />

          <div v-if="generating" class="text-body-2 mt-3" role="status">正在生成，可能需要数分钟。请勿重复提交；离开页面不会自动撤销供应商费用。</div>

          <v-alert
            v-if="generationError"
            class="mt-4"
            :text="generationError"
            type="error"
            variant="tonal"
          />

          <div v-if="result" class="mt-4">
            <v-img alt="管理员试生成图片" class="rounded-lg" max-height="420" :src="imageSrc" />
            <div class="text-body-2 my-3">模型：{{ result.model }} · 耗时 {{ elapsed }} 秒</div>
            <v-btn :download="imageFilename" :href="imageSrc" text="下载原图" />
            <div class="text-subtitle-2 mt-4">供应商原始用量（不等于费用）</div>
            <pre class="usage-json">{{ JSON.stringify(result.usage ?? { 说明: '供应商未返回用量，请查看用量统计中的估算标记' }, null, 2) }}</pre>
          </div>
        </v-card-text>
      </v-card>
    </template>

    <ProviderEditor :open="editorOpen" :provider="editing" @close="editorOpen = false" @saved="saved" />
  </div>
</template>
<script setup lang="ts">
  import type { DiscoveredModel, ImageResult } from '@/api/channels'
  import type { ModelAdminInfo, ProviderInfo } from '@/types/api'
  import { computed, onMounted, ref, watch } from 'vue'
  import { useRoute } from 'vue-router'
  import { listModels, listProviders, updateProvider } from '@/api/catalog'
  import { probeImages, testImage } from '@/api/channels'
  import ChannelModels from '@/components/channels/ChannelModels.vue'
  import ProviderEditor from '@/components/channels/ProviderEditor.vue'
  import { usePermission } from '@/composables/usePermission'
  import { channelImageUrl } from '@/lib/channel-image'
  const route = useRoute()
  const { can } = usePermission()
  const providers = ref<ProviderInfo[]>([])
  const models = ref<ModelAdminInfo[]>([])
  const selectedId = ref<number | null>(null)
  const selected = computed(() => providers.value.find(p => p.id === selectedId.value))
  const channelModels = computed(() => models.value.filter(m => m.provider_id === selectedId.value && m.capability === 'image'))
  const discovered = ref<DiscoveredModel[]>([])
  const checkedAt = ref('')
  const loading = ref(false)
  const probing = ref(false)
  const error = ref('')
  const editorOpen = ref(false)
  const editing = ref<ProviderInfo | null>(null)
  const testModelId = ref<number | null>(null)
  const prompt = ref('')
  const size = ref('1024x1024')
  const files = ref<File[]>([])
  const sizes = [{ title: '正方形 1:1', value: '1024x1024' }, { title: '横向 3:2', value: '1536x1024' }, { title: '纵向 2:3', value: '1024x1536' }]
  const generating = ref(false)
  const generationError = ref('')
  const result = ref<ImageResult | null>(null)
  const elapsed = ref('')
  const imageSrc = computed(() => channelImageUrl(result.value?.data[0]))
  const imageFilename = computed(() => `生图-${Date.now()}.${result.value?.data[0]?.mime_type === 'image/jpeg' ? 'jpg' : (result.value?.data[0]?.mime_type === 'image/webp' ? 'webp' : 'png')}`)
  let epoch = 0
  let loadEpoch = 0
  watch(selectedId, () => {
    epoch++
    discovered.value = []
    checkedAt.value = ''
    probing.value = false
    testModelId.value = null
    result.value = null
    generationError.value = ''
    files.value = []
  })
  async function load (id?: number) {
    const token = ++loadEpoch
    loading.value = true
    error.value = ''
    try {
      const [p, m] = await Promise.all([listProviders(), listModels()])
      if (token !== loadEpoch) return
      providers.value = p.items.filter(p => p.kind !== 'codex' && !p.base_url.startsWith('codex://'))
      models.value = m.items
      if (id && providers.value.some(p => p.id === id)) selectedId.value = id
      else if (!providers.value.some(p => p.id === selectedId.value)) selectedId.value = providers.value[0]?.id ?? null
      if (!channelModels.value.some(m => m.id === testModelId.value)) testModelId.value = null
    } catch (error_) {
      if (token === loadEpoch) error.value = error_ instanceof Error ? error_.message : '目录加载失败'
    } finally {
      if (token === loadEpoch) loading.value = false
    }
  }
  function edit (p: ProviderInfo | null) {
    editing.value = p
    editorOpen.value = true
  }
  async function saved (id: number) {
    editorOpen.value = false
    discovered.value = []
    checkedAt.value = ''
    epoch++
    await load(id)
  }
  async function toggle () {
    if (!selected.value) return
    loading.value = true
    try {
      await updateProvider(selected.value.id, { status: selected.value.status === 1 ? 0 : 1 })
      await load()
    } catch (error_) {
      error.value = error_ instanceof Error ? error_.message : '修改失败'
    } finally {
      loading.value = false
    }
  }
  async function probe () {
    if (!selectedId.value || probing.value) return
    const id = selectedId.value
    const token = epoch
    probing.value = true
    error.value = ''
    try {
      const p = await probeImages(id)
      if (token === epoch) {
        discovered.value = p.models
        checkedAt.value = new Date().toLocaleString()
      }
    } catch (error_) {
      if (token === epoch) error.value = error_ instanceof Error ? error_.message : '探测失败'
    } finally {
      if (token === epoch) probing.value = false
    }
  }
  async function generate () {
    if (generating.value || !testModelId.value) return
    generating.value = true
    generationError.value = ''
    result.value = null
    const token = epoch
    const start = performance.now()
    try {
      if (files.value.length > 3 || files.value.some(f => f.size > 8 * 1024 * 1024 || !['image/png', 'image/jpeg', 'image/webp'].includes(f.type))) throw new Error('参考图最多 3 张 PNG/JPEG/WebP，每张不超过 8 MiB')
      const reference_images = await Promise.all(files.value.map(f => new Promise<string>((resolve, reject) => {
        const reader = new FileReader()
        reader.addEventListener('load', () => resolve(String(reader.result)))
        reader.addEventListener('error', () => reject(new Error('参考图读取失败')), { once: true })
        reader.readAsDataURL(f)
      })))
      const response = await testImage({ model_id: testModelId.value, prompt: prompt.value, size: size.value, reference_images })
      if (token === epoch) {
        result.value = response
        elapsed.value = ((performance.now() - start) / 1000).toFixed(1)
      }
    } catch (error_) {
      if (token === epoch) generationError.value = error_ instanceof Error ? error_.message : '试生成失败；请先检查用量记录，不自动重试'
    } finally {
      generating.value = false
    }
  }
  watch(() => route.query.provider_id, () => {
    if (route.query.tab === 'images') void load(Number(route.query.provider_id) || undefined)
  })
  onMounted(() => load(Number(route.query.provider_id) || undefined))
</script>
<style scoped>
.channel-page { min-width: 0; }
.usage-json { white-space: pre-wrap; overflow-wrap: anywhere; font-size: 12px; margin-top: 12px; }
</style>
