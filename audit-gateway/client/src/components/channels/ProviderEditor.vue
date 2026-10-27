<template>
  <v-dialog max-width="580" :model-value="open" persistent @update:model-value="$emit('close')">
    <v-card :title="provider ? '编辑渠道' : '新增生图渠道'">
      <v-card-text>
        <v-alert
          v-if="error"
          class="mb-4"
          :text="error"
          type="error"
          variant="tonal"
        />

        <v-text-field v-model="form.code" :disabled="!!provider" label="渠道编码" maxlength="64" />
        <v-text-field v-model="form.name" label="中文名称" maxlength="64" />

        <v-text-field
          v-if="!codex"
          v-model="form.base_url"
          hint="兼容接口地址，例如 https://ai-api.kkidc.com/v1"
          label="上游地址"
          persistent-hint
        />

        <v-text-field
          v-if="!codex"
          v-model="form.api_key"
          autocomplete="new-password"
          :hint="provider ? '已配置的 Key 不会回显；留空保留' : '仅提交到网关，加密保存'"
          label="供应商 API Key"
          persistent-hint
          type="password"
        />

        <v-alert
          v-else
          class="mb-4"
          text="账号凭据请通过重新授权更新；这里仅修改渠道设置。"
          type="info"
          variant="tonal"
        />

        <v-text-field
          v-model.number="form.timeout_seconds"
          label="超时（秒，1–600）"
          max="600"
          min="1"
          type="number"
        />

        <v-textarea v-model="form.remark" label="备注" maxlength="255" rows="2" />
      </v-card-text>

      <v-card-actions>
        <v-spacer /><v-btn :disabled="saving" text="取消" @click="close" />
        <v-btn color="primary" :loading="saving" text="保存" @click="save" />
      </v-card-actions>
    </v-card>
  </v-dialog>
</template>
<script setup lang="ts">
  import type { ProviderInfo } from '@/types/api'
  import { reactive, ref, watch } from 'vue'
  import { createProvider, updateProvider } from '@/api/catalog'
  const props = defineProps<{ open: boolean, provider: ProviderInfo | null, codex?: boolean }>()
  const emit = defineEmits<{ close: [], saved: [id: number] }>()
  const error = ref('')
  const saving = ref(false)
  const form = reactive({ code: '', name: '', base_url: 'https://ai-api.kkidc.com/v1', api_key: '', timeout_seconds: 600, remark: '' })
  watch(() => props.open, open => {
    if (!open) return
    error.value = ''
    Object.assign(form, { code: '', name: '', base_url: 'https://ai-api.kkidc.com/v1', api_key: '', timeout_seconds: 600, remark: '' }, props.provider, { api_key: '' })
  })
  function close () {
    form.api_key = ''
    emit('close')
  }
  async function save () {
    if (saving.value) return
    error.value = ''
    if (!form.name.trim() || !form.code.trim() || (!props.provider && !form.api_key.trim())) {
      error.value = '请填写渠道编码、名称和 Key'
      return
    }
    saving.value = true
    try {
      const common = { name: form.name.trim(), timeout_seconds: Number(form.timeout_seconds), remark: form.remark || undefined }
      let id = props.provider?.id
      if (id) await updateProvider(id, { ...common, ...(props.codex ? {} : { base_url: form.base_url.trim(), api_key: form.api_key || undefined }) })
      else id = (await createProvider({ ...common, code: form.code.trim(), base_url: form.base_url.trim(), api_key: form.api_key })).id
      close()
      emit('saved', id)
    } catch (error_) {
      error.value = error_ instanceof Error ? error_.message : '保存失败'
    } finally {
      saving.value = false
      form.api_key = ''
    }
  }
</script>
