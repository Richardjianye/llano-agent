<template>
  <div class="flex flex-col gap-4">
    <v-card border flat rounded="lg">
      <v-card-item>
        <v-card-title>网站产物</v-card-title>
        <v-card-subtitle>上传单个 HTML 或根目录含 index.html 的 ZIP 网站包。先预览，再发布固定链接。</v-card-subtitle>

        <template #append>
          <v-btn color="primary" prepend-icon="mdi-plus" @click="createOpen = true">新建网站</v-btn>
        </template>
      </v-card-item>
    </v-card>

    <v-alert v-if="error" closable type="error" @click:close="error = ''">{{ error }}</v-alert>
    <v-alert v-if="notice" closable type="success" @click:close="notice = ''">{{ notice }}</v-alert>

    <v-card border flat rounded="lg">
      <v-data-table
        :headers="headers"
        item-value="id"
        :items="sites"
        :loading="loading"
        no-data-text="还没有网站产物"
      >
        <template #[`item.title`]="{ item }">
          <div class="font-weight-medium">{{ item.title }}</div>
          <div class="text-caption text-medium-emphasis">{{ item.id }}</div>
        </template>

        <template #[`item.status`]="{ item }">
          <v-chip :color="item.status === 'published' ? 'success' : item.status === 'withdrawn' ? 'warning' : 'default'" size="small" variant="tonal">
            {{ statusName(item.status) }}
          </v-chip>
        </template>

        <template #[`item.visibility`]="{ item }">{{ visibilityName(item.visibility) }}</template>
        <template #[`item.version`]="{ item }">v{{ item.published_version ?? '—' }} / 最新 v{{ item.latest_version }}</template>

        <template #[`item.actions`]="{ item }">
          <v-btn size="small" variant="text" @click="void openEditor(item)">管理</v-btn>
          <v-btn v-if="item.status === 'published'" size="small" variant="text" @click="void copyLink(item)">复制链接</v-btn>
        </template>
      </v-data-table>
    </v-card>

    <v-dialog v-model="createOpen" max-width="560">
      <v-card title="新建网站">
        <v-card-text class="flex flex-col gap-3">
          <v-text-field v-model="newTitle" hide-details label="网站名称" variant="outlined" />

          <div>
            <label class="text-body-2" for="site-create-file">HTML 文件或 ZIP 网站包</label>

            <input
              id="site-create-file"
              accept=".html,.htm,.zip"
              class="block mt-2"
              type="file"
              @change="onCreateFile"
            >
          </div>

          <div class="text-caption text-medium-emphasis">新网站默认仅自己可见。ZIP 根目录需有 index.html，CSS、图片等资源请使用相对路径；上传包最大 20 MB。</div>
        </v-card-text>

        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="createOpen = false">取消</v-btn>
          <v-btn color="primary" :disabled="!newTitle.trim() || !newFile" :loading="acting" @click="void create()">创建并预览</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <v-dialog v-model="editorOpen" max-width="800" scrollable>
      <v-card v-if="editing" :title="editing.title">
        <v-card-text class="flex flex-col gap-4">
          <v-text-field v-model="editing.title" hide-details label="网站名称" variant="outlined" />

          <v-select
            v-model="editing.visibility"
            hide-details
            :items="visibilityOptions"
            label="发布后谁能访问"
            variant="outlined"
          />

          <template v-if="editing.visibility === 'users' || editing.visibility === 'depts'">
            <v-text-field
              v-model="subjectQuery"
              hide-details
              label="搜索分享对象"
              prepend-inner-icon="mdi-magnify"
              variant="outlined"
              @update:model-value="void loadSubjects()"
            />

            <div class="flex flex-wrap gap-2">
              <v-chip v-for="id in editing.visibility === 'users' ? editing.allowed_users : editing.allowed_depts" :key="id" closable @click:close="removeSubject(id)">
                {{ id }}
              </v-chip>
            </div>

            <v-list
              border
              class="overflow-y-auto"
              density="compact"
              max-height="170"
              rounded="lg"
            >
              <v-list-item
                v-for="option in editing.visibility === 'users' ? subjects.users : subjects.depts"
                :key="option.value"
                :subtitle="option.value"
                :title="option.title"
                @click="addSubject(option.value)"
              />

              <v-list-item v-if="(editing.visibility === 'users' ? subjects.users : subjects.depts).length === 0" title="输入名称搜索" />
            </v-list>
          </template>

          <div class="flex items-center gap-2 flex-wrap">
            <v-btn color="primary" :loading="acting" variant="tonal" @click="void save()">保存访问范围</v-btn>
            <span class="text-caption text-medium-emphasis">访问规则对已发布页面与所有资源立即生效。</span>
          </div>

          <v-divider />
          <div class="text-subtitle-1">版本</div>

          <div>
            <label class="text-body-2" for="site-version-file">上传新版本</label>

            <input
              id="site-version-file"
              accept=".html,.htm,.zip"
              class="block mt-2"
              type="file"
              @change="onVersionFile"
            >

            <v-btn
              class="mt-2"
              :disabled="!versionFile"
              :loading="acting"
              size="small"
              variant="tonal"
              @click="void uploadVersion()"
            >上传版本</v-btn>
          </div>

          <v-list border rounded="lg">
            <v-list-item v-for="version in versions" :key="version.version" :subtitle="`${version.files.length} 个文件 · ${formatBytes(version.bytes_total)}`" :title="`v${version.version} · ${version.source_kind === 'zip' ? 'ZIP 网站包' : '单页 HTML'}`">
              <template #append>
                <v-chip v-if="editing.published_version === version.version && editing.status === 'published'" color="success" size="small">已发布</v-chip>
                <v-btn size="small" variant="text" @click="showPreview(version.preview_url)">预览</v-btn>
                <v-btn :disabled="acting" size="small" variant="text" @click="publishTarget = version.version">{{ editing.published_version && editing.published_version > version.version ? '回滚到此版' : '发布' }}</v-btn>
              </template>
            </v-list-item>
          </v-list>

          <div v-if="editing.status === 'published'" class="flex flex-wrap items-center gap-2">
            <v-btn color="warning" :loading="acting" variant="text" @click="void withdraw()">撤回网站</v-btn>
            <v-btn variant="text" @click="void copyLink(editing)">复制固定链接</v-btn>
          </div>

          <v-divider />

          <div>
            <div class="text-subtitle-1">网关集成示例</div>
            <div class="text-body-2 text-medium-emphasis mb-2">演示角色控制组件可见性及数据接口读取权限。网站所有者可配置，访问者需先登录且符合网站发布范围。</div>
            <v-btn variant="tonal" @click="void openDemo(editing.id)">打开角色权限示例</v-btn>
            <v-btn v-if="editing.status === 'published'" variant="text" @click="void copyDemoLink(editing.id)">复制示例链接</v-btn>
          </div>
        </v-card-text>

        <v-card-actions><v-spacer /><v-btn variant="text" @click="editorOpen = false">关闭</v-btn></v-card-actions>
      </v-card>
    </v-dialog>

    <v-dialog v-model="previewOpen" fullscreen>
      <v-card>
        <v-toolbar density="compact" title="网站预览">
          <v-spacer />
          <v-btn aria-label="关闭预览" icon="mdi-close" @click="previewOpen = false" />
        </v-toolbar>

        <v-alert density="compact" type="info" variant="tonal">私有预览会禁用页面脚本；CSS、图片等相对资源仍可加载。</v-alert>

        <iframe
          v-if="previewOpen"
          class="preview-frame"
          sandbox="allow-same-origin"
          :src="previewUrl"
          title="网站预览"
        />
      </v-card>
    </v-dialog>

    <v-dialog v-model="demoOpen" fullscreen @update:model-value="onDemoDialogChange">
      <SiteRoleDemo v-if="demoOpen && demoId" :key="demoId" :site-id="demoId" @close="void closeDemo()" />
    </v-dialog>

    <v-dialog max-width="500" :model-value="publishTarget !== null" @update:model-value="publishTarget = null">
      <v-card title="确认发布">
        <v-card-text>发布 v{{ publishTarget }} 后，{{ editing?.visibility === 'public' ? '任何获得链接的人都可访问页面及资源，页面脚本可在隔离沙箱中运行。' : `仅${visibilityName(editing?.visibility ?? 'private')}可以访问页面及资源；为保护网关登录态，受限页面禁用脚本。` }}</v-card-text>

        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="publishTarget = null">取消</v-btn>
          <v-btn color="primary" :loading="acting" @click="void publish()">确认发布</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>
  </div>
</template>

<script setup lang="ts">
  import { onMounted, ref, watch } from 'vue'
  import { useRoute, useRouter } from 'vue-router'
  import { addSiteVersion, createSite, listSites, listSiteVersions, publishSite, saveSite, searchSiteSubjects, type Site, type SiteSubjects, type SiteVersion, withdrawSite } from '@/api/sites'
  import SiteRoleDemo from '@/components/sites/SiteRoleDemo.vue'

  const route = useRoute()
  const router = useRouter()

  const headers = [
    { title: '网站', key: 'title' }, { title: '状态', key: 'status' },
    { title: '访问范围', key: 'visibility' }, { title: '版本', key: 'version' },
    { title: '', key: 'actions', sortable: false },
  ]
  const visibilityOptions = [
    { title: '仅自己', value: 'private' }, { title: '指定用户', value: 'users' },
    { title: '指定部门', value: 'depts' }, { title: '公开', value: 'public' },
  ]
  const sites = ref<Site[]>([])
  const versions = ref<SiteVersion[]>([])
  const loading = ref(false)
  const acting = ref(false)
  const error = ref('')
  const notice = ref('')
  const createOpen = ref(false)
  const editorOpen = ref(false)
  const previewOpen = ref(false)
  const publishTarget = ref<number | null>(null)
  const previewUrl = ref('')
  const demoId = ref('')
  const demoOpen = ref(false)
  const newTitle = ref('')
  const newFile = ref<File | null>(null)
  const versionFile = ref<File | null>(null)
  const editing = ref<Site | null>(null)
  const subjectQuery = ref('')
  const subjects = ref<SiteSubjects>({ users: [], depts: [] })

  function statusName (status: Site['status']) {
    return { draft: '草稿', published: '已发布', withdrawn: '已撤回' }[status]
  }
  function visibilityName (visibility: Site['visibility']) {
    return { private: '自己', users: '指定用户', depts: '指定部门', public: '所有人' }[visibility]
  }
  function formatBytes (size: number) {
    return size >= 1024 * 1024 ? `${(size / 1024 / 1024).toFixed(1)} MB` : `${Math.ceil(size / 1024)} KB`
  }
  function onCreateFile (event: Event) {
    newFile.value = (event.target as HTMLInputElement).files?.[0] ?? null
  }
  function onVersionFile (event: Event) {
    versionFile.value = (event.target as HTMLInputElement).files?.[0] ?? null
  }
  async function load () {
    loading.value = true
    try {
      sites.value = await listSites()
    } catch (error_) {
      error.value = messageOf(error_)
    } finally {
      loading.value = false
    }
  }
  function messageOf (cause: unknown) {
    return cause instanceof Error ? cause.message : '操作失败'
  }
  async function run (action: () => Promise<void>) {
    acting.value = true
    error.value = ''
    notice.value = ''
    try {
      await action()
    } catch (error_) {
      error.value = messageOf(error_)
    } finally {
      acting.value = false
    }
  }
  async function create () {
    if (!newFile.value) return
    await run(async () => {
      const site = await createSite(newTitle.value.trim(), newFile.value!)
      createOpen.value = false
      newTitle.value = ''
      newFile.value = null
      await load()
      await openEditor(site)
      showPreview(`/sites/${site.id}/preview/1/index.html`)
    })
  }
  async function openEditor (site: Site) {
    editing.value = { ...site, allowed_users: [...site.allowed_users], allowed_depts: [...site.allowed_depts] }
    versionFile.value = null
    subjectQuery.value = ''
    editorOpen.value = true
    try {
      versions.value = await listSiteVersions(site.id)
      await loadSubjects()
    } catch (error_) {
      error.value = messageOf(error_)
    }
  }
  async function loadSubjects () {
    try {
      subjects.value = await searchSiteSubjects(subjectQuery.value)
    } catch {
      subjects.value = { users: [], depts: [] }
    }
  }
  function addSubject (id: string) {
    if (!editing.value) return
    const items = editing.value.visibility === 'users' ? editing.value.allowed_users : editing.value.allowed_depts
    if (!items.includes(id)) items.push(id)
  }
  function removeSubject (id: string) {
    if (!editing.value) return
    const items = editing.value.visibility === 'users' ? editing.value.allowed_users : editing.value.allowed_depts
    const index = items.indexOf(id)
    if (index !== -1) items.splice(index, 1)
  }
  async function save () {
    if (!editing.value) return
    await run(async () => {
      editing.value = await saveSite(editing.value!)
      await load()
      notice.value = '访问范围已保存'
    })
  }
  async function uploadVersion () {
    if (!editing.value || !versionFile.value) return
    await run(async () => {
      const siteId = editing.value!.id
      const result = await addSiteVersion(siteId, versionFile.value!)
      versionFile.value = null
      versions.value = await listSiteVersions(siteId)
      await load()
      editing.value = sites.value.find(site => site.id === siteId) ?? editing.value
      showPreview(result.preview_url)
    })
  }
  async function publish () {
    if (!editing.value || publishTarget.value === null) return
    await run(async () => {
      const saved = await saveSite(editing.value!)
      editing.value = await publishSite(saved.id, publishTarget.value!)
      publishTarget.value = null
      await load()
      notice.value = '网站已发布，固定链接现已生效'
    })
  }
  async function withdraw () {
    if (!editing.value) return
    await run(async () => {
      editing.value = await withdrawSite(editing.value!.id)
      await load()
      notice.value = '网站已撤回，所有资源链接已失效'
    })
  }
  function showPreview (url: string) {
    previewUrl.value = url
    previewOpen.value = true
  }
  async function copyLink (site: Site) {
    await navigator.clipboard.writeText(new URL(site.url, window.location.origin).href)
    notice.value = '固定链接已复制'
  }
  async function openDemo (siteId: string) {
    editorOpen.value = false
    await router.replace({ query: { ...route.query, demo: siteId } })
  }
  async function closeDemo () {
    const { demo: _demo, ...query } = route.query
    await router.replace({ query })
  }
  function onDemoDialogChange (open: boolean) {
    if (!open) void closeDemo()
  }
  async function copyDemoLink (siteId: string) {
    const url = new URL(router.resolve({ name: 'sites', query: { demo: siteId } }).href, window.location.origin)
    await navigator.clipboard.writeText(url.href)
    notice.value = '角色权限示例链接已复制'
  }
  watch(() => route.query.demo, value => {
    demoId.value = typeof value === 'string' ? value : ''
    demoOpen.value = Boolean(demoId.value)
  }, { immediate: true })
  onMounted(() => {
    void load()
  })
</script>

<style scoped>
.preview-frame { width: 100%; height: calc(100vh - 48px); border: 0; background: white; }
</style>
