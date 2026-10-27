<template>
  <!--
    技能安全事件页（阶段 7 · S7-C 的管理端）

    这个页面记录已发生的安全事件，供管理员事后核查。

    产品前提是技能来源不封禁 —— 自有 skill hub 是给各部门下技能指标用的，
    把员工的工作经验转成公司资产。扫描器判 dangerous 会自动隔离，但员工可以
    自己把它放回去，代价是在这里留一行。所以一条「隔离」本身不说明谁做错了
    什么，配对的「员工放回」才是要看的东西。

    看这个页面的人真正要回答两个问题，页面结构就是照着这两个问题排的：
    1. 这条隔离是不是误报？→ 详情里的命中项，看不出来就调样本源码
    2. 同一个技能包，有多少人把它放回去了？→ 详情里按 bundle_hash 串起来的「同包事件」
    3. 哪个已发布技能触发了扫描提醒？→ 查看样本并线下联系发布者
  -->

  <!-- ---------- 筛选条 ---------- -->
  <v-card border class="mb-4" flat rounded="lg">
    <v-card-text class="pb-2">
      <div class="flex flex-wrap items-center gap-3">
        <v-select
          v-model="event"
          clearable
          density="compact"
          hide-details
          :items="SKILL_EVENTS"
          label="事件类型"
          max-width="180"
          variant="outlined"
          @update:model-value="search"
        />

        <v-select
          v-model="verdict"
          clearable
          density="compact"
          hide-details
          :items="SKILL_VERDICTS"
          label="扫描结论"
          max-width="160"
          variant="outlined"
          @update:model-value="search"
        />

        <!--
          部门下拉只对拿得到部门列表的人显示。
          skill_security:view 给到了管理部门审计，而 dept:view 只给开发者与总裁
          —— 审计员看得到这个页面，却调不动 /admin/department/list。
          与其让他们撞一个 403，不如不摆这个筛选。
          TODO: 真正的解法是给一个不需要 dept:view 的部门候选接口。
        -->
        <v-autocomplete
          v-if="can('dept:view')"
          v-model="deptId"
          clearable
          density="compact"
          hide-details
          item-title="name"
          item-value="id"
          :items="departments"
          label="部门"
          max-width="180"
          no-data-text="没有匹配项"
          variant="outlined"
          @update:model-value="search"
        />

        <v-text-field
          v-model="skillName"
          clearable
          density="compact"
          hide-details
          label="技能名"
          max-width="200"
          placeholder="可只填一段"
          prepend-inner-icon="mdi-magnify"
          variant="outlined"
          @keyup.enter="search"
        />

        <!-- uid 而不是姓名：这张表存的身份快照来自 API Key 解析，只有 uid。
             从「用户与角色」页复制过来粘这里 -->
        <v-text-field
          v-model="userUid"
          clearable
          density="compact"
          hide-details
          label="上报人 uid"
          max-width="220"
          placeholder="从用户页复制"
          prepend-inner-icon="mdi-identifier"
          variant="outlined"
          @keyup.enter="search"
        />

        <v-btn
          color="primary"
          :loading="loading"
          prepend-icon="mdi-magnify"
          text="查询"
          variant="tonal"
          @click="search"
        />
      </div>
    </v-card-text>
  </v-card>

  <!-- ---------- 列表 ---------- -->
  <v-card border flat rounded="lg">
    <v-card-item>
      <v-card-title class="text-subtitle-1">技能安全事件</v-card-title>

      <v-card-subtitle>
        共 {{ total }} 条 · 点击任意一行看命中项与同包事件
      </v-card-subtitle>
    </v-card-item>

    <v-divider />

    <v-data-table-server
      density="comfortable"
      :headers="headers"
      hover
      item-value="id"
      :items="items"
      :items-length="total"
      :items-per-page="pageSize"
      :loading="loading"
      no-data-text="该条件下没有事件"
      :page="page"
      @update:options="onOptions"
    >
      <template #[`item.skill_name`]="{ item }">
        <div class="font-weight-medium truncate">{{ item.skill_name }}</div>

        <div class="text-caption text-medium-emphasis">
          来源 {{ item.source || '未知' }}
          <span v-if="item.scanner_version">· 扫描器 {{ item.scanner_version }}</span>
        </div>
      </template>

      <template #[`item.event`]="{ item }">
        <v-chip
          :color="SKILL_EVENT_COLORS[item.event] ?? 'grey'"
          size="small"
          :text="SKILL_EVENT_LABELS[item.event] ?? item.event"
          variant="tonal"
        />
      </template>

      <template #[`item.verdict`]="{ item }">
        <v-chip
          v-if="item.verdict"
          :color="SKILL_VERDICT_COLORS[item.verdict] ?? 'grey'"
          size="small"
          :text="item.verdict"
          variant="tonal"
        />

        <span v-else class="text-medium-emphasis">—</span>

        <div v-if="item.finding_count > 0" class="text-caption text-medium-emphasis">
          {{ item.finding_count }} 项命中
        </div>
      </template>

      <template #[`item.user_uid`]="{ item }">
        <div class="text-body-2">{{ item.user_uid }}</div>

        <div class="text-caption text-medium-emphasis">
          {{ item.dept_code || '未分配部门' }}
          <!-- device 是客户端自报的，只用来定位「去哪台机器上看」，不参与任何判定 -->
          <span v-if="item.device">· {{ item.device }}</span>
        </div>
      </template>

      <template #[`item.created_at`]="{ item }">
        <div class="text-body-2">{{ formatTime(item.created_at) }}</div>

        <!--
          客户端自报的发生时间只在与网关收到的时间明显对不上时才显示。
          那台机器的时钟或时区不受我们控制，平时把两个时间并排摆着只是噪声；
          真差得多的时候，它是「这台机器时间不对」的唯一线索。
        -->
        <div v-if="clockSkewText(item)" class="text-caption text-warning">
          {{ clockSkewText(item) }}
        </div>
      </template>

      <template #[`item.actions`]="{ item }">
        <v-btn
          density="comfortable"
          icon="mdi-chevron-right"
          variant="text"
          @click="openDetail(item.id)"
        />
      </template>
    </v-data-table-server>
  </v-card>

  <!-- ==================== 详情抽屉 ==================== -->
  <v-navigation-drawer
    v-model="drawer"
    location="right"
    temporary
    :width="760"
  >
    <template v-if="detail">
      <v-toolbar color="surface" density="comfortable">
        <div class="px-4 py-1 min-w-0">
          <div class="text-subtitle-1 font-weight-medium truncate">
            {{ detail.skill_name }}
          </div>

          <div class="text-caption text-medium-emphasis">
            {{ formatTime(detail.created_at) }} · {{ detail.user_uid }}
            · {{ detail.dept_code || '未分配部门' }}
          </div>
        </div>

        <v-spacer />

        <v-btn icon="mdi-close" variant="text" @click="drawer = false" />
      </v-toolbar>

      <v-divider />

      <div class="p-4">
        <!-- ---------- 元信息 ---------- -->
        <div class="flex flex-wrap items-center gap-2 mb-4">
          <v-chip
            :color="SKILL_EVENT_COLORS[detail.event] ?? 'grey'"
            size="small"
            :text="SKILL_EVENT_LABELS[detail.event] ?? detail.event"
            variant="tonal"
          />

          <v-chip
            v-if="detail.verdict"
            :color="SKILL_VERDICT_COLORS[detail.verdict] ?? 'grey'"
            size="small"
            :text="detail.verdict"
            variant="tonal"
          />

          <v-chip size="small" variant="tonal">
            来源 {{ detail.source || '未知' }}
          </v-chip>

          <v-chip v-if="detail.scanner_version" size="small" variant="tonal">
            扫描器 {{ detail.scanner_version }}
          </v-chip>

          <v-chip v-if="detail.device" prepend-icon="mdi-laptop" size="small" variant="tonal">
            {{ detail.device }}
          </v-chip>
        </div>

        <!--
          放回次数单拎出来做横幅，是这个页面唯一「需要有人做点什么」的信号。
          一次隔离配一次放回是常态（本人觉得是误报）；同一个包被很多人放回，
          要么这条规则该改，要么这个技能真的在扩散。
        -->
        <v-alert
          v-if="restoredCount > 0"
          class="mb-4"
          density="compact"
          type="error"
          variant="tonal"
        >
          这个技能包（<code>{{ shortHash(detail.bundle_hash) }}</code>）
          共有 <strong>{{ restoredCount }}</strong> 次「员工放回」，
          涉及 <strong>{{ restoredUserCount }}</strong> 个人。
          放回是被允许的动作 —— 数字高说明要复核的是规则本身，不是这些人。
        </v-alert>

        <v-alert
          v-if="detail.reason"
          class="mb-4"
          density="compact"
          type="info"
          variant="tonal"
        >
          <div class="text-caption font-weight-medium mb-1">扫描器给的隔离原因</div>

          {{ detail.reason }}
        </v-alert>

        <!-- ---------- 事实字段 ---------- -->
        <v-list class="bg-transparent mb-4" density="compact">
          <v-list-item v-if="detail.origin_relpath">
            <v-list-item-subtitle class="text-caption">隔离前的路径</v-list-item-subtitle>

            <div class="text-body-2 break-all">{{ detail.origin_relpath }}</div>
          </v-list-item>

          <v-list-item>
            <v-list-item-subtitle class="text-caption">
              发生时间（客户端自报，UTC 原文）
            </v-list-item-subtitle>

            <div class="text-body-2">{{ detail.occurred_at || '—' }}</div>
          </v-list-item>

          <v-list-item>
            <v-list-item-subtitle class="text-caption">技能包哈希</v-list-item-subtitle>

            <div class="text-body-2 break-all font-mono">{{ detail.bundle_hash || '—' }}</div>
          </v-list-item>
        </v-list>

        <!-- ---------- 命中项 ---------- -->
        <div class="text-subtitle-2 mb-2">
          命中项（{{ detail.findings.length }}）
        </div>

        <!--
          finding_count 有值但 findings 是空的，有两种可能：放回事件本来就只报
          数量不报明细，或者那条 findings_json 坏了（后端解析失败会静默给空数组，
          好让详情页其它字段照样打得开）。两种都要说出来 —— 「没有命中项」和
          「命中项没拿到」会导出完全相反的复核结论。
        -->
        <v-alert
          v-if="detail.findings.length === 0"
          class="mb-4"
          density="compact"
          type="warning"
          variant="tonal"
        >
          <template v-if="detail.finding_count > 0">
            这条事件记着 {{ detail.finding_count }} 项命中，但明细没有拿到 ——
            放回事件只上报数量不上报明细，找同一个技能包的隔离事件看明细。
          </template>

          <template v-else>没有命中项记录。</template>
        </v-alert>

        <v-expansion-panels v-else class="mb-4" variant="accordion">
          <v-expansion-panel v-for="(finding, index) in detail.findings" :key="index">
            <v-expansion-panel-title>
              <div class="flex items-center gap-2 min-w-0">
                <v-chip
                  :color="SKILL_SEVERITY_COLORS[finding.severity ?? ''] ?? 'grey'"
                  size="x-small"
                  :text="finding.severity ?? '未知'"
                  variant="tonal"
                />

                <span class="text-body-2 truncate">
                  {{ finding.pattern_id || '未命名规则' }}
                </span>

                <span class="text-caption text-medium-emphasis truncate">
                  {{ finding.file }}<template v-if="finding.line">:{{ finding.line }}</template>
                </span>
              </div>
            </v-expansion-panel-title>

            <v-expansion-panel-text>
              <div class="text-body-2 mb-2">{{ finding.description }}</div>

              <div v-if="finding.category" class="text-caption text-medium-emphasis mb-2">
                类别 {{ finding.category }}
              </div>

              <!-- 命中那一行的原文。客户端已按 120 字符截过，这里不再截：
                   剩下的每个字符都可能是判断误报与否的依据 -->
              <pre v-if="finding.match" class="code-block">{{ finding.match }}</pre>
            </v-expansion-panel-text>
          </v-expansion-panel>
        </v-expansion-panels>

        <!-- ---------- 样本源码 ---------- -->
        <div class="text-subtitle-2 mb-2">样本源码</div>

        <!--
          按钮而不是自动加载：取代码是一个单独的动作。
          现在还没有查阅日志（TODO），但接口已经分开了，日志补上时挂点就在这里 ——
          要是做成"打开详情就顺手预取"，那条日志一上线就全是没有人真做过的查阅。
        -->
        <v-alert
          v-if="!can('skill_security:sample')"
          class="mb-4"
          density="compact"
          type="info"
          variant="tonal"
        >
          你没有查看样本源码的权限。判断一段 curl 是外发密钥还是正常调接口是工程判断，
          这个权限点只给到能读代码的人 —— 需要复核请找开发者。
        </v-alert>

        <v-alert
          v-else-if="detail.sample_id === null"
          class="mb-4"
          density="compact"
          type="info"
          variant="tonal"
        >
          这条事件没有带样本。放回事件本来就不带 —— 同一个技能包的样本在隔离时
          已经传过一次，去同包事件里那条隔离看。
        </v-alert>

        <template v-else>
          <v-btn
            v-if="!sample"
            class="mb-4"
            :loading="sampleLoading"
            prepend-icon="mdi-code-braces"
            text="调取样本源码"
            variant="tonal"
            @click="loadSample(detail.sample_id)"
          />

          <template v-else>
            <!--
              ⚠️ 这条横幅不能收成小字。漏了它，读到一份看着人畜无害的样本就会
              得出「误报」的结论，而真正的东西在那个没传上来的 12MB 文件里。
            -->
            <v-alert
              v-if="sample.truncated"
              class="mb-3"
              density="compact"
              type="warning"
              variant="tonal"
            >
              <strong>你看到的不是全部。</strong>
              {{ sample.file_count }} 个文件里只有 {{ sample.included_count }} 个带了内容
              （{{ formatBytes(sample.included_bytes) }}），
              原因：{{ TRUNCATED_REASONS[sample.truncated_reason ?? ''] ?? sample.truncated_reason }}。
              没传上来的文件在下面列着路径与 sha256，要看内容得去那台机器上取。
            </v-alert>

            <div class="text-caption text-medium-emphasis mb-3">
              这份样本按内容去重，被上报过 {{ sample.seen_count }} 次
              （{{ formatTime(sample.first_seen_at) }} 起）。
            </div>

            <v-expansion-panels class="mb-4" variant="accordion">
              <v-expansion-panel v-for="file in sample.files" :key="file.path">
                <v-expansion-panel-title>
                  <div class="flex items-center gap-2 min-w-0">
                    <span class="text-body-2 truncate font-mono">{{ file.path }}</span>

                    <v-chip
                      v-if="!file.included"
                      color="warning"
                      size="x-small"
                      :text="OMITTED_REASONS[file.omitted ?? ''] ?? (file.error ? '读取失败' : '未上传')"
                      variant="tonal"
                    />

                    <span v-if="file.size_bytes" class="text-caption text-medium-emphasis">
                      {{ formatBytes(file.size_bytes) }}
                    </span>
                  </div>
                </v-expansion-panel-title>

                <v-expansion-panel-text>
                  <pre v-if="file.included" class="code-block">{{ file.content }}</pre>

                  <template v-else>
                    <div class="text-body-2 mb-2">
                      <template v-if="file.error">读不出来：{{ file.error }}</template>

                      <template v-else>
                        内容没有上传（{{ OMITTED_REASONS[file.omitted ?? ''] ?? file.omitted }}）。
                        下面的 sha256 是在员工机器上算的，可以拿它去核对。
                      </template>
                    </div>

                    <div v-if="file.sha256" class="text-caption font-mono break-all">
                      {{ file.sha256 }}
                    </div>
                  </template>
                </v-expansion-panel-text>
              </v-expansion-panel>
            </v-expansion-panels>
          </template>
        </template>

        <!-- ---------- 同包事件 ---------- -->
        <div class="text-subtitle-2 mb-2">
          同一个技能包的其它事件（{{ detail.related.length }}）
        </div>

        <v-alert
          v-if="detail.related.length === 0"
          density="compact"
          type="info"
          variant="tonal"
        >
          <template v-if="detail.bundle_hash">这个技能包只有这一条事件。</template>

          <!-- 没有 bundle_hash 就串不起来，这与「确实只发生过一次」是两回事 -->
          <template v-else>
            这条事件没有技能包哈希，无法与其它事件串联 ——
            上报它的客户端版本还没带这个字段。
          </template>
        </v-alert>

        <v-list v-else class="bg-transparent" density="compact">
          <v-list-item
            v-for="row in detail.related"
            :key="row.id"
            class="rounded mb-1"
            link
            @click="openDetail(row.id)"
          >
            <template #prepend>
              <v-chip
                class="me-3"
                :color="SKILL_EVENT_COLORS[row.event] ?? 'grey'"
                size="x-small"
                :text="SKILL_EVENT_LABELS[row.event] ?? row.event"
                variant="tonal"
              />
            </template>

            <v-list-item-title class="text-body-2">
              {{ row.user_uid }}
              <span class="text-medium-emphasis">· {{ row.dept_code || '未分配部门' }}</span>
            </v-list-item-title>

            <v-list-item-subtitle class="text-caption">
              {{ formatTime(row.created_at) }}
              <span v-if="row.device">· {{ row.device }}</span>
            </v-list-item-subtitle>
          </v-list-item>
        </v-list>
      </div>
    </template>
  </v-navigation-drawer>
</template>

<script lang="ts" setup>
  import type { Department, SkillEventDetail, SkillEventRow, SkillSample } from '@/types/api'
  import { computed, onMounted, ref } from 'vue'
  import * as departmentApi from '@/api/department'
  import * as skillApi from '@/api/skill-security'
  import { usePermission } from '@/composables/usePermission'
  import { useToastStore } from '@/stores/toast'
  import {
    SKILL_EVENT_COLORS,
    SKILL_EVENT_LABELS,
    SKILL_EVENTS,
    SKILL_SEVERITY_COLORS,
    SKILL_VERDICT_COLORS,
    SKILL_VERDICTS,
  } from '@/types/api'

  const toast = useToastStore()
  const { can } = usePermission()

  /** 客户端 collect_sample 里 truncated_reason 的两个取值 */
  const TRUNCATED_REASONS: Record<string, string> = {
    file_count: '文件数超过上限',
    total_bytes: '总字节数超过上限',
  }

  /** 单个文件没带内容的原因（tools/skill_report.py 的 entry.omitted） */
  const OMITTED_REASONS: Record<string, string> = {
    too_large: '文件过大',
    binary: '二进制',
    budget_exhausted: '总量超限',
  }

  const headers = [
    { title: '技能', key: 'skill_name' },
    { title: '事件', key: 'event', width: 110, sortable: false },
    { title: '结论', key: 'verdict', width: 130, sortable: false },
    { title: '上报人', key: 'user_uid', width: 240, sortable: false },
    { title: '网关收到时间', key: 'created_at', width: 200, sortable: false },
    { title: '', key: 'actions', align: 'end' as const, sortable: false, width: 60 },
  ]

  const items = ref<SkillEventRow[]>([])
  const departments = ref<Department[]>([])
  const total = ref(0)
  const page = ref(1)
  const pageSize = ref(20)
  const loading = ref(false)

  const event = ref<string | null>(null)
  const verdict = ref<string | null>(null)
  const deptId = ref<number | null>(null)
  const skillName = ref('')
  const userUid = ref('')

  const drawer = ref(false)
  const detail = ref<SkillEventDetail | null>(null)
  const sample = ref<SkillSample | null>(null)
  const sampleLoading = ref(false)

  /**
   * 同包的放回事件 —— 当前这条也算进去，它自己可能就是一条放回
   *
   * 没有 bundle_hash 就一条都不算：那时 related 必然是空的，只剩当前这一条，
   * 数出来的「1 次放回」说的是「我们只看得见一条」，不是「只发生过一次」。
   */
  const restoredEvents = computed(() => {
    if (!detail.value?.bundle_hash) {
      return []
    }
    const all = [
      ...detail.value.related,
      { event: detail.value.event, user_uid: detail.value.user_uid },
    ]
    return all.filter(row => row.event === 'skill_quarantine_restored')
  })

  const restoredCount = computed(() => restoredEvents.value.length)

  /**
   * 放回涉及多少个人
   *
   * 与次数分开显示：同一个人反复装反复放回，和十个人各放回一次，
   * 是完全不同的两件事 —— 前者是一个人的习惯，后者是这条规则拦错了。
   */
  const restoredUserCount = computed(
    () => new Set(restoredEvents.value.map(row => row.user_uid)).size,
  )

  function formatTime (value: string | null): string {
    if (!value) {
      return '—'
    }
    return value.replace('T', ' ').slice(0, 19)
  }

  function formatBytes (bytes: number): string {
    if (bytes >= 1024 * 1024) {
      return `${(bytes / 1024 / 1024).toFixed(1)} MB`
    }
    if (bytes >= 1024) {
      return `${Math.round(bytes / 1024)} KB`
    }
    return `${bytes} B`
  }

  function shortHash (value: string | null): string {
    return value ? value.slice(0, 12) : '无哈希'
  }

  /**
   * 客户端时间与网关时间差得离谱时给一句提示
   *
   * occurred_at 是客户端 UTC 原文，created_at 是网关本地时间 —— 两者本来就不同源，
   * 差几分钟是排队重传的正常现象。超过一天基本只有两种解释：那台机器时钟不对，
   * 或者事件在离线队列里压了很久。哪种都值得知道，因为「这条是什么时候发生的」
   * 会直接影响复核结论。
   */
  function clockSkewText (row: SkillEventRow): string {
    if (!row.occurred_at || !row.created_at) {
      return ''
    }
    const occurred = Date.parse(row.occurred_at.endsWith('Z') ? row.occurred_at : `${row.occurred_at}Z`)
    const received = Date.parse(row.created_at)
    if (Number.isNaN(occurred) || Number.isNaN(received)) {
      return ''
    }
    const days = Math.round(Math.abs(received - occurred) / 86_400_000)
    if (days < 1) {
      return ''
    }
    return `客户端自报发生于 ${row.occurred_at}（相差 ${days} 天）`
  }

  function query (): skillApi.SkillEventQuery {
    return {
      event: event.value || undefined,
      verdict: verdict.value || undefined,
      dept_id: deptId.value ?? undefined,
      skill_name: skillName.value.trim() || undefined,
      user_uid: userUid.value.trim() || undefined,
    }
  }

  async function loadList () {
    loading.value = true
    try {
      const result = await skillApi.fetchSkillEvents({
        ...query(),
        page: page.value,
        page_size: pageSize.value,
      })
      items.value = result.items ?? []
      total.value = result.total ?? 0
    } catch (error) {
      toast.error(error, '加载技能安全事件失败')
    } finally {
      loading.value = false
    }
  }

  function search () {
    page.value = 1
    loadList()
  }

  function onOptions (options: { page: number, itemsPerPage: number }) {
    page.value = options.page
    pageSize.value = options.itemsPerPage
    loadList()
  }

  /** 打开详情。样本**不在**这里取，见模板里那个按钮上的说明 */
  async function openDetail (id: number) {
    sample.value = null
    try {
      detail.value = await skillApi.fetchSkillEvent(id)
      drawer.value = true
    } catch (error) {
      toast.error(error, '加载事件详情失败')
    }
  }

  async function loadSample (sampleId: number) {
    sampleLoading.value = true
    try {
      sample.value = await skillApi.fetchSkillSample(sampleId)
    } catch (error) {
      toast.error(error, '调取样本失败')
    } finally {
      sampleLoading.value = false
    }
  }

  /**
   * ⚠️ 这里**不**调 loadList
   *
   * v-data-table-server 的 useOptions 里那个 watch 带 `immediate: true`，
   * 挂载时就会发一次 update:options，首屏数据由 onOptions 拉。
   * 再补一次 onMounted 加载的话，每次进页面都是两个一模一样的列表请求。
   */
  onMounted(async () => {
    // 拿不到部门列表不影响主流程：筛选少一个下拉，页面照常用
    if (can('dept:view')) {
      try {
        const result = await departmentApi.listDepartments()
        departments.value = result.items ?? []
      } catch {
        departments.value = []
      }
    }
  })
</script>

<style scoped>
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
