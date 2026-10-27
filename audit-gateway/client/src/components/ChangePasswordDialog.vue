<!--
  自助修改密码。挂在头像下拉里，任何角色都能用。

  ## 为什么要填当前密码

  这是「修改密码」与「重置密码」的全部区别，不是多余的一步：

  不填的话，任何拿到一个有效会话的人（偷到 Cookie、借用了没锁屏的电脑）都能
  改掉密码把机主锁在门外 —— 一次会话劫持直接升级成永久接管，而机主能看到的
  线索只有「我的密码怎么不对了」。

  管理员对下属的「重置」不需要旧密码，那条路要求更高的角色；超管自己被锁在
  外面时走服务器上的 scripts/reset_account_password.py，那条路要求服务器权限。
  三条路各自要求「不同的东西」，这才是它们互为后备的原因。
-->

<template>
  <v-dialog v-model="open" max-width="520" persistent>
    <v-card title="修改密码">
      <v-card-text>
        <v-form ref="formRef" @submit.prevent="submit">
          <v-text-field
            v-model="form.current"
            autocomplete="current-password"
            density="comfortable"
            label="当前密码"
            :rules="[required]"
            :type="reveal ? 'text' : 'password'"
            variant="outlined"
          />

          <v-text-field
            v-model="form.next"
            autocomplete="new-password"
            class="mt-2"
            density="comfortable"
            hint="至少 8 位"
            label="新密码"
            persistent-hint
            :rules="[required, minLength, notSameAsCurrent]"
            :type="reveal ? 'text' : 'password'"
            variant="outlined"
          />

          <v-text-field
            v-model="form.confirm"
            autocomplete="new-password"
            class="mt-4"
            density="comfortable"
            label="确认新密码"
            :rules="[required, matches]"
            :type="reveal ? 'text' : 'password'"
            variant="outlined"
          />

          <v-checkbox
            v-model="reveal"
            color="primary"
            density="compact"
            hide-details
            label="显示密码"
          />

          <v-divider class="my-3" />

          <!--
            默认不勾。API Key 是用户另外签发的凭据（桌面客户端用的就是它），
            例行改密就把所有客户端踢下线，只会让人不愿意改密码。
            怀疑外泄时才勾 —— 那时攻击者可能已经用旧密码建了 Key，
            光改密码是踢不掉它的。
          -->
          <v-checkbox
            v-model="form.revokeKeys"
            color="warning"
            density="compact"
            hide-details
            label="同时吊销我的全部 API Key（怀疑凭据外泄时勾选）"
          />

          <v-alert
            class="mt-3"
            density="compact"
            text="修改后，你在其它设备上的登录会立即失效，当前这个页面不受影响。"
            type="info"
            variant="tonal"
          />
        </v-form>
      </v-card-text>

      <v-card-actions>
        <v-spacer />
        <v-btn text="取消" variant="text" @click="open = false" />

        <v-btn
          color="primary"
          :loading="submitting"
          text="确认修改"
          variant="flat"
          @click="submit"
        />
      </v-card-actions>
    </v-card>
  </v-dialog>
</template>

<script setup lang="ts">
  import { reactive, ref, watch } from 'vue'
  import { changeOwnPassword } from '@/api/account'
  import { useToastStore } from '@/stores/toast'

  // defineModel 而不是手写 props + emit：后者要 emit 一个叫 update:modelValue
  // 的事件，而那个名字是 v-model 的约定，改成 kebab-case 就不是 v-model 了。
  const open = defineModel<boolean>({ required: true })

  const toast = useToastStore()
  const formRef = ref()
  const submitting = ref(false)
  const reveal = ref(false)

  const form = reactive({
    current: '',
    next: '',
    confirm: '',
    revokeKeys: false,
  })

  const required = (v: string) => !!v || '必填'
  const minLength = (v: string) => v.length >= 8 || '至少 8 位'
  const matches = (v: string) => v === form.next || '两次输入不一致'
  const notSameAsCurrent = (v: string) => v !== form.current || '新密码不能与当前密码相同'

  function reset () {
    form.current = ''
    form.next = ''
    form.confirm = ''
    form.revokeKeys = false
    reveal.value = false
    formRef.value?.resetValidation()
  }

  // 关掉就清空。留在内存里的明文密码没有任何用处，而下次打开时看到上次输入的
  // 内容只会让人以为改过了。
  watch(open, value => {
    if (!value) {
      reset()
    }
  })

  async function submit () {
    const { valid } = await formRef.value.validate()
    if (!valid) {
      return
    }

    submitting.value = true
    try {
      await changeOwnPassword({
        current_password: form.current,
        new_password: form.next,
        revoke_api_keys: form.revokeKeys,
      })
      toast.success('密码已修改，其它设备上的登录已失效')
      open.value = false
    } catch (error: any) {
      // 后端把「当前密码不正确」放在 message 里，原样透出去 —— 自己编一句
      // 泛泛的「操作失败」，用户就分不清是密码错了还是网络断了。
      toast.error(error?.message || '修改失败')
    } finally {
      submitting.value = false
    }
  }
</script>
