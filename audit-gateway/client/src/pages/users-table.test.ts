import { describe, expect, it } from 'vitest'
import source from './users.vue?raw'

describe('用户列表', () => {
  it('使用服务端分页而不是截断为前 200 个账号', () => {
    expect(source).toContain('<v-data-table-server')
    expect(source).toContain(':items-length="totalAccounts"')
    expect(source).toContain('offset: (page.value - 1) * pageSize.value')
    expect(source).not.toContain('limit: 200')
  })

  it('同时展示最后登录和来自真实请求的最近活跃时间', () => {
    expect(source).toContain('key: \'last_login_at\'')
    expect(source).toContain('key: \'last_request_at\'')
    expect(source).toContain('尚未使用')
  })

  it('快速翻页或筛选时只接受最新一次请求结果', () => {
    expect(source).toContain('const sequence = ++loadSequence')
    expect(source).toContain('sequence === loadSequence')
  })

  it('把服务端表格的排序字段与方向传给分页接口', () => {
    expect(source).toContain('@update:options="onTableOptions"')
    expect(source).toContain('sort_by: sortBy.value[0]?.key')
    expect(source).toContain('sort_order: sortBy.value[0]?.order')
  })

  it('停用和重新启用前明确告知 API 密钥不会恢复', () => {
    expect(source).toContain('API 密钥都会被永久吊销')
    expect(source).toContain('重新启用也不会自动恢复')
    expect(source).toContain('已有客户端需重新签发密钥')
  })

  it('明确告知角色或部门调整会立即撤销后台会话', () => {
    expect(source).toContain('现有后台登录会话将立即失效')
    expect(source).toContain('现有登录会话已失效，需重新登录')
    expect(source).not.toContain('登录态有缓存')
  })
})
