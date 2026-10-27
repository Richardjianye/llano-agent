<template>
  <div class="flex flex-col gap-4">
    <!-- 技能指标：这是 hub 存在的理由，所以放在最上面而不是折叠在某个 tab 里 -->
    <v-card border flat rounded="lg">
      <v-card-item>
        <v-card-title class="text-subtitle-1">技能指标</v-card-title>

        <v-card-subtitle>
          各部门把工作经验转成了多少公司资产。<b>累计值，不是本月</b>
        </v-card-subtitle>

        <template #append>
          <v-btn
            :loading="statsLoading"
            prepend-icon="mdi-refresh"
            variant="text"
            @click="loadStats"
          >
            刷新
          </v-btn>
        </template>
      </v-card-item>

      <v-divider />

      <v-data-table
        density="comfortable"
        :headers="deptHeaders"
        item-value="dept_code"
        :items="stats.by_dept"
        :items-per-page="10"
        :loading="statsLoading"
        no-data-text="还没有人发布过技能"
      >
        <template #[`item.dept_name`]="{ item }">{{ item.dept_name || (item.dept_code ? '部门名称未配置' : '未归属部门') }}</template>
      </v-data-table>

      <template v-if="stats.by_publisher.length > 0">
        <v-divider />

        <v-expansion-panels flat>
          <v-expansion-panel title="按人查看">
            <template #text>
              <v-data-table
                density="compact"
                :headers="publisherHeaders"
                item-value="uid"
                :items="stats.by_publisher"
                :items-per-page="10"
              >
                <template #[`item.dept_name`]="{ item }">{{ item.dept_name || (item.dept_code ? '部门名称未配置' : '未归属部门') }}</template>
              </v-data-table>
            </template>
          </v-expansion-panel>
        </v-expansion-panels>
      </template>
    </v-card>

    <!-- 技能列表 -->
    <v-card border flat rounded="lg">
      <v-card-item>
        <v-card-title class="text-subtitle-1">技能库</v-card-title>

        <v-card-subtitle>
          员工发布的技能进本部门空间，只有本部门（及下级部门）看得到；主管晋级后全公司可见。
          这里能把出问题的技能下架——下架之后客户端搜不到、
          装不了、也发不了新版，但已经装在员工机器上的那些不会消失
        </v-card-subtitle>
      </v-card-item>

      <v-divider />

      <div class="flex flex-wrap gap-3 p-4">
        <v-text-field
          v-model="query.keyword"
          clearable
          density="compact"
          hide-details
          label="技能名 / 标识 / 发布者"
          max-width="280"
          prepend-inner-icon="mdi-magnify"
          variant="outlined"
          @keyup.enter="reload"
        />

        <v-select
          v-model="query.status"
          density="compact"
          hide-details
          :items="statusOptions"
          label="状态"
          max-width="160"
          variant="outlined"
          @update:model-value="reload"
        />

        <v-btn :loading="loading" variant="tonal" @click="reload">查询</v-btn>
      </div>

      <v-data-table-server
        density="comfortable"
        :headers="headers"
        item-value="id"
        :items="items"
        :items-length="total"
        :items-per-page="pageSize"
        :loading="loading"
        no-data-text="没有匹配的技能"
        :page="page"
        @update:options="onOptions"
      >
        <template #[`item.name`]="{ item }">
          <div class="font-weight-medium">{{ item.name }}</div>

          <div class="text-caption text-medium-emphasis">
            <code>{{ item.slug }}</code> · v{{ item.version }}
          </div>
        </template>

        <template #[`item.owner_username`]="{ item }">
          {{ item.owner_username || item.owner_uid }}

          <div class="text-caption text-medium-emphasis">
            {{ item.owner_dept_name || (item.owner_dept_code ? '部门名称未配置' : '未归属部门') }}
          </div>
        </template>

        <template #[`item.scope`]="{ item }">
          <v-chip
            :color="item.scope === 'company' ? 'primary' : 'default'"
            size="small"
            :text="item.scope === 'company' ? '公司空间' : '部门空间'"
            variant="tonal"
          />
        </template>

        <template #[`item.status`]="{ item }">
          <v-chip
            :color="item.status === 'listed' ? 'success' : 'error'"
            size="small"
            :text="item.status === 'listed' ? '已上架' : '已下架'"
            variant="tonal"
          />

          <!-- 下架原因必须显示：员工来问「我搜不到 xxx 了」时，答案就在这一行 -->
          <div
            v-if="item.status === 'delisted' && item.delisted_reason"
            class="text-caption text-medium-emphasis mt-1"
          >
            {{ item.delisted_reason }}
          </div>
        </template>

        <template #[`item.install_count`]="{ item }">
          {{ item.install_count }}

          <!--
            这个数是「取包次数」，不是装机量：客户端取完还要过扫描与安装策略，
            任何一步都可能失败。不标出来的话，拿它当装机量汇报会偏高。
          -->
          <div class="text-caption text-medium-emphasis">取包次数</div>
        </template>

        <template #[`item.updated_at`]="{ item }">
          <span class="text-caption">{{ formatDateTime(item.updated_at) }}</span>
        </template>

        <template #[`item.actions`]="{ item }">
          <!--
            审阅：读技能包本体。没有它，下架就只能凭技能名猜——而下架是这一页
            唯一的止血手段。技能安全那一页早就能读样本源码了。
          -->
          <v-btn
            size="small"
            variant="text"
            @click="openFiles(item)"
          >
            审阅
          </v-btn>

          <v-btn
            size="small"
            variant="text"
            @click="openVersions(item)"
          >
            版本
          </v-btn>

          <!-- 撤回是降级、下架是否决，所以是两个按钮不是一个下拉 -->
          <v-btn
            v-if="canPromote && item.status === 'listed'"
            size="small"
            variant="text"
            @click="item.scope === 'company' ? doWithdraw(item) : doPromote(item)"
          >
            {{ item.scope === 'company' ? '取消精选' : '设为精选' }}
          </v-btn>

          <v-btn
            v-if="canEdit"
            :color="item.status === 'listed' ? 'error' : 'primary'"
            size="small"
            variant="text"
            @click="item.status === 'listed' ? openDelist(item) : doRelist(item)"
          >
            {{ item.status === 'listed' ? '下架' : '恢复' }}
          </v-btn>
        </template>
      </v-data-table-server>
    </v-card>

    <!-- 下架 -->
    <v-dialog v-model="delistOpen" max-width="520">
      <v-card>
        <v-card-title class="text-subtitle-1">
          下架 {{ delistTarget?.name }}
        </v-card-title>

        <v-card-text>
          <v-alert class="mb-4" density="compact" type="warning" variant="tonal">
            下架后客户端立刻搜不到、装不了，发布者也发不了新版。
            <b>已经装在员工机器上的那份不会消失</b> —— 要让它从机器上下来，
            靠的是技能安全那条链路（扫描命中即隔离），不是这里。
          </v-alert>

          <v-textarea
            v-model="delistReason"
            auto-grow
            hint="会显示在这个列表里。员工来问「我搜不到 xxx 了」时，你要能答上来"
            label="下架原因"
            persistent-hint
            rows="2"
            variant="outlined"
          />
        </v-card-text>

        <v-card-actions>
          <v-spacer />

          <v-btn variant="text" @click="delistOpen = false">取消</v-btn>

          <v-btn color="error" :loading="acting" variant="tonal" @click="doDelist">
            确认下架
          </v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <!-- 版本历史 -->
    <v-dialog v-model="versionsOpen" max-width="760">
      <v-card>
        <v-card-title class="text-subtitle-1">
          {{ versionsTarget?.name }} 的版本历史
        </v-card-title>

        <v-card-text>
          <v-data-table
            density="compact"
            :headers="versionHeaders"
            item-value="version"
            :items="versions"
            :loading="versionsLoading"
            no-data-text="没有版本记录"
          >
            <template #[`item.bundle_hash`]="{ item }">
              <code class="text-caption">{{ item.bundle_hash.slice(0, 12) }}</code>
            </template>

            <template #[`item.total_bytes`]="{ item }">
              {{ (item.total_bytes / 1024).toFixed(1) }} KB
              <span class="text-caption text-medium-emphasis">
                / {{ item.file_count }} 个文件
              </span>
            </template>

            <!-- 历史版本也能审阅：出问题的往往不是当前那一版 -->
            <template #[`item.created_at`]="{ item }">
              <span class="text-caption">{{ formatDateTime(item.created_at) }}</span>
            </template>

            <template #[`item.review`]="{ item }">
              <v-btn
                size="small"
                variant="text"
                @click="openFiles(versionsTarget!, item.version)"
              >
                审阅
              </v-btn>
            </template>
          </v-data-table>
        </v-card-text>

        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="versionsOpen = false">关闭</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <!--
      审阅：技能包本体。
      与技能安全的样本查看长得像是有意的——同一件事在两个页面不该有两套操作。
      不同之处只有一个，而且要说出来：这里的内容是**完整**的，没有「你看到的
      不是全部」那种情况，因为发布走的是完整包，不是抽样。
    -->
    <v-dialog v-model="filesOpen" max-width="900">
      <v-card>
        <v-card-title class="text-subtitle-1">
          {{ filesTarget?.name }}
          <span v-if="files" class="text-caption text-medium-emphasis ml-2">
            v{{ files.version }} · {{ files.file_count }} 个文件 ·
            {{ (files.total_bytes / 1024).toFixed(1) }} KB
          </span>
        </v-card-title>

        <v-card-text>
          <div v-if="filesLoading" class="py-8 text-center">
            <v-progress-circular indeterminate />
          </div>

          <template v-else-if="files">
            <div class="text-caption text-medium-emphasis mb-3">
              由 {{ files.publisher_username || '（未知）' }} 于
              {{ files.published_at || '未知时间' }} 发布。内容指纹
              <code>{{ files.bundle_hash.slice(0, 12) }}</code>
              由服务端按「文件路径 + 各自 sha256」算出，就是分发给同事的那一份。
            </div>

            <v-alert
              v-if="files.changelog"
              class="mb-3"
              density="compact"
              type="info"
              variant="tonal"
            >
              {{ files.changelog }}
            </v-alert>

            <v-alert
              v-if="files.files.length === 0"
              density="compact"
              type="warning"
              variant="tonal"
            >
              这一版读不出文件内容。库里那一行可能坏了 —— 这本身就值得查一下，
              因为客户端取到的会是同一份。
            </v-alert>

            <v-expansion-panels v-else variant="accordion">
              <v-expansion-panel v-for="file in files.files" :key="file.path">
                <v-expansion-panel-title>
                  <div class="flex items-center gap-2 min-w-0">
                    <span class="text-body-2 truncate font-mono">{{ file.path }}</span>

                    <v-chip
                      v-if="file.encoding === 'base64'"
                      size="x-small"
                      text="二进制"
                      variant="tonal"
                    />
                  </div>
                </v-expansion-panel-title>

                <v-expansion-panel-text>
                  <!--
                    二进制不展开：把一张 png 的 base64 铺在屏幕上，除了把真正
                    要读的代码挤出视野之外没有任何作用。
                  -->
                  <div v-if="file.encoding === 'base64'" class="text-body-2">
                    二进制附件，{{ Math.round(file.content.length * 0.75 / 1024) }} KB 左右，
                    这里不展开。要看内容就把技能装到自己机器上。
                  </div>

                  <pre v-else class="code-block">{{ file.content }}</pre>
                </v-expansion-panel-text>
              </v-expansion-panel>
            </v-expansion-panels>
          </template>
        </v-card-text>

        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="filesOpen = false">关闭</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>
  </div>
</template>

<script setup lang="ts">
  /**
   * 自有 skill hub 的管理页 —— 阶段 11 · S11-G
   *
   * 页面结构反映产品意图：**技能指标放在最上面**，技能列表在下面。hub 存在的
   * 理由是给各部门下技能指标，把工作经验转成公司资产；下架只是出事时的止血手段，
   * 不是日常动作。反过来排的话，这个页面看上去会像一个审核后台，而我们**没有**
   * 审核流程（先发后审）。
   */
  import type { SkillHubFiles, SkillHubRow, SkillHubStats, SkillHubVersion } from '@/types/api'
  import { onMounted, reactive, ref } from 'vue'
  import {
    delistSkill,
    fetchSkillHubFiles,
    fetchSkillHubList,
    fetchSkillHubStats,
    fetchSkillHubVersions,
    promoteSkill,
    relistSkill,
    withdrawSkill,
  } from '@/api/skill-hub'
  import { usePermission } from '@/composables/usePermission'
  import { formatDateTime } from '@/lib/datetime'

  const { can } = usePermission()
  // 下架/恢复按钮由这里控制。路由只要 :view —— 挂 :edit 会把只看指标的人
  // 整个挡在门外，而技能指标本来就该给他们看。
  const canEdit = can('skill_hub:edit')
  // 晋级/撤回是主管的工具，下架是管理部门的 —— 两个按钮走两个权限点。
  const canPromote = can('skill_hub:promote')

  const headers = [
    { title: '技能', key: 'name' },
    { title: '空间', key: 'scope' },
    { title: '发布者', key: 'owner_username' },
    { title: '状态', key: 'status' },
    { title: '热度', key: 'install_count' },
    { title: '更新时间', key: 'updated_at' },
    { title: '', key: 'actions', sortable: false, align: 'end' as const },
  ]

  const deptHeaders = [
    { title: '部门', key: 'dept_name' },
    { title: '技能数', key: 'skill_count' },
    { title: '发布次数', key: 'version_count' },
  ]

  const versionHeaders = [
    { title: '版本', key: 'version' },
    { title: '发布者', key: 'publisher_username' },
    { title: '说明', key: 'changelog' },
    { title: '内容指纹', key: 'bundle_hash' },
    { title: '体积', key: 'total_bytes' },
    { title: '时间', key: 'created_at' },
    { title: '', key: 'review', sortable: false, align: 'end' as const },
  ]

  const publisherHeaders = [
    { title: '发布者', key: 'username' },
    { title: '部门', key: 'dept_name' },
    { title: '技能数', key: 'skill_count' },
    { title: '发布次数', key: 'version_count' },
  ]

  const statusOptions = [
    { title: '全部', value: '' },
    { title: '已上架', value: 'listed' },
    { title: '已下架', value: 'delisted' },
  ]

  const query = reactive({ keyword: '', status: '' })
  const items = ref<SkillHubRow[]>([])
  const total = ref(0)
  const page = ref(1)
  const pageSize = ref(20)
  const loading = ref(false)

  const stats = ref<SkillHubStats>({ by_dept: [], by_publisher: [] })
  const statsLoading = ref(false)

  const delistOpen = ref(false)
  const delistTarget = ref<SkillHubRow | null>(null)
  const delistReason = ref('')
  const acting = ref(false)

  const versionsOpen = ref(false)
  const versionsTarget = ref<SkillHubRow | null>(null)
  const versions = ref<SkillHubVersion[]>([])
  const versionsLoading = ref(false)

  const filesOpen = ref(false)
  const filesTarget = ref<SkillHubRow | null>(null)
  const files = ref<SkillHubFiles | null>(null)
  const filesLoading = ref(false)

  async function load () {
    loading.value = true
    try {
      const data = await fetchSkillHubList({
        keyword: query.keyword || undefined,
        status: query.status || undefined,
        page: page.value,
        page_size: pageSize.value,
      })
      items.value = data.items
      total.value = data.total
    } finally {
      loading.value = false
    }
  }

  function reload () {
    page.value = 1
    load()
  }

  function onOptions (options: { page: number, itemsPerPage: number }) {
    page.value = options.page
    pageSize.value = options.itemsPerPage
    load()
  }

  async function loadStats () {
    statsLoading.value = true
    try {
      stats.value = await fetchSkillHubStats()
    } finally {
      statsLoading.value = false
    }
  }

  function openDelist (item: SkillHubRow) {
    delistTarget.value = item
    delistReason.value = ''
    delistOpen.value = true
  }

  async function doDelist () {
    if (!delistTarget.value) return
    acting.value = true
    try {
      await delistSkill(delistTarget.value.id, delistReason.value)
      delistOpen.value = false
      // 下架会改变部门指标里的可见技能数，两边一起刷新，否则页面上半部分
      // 显示的还是下架前的数字。
      await Promise.all([load(), loadStats()])
    } finally {
      acting.value = false
    }
  }

  async function doRelist (item: SkillHubRow) {
    acting.value = true
    try {
      await relistSkill(item.id)
      await Promise.all([load(), loadStats()])
    } finally {
      acting.value = false
    }
  }

  async function doPromote (item: SkillHubRow) {
    acting.value = true
    try {
      await promoteSkill(item.id)
      await Promise.all([load(), loadStats()])
    } finally {
      acting.value = false
    }
  }

  async function doWithdraw (item: SkillHubRow) {
    acting.value = true
    try {
      await withdrawSkill(item.id)
      await Promise.all([load(), loadStats()])
    } finally {
      acting.value = false
    }
  }

  /**
   * 审阅一个技能包。不传 version 就是当前该装的那一版。
   *
   * 每次打开都重新取，不缓存：审阅的对象是「现在分发出去的是什么」，
   * 而技能随时可能发新版 —— 拿着一份缓存做下架决策是最糟的一种错。
   */
  async function openFiles (item: SkillHubRow, version?: number) {
    filesTarget.value = item
    files.value = null
    filesOpen.value = true
    filesLoading.value = true
    try {
      files.value = await fetchSkillHubFiles(item.slug, version)
    } finally {
      filesLoading.value = false
    }
  }

  async function openVersions (item: SkillHubRow) {
    versionsTarget.value = item
    versions.value = []
    versionsOpen.value = true
    versionsLoading.value = true
    try {
      const data = await fetchSkillHubVersions(item.slug)
      versions.value = data.versions
    } finally {
      versionsLoading.value = false
    }
  }

  onMounted(() => {
    load()
    loadStats()
  })
</script>

<style scoped>
/* 与技能安全页的样本查看逐字一致：同一件事在两个页面不该长得不一样。
   哪天要改成真正的语法高亮，两处一起改。 */
.code-block {
  background: rgb(var(--v-theme-surface-light));
  border-radius: 4px;
  font-family: 'Roboto Mono', monospace;
  font-size: 12px;
  line-height: 1.6;
  max-height: 420px;
  overflow: auto;
  padding: 12px;
  white-space: pre-wrap;
  word-break: break-all;
}
</style>
