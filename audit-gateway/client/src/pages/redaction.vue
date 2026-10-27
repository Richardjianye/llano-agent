<template>
  <div class="flex flex-col gap-4">
    <v-card border flat rounded="lg">
      <v-card-item>
        <v-card-title class="text-subtitle-1">屏蔽词</v-card-title>

        <v-card-subtitle>
          出网内容里命中这些词会被替换成占位符。两个方向都走这一份：发往模型上游，
          以及群聊里发给其他部门/其他公司的人
        </v-card-subtitle>

        <template #append>
          <v-btn color="primary" prepend-icon="mdi-plus" variant="tonal" @click="openNew">
            添加
          </v-btn>
        </template>
      </v-card-item>

      <v-divider />

      <v-alert class="m-4" density="compact" type="warning" variant="tonal">
        改这里是<b>安全配置</b>，不是内容配置。停用一个词会让这条防线对<b>之后</b>的
        内容失效，而<b>已经发出去的消息不会追溯遮蔽</b>——群聊消息的脱敏发生在写入
        那一刻，库里存的就是当时的结果。
      </v-alert>

      <v-data-table
        density="comfortable"
        :headers="headers"
        item-value="id"
        :items="rules"
        :items-per-page="20"
        :loading="loading"
        no-data-text="还没有配置任何词（仍在按环境变量 MASK_KEYWORDS 的基线遮蔽）"
      >
        <template #[`item.scope`]="{ item }">
          <v-chip
            :color="item.scope === 'global' ? 'primary' : 'default'"
            size="small"
            :text="item.scope === 'global' ? '全局' : deptLabel(item.scope_value)"
            variant="tonal"
          />
        </template>

        <template #[`item.status`]="{ item }">
          <v-chip
            :color="item.status ? 'success' : 'default'"
            size="small"
            :text="item.status ? '生效中' : '已停用'"
            variant="tonal"
          />
        </template>

        <template #[`item.updated_at`]="{ item }">
          <span class="text-caption">{{ formatDateTime(item.updated_at) }}</span>
        </template>

        <template #[`item.actions`]="{ item }">
          <v-btn size="small" variant="text" @click="openEdit(item)">编辑</v-btn>

          <v-btn
            v-if="item.status"
            color="warning"
            size="small"
            variant="text"
            @click="doDisable(item)"
          >
            停用
          </v-btn>
        </template>
      </v-data-table>
    </v-card>

    <!--
      生效词表单独一块：列表页看不出最终结果 —— 生效 = 环境变量基线 ∪ global 行
      ∪ 本部门及所有上级的行。而且**顺序有意义**（长词优先），配的人得看得到。

      不选部门时看到的就是**纯基线**：非空即说明 MASK_KEYWORDS 还配着东西，
      而那份是对所有部门无条件生效的（2026-09-03 就是这么漏到全公司的）。
    -->
    <v-card border flat rounded="lg">
      <v-card-item>
        <v-card-title class="text-subtitle-1">实际生效的词表</v-card-title>

        <v-card-subtitle>
          环境变量基线 ∪ 全局规则 ∪ 该部门及其上级的规则。<b>按显示顺序匹配</b>——
          长词必须在前，否则「示例公司科技」会被短词切成「[公司]科技」
        </v-card-subtitle>

        <template #append>
          <div class="flex gap-2 items-center">
            <v-select
              v-model.number="probeDept"
              clearable
              density="compact"
              hide-details
              item-title="label"
              item-value="value"
              :items="probeDeptOptions"
              label="部门（空=只看基线）"
              max-width="220"
              variant="outlined"
            />

            <v-btn :loading="probing" variant="tonal" @click="loadEffective">查看</v-btn>
          </div>
        </template>
      </v-card-item>

      <v-divider />

      <div class="flex flex-wrap gap-2 p-4">
        <v-chip v-for="(k, i) in effective" :key="k" size="small" variant="tonal">
          {{ i + 1 }}. {{ k }}
        </v-chip>

        <!--
          空的含义要分两种说。不选部门时看到的是**纯基线**，空是 2026-09-03
          之后的正常状态；选了部门还是空，才是「这个部门没被遮」。
          原来这里两种情况共用一句「防线是关着的」，会把正常状态报成故障。
        -->
        <span v-if="!probing && effective.length === 0" class="text-medium-emphasis">
          {{ probeDept === null
            ? '基线是空的——正常。词表现在只按部门配，环境变量 MASK_KEYWORDS 不再叠加全局词'
            : '这个部门一个词都没有——对它来说关键词防线是关着的' }}
        </span>
      </div>
    </v-card>

    <v-dialog v-model="editOpen" max-width="520">
      <v-card>
        <v-card-title class="text-subtitle-1">{{ form.rule_id ? '编辑' : '添加' }}屏蔽词</v-card-title>

        <v-card-text>
          <v-text-field v-model="form.keyword" label="要遮的词" variant="outlined" />

          <v-select
            v-model="form.scope"
            class="mt-4"
            :items="[{ title: '全局（所有部门）', value: 'global' }, { title: '指定部门', value: 'dept' }]"
            label="作用域"
            variant="outlined"
          />

          <!--
            ⚠️ 这里**必须是下拉，不能是文本框**。后端比的是 caller.dept_id（数字），
            2026-09-03 有人在这个位置手打了部门 code「finance」，三条规则于是对谁
            都不生效 —— 而列表页看上去完全正常，没有任何一处会报错。
          -->
          <v-autocomplete
            v-if="form.scope === 'dept'"
            v-model="form.scope_value"
            class="mt-4"
            item-title="label"
            item-value="value"
            :items="deptOptions"
            label="部门"
            no-data-text="没有匹配项"
            variant="outlined"
          />

          <v-text-field
            v-model="form.remark"
            class="mt-4"
            hint="为什么要遮它。半年后回头看这张表时，这一栏是唯一能回答的东西"
            label="备注"
            persistent-hint
            variant="outlined"
          />
        </v-card-text>

        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="editOpen = false">取消</v-btn>
          <v-btn color="primary" :loading="saving" variant="tonal" @click="doSave">保存</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <v-snackbar v-model="toastOpen" :timeout="4000">{{ toast }}</v-snackbar>
  </div>
</template>

<script setup lang="ts">
  /**
   * 屏蔽词管理 —— 阶段 18
   *
   * 形状照配额/白名单页：scope + scope_value。照抄是有意的——后台已经有两个
   * 「按部门配」的页面，第三个长得不一样只会让人每次都要重新读一遍规则。
   *
   * ⚠️ 部门一律从下拉选，`scope_value` 存的是部门 **ID 的字符串**（和配额页
   * 一样是 `String(dept.id)`）。这一页原来是个自由输入的「部门ID」文本框，
   * 2026-09-03 有人往里填了部门 code，规则静默失效了两天。
   */
  import type { Department, RedactionRule } from '@/types/api'
  import { computed, onMounted, ref } from 'vue'
  import { listDepartments } from '@/api/department'
  import {
    disableRedactionRule,
    fetchEffective,
    fetchRedactionRules,
    saveRedactionRule,
  } from '@/api/redaction'
  import { formatDateTime } from '@/lib/datetime'

  const headers = [
    { title: '词', key: 'keyword' },
    { title: '作用域', key: 'scope' },
    { title: '备注', key: 'remark' },
    { title: '状态', key: 'status' },
    { title: '更新时间', key: 'updated_at' },
    { title: '', key: 'actions', sortable: false, align: 'end' as const },
  ]

  const rules = ref<RedactionRule[]>([])
  const loading = ref(false)
  const effective = ref<string[]>([])
  const probeDept = ref<number | null>(null)
  const probing = ref(false)

  const departments = ref<Department[]>([])

  /** 存进 scope_value 的是 String(dept.id) —— 后端比的是 dept_id，不是 code */
  const deptOptions = computed(() => departments.value.map(dept => ({
    value: String(dept.id), label: dept.name,
  })))

  /** 生效词表的探针按数字部门 ID 查，和 fetchEffective 的入参一致 */
  const probeDeptOptions = computed(() => departments.value.map(dept => ({
    value: dept.id, label: dept.name,
  })))

  /** 表格里把部门 ID 显示成名字。认不出来的值原样带出来并标记 —— 存量脏数据
   *  （比如写成 code 的那几行）必须一眼能看见，不能被渲染成一个像样的名字。 */
  function deptLabel (scopeValue: string) {
    const hit = departments.value.find(dept => String(dept.id) === scopeValue)
    return hit ? `部门 ${hit.name}` : `部门 ${scopeValue}（无效）`
  }

  const editOpen = ref(false)
  const saving = ref(false)
  const form = ref({ keyword: '', scope: 'global', scope_value: '', remark: '', rule_id: 0, status: 1 })

  const toast = ref('')
  const toastOpen = ref(false)

  function say (text: string) {
    toast.value = text
    toastOpen.value = true
  }

  async function load () {
    loading.value = true
    try {
      rules.value = (await fetchRedactionRules()).items
    } finally {
      loading.value = false
    }
  }

  async function loadEffective () {
    probing.value = true
    try {
      const dept = probeDept.value === null || Number.isNaN(probeDept.value) ? undefined : probeDept.value
      effective.value = (await fetchEffective(dept)).keywords
    } finally {
      probing.value = false
    }
  }

  function openNew () {
    form.value = { keyword: '', scope: 'global', scope_value: '', remark: '', rule_id: 0, status: 1 }
    editOpen.value = true
  }

  function openEdit (item: RedactionRule) {
    form.value = { ...item, rule_id: item.id }
    editOpen.value = true
  }

  async function doSave () {
    saving.value = true
    try {
      await saveRedactionRule({
        keyword: form.value.keyword,
        scope: form.value.scope,
        scope_value: form.value.scope_value,
        remark: form.value.remark,
        rule_id: form.value.rule_id || undefined,
        status: form.value.status,
      })
      editOpen.value = false
      await Promise.all([load(), loadEffective()])
    } catch (error) {
      say(`保存失败：${(error as Error).message}`)
    } finally {
      saving.value = false
    }
  }

  async function doDisable (item: RedactionRule) {
    try {
      await disableRedactionRule(item.id)
      await Promise.all([load(), loadEffective()])
    } catch (error) {
      say(`停用失败：${(error as Error).message}`)
    }
  }

  onMounted(async () => {
    departments.value = (await listDepartments(true)).items
    await load()
    await loadEffective()
  })
</script>
