<template>
  <div class="flex gap-4" style="min-height: 70vh">
    <!-- 项目列表 -->
    <v-card
      border
      class="shrink-0"
      flat
      rounded="lg"
      width="260"
    >
      <v-card-item>
        <v-card-title class="text-subtitle-1">我的项目</v-card-title>
        <v-card-subtitle class="text-caption">只读看板</v-card-subtitle>
      </v-card-item>

      <v-divider />

      <v-list density="compact" :selected="[active]" @update:selected="onSelect">
        <v-list-item
          v-for="p in projects"
          :key="p.code"
          :subtitle="p.my_role === 'owner' ? '负责人' : p.my_role === 'viewer' ? '只读' : '成员'"
          :title="p.name"
          :value="p.code"
        />

        <v-list-item v-if="!loading && projects.length === 0" class="text-medium-emphasis">
          还没有加入任何项目
        </v-list-item>
      </v-list>
    </v-card>

    <!-- 群聊 + 任务 -->
    <v-card
      v-if="active"
      border
      class="flex-1 flex flex-col"
      flat
      rounded="lg"
    >
      <v-tabs v-model="tab" density="compact">
        <v-tab value="chat">群聊</v-tab>

        <v-tab value="tasks">
          任务
          <v-badge v-if="openTasks" color="primary" :content="openTasks" inline />
        </v-tab>

        <v-tab value="members">成员</v-tab>
      </v-tabs>

      <div v-if="activeProject" class="flex items-center gap-2 px-4 py-2 text-caption">
        <span class="text-medium-emphasis">项目仓库</span>

        <v-btn
          v-if="activeProject.git_repo_status === 'ready' && activeProject.git_repo_url"
          :href="activeProject.git_repo_url"
          rel="noreferrer"
          size="small"
          target="_blank"
          text="打开 Gitea"
          variant="text"
        />

        <v-chip
          v-else
          :color="activeProject.git_repo_status === 'failed' ? 'error' : 'warning'"
          size="x-small"
          :text="repoStatusText(activeProject)"
          variant="tonal"
        />
      </div>

      <v-divider />

      <!-- ── 群聊 ── -->
      <template v-if="tab === 'chat'">
        <div ref="scroller" class="flex-1 overflow-y-auto p-4" style="max-height: 55vh">
          <div v-for="m in messages" :key="m.id" class="mb-3">
            <div class="flex items-center gap-2">
              <span class="font-weight-medium">
                {{ m.sender_agent_label || m.sender_username || m.sender_uid }}
              </span>

              <!-- agent 发言必须一眼看得出来：它说的话归属它主人，
                   而读的人有权知道对面是人还是 agent -->
              <v-chip v-if="m.sender_kind === 'agent'" size="x-small" text="agent" variant="tonal" />

              <span class="text-caption text-medium-emphasis">{{ m.created_at }}</span>

              <!-- 收的人有权知道看到的不是原话 -->
              <v-chip
                v-if="m.redacted"
                color="warning"
                size="x-small"
                text="已遮蔽关键词"
                variant="tonal"
              />
            </div>

            <div class="text-body-2" style="white-space: pre-wrap">{{ m.content }}</div>
          </div>

          <div v-if="messages.length === 0" class="text-medium-emphasis">还没有消息</div>
        </div>

        <v-divider />

        <div class="p-3 text-caption text-medium-emphasis">
          发言在客户端「项目协作」页进行。这里只看。
        </div>
      </template>

      <!-- ── 任务 ── -->
      <div v-else-if="tab === 'tasks'" class="p-4">
        <v-list density="comfortable">
          <v-list-item v-for="t in tasks" :key="t.id" border class="mb-2 rounded-lg">
            <template #title>
              <div class="flex items-center gap-2">
                <span>#{{ t.seq }} {{ t.title }}</span>

                <v-chip :color="statusColor(t.status)" size="x-small" :text="statusText(t.status)" variant="tonal" />

                <!--
                  ⚠️ 这个标记必须显示。漏掉它，「群里来的」和「自己写的」在界面上
                  长得一模一样 —— 看板上看不出哪个任务来路不明，就等于把 P0-6
                  的判断依据从审计视角里抹掉了。
                -->
                <v-chip
                  v-if="t.untrusted"
                  color="warning"
                  size="x-small"
                  text="来自群聊"
                  variant="tonal"
                />

                <v-chip
                  v-if="t.returned_count > 0"
                  size="x-small"
                  :text="`被退回 ${t.returned_count} 次`"
                  variant="tonal"
                />
              </div>
            </template>

            <template #subtitle>
              <span v-if="t.claimed_by_username">
                {{ t.claimed_by_username }}
                {{ t.status === 'claimed' ? '已认领，待确认开工' : '进行中' }}
              </span>

              <span v-else>未认领 · 由 {{ t.created_by_username || t.created_by_uid }} 创建</span>
            </template>
          </v-list-item>

          <v-list-item v-if="tasks.length === 0" class="text-medium-emphasis">
            还没有任务
          </v-list-item>
        </v-list>

        <div class="text-caption text-medium-emphasis mt-2">
          建任务与认领开工在客户端进行。认领后那一步「通读任务原文再确认」是 P0-6
          的人在环闸门，只在本人的客户端上出现。
        </div>
      </div>

      <!-- ── 成员 ── -->
      <div v-else class="p-4">
        <v-list density="compact">
          <v-list-item
            v-for="m in members"
            :key="m.id"
            :subtitle="`${m.role} · ${m.dept_code || '未归属'}`"
            :title="m.agent_label || m.username || m.user_uid"
          >
            <template #prepend>
              <v-icon :icon="m.member_kind === 'agent' ? 'mdi-robot-outline' : 'mdi-account-outline'" />
            </template>
          </v-list-item>
        </v-list>

        <div class="text-caption text-medium-emphasis mt-2">
          把自己的 agent 拉进项目在客户端进行 —— 只能拉自己的，服务端在 agent
          分支一律用调用者本人的 uid。
        </div>
      </div>
    </v-card>

    <v-card
      v-else
      border
      class="flex-1 flex items-center justify-center text-medium-emphasis"
      flat
      rounded="lg"
    >
      左边选一个项目
    </v-card>
  </div>
</template>

<script setup lang="ts">
  /**
   * 协作项目 —— 阶段 17，2026-09-07 改为**只读看板**
   *
   * ## 这一页只读
   *
   * 建项目、发言、建任务、认领开工、拉自己的 agent —— 全部回到客户端「项目协作」
   * 页。理由是定位：协作发生在员工的客户端上，后台是给人看的看板。两边都能写的
   * 话，就有两套入口、两套错误提示、两份「这个动作到底该在哪做」的记忆。
   *
   * 尤其是**认领开工那一步不该在这里**：P0-6 的人在环闸门是「主人在自己的客户端
   * 上通读任务原文后确认」。在后台点一下确认，确认的是同一个人没错，但那台机器
   * 上没有随后要执行任务的上下文 —— 把闸门和它守着的东西分到两个地方，闸门就成
   * 了一次纯仪式。
   *
   * ## 权限
   *
   * routes.ts 里没给它 permission。协作是给全体员工的，而「谁能看哪个项目」由
   * 服务端按成员判定 —— 不是成员就拿不到数据，且「不是成员」与「项目不存在」回
   * 同一个 404（区分开等于告诉外人「有这么个项目，只是你进不去」）。
   *
   * 前端再加一道角色判断只会造成两份判定，而它们会漂。
   */
  import type { ProjectMember, ProjectMessage, ProjectRow, ProjectTask } from '@/types/api'
  import { computed, nextTick, onMounted, ref, watch } from 'vue'
  import {
    fetchMembers,
    fetchMessages,
    fetchMyProjects,
    fetchTasks,
  } from '@/api/collab'

  const projects = ref<ProjectRow[]>([])
  const active = ref('')
  const tab = ref('chat')
  const loading = ref(false)

  const messages = ref<ProjectMessage[]>([])
  const scroller = ref<HTMLElement | null>(null)

  const tasks = ref<ProjectTask[]>([])
  const members = ref<ProjectMember[]>([])

  const openTasks = computed(() => tasks.value.filter(t => t.status === 'open').length)
  const activeProject = computed(() => projects.value.find(project => project.code === active.value))

  function repoStatusText (project: ProjectRow) {
    if (project.git_repo_status === 'failed') return '创建失败'
    if (project.git_repo_status === 'provisioning') return '正在创建'
    return '等待创建'
  }

  function statusText (s: string) {
    return ({ open: '未认领', claimed: '待确认', active: '进行中', done: '已完成', cancelled: '已取消' })[s] ?? s
  }

  function statusColor (s: string) {
    return ({ open: 'default', claimed: 'warning', active: 'primary', done: 'success' })[s] ?? 'default'
  }

  async function loadProjects () {
    loading.value = true
    try {
      projects.value = (await fetchMyProjects()).projects
      if (!active.value && projects.value.length > 0) {
        active.value = projects.value[0].code
      }
    } finally {
      loading.value = false
    }
  }

  async function loadActive () {
    if (!active.value) return
    const [m, t, mem] = await Promise.all([
      fetchMessages(active.value),
      fetchTasks(active.value),
      fetchMembers(active.value),
    ])
    messages.value = m.messages
    tasks.value = t.tasks
    members.value = mem.members
    await nextTick()
    if (scroller.value) {
      scroller.value.scrollTop = scroller.value.scrollHeight
    }
  }

  function onSelect (value: unknown) {
    const next = Array.isArray(value) ? value[0] : value
    if (typeof next === 'string' && next) {
      active.value = next
    }
  }

  watch(active, loadActive)
  onMounted(async () => {
    await loadProjects()
    await loadActive()
  })
</script>
