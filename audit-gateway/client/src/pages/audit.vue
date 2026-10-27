<template>
  <!--
    会话审计页（决策 #8 的最后一块）

    交互选的是「列表 + 详情抽屉」而不是「表格里直接展开全文」，
    这不是审美偏好：列表接口因此可以不下发全文，于是每一次「看了谁的哪一条」
    都能精确落进 audit_access_logs。表格直接展开的话，
    查阅日志只能记「看了第 3 页」，事后追责基本没用。

    ⚠️ 由此推出一条前端纪律：**详情只能由点击触发，绝不预加载**。
    为了「更流畅」而提前批量拉详情，会凭空造出一堆「查看全文」记录，
    把真正的查阅行为淹没。
  -->

  <v-tabs v-model="tab" class="mb-4" color="primary" density="comfortable">
    <v-tab prepend-icon="mdi-forum-outline" value="conversations">会话记录</v-tab>

    <v-tab prepend-icon="mdi-eye-outline" value="access">查阅日志</v-tab>
  </v-tabs>

  <!-- ==================== 会话记录 ==================== -->
  <template v-if="tab === 'conversations'">
    <!-- ---------- 筛选条 ---------- -->
    <v-card border class="mb-4" flat rounded="lg">
      <v-card-text class="pb-2">
        <div class="audit-filters flex flex-wrap items-start gap-3">
          <v-text-field
            v-model="start"
            class="audit-filter audit-filter--date"
            density="compact"
            hide-details
            label="开始日期"
            type="date"
            variant="outlined"
          />

          <v-text-field
            v-model="end"
            class="audit-filter audit-filter--date"
            density="compact"
            hide-details
            label="结束日期"
            type="date"
            variant="outlined"
          />

          <v-autocomplete
            v-model="deptId"
            class="audit-filter audit-filter--select"
            clearable
            density="compact"
            hide-details
            item-title="name"
            item-value="id"
            :items="filters.departments"
            label="部门"
            no-data-text="没有匹配项"
            variant="outlined"
            @update:model-value="onDeptChange"
          />

          <!-- 可搜索：公司几百号人，纯下拉滚不动。
               选了部门就只列该部门的人，避免两个筛选互相矛盾 -->
          <v-autocomplete
            v-model="userUid"
            class="audit-filter audit-filter--text"
            clearable
            density="compact"
            hide-details
            item-title="label"
            item-value="uid"
            :items="userOptions"
            label="用户"
            no-data-text="没有匹配项"
            variant="outlined"
          />

          <v-autocomplete
            v-model="modelId"
            class="audit-filter audit-filter--text"
            clearable
            density="compact"
            hide-details
            item-title="name"
            item-value="id"
            :items="filters.models"
            label="模型"
            no-data-text="没有匹配项"
            variant="outlined"
          />

          <v-select
            v-model="attachmentFilter"
            class="audit-filter audit-filter--select"
            density="compact"
            hide-details
            :items="ATTACHMENT_FILTERS"
            label="附件"
            variant="outlined"
          />

          <v-text-field
            v-model="keyword"
            class="audit-filter audit-filter--wide"
            clearable
            density="compact"
            hide-details
            label="内容关键词"
            placeholder="至少 2 个字"
            prepend-inner-icon="mdi-magnify"
            variant="outlined"
            @keyup.enter="search"
          />

          <!-- 从「用量统计 → 调用明细」复制请求ID 粘进来，直接定位到那一次调用 -->
          <v-text-field
            v-model="requestId"
            class="audit-filter audit-filter--wide"
            clearable
            density="compact"
            hide-details
            label="请求ID"
            placeholder="从用量明细复制"
            prepend-inner-icon="mdi-identifier"
            variant="outlined"
            @keyup.enter="search"
          />

          <!--
            标题生成、命令审批扫描这些机器轮次默认不显示 —— 它们会把
            「第一个请求总是失败」这类噪声顶到最前面。
            ⚠️ 只是不显示，**全量存储不变**：打开这个开关就都回来了。
          -->
          <v-switch
            v-model="includeMachineTurns"
            class="audit-filter--switch"
            color="primary"
            density="compact"
            hide-details
            label="含机器轮次"
            @update:model-value="search"
          />

          <v-btn
            color="primary"
            :loading="loading"
            prepend-icon="mdi-magnify"
            text="查询"
            variant="tonal"
            @click="search"
          />

          <v-btn
            prepend-icon="mdi-download-outline"
            text="导出摘要"
            variant="text"
            @click="exportCsv"
          />
        </div>
      </v-card-text>
    </v-card>

    <!-- ---------- 列表 ---------- -->
    <v-card border flat rounded="lg">
      <v-card-item>
        <v-card-title class="text-subtitle-1">会话记录</v-card-title>

        <v-card-subtitle>
          共 {{ total }} 条 · {{ rangeText }} · 点击任意一行查看全文
        </v-card-subtitle>

        <template #append>
          <!-- 提示保留但收成一个图标：审计员必须知道自己也在被记录，
               但不需要每次进页面都被一整条横幅提醒一遍 -->
          <v-icon color="medium-emphasis" icon="mdi-shield-account-outline" size="small">
            <v-tooltip activator="parent" location="bottom" max-width="320">
              你的检索、查看全文、查看附件与导出都会被记入查阅日志。
            </v-tooltip>
          </v-icon>
        </template>
      </v-card-item>

      <v-divider />

      <!--
        一行 = 一个会话，不是一次调用（决策 #52 · C1）。
        一次提问在底层散成十几条记录：标题生成 1 条、主对话 N 个 agent 轮次、
        命令审批扫描若干条 —— 平铺出来彼此看不出关系，而真正想看的
        「这个人问了什么」淹在里面。
      -->
      <v-data-table-server
        v-model:expanded="expanded"
        density="comfortable"
        :headers="sessionHeaders"
        hover
        item-value="session_key"
        :items="sessions"
        :items-length="total"
        :items-per-page="pageSize"
        :loading="loading"
        no-data-text="该条件下没有会话记录"
        :page="page"
        show-expand
        @update:expanded="onExpand"
        @update:options="onOptions"
      >
        <template #[`item.title`]="{ item }">
          <div class="preview-cell">
            <div class="truncate">{{ item.title || '（无文本内容）' }}</div>

            <div class="text-caption text-medium-emphasis">
              {{ formatTime(item.started_at) }}
              <template v-if="item.ended_at !== item.started_at">
                ~ {{ formatTime(item.ended_at)?.slice(11) }}
              </template>

              <!--
                没有会话 ID 的记录照常显示，只是标出来。stock 客户端、
                绕过客户端的直连永远没有它 —— 「分不了组就不显示」
                正好把最该被看见的那类调用藏起来了。
              -->
              <span v-if="!item.linked" class="ms-2">· 未串联（单条记录）</span>
            </div>
          </div>
        </template>

        <template #[`item.username`]="{ item }">
          {{ item.username }}

          <div class="text-caption text-medium-emphasis">
            {{ item.dept_name ?? '未分配部门' }} · {{ roleLabel(item.role) }}
          </div>
        </template>

        <template #[`item.turns`]="{ item }">
          <v-chip
            color="primary"
            size="small"
            :text="`${item.turns} 次调用`"
            variant="tonal"
          />
        </template>

        <template #[`item.total_tokens`]="{ item }">
          <span class="text-caption">{{ item.total_tokens.toLocaleString() }}</span>
        </template>

        <template #[`item.attachment_count`]="{ item }">
          <v-chip
            v-if="item.attachment_count > 0"
            color="deep-purple"
            prepend-icon="mdi-paperclip"
            size="small"
            :text="String(item.attachment_count)"
            variant="tonal"
          />

          <span v-else class="text-medium-emphasis">—</span>
        </template>

        <template #[`item.flags`]="{ item }">
          <v-chip
            v-if="item.has_failure"
            color="error"
            size="small"
            text="有失败"
            variant="tonal"
          />

          <!-- 截断必须在列表就标出来：详情页看到的不是全部，
               而「不是全部」这件事会直接影响审计结论 -->
          <v-chip
            v-if="item.has_truncated"
            class="ms-1"
            color="warning"
            size="x-small"
            text="截断"
            variant="tonal"
          />

          <span v-if="!item.has_failure && !item.has_truncated" class="text-medium-emphasis">—</span>
        </template>

        <!-- ---------- 展开：这个会话里的每一次调用 ---------- -->
        <template #expanded-row="{ columns, item }">
          <tr>
            <td class="p-0" :colspan="columns.length">
              <v-sheet class="p-3" color="surface-light">
                <div v-if="turnsLoading[item.session_key]" class="text-caption py-2">
                  正在载入这个会话里的调用记录…
                </div>

                <template v-else-if="!item.linked">
                  <!--
                    没有会话 ID 的行本来就只有一条记录，不用再去拉 ——
                    拿 `#12` 当 session_id 查必然是 0 条。直接给入口。
                  -->
                  <v-list class="bg-transparent" density="compact">
                    <v-list-item link @click="openDetail(item.first_id)">
                      <v-list-item-title class="text-body-2">
                        {{ item.title || '（无文本内容）' }}
                      </v-list-item-title>

                      <v-list-item-subtitle class="text-caption">
                        这条记录没有会话 ID，无法与其他调用串联 ——
                        可能来自未经我们客户端的直连调用。
                      </v-list-item-subtitle>
                    </v-list-item>
                  </v-list>
                </template>

                <template v-else>
                  <v-list class="bg-transparent" density="compact">
                    <!--
                      ⚠️ 会话内每一行的主标题是**模型回复**，不是用户输入。
                      Hermes 每轮把完整历史重发一遍，所以「本次用户输入」
                      从头到尾都是同一句 —— 拿它当标题的话 58 行长得一模一样，
                      整个展开区读不出任何东西（2026-08-24 负责人截图反馈）。
                      真正逐轮不同的是模型说了什么、调了什么工具。
                    -->
                    <v-list-item
                      v-for="turn in (turns[item.session_key] ?? [])"
                      :key="turn.id"
                      class="rounded mb-1"
                      link
                      @click="openDetail(turn.id)"
                    >
                      <template #prepend>
                        <span class="text-caption text-medium-emphasis me-3">
                          {{ formatTime(turn.created_at)?.slice(11) }}
                        </span>
                      </template>

                      <v-list-item-title class="text-body-2">
                        {{ turn.response_preview || '（本轮无回复，只调了工具）' }}
                      </v-list-item-title>

                      <!--
                        问题只在**变了**的时候才显示。会话内绝大多数轮次
                        repeats_question 都是 true，全列出来就是同一句话刷屏。
                      -->
                      <v-list-item-subtitle
                        v-if="!turn.repeats_question"
                        class="text-caption"
                      >
                        问：{{ turn.preview || '（无文本内容）' }}
                      </v-list-item-subtitle>

                      <template #append>
                        <!--
                          这一轮**新增**的工具调用，不是上下文里的累计。
                          null = 算不出（会话第一轮、跨页、上下文被压缩过），
                          和「这一轮没调工具」是两回事，所以显示得不一样。
                        -->
                        <v-chip
                          v-for="tool in (turn.tools ?? [])"
                          :key="tool.name"
                          class="me-1"
                          size="x-small"
                          :text="`${tool.name} × ${tool.count}`"
                          variant="tonal"
                        />

                        <v-chip
                          v-if="turn.turn_kind !== 'chat'"
                          class="me-1"
                          color="grey"
                          size="x-small"
                          :text="turnKindLabel(turn.turn_kind)"
                          variant="tonal"
                        />

                        <v-chip
                          v-if="turn.status !== 'success'"
                          color="error"
                          size="x-small"
                          text="失败"
                          variant="tonal"
                        />
                      </template>
                    </v-list-item>
                  </v-list>

                  <div
                    v-if="(turns[item.session_key] ?? []).length === 0"
                    class="text-caption text-medium-emphasis py-2"
                  >
                    这个会话里没有符合当前筛选条件的调用记录。
                  </div>

                  <div
                    v-else-if="hasMoreTurns(item.session_key)"
                    class="flex items-center gap-3 pt-2"
                  >
                    <v-btn
                      :loading="turnsMoreLoading[item.session_key]"
                      prepend-icon="mdi-chevron-down"
                      size="small"
                      variant="tonal"
                      @click="loadMoreTurns(item.session_key)"
                    >
                      加载更多
                    </v-btn>

                    <span class="text-caption text-medium-emphasis">
                      已列出 {{ (turns[item.session_key] ?? []).length }} 次，
                      共 {{ turnsTotal[item.session_key] ?? item.turns }} 次
                    </span>
                  </div>
                </template>
              </v-sheet>
            </td>
          </tr>
        </template>
      </v-data-table-server>
    </v-card>
  </template>

  <!-- ==================== 查阅日志 ==================== -->
  <template v-else>
    <v-card border class="mb-4" flat rounded="lg">
      <v-card-text class="pb-2">
        <div class="audit-filters flex flex-wrap items-start gap-3">
          <v-text-field
            v-model="logStart"
            class="audit-filter audit-filter--date"
            density="compact"
            hide-details
            label="开始日期"
            type="date"
            variant="outlined"
          />

          <v-text-field
            v-model="logEnd"
            class="audit-filter audit-filter--date"
            density="compact"
            hide-details
            label="结束日期"
            type="date"
            variant="outlined"
          />

          <v-select
            v-model="logAction"
            class="audit-filter audit-filter--select"
            clearable
            density="compact"
            hide-details
            item-title="title"
            item-value="value"
            :items="AUDIT_ACTIONS"
            label="动作"
            variant="outlined"
          />

          <!-- 按被查阅人筛选：回答「有没有人在专门盯着某个人」 -->
          <v-autocomplete
            v-model="logTargetUid"
            class="audit-filter audit-filter--wide"
            clearable
            density="compact"
            hide-details
            item-title="label"
            item-value="uid"
            :items="allUserOptions"
            label="被查阅人"
            no-data-text="没有匹配项"
            variant="outlined"
          />

          <v-btn
            color="primary"
            :loading="logLoading"
            prepend-icon="mdi-magnify"
            text="查询"
            variant="tonal"
            @click="loadLogs"
          />

          <!-- 这份导出不含任何会话内容，是拿去交材料用的 -->
          <v-btn
            prepend-icon="mdi-download-outline"
            text="导出"
            variant="text"
            @click="exportLogsCsv"
          />
        </div>
      </v-card-text>
    </v-card>

    <v-card border flat rounded="lg">
      <v-card-item>
        <v-card-title class="text-subtitle-1">查阅日志</v-card-title>

        <v-card-subtitle>共 {{ logTotal }} 条 · 谁、何时、看了谁的什么</v-card-subtitle>
      </v-card-item>

      <v-divider />

      <v-data-table-server
        density="comfortable"
        :headers="logHeaders"
        item-value="id"
        :items="logs"
        :items-length="logTotal"
        :items-per-page="logPageSize"
        :loading="logLoading"
        no-data-text="该区间内没有查阅记录"
        :page="logPage"
        @update:options="onLogOptions"
      >
        <template #[`item.created_at`]="{ item }">
          <span class="text-caption">{{ formatTime(item.created_at) }}</span>
        </template>

        <template #[`item.actor_name`]="{ item }">
          {{ item.actor_name }}

          <div class="text-caption text-medium-emphasis">
            {{ roleLabel(item.actor_role) }} · {{ item.client_ip ?? '未知 IP' }}
          </div>
        </template>

        <template #[`item.action`]="{ item }">
          <v-chip
            :color="AUDIT_ACTION_COLORS[item.action] ?? 'grey'"
            size="small"
            :text="item.action_label"
            variant="tonal"
          />
        </template>

        <template #[`item.target`]="{ item }">
          <!-- 列表检索没有具体目标：只记了条件与命中数 -->
          <template v-if="item.target_conversation_id">
            <span class="font-weight-medium">{{ item.target_name ?? '未知用户' }}</span>

            <span class="text-caption text-medium-emphasis">
              的会话 #{{ item.target_conversation_id }}
              <template v-if="item.target_attachment_id">
                · 附件 #{{ item.target_attachment_id }}
              </template>
            </span>
          </template>

          <span v-else class="text-medium-emphasis text-caption">
            {{ item.query_json ?? '无筛选条件' }}
          </span>
        </template>

        <template #[`item.result_count`]="{ item }">
          {{ item.result_count }}
        </template>
      </v-data-table-server>
    </v-card>
  </template>

  <!-- ==================== 详情抽屉 ==================== -->
  <v-navigation-drawer
    v-model="drawer"
    location="right"
    temporary
    :width="720"
  >
    <template v-if="detail">
      <v-toolbar color="surface" density="comfortable">
        <div class="px-4 py-1">
          <!-- 时间放在标题位置：审计场景下「什么时候发生的」比「谁发的」还常被问到 -->
          <div class="text-subtitle-1 font-weight-medium">
            {{ formatTime(detail.created_at) }}
          </div>

          <div class="text-caption text-medium-emphasis">
            {{ detail.username }} · {{ detail.dept_name ?? '未分配部门' }}
            · {{ roleLabel(detail.role) }} · {{ detail.model_name ?? '未知模型' }}
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
            :color="detail.status === 'success' ? 'success' : 'error'"
            size="small"
            :text="detail.status === 'success' ? '成功' : '失败'"
            variant="tonal"
          />

          <v-chip v-if="detail.aborted" color="warning" size="small" variant="tonal">
            用户中断
          </v-chip>

          <v-chip size="small" variant="tonal">
            {{ detail.stream ? '流式' : '直接' }}
          </v-chip>

          <v-chip v-if="detail.masked" color="info" size="small" variant="tonal">
            已脱敏
          </v-chip>

          <v-chip
            v-if="detail.attachments.length > 0"
            color="deep-purple"
            prepend-icon="mdi-paperclip"
            size="small"
            :text="`${detail.attachments.length} 个附件`"
            variant="tonal"
          />

          <v-spacer />

          <span class="text-caption text-medium-emphasis">
            {{ detail.request_id }}
          </span>
        </div>

        <!-- 截断提醒必须显眼：看到的不是全部，会直接影响审计结论 -->
        <v-alert
          v-if="detail.truncated"
          class="mb-4"
          density="compact"
          icon="mdi-content-cut"
          type="warning"
          variant="tonal"
        >
          本条内容入库时超长已被截断，你看到的不是全部。
        </v-alert>

        <!-- ---------- 用户问了什么 ---------- -->
        <div class="text-subtitle-2 mb-2">本次用户输入</div>

        <AuditMessage
          initially-expanded
          role="user"
          :text="detail.user_input || '（无用户输入）'"
        />

        <div
          v-if="attachmentsOf(detail.user_input_markers).length > 0"
          class="flex flex-wrap gap-2 mt-2"
        >
          <AttachmentCard
            v-for="att in attachmentsOf(detail.user_input_markers)"
            :key="att.id"
            :attachment="att"
          />
        </div>

        <!-- ---------- 模型回复 ---------- -->
        <v-divider class="my-4" />

        <div class="text-subtitle-2 mb-2">模型回复</div>

        <AuditMessage role="assistant" :text="detail.response_text || '（无回复）'" />

        <!-- ---------- 上下文：只有指纹与骨架，没有正文 ---------- -->
        <v-divider class="my-4" />

        <div class="flex items-center gap-2 mb-2">
          <span class="text-subtitle-2">本次调用的上下文</span>

          <v-icon
            color="medium-emphasis"
            icon="mdi-help-circle-outline"
            size="x-small"
          >
            <v-tooltip activator="parent" location="bottom" max-width="360">
              审计只留「问了什么 / 答了什么」。系统提示词每条记录一模一样、
              员工也改不了，只存指纹；本地工具的结果只记体量不记正文。
              MCP 调用另有完整记录，见「MCP 调用」页。
            </v-tooltip>
          </v-icon>
        </div>

        <div class="text-body-2 text-medium-emphasis mb-2">
          <template v-if="detail.system_prompt_sha256">
            系统提示词 {{ detail.system_prompt_chars }} 字 ·
            <code>{{ detail.system_prompt_sha256.slice(0, 12) }}</code>
            <span class="text-caption">（只存指纹，正文不入库）</span>
          </template>

          <template v-else>本次调用没有系统提示词</template>
        </div>

        <!--
          ⚠️ 这一段是知情取舍的落点，别把它做得像「加载失败」：
          本地工具的结果确实出了网（员工的 agent 读了本机文件、抓了内网页面），
          停存正文之后这里只剩体量。它保住的信号是「这次对话往模型里灌了多少
          本地数据」—— 所以总字数要显眼，而不是藏在列表末尾。
        -->
        <template v-if="detail.tools && detail.tools.length > 0">
          <div class="text-body-2 mb-1">
            本地工具 {{ detail.tools.length }} 次，共
            <strong>{{ detail.tool_chars_total.toLocaleString() }}</strong> 字进入模型上下文
            <span class="text-caption text-medium-emphasis">（只记体量，正文不入库）</span>
          </div>

          <!--
            ⚠️ 默认按工具名汇总，不是把每一次都摊开。
            2026-08-23 实测：一次 agent 会话里 63 次工具调用，全摊开是一面
            读不出任何东西的 chip 墙，还把下面的内容全顶走了。审计在这一层
            要回答的是「用了哪些工具、各灌了多少数据进去」，那是**汇总**问题；
            逐次明细是排查问题，才需要展开。
          -->
          <v-chip-group class="flex flex-wrap" column>
            <v-chip
              v-for="tool in detail.tool_summary"
              :key="tool.name"
              size="small"
              variant="tonal"
            >
              {{ tool.name }} × {{ tool.count }} · {{ tool.chars.toLocaleString() }} 字
            </v-chip>
          </v-chip-group>

          <v-btn
            v-if="detail.tools.length > detail.tool_summary.length"
            class="mt-1"
            density="compact"
            :prepend-icon="toolsExpanded ? 'mdi-chevron-up' : 'mdi-chevron-down'"
            size="small"
            :text="toolsExpanded
              ? '收起逐次明细'
              : `展开逐次明细（${detail.tools.length} 次）`"
            variant="text"
            @click="toolsExpanded = !toolsExpanded"
          />

          <v-list v-if="toolsExpanded" class="bg-transparent" density="compact">
            <v-list-item
              v-for="(tool, index) in detail.tools"
              :key="index"
              class="px-0"
            >
              <template #prepend>
                <span class="text-caption text-medium-emphasis me-3">
                  {{ index + 1 }}
                </span>
              </template>

              <v-list-item-title class="text-body-2">
                {{ tool.name }}
              </v-list-item-title>

              <template #append>
                <span class="text-caption">{{ tool.chars.toLocaleString() }} 字</span>
              </template>
            </v-list-item>
          </v-list>
        </template>

        <div v-else class="text-body-2 text-medium-emphasis">
          本次调用没有使用本地工具
        </div>

        <!-- ---------- 未被引用的附件 ---------- -->
        <template v-if="orphanAttachments.length > 0">
          <v-divider class="my-4" />

          <div class="text-subtitle-2 mb-2">其他附件</div>

          <div class="flex flex-wrap gap-2">
            <AttachmentCard
              v-for="att in orphanAttachments"
              :key="att.id"
              :attachment="att"
            />
          </div>
        </template>
      </div>
    </template>

    <div v-else-if="detailLoading" class="p-8 text-center">
      <v-progress-circular color="primary" indeterminate />
    </div>
  </v-navigation-drawer>
</template>

<script lang="ts" setup>
  import type {
    AuditAccessLogRow,
    AuditAttachment,
    AuditDetail,
    AuditFilterOptions,
    AuditListRow,
    AuditSessionRow,
  } from '@/types/api'
  import { computed, onMounted, ref } from 'vue'
  import * as auditApi from '@/api/audit'
  import AttachmentCard from '@/components/AttachmentCard.vue'
  import AuditMessage from '@/components/AuditMessage.vue'
  import { roleLabel } from '@/constants/roles'
  import { useToastStore } from '@/stores/toast'
  import { AUDIT_ACTION_COLORS, AUDIT_ACTIONS } from '@/types/api'

  const toast = useToastStore()

  const tab = ref<'conversations' | 'access'>('conversations')

  // ---------------- 筛选条件 ----------------
  const start = ref('')
  const end = ref('')
  const deptId = ref<number | null>(null)
  const userUid = ref<string | null>(null)
  const modelId = ref<number | null>(null)
  const keyword = ref('')
  const requestId = ref('')
  /** 三态：'' 不限 / 'yes' 有附件 / 'no' 无附件 */
  const attachmentFilter = ref('')
  /** 默认关：标题生成、审批扫描会把噪声顶到最前面。存储不受影响，只是不显示 */
  const includeMachineTurns = ref(false)

  const ATTACHMENT_FILTERS = [
    { title: '不限', value: '' },
    { title: '有附件', value: 'yes' },
    { title: '无附件', value: 'no' },
  ]

  // ---------------- 列表数据 ----------------
  const sessions = ref<AuditSessionRow[]>([])
  /** 展开的会话键。v-data-table-server 的 expanded 走 item-value，即 session_key */
  const expanded = ref<string[]>([])
  /** 每个会话展开后加载到的调用记录，按 session_key 缓存 */
  const turns = ref<Record<string, AuditListRow[]>>({})
  const turnsLoading = ref<Record<string, boolean>>({})
  // 「加载更多」：每个会话已拉到第几页、命中总数、是否正在拉下一页
  const turnsPage = ref<Record<string, number>>({})
  const turnsTotal = ref<Record<string, number>>({})
  const turnsMoreLoading = ref<Record<string, boolean>>({})
  const TURNS_PAGE_SIZE = 100
  const total = ref(0)
  const page = ref(1)
  const pageSize = ref(20)
  const loading = ref(false)
  const rangeText = ref('')
  const filters = ref<AuditFilterOptions>({ departments: [], models: [], users: [] })

  /**
   * 用户下拉的候选项
   *
   * 选了部门就只列该部门的人。否则会出现「部门选研发部、用户选财务小王」
   * 这种自相矛盾的组合，查出来必然是 0 条，而人会以为是没有记录。
   */
  const userOptions = computed(() =>
    filters.value.users
      .filter(u => deptId.value === null || u.dept_id === deptId.value)
      .map(u => ({
        uid: u.uid,
        // 同名同姓不罕见，带上部门才选得准
        label: u.dept_name ? `${u.name} · ${u.dept_name}` : u.name,
      })),
  )

  /**
   * 全部用户 —— 查阅日志那个 Tab 用
   *
   * 不复用 userOptions：那份跟着「会话记录」Tab 的部门筛选走，
   * 在另一个 Tab 里会莫名其妙少掉一批人，而少掉的原因在当前页面上完全看不出来。
   */
  const allUserOptions = computed(() =>
    filters.value.users.map(u => ({
      uid: u.uid,
      label: u.dept_name ? `${u.name} · ${u.dept_name}` : u.name,
    })),
  )

  /** 换部门时清掉已选用户，避免留下一个不属于该部门的人 */
  function onDeptChange () {
    if (userUid.value && !userOptions.value.some(u => u.uid === userUid.value)) {
      userUid.value = null
    }
  }

  // ---------------- 详情抽屉 ----------------
  const drawer = ref(false)
  const detail = ref<AuditDetail | null>(null)
  const detailLoading = ref(false)

  // ---------------- 查阅日志 ----------------
  const logs = ref<AuditAccessLogRow[]>([])
  const logTotal = ref(0)
  const logPage = ref(1)
  const logPageSize = ref(20)
  const logLoading = ref(false)
  const logStart = ref('')
  const logEnd = ref('')
  const logAction = ref<string | null>(null)
  /**
   * 按**被查阅人**筛选
   *
   * 按查阅人翻只能看出「这个审计员很勤快」；按被查阅人翻才回答得了
   * 「有没有人在专门盯着某个人的会话」—— 那才是这张表存在的意义。
   */
  const logTargetUid = ref<string | null>(null)

  const sessionHeaders = [
    { title: '会话', key: 'title', sortable: false },
    { title: '用户', key: 'username', sortable: false, width: 160 },
    { title: '模型', key: 'model_name', sortable: false, width: 150 },
    { title: '调用', key: 'turns', sortable: false, width: 110 },
    { title: 'Token', key: 'total_tokens', sortable: false, width: 110 },
    { title: '附件', key: 'attachment_count', sortable: false, width: 80 },
    { title: '标记', key: 'flags', sortable: false, width: 130 },
  ]

  const TURN_KIND_LABELS: Record<string, string> = {
    approval: '命令审批',
    other: '辅助调用',
    title: '标题生成',
  }

  function turnKindLabel (kind: string): string {
    return TURN_KIND_LABELS[kind] ?? kind
  }

  const logHeaders = [
    { title: '时间', key: 'created_at', sortable: false, width: 160 },
    { title: '查阅人', key: 'actor_name', sortable: false, width: 180 },
    { title: '动作', key: 'action', sortable: false, width: 120 },
    { title: '对象 / 条件', key: 'target', sortable: false },
    { title: '条数', key: 'result_count', sortable: false, width: 80 },
  ]

  function formatTime (iso: string | null): string {
    if (!iso) {
      return '—'
    }
    return iso.replace('T', ' ').slice(0, 19)
  }

  /** 日期留空交给后端给默认窗口，前端不重复实现一遍默认区间逻辑 */
  function query (): auditApi.AuditQuery {
    return {
      start: start.value || undefined,
      end: end.value || undefined,
      dept_id: deptId.value ?? undefined,
      user_uid: userUid.value ?? undefined,
      model_id: modelId.value ?? undefined,
      keyword: keyword.value || undefined,
      request_id: requestId.value.trim() || undefined,
      has_attachment: attachmentFilter.value === ''
        ? undefined
        : attachmentFilter.value === 'yes',
      include_machine_turns: includeMachineTurns.value || undefined,
    }
  }

  // ---------------- 工具骨架 ----------------

  const toolsExpanded = ref(false)

  // ---------------- 附件归属 ----------------

  /**
   * ⚠️ `markers` 容忍 undefined，不是防御性编程的惯性。
   *
   * 2026-08-23 踩过一次：接口按决策 #52 换了返回结构，前端 dist 还是旧产物，
   * 旧代码读一个已经不存在的字段，`undefined.flatMap` 直接把整个抽屉炸成白屏。
   * 前后端分开部署，这种不同步**一定还会再发生**；那时候少显示一块附件，
   * 比让审计员打开详情看到一片空白要好得多 —— 后者会被当成「这条记录没内容」。
   */
  function attachmentsOf (markers: string[] | undefined): AuditAttachment[] {
    if (!detail.value || !markers || markers.length === 0) {
      return []
    }
    return detail.value.attachments.filter(a => markers.includes(a.marker))
  }

  /**
   * 没有被任何消息引用到的附件
   *
   * 正常情况下不会有 —— 但正文被截断时占位符可能一起被切掉，
   * 那时附件表里的记录仍在。宁可多显示一块「其他附件」，
   * 也不能让审计员完全看不到这些文件存在过。
   */
  const orphanAttachments = computed<AuditAttachment[]>(() => {
    if (!detail.value) {
      return []
    }
    // 同 attachmentsOf：字段缺了就当成「没有引用」，让附件全落到「其他附件」，
    // 而不是让这个 computed 抛异常把整个抽屉带走
    const referenced = new Set([
      ...(detail.value.user_input_markers ?? []),
      ...(detail.value.response_markers ?? []),
    ])
    return (detail.value.attachments ?? []).filter(a => !referenced.has(a.marker))
  })

  // ---------------- 加载 ----------------

  async function loadList () {
    loading.value = true
    try {
      const result = await auditApi.fetchAuditSessions({
        ...query(),
        page: page.value,
        page_size: pageSize.value,
      })
      sessions.value = result.items
      total.value = result.total
      rangeText.value = `${result.range.start} ~ ${result.range.end}`
      // 换了一页/换了筛选条件，之前展开的那些已经不在这一页上了。
      // 不清的话，缓存会跟着 session_key 复用到另一批数据上
      expanded.value = []
      turns.value = {}
      turnsPage.value = {}
      turnsTotal.value = {}
      turnsMoreLoading.value = {}
    } catch (error) {
      toast.error(error, '加载会话记录失败')
    } finally {
      loading.value = false
    }
  }

  /**
   * 展开某个会话 —— 拉这个会话里的每一次调用
   *
   * 走的是**同一个列表接口**，只是带上 session_id。不另开接口是刻意的：
   * 否则会变成两套摘要逻辑，迟早只改其中一套。
   *
   * 没有 session_id 的行（`#<id>`）不用拉：它本来就只有一条记录，
   * 而且拿 `#12` 去当 session_id 查必然是 0 条。
   */
  async function onExpand (keys: readonly string[]) {
    for (const key of keys) {
      const row = sessions.value.find(s => s.session_key === key)
      if (!row?.session_id || turns.value[key] || turnsLoading.value[key]) {
        continue
      }

      turnsLoading.value = { ...turnsLoading.value, [key]: true }
      try {
        const result = await auditApi.fetchAuditList({
          ...query(),
          session_id: row.session_id,
          page: 1,
          page_size: TURNS_PAGE_SIZE,
        })
        turns.value = { ...turns.value, [key]: result.items }
        turnsPage.value = { ...turnsPage.value, [key]: 1 }
        turnsTotal.value = { ...turnsTotal.value, [key]: result.total }
      } catch (error) {
        toast.error(error, '加载会话内的调用记录失败')
      } finally {
        turnsLoading.value = { ...turnsLoading.value, [key]: false }
      }
    }
  }

  function hasMoreTurns (key: string): boolean {
    const loaded = turns.value[key]?.length ?? 0
    return loaded > 0 && loaded < (turnsTotal.value[key] ?? 0)
  }

  /**
   * 会话内「加载更多」—— 接着拉下一页，追加到已列出的调用后面
   *
   * 列表按 id 倒序，翻页期间有新调用进来会把后面的行整体顶下去，
   * 下一页开头可能重复上一页的末尾，所以按 id 去重再追加。
   */
  async function loadMoreTurns (key: string) {
    const row = sessions.value.find(s => s.session_key === key)
    if (!row?.session_id || turnsMoreLoading.value[key] || !hasMoreTurns(key)) {
      return
    }

    const nextPage = (turnsPage.value[key] ?? 1) + 1
    turnsMoreLoading.value = { ...turnsMoreLoading.value, [key]: true }
    try {
      const result = await auditApi.fetchAuditList({
        ...query(),
        session_id: row.session_id,
        page: nextPage,
        page_size: TURNS_PAGE_SIZE,
      })
      const current = turns.value[key] ?? []
      const seen = new Set(current.map(t => t.id))
      const fresh = result.items.filter(t => !seen.has(t.id))
      const merged = [...current]

      // 「和上一轮问的是同一句」是服务端按页内相邻两行算的，页边界那一对算不到：
      // 上一页最后一行拿不到下一页第一行。这里补上，免得每页交界处多刷一句重复的问题
      const last = merged.at(-1)
      if (last && fresh[0] && last.preview && last.preview === fresh[0].preview) {
        merged[merged.length - 1] = { ...last, repeats_question: true }
      }
      merged.push(...fresh)

      turns.value = { ...turns.value, [key]: merged }
      turnsPage.value = { ...turnsPage.value, [key]: nextPage }
      turnsTotal.value = { ...turnsTotal.value, [key]: result.total }
      // 一整页都是重复、或者服务端已经没有更多：不再显示按钮，防止空转
      if (fresh.length === 0) {
        turnsTotal.value = { ...turnsTotal.value, [key]: merged.length }
      }
    } catch (error) {
      toast.error(error, '加载更多调用记录失败')
    } finally {
      turnsMoreLoading.value = { ...turnsMoreLoading.value, [key]: false }
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

  /**
   * 打开详情抽屉
   *
   * 全文只在这一刻被拉取，对应后端一条精确到单条的查阅日志。
   * **不要改成跟着列表预加载** —— 那会凭空造出一堆「查看全文」记录。
   */
  async function openDetail (id: number) {
    drawer.value = true
    detail.value = null
    toolsExpanded.value = false
    detailLoading.value = true
    try {
      detail.value = await auditApi.fetchAuditDetail(id)
    } catch (error) {
      toast.error(error, '加载会话详情失败')
      drawer.value = false
    } finally {
      detailLoading.value = false
    }
  }

  function exportCsv () {
    // 导出接口返回文件流，直接开新窗口让浏览器接管下载
    window.open(auditApi.buildAuditExportUrl(query()), '_blank')
  }

  /** 查阅日志的筛选条件 —— 查询与导出共用，免得两边条件对不上 */
  function logQuery (): auditApi.AccessLogQuery {
    return {
      start: logStart.value || undefined,
      end: logEnd.value || undefined,
      action: logAction.value ?? undefined,
      target_user_uid: logTargetUid.value ?? undefined,
    }
  }

  /**
   * 导出查阅日志
   *
   * 这份能放心导：**一个字的聊天内容都没有**，只有「谁看了谁的什么」。
   * 会话导出之所以只出摘要，是因为全文落到本地磁盘就脱离了留存期限管辖。
   */
  function exportLogsCsv () {
    window.open(auditApi.buildAccessLogExportUrl(logQuery()), '_blank')
  }

  async function loadLogs () {
    logLoading.value = true
    try {
      const result = await auditApi.fetchAccessLogs({
        ...logQuery(),
        page: logPage.value,
        page_size: logPageSize.value,
      })
      logs.value = result.items
      logTotal.value = result.total
    } catch (error) {
      toast.error(error, '加载查阅日志失败')
    } finally {
      logLoading.value = false
    }
  }

  function onLogOptions (options: { page: number, itemsPerPage: number }) {
    logPage.value = options.page
    logPageSize.value = options.itemsPerPage
    loadLogs()
  }

  onMounted(async () => {
    try {
      filters.value = await auditApi.fetchAuditFilters()
    } catch {
      // 下拉框拿不到不影响主流程，静默降级成空列表
    }
    await loadList()
  })
</script>

<style scoped>
/* 摘要列限宽：不限的话一行长文本会把表格撑到横向滚动 */
.preview-cell {
  max-width: 420px;
}

.truncate {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* 聊天气泡。white-space: pre-wrap 保留换行——
   代码片段和多行提示词是审计里最常见的内容，压成一行就没法读了 */
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

.audit-filter {
  flex: 1 1 180px;
  max-width: 240px;
}

.audit-filter--date {
  min-width: min(100%, 170px);
  max-width: 190px;
}

.audit-filter--select {
  min-width: min(100%, 180px);
}

.audit-filter--text {
  min-width: min(100%, 200px);
}

.audit-filter--wide {
  min-width: min(100%, 220px);
  max-width: 280px;
}

.audit-filter--switch {
  flex: 0 0 auto;
  min-width: 150px;
}

@media (max-width: 600px) {
  .audit-filter {
    flex-basis: 100%;
    max-width: none;
  }
}
</style>
