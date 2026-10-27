<template>
  <v-card border flat rounded="lg">
    <v-card-item>
      <v-card-title class="text-subtitle-1">部门管理</v-card-title>

      <v-card-subtitle>共 {{ departments.length }} 个部门</v-card-subtitle>

      <template #append>
        <div class="flex gap-2">
          <v-switch
            v-model="includeDisabled"
            color="primary"
            density="compact"
            hide-details
            label="显示已停用"
            @update:model-value="load"
          />

          <v-btn
            v-perm="'dept:edit'"
            color="primary"
            prepend-icon="mdi-plus"
            variant="tonal"
            @click="openCreate"
          >
            新建部门
          </v-btn>
        </div>
      </template>
    </v-card-item>

    <v-divider />

    <v-card-text>
      <v-text-field
        v-model="searchKeyword"
        class="max-w-xl"
        clearable
        density="compact"
        hide-details
        label="搜索部门名称 / 编码 / 备注"
        prepend-inner-icon="mdi-magnify"
        variant="outlined"
      />
    </v-card-text>

    <v-data-table
      density="comfortable"
      :headers="headers"
      hover
      item-value="id"
      :items="filteredDepartments"
      :items-per-page="20"
      :loading="loading"
      :no-data-text="searchKeyword ? '没有匹配的部门' : '暂无部门'"
    >
      <template #[`item.name`]="{ item }">
        <span class="font-weight-medium">{{ item.name }}</span>
      </template>

      <template #[`item.code`]="{ item }">
        <code class="text-caption">{{ item.code }}</code>
      </template>

      <template #[`item.status`]="{ item }">
        <v-chip
          :color="item.status === 1 ? 'success' : 'grey'"
          size="small"
          :text="item.status === 1 ? '正常' : '已停用'"
          variant="tonal"
        />
      </template>

      <template #[`item.actions`]="{ item }">
        <v-btn
          size="small"
          text="成员"
          variant="text"
          @click="openMembers(item)"
        />

        <v-btn
          v-perm="'dept:edit'"
          size="small"
          text="编辑"
          variant="text"
          @click="openEdit(item)"
        />

        <v-btn
          v-if="item.status === 1"
          v-perm="'dept:edit'"
          color="warning"
          size="small"
          text="停用"
          variant="text"
          @click="toggleStatus(item, 0)"
        />

        <v-btn
          v-else
          v-perm="'dept:edit'"
          color="success"
          size="small"
          text="启用"
          variant="text"
          @click="toggleStatus(item, 1)"
        />
      </template>
    </v-data-table>
  </v-card>

  <!-- 新建 / 编辑 -->
  <v-dialog v-model="formDialog" max-width="520" persistent>
    <v-card :title="editing ? '编辑部门' : '新建部门'">
      <v-card-text>
        <v-form ref="formRef" @submit.prevent="submitForm">
          <v-text-field
            v-model="form.name"
            density="comfortable"
            label="部门名称"
            :rules="[required]"
            variant="outlined"
          />

          <v-text-field
            v-model="form.code"
            class="mt-2"
            density="comfortable"
            :disabled="editing"
            hint="稳定标识，创建后不可修改"
            label="部门编码"
            persistent-hint
            :rules="editing ? [] : [required, codeRule]"
            variant="outlined"
          />

          <v-select
            v-model="form.parent_id"
            class="mt-4"
            clearable
            density="comfortable"
            item-title="name"
            item-value="id"
            :items="parentOptions"
            label="上级部门（可空）"
            variant="outlined"
          />

          <v-textarea
            v-model="form.remark"
            class="mt-2"
            density="comfortable"
            label="备注"
            rows="2"
            variant="outlined"
          />

          <v-text-field
            v-if="editing"
            v-model="form.dingtalk_dept_id"
            class="mt-2"
            hint="只用于新员工的部门候选，不自动调整已有账号"
            label="钉钉部门 ID（审核后填写）"
            persistent-hint
            variant="outlined"
          />
        </v-form>
      </v-card-text>

      <v-card-actions>
        <v-spacer />
        <v-btn text="取消" @click="formDialog = false" />

        <v-btn
          color="primary"
          :loading="submitting"
          text="保存"
          variant="tonal"
          @click="submitForm"
        />
      </v-card-actions>
    </v-card>
  </v-dialog>

  <!-- 成员 -->
  <v-dialog v-model="membersDialog" max-width="640">
    <v-card :title="`${currentDept?.name ?? ''} · 成员`">
      <v-divider />

      <v-list v-if="members.length > 0" density="comfortable" max-height="420">
        <v-list-item
          v-for="m in members"
          :key="m.uid"
          :subtitle="m.username ?? ''"
          :title="m.realname || m.username || m.uid"
        >
          <template #append>
            <v-chip
              class="me-2"
              :color="roleColor(m.permission)"
              label
              size="small"
              :text="roleLabel(m.permission)"
            />

            <v-chip
              v-if="m.status !== 1"
              color="grey"
              size="small"
              text="已禁用"
              variant="tonal"
            />
          </template>
        </v-list-item>
      </v-list>

      <v-card-text v-else class="text-medium-emphasis">
        该部门暂无成员。可在「用户与角色」页把人员调入。
      </v-card-text>

      <v-card-actions>
        <v-spacer />
        <v-btn text="关闭" @click="membersDialog = false" />
      </v-card-actions>
    </v-card>
  </v-dialog>
</template>

<script lang="ts" setup>
  import type { Department, DepartmentMember } from '@/types/api'
  import { computed, onMounted, reactive, ref } from 'vue'
  import * as deptApi from '@/api/department'
  import { roleColor, roleLabel } from '@/constants/roles'
  import { filterDepartments } from '@/pages/department-filter'
  import { useToastStore } from '@/stores/toast'

  const toast = useToastStore()

  const headers = [
    { title: '部门名称', key: 'name' },
    { title: '编码', key: 'code' },
    { title: '备注', key: 'remark' },
    { title: '状态', key: 'status', width: 100 },
    { title: '操作', key: 'actions', sortable: false, align: 'end' as const, width: 240 },
  ]

  const departments = ref<Department[]>([])
  const loading = ref(false)
  const includeDisabled = ref(false)
  const searchKeyword = ref<string | null>('')

  const formDialog = ref(false)
  const membersDialog = ref(false)
  const submitting = ref(false)
  const editing = ref(false)
  const currentDept = ref<Department | null>(null)
  const members = ref<DepartmentMember[]>([])

  const formRef = ref<{ validate: () => Promise<{ valid: boolean }> } | null>(null)
  const form = reactive({
    name: '',
    code: '',
    parent_id: null as number | null,
    remark: '',
    dingtalk_dept_id: '',
  })

  const required = (v: string) => (v && v.trim() !== '') || '必填'
  function codeRule (v: string) {
    return /^[a-z0-9-]+$/.test(v) || '只能用小写字母、数字和短横线'
  }

  /** 上级部门不能选自己 */
  const parentOptions = computed(() =>
    departments.value.filter(d => d.id !== currentDept.value?.id),
  )

  const filteredDepartments = computed(() =>
    filterDepartments(departments.value, searchKeyword.value),
  )

  async function load () {
    loading.value = true
    try {
      const result = await deptApi.listDepartments(includeDisabled.value)
      departments.value = result.items
    } catch (error) {
      toast.error(error, '加载部门失败')
    } finally {
      loading.value = false
    }
  }

  function openCreate () {
    editing.value = false
    currentDept.value = null
    Object.assign(form, { name: '', code: '', parent_id: null, remark: '', dingtalk_dept_id: '' })
    formDialog.value = true
  }

  function openEdit (dept: Department) {
    editing.value = true
    currentDept.value = dept
    Object.assign(form, {
      name: dept.name,
      code: dept.code,
      parent_id: dept.parent_id,
      remark: dept.remark ?? '',
      dingtalk_dept_id: dept.dingtalk_dept_id ?? '',
    })
    formDialog.value = true
  }

  async function submitForm () {
    const result = await formRef.value?.validate()
    if (!result?.valid) {
      return
    }

    submitting.value = true
    try {
      if (editing.value && currentDept.value) {
        await deptApi.updateDepartment(currentDept.value.id, {
          name: form.name,
          parent_id: form.parent_id,
          remark: form.remark,
          dingtalk_dept_id: form.dingtalk_dept_id.trim() || undefined,
        })
        toast.success('部门已更新')
      } else {
        await deptApi.createDepartment({
          code: form.code,
          name: form.name,
          parent_id: form.parent_id,
          remark: form.remark,
        })
        toast.success('部门已创建')
      }
      formDialog.value = false
      await load()
    } catch (error) {
      toast.error(error, '保存失败')
    } finally {
      submitting.value = false
    }
  }

  async function toggleStatus (dept: Department, status: number) {
    try {
      await deptApi.updateDepartment(dept.id, { status })
      toast.success(status === 1 ? '部门已启用' : '部门已停用')
      await load()
    } catch (error) {
      // 部门下还有人时后端会拒绝，把原话透出来
      toast.error(error, '操作失败')
    }
  }

  async function openMembers (dept: Department) {
    currentDept.value = dept
    members.value = []
    membersDialog.value = true
    try {
      const result = await deptApi.listDepartmentMembers(dept.id)
      members.value = result.items
    } catch (error) {
      toast.error(error, '加载成员失败')
    }
  }

  onMounted(load)
</script>
