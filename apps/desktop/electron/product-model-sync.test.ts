import { describe, expect, it } from 'vitest'

import {
  decideModelSelection,
  firstGrantedModel,
  grantedModelsFromCatalog,
  resolveModelForSend,
} from './product-model-sync'

const GRANTED = ['deepseek-v4-pro', 'doubao-seed-2-0-lite-260428', 'deepseek-v4-flash']

describe('firstGrantedModel', () => {
  it('按字母序取第一个，而不是网关返回的顺序', () => {
    // 取原始顺序的话这里会拿到 deepseek-v4-pro。上游清单顺序没有稳定性保证，
    // 顺序一变默认模型就变，用户会觉得"它自己改了"
    expect(firstGrantedModel(GRANTED)).toBe('deepseek-v4-flash')
  })

  it('忽略空白项', () => {
    expect(firstGrantedModel(['  ', '', 'glm-5'])).toBe('glm-5')
  })

  it('清单为空时返回 null', () => {
    expect(firstGrantedModel([])).toBeNull()
  })
})

describe('decideModelSelection', () => {
  it('新装档：自动选字母序第一个', () => {
    expect(decideModelSelection(GRANTED, null)).toEqual({
      model: 'deepseek-v4-flash',
      reason: 'selected',
    })
    expect(decideModelSelection(GRANTED, '   ')).toEqual({
      model: 'deepseek-v4-flash',
      reason: 'selected',
    })
  })

  it('已选且仍有授权：不动', () => {
    // 用户自己挑过 pro，就不该在每次登录时被悄悄改回 flash
    expect(decideModelSelection(GRANTED, 'deepseek-v4-pro')).toEqual({
      model: null,
      reason: 'kept',
    })
  })

  it('已选的模型被收回授权：退回第一个可用的', () => {
    // 不处理的话客户端会一直记着失效模型，用户每次发消息都撞 model_not_allowed，
    // 而报错里完全看不出"是你原来那个模型被停了"
    expect(decideModelSelection(GRANTED, 'gpt-5.6-luna')).toEqual({
      model: 'deepseek-v4-flash',
      reason: 'regranted',
    })
  })

  it('一个模型都没开通：不选，并让调用方拦住', () => {
    expect(decideModelSelection([], null)).toEqual({ model: null, reason: 'no-grants' })
    // 即使本地还记着一个模型，没有授权也一样是 no-grants ——
    // 放他进聊天界面只会得到一句 model_not_allowed
    expect(decideModelSelection([], 'deepseek-v4-flash')).toEqual({
      model: null,
      reason: 'no-grants',
    })
  })
})

describe('grantedModelsFromCatalog', () => {
  it('解析 /v1/models 的 OpenAI 格式响应', () => {
    expect(
      grantedModelsFromCatalog({ object: 'list', data: [{ id: 'a' }, { id: 'b' }] }),
    ).toEqual(['a', 'b'])
  })

  it('空清单是合法响应，不是错误', () => {
    // 网关对无授权账号返回 {"object":"list","data":[]}，登录仍应成功；
    // 把它当成解析失败会让"没开通模型"表现成"网关坏了"
    expect(grantedModelsFromCatalog({ object: 'list', data: [] })).toEqual([])
  })

  it('形状不对时退回空清单而不是抛异常', () => {
    expect(grantedModelsFromCatalog(null)).toEqual([])
    expect(grantedModelsFromCatalog({ data: 'nope' })).toEqual([])
  })
})

describe('resolveModelForSend', () => {
  const never = async () => {
    throw new Error('不该被调用')
  }

  it('已有模型且仍在授权清单里：放行，不改配置', async () => {
    await expect(
      resolveModelForSend({
        currentModel: 'deepseek-v4-flash',
        fetchGranted: async () => GRANTED,
        applyModel: never as never,
      }),
    ).resolves.toEqual({ status: 'ready', model: 'deepseek-v4-flash' })
  })

  it('已登录后默认模型被管理员下架：当场换成字母序第一个，并带回被换掉的名字', async () => {
    // 2026-09-18 的真实现场：配置里的默认值是 9 月 2 日登录时写入的
    // deepseek-v4-flash，网关已只剩 v4.1 与豆包。原先这里「有模型就放行」，
    // 新对话全部 403 model_not_allowed，只有退出重登才会换
    const applied: string[] = []

    await expect(
      resolveModelForSend({
        currentModel: 'deepseek-v4-flash',
        fetchGranted: async () => ['doubao-seed-2-0-lite-260428', 'deepseek-v4.1-flash'],
        applyModel: async model => {
          applied.push(model)

          return true
        },
      }),
    ).resolves.toEqual({ status: 'ready', model: 'deepseek-v4.1-flash', replacedFrom: 'deepseek-v4-flash' })
    expect(applied).toEqual(['deepseek-v4.1-flash'])
  })

  it('已有模型、清单问不到：照常放行，不因为网络抖动把人换走', async () => {
    await expect(
      resolveModelForSend({
        currentModel: 'deepseek-v4-flash',
        fetchGranted: async () => null,
        applyModel: never as never,
      }),
    ).resolves.toEqual({ status: 'ready', model: 'deepseek-v4-flash' })
  })

  it('已有模型、但账号已一个模型都没有：报 no-grants', async () => {
    await expect(
      resolveModelForSend({
        currentModel: 'deepseek-v4-flash',
        fetchGranted: async () => [],
        applyModel: never as never,
      }),
    ).resolves.toEqual({ status: 'no-grants' })
  })

  it('默认模型被下架但写配置失败：不假装换好了，照旧放行原模型', async () => {
    await expect(
      resolveModelForSend({
        currentModel: 'deepseek-v4-flash',
        fetchGranted: async () => ['deepseek-v4.1-flash'],
        applyModel: async () => false,
      }),
    ).resolves.toEqual({ status: 'ready', model: 'deepseek-v4-flash' })
  })

  it('登录时没模型、此刻管理员已开通：当场选上并放行', async () => {
    // 这条是整个设计的意义所在：用户不需要知道「要重新登录才生效」
    const applied: string[] = []

    await expect(
      resolveModelForSend({
        currentModel: null,
        fetchGranted: async () => GRANTED,
        applyModel: async model => {
          applied.push(model)

          return true
        },
      }),
    ).resolves.toEqual({ status: 'ready', model: 'deepseek-v4-flash' })
    expect(applied).toEqual(['deepseek-v4-flash'])
  })

  it('重查后仍然一个都没有：报 no-grants，让界面提示找管理员', async () => {
    await expect(
      resolveModelForSend({
        currentModel: '',
        fetchGranted: async () => [],
        applyModel: never as never,
      }),
    ).resolves.toEqual({ status: 'no-grants' })
  })

  it('重查本身失败：报 unavailable，绝不说成「没开通」', async () => {
    // 把网关连不上说成「联系管理员开通」，他找过去管理员一看权限明明有，
    // 两边都白跑 —— 这条边界比 no-grants 本身更容易被写错
    await expect(
      resolveModelForSend({
        currentModel: null,
        fetchGranted: async () => null,
        applyModel: never as never,
      }),
    ).resolves.toEqual({ status: 'unavailable' })
  })

  it('选上了但写配置失败：算 unavailable 而不是 ready', async () => {
    // 报 ready 的话消息照样发出去，再撞一次 400——等于把一个明确的失败
    // 变成一个含糊的失败
    await expect(
      resolveModelForSend({
        currentModel: null,
        fetchGranted: async () => GRANTED,
        applyModel: async () => false,
      }),
    ).resolves.toEqual({ status: 'unavailable' })
  })
})
