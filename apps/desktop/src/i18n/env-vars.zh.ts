import type { Locale } from './types'

export interface EnvVarText {
  description: string
  prompt?: string
}

/** 环境变量的中文说明。键是 env var 名，必须和 OPTIONAL_ENV_VARS 里的完全一致。 */
export const ENV_VAR_ZH: Record<string, EnvVarText> = {
  EXA_API_KEY: {
    description: 'Exa 搜索密钥，用于让智能体检索网页并读取搜索结果正文',
    prompt: 'Exa 搜索密钥'
  },
  PARALLEL_API_KEY: {
    description: 'Parallel 搜索密钥，用于让智能体检索网页并提取正文',
    prompt: 'Parallel 搜索密钥'
  },
  FIRECRAWL_API_KEY: {
    description: 'Firecrawl 网页抓取密钥，用于让智能体搜索网页并读取正文',
    prompt: 'Firecrawl 抓取密钥'
  },
  FIRECRAWL_API_URL: {
    description: '自建 Firecrawl 服务的接口地址；使用云端服务时留空',
    prompt: 'Firecrawl 服务地址'
  },
  FIRECRAWL_GATEWAY_URL: {
    description: 'Firecrawl 工具网关的完整地址；通常无需填写',
    prompt: 'Firecrawl 网关地址'
  },
  TOOL_GATEWAY_DOMAIN: {
    description: '托管工具网关共用的域名后缀，用于自动生成各工具的服务地址',
    prompt: '工具网关域名'
  },
  TOOL_GATEWAY_SCHEME: {
    description: '托管工具网关使用的连接协议；默认使用 HTTPS，本地测试可改为 HTTP',
    prompt: '工具网关协议'
  },
  TOOL_GATEWAY_USER_TOKEN: {
    description: '工具网关访问令牌；通常由工作台登录状态自动提供，仅在单独部署时填写',
    prompt: '工具网关令牌'
  },
  TAVILY_API_KEY: {
    description: 'Tavily 搜索密钥，用于让智能体搜索网页并提取内容',
    prompt: 'Tavily 搜索密钥'
  },
  SEARXNG_URL: {
    description: '自建 SearXNG 搜索服务的地址，用于免费网页搜索',
    prompt: 'SearXNG 服务地址'
  },
  BRAVE_SEARCH_API_KEY: {
    description: 'Brave Search 搜索密钥，用于让智能体查询网页',
    prompt: 'Brave 搜索密钥'
  },
  BROWSERBASE_API_KEY: {
    description: 'Browserbase 云浏览器密钥；只使用本地浏览器时无需填写',
    prompt: 'Browserbase 密钥'
  },
  BROWSERBASE_PROJECT_ID: {
    description: 'Browserbase 云浏览器项目编号；只使用本地浏览器时无需填写',
    prompt: 'Browserbase 项目'
  },
  BROWSER_USE_API_KEY: {
    description: 'Browser Use 云浏览器密钥；只使用本地浏览器时无需填写',
    prompt: 'Browser Use 密钥'
  },
  FIRECRAWL_BROWSER_TTL: {
    description: 'Firecrawl 浏览器会话的保留秒数；留空时使用 300 秒',
    prompt: '浏览器保留秒数'
  },
  AGENT_BROWSER_ENGINE: {
    description: '本地浏览器引擎：自动模式默认使用 Chrome；Lightpanda 更快但不支持截图',
    prompt: '本地浏览器引擎'
  },
  CAMOFOX_URL: {
    description: 'Camofox 防检测浏览器服务地址，用于连接本地或远程浏览器',
    prompt: 'Camofox 服务地址'
  },
  CAMOFOX_API_KEY: {
    description: 'Camofox 服务访问密钥；远程服务启用鉴权时填写',
    prompt: 'Camofox 访问密钥'
  },
  FAL_KEY: {
    description: 'FAL 视频生成服务密钥，用于生成视频',
    prompt: 'FAL 生成密钥'
  },
  KREA_API_KEY: {
    description: 'Krea 生成服务密钥，用于生成中型和大型图片',
    prompt: 'Krea 生成密钥'
  },
  VOICE_TOOLS_OPENAI_KEY: {
    description: 'OpenAI 语音服务密钥，用于 Whisper 语音转文字和语音合成',
    prompt: 'OpenAI 语音密钥'
  },
  ELEVENLABS_API_KEY: {
    description: 'ElevenLabs 语音服务密钥，用于高品质语音合成和 Scribe 语音转文字',
    prompt: 'ElevenLabs 语音密钥'
  },
  MISTRAL_API_KEY: {
    description: 'Mistral 语音服务密钥，用于 Voxtral 语音合成和语音转文字',
    prompt: 'Mistral 语音密钥'
  },
  PORCUPINE_ACCESS_KEY: {
    description: 'Picovoice 访问密钥，用于启用 Porcupine 语音唤醒；默认可使用免费的 openWakeWord',
    prompt: '语音唤醒密钥'
  },
  GITHUB_TOKEN: {
    description: 'GitHub 访问令牌，用于提高技能库请求额度和发布技能',
    prompt: 'GitHub 访问令牌'
  },
  HONCHO_API_KEY: {
    description: 'Honcho 记忆服务密钥，用于保存和检索长期记忆',
    prompt: 'Honcho 记忆密钥'
  },
  HONCHO_BASE_URL: {
    description: '自建 Honcho 记忆服务的地址；自建服务无需密钥',
    prompt: 'Honcho 服务地址'
  },
  HINDSIGHT_API_KEY: {
    description: 'Hindsight 记忆服务密钥，用于保存和检索关联式长期记忆',
    prompt: 'Hindsight 记忆密钥'
  },
  HINDSIGHT_API_URL: {
    description: 'Hindsight 记忆服务的接口地址；留空时使用默认云端服务',
    prompt: 'Hindsight 服务地址'
  },
  SUPERMEMORY_API_KEY: {
    description: 'Supermemory 记忆服务密钥，用于保存当前对话范围内的长期记忆',
    prompt: 'Supermemory 记忆密钥'
  },
  MEM0_API_KEY: {
    description: 'Mem0 记忆服务密钥，用于保存和检索语义长期记忆',
    prompt: 'Mem0 记忆密钥'
  },
  RETAINDB_API_KEY: {
    description: 'RetainDB 记忆服务密钥，用于保存和检索长期记忆',
    prompt: 'RetainDB 记忆密钥'
  },
  RETAINDB_BASE_URL: {
    description: '自建 RetainDB 记忆服务的地址；留空时使用默认云端服务',
    prompt: 'RetainDB 服务地址'
  },
  BRV_API_KEY: {
    description: 'ByteRover 云同步密钥；默认以本地数据为主，不同步时无需填写',
    prompt: 'ByteRover 同步密钥'
  },
  OPENVIKING_API_KEY: {
    description: 'OpenViking 服务密钥；本地开发模式可留空',
    prompt: 'OpenViking 服务密钥'
  },
  OPENVIKING_ENDPOINT: {
    description: 'OpenViking 服务地址；留空时连接本机默认端口',
    prompt: 'OpenViking 服务地址'
  },
  HERMES_LANGFUSE_PUBLIC_KEY: {
    description: 'Langfuse 项目公钥，用于记录模型调用的可观测数据',
    prompt: 'Langfuse 项目公钥'
  },
  HERMES_LANGFUSE_SECRET_KEY: {
    description: 'Langfuse 项目密钥，与项目公钥配合上传可观测数据',
    prompt: 'Langfuse 项目密钥'
  },
  HERMES_LANGFUSE_BASE_URL: {
    description: 'Langfuse 服务地址；自建服务时填写，使用云端服务时留空',
    prompt: 'Langfuse 服务地址'
  },
  TELEGRAM_BOT_TOKEN: {
    description: 'Telegram 机器人令牌，由 BotFather 创建，包含机器人编号和密钥',
    prompt: 'Telegram 机器人令牌'
  },
  TELEGRAM_ALLOWED_USERS: {
    description: '允许直接使用机器人的 Telegram 用户编号，多个编号用英文逗号分隔；留空时通过私聊配对审批',
    prompt: 'Telegram 用户编号'
  },
  TELEGRAM_PROXY: {
    description: 'Telegram 连接使用的代理地址，可使用 HTTP、HTTPS 或 SOCKS5 协议',
    prompt: 'Telegram 代理地址'
  },
  DISCORD_BOT_TOKEN: {
    description: 'Discord 机器人令牌，用于连接并接收消息',
    prompt: 'Discord 机器人令牌'
  },
  DISCORD_ALLOWED_USERS: {
    description: '允许使用机器人的 Discord 用户编号，多个编号用英文逗号分隔',
    prompt: 'Discord 用户编号'
  },
  DISCORD_REPLY_TO_MODE: {
    description: 'Discord 引用回复方式：关闭引用、仅首条引用或每条消息都引用',
    prompt: 'Discord 回复方式'
  },
  SLACK_BOT_TOKEN: {
    description: 'Slack 机器人令牌，用于发送消息、读取会话和处理文件',
    prompt: 'Slack 机器人令牌'
  },
  SLACK_APP_TOKEN: {
    description: 'Slack 应用级令牌，用于通过 Socket Mode 保持消息连接',
    prompt: 'Slack 应用令牌'
  },
  SLACK_ALLOWED_USERS: {
    description: '允许使用机器人的 Slack 成员编号，多个编号用英文逗号分隔；未填写时消息可能被拒绝',
    prompt: 'Slack 成员编号'
  },
  MATTERMOST_URL: {
    description: 'Mattermost 服务器地址，用于连接所在团队的消息服务',
    prompt: 'Mattermost 地址'
  },
  MATTERMOST_TOKEN: {
    description: 'Mattermost 机器人令牌或个人访问令牌，用于登录消息服务',
    prompt: 'Mattermost 令牌'
  },
  MATTERMOST_ALLOWED_USERS: {
    description: '允许使用机器人的 Mattermost 用户编号，多个编号用英文逗号分隔',
    prompt: 'Mattermost 用户编号'
  },
  MATTERMOST_REQUIRE_MENTION: {
    description: '是否仅在 Mattermost 频道中被点名时回复；默认开启',
    prompt: 'Mattermost 点名回复'
  },
  MATTERMOST_FREE_RESPONSE_CHANNELS: {
    description: '无需点名即可回复的 Mattermost 频道编号，多个编号用英文逗号分隔',
    prompt: 'Mattermost 直回频道'
  },
  MATRIX_HOMESERVER: {
    description: 'Matrix 主服务器地址，用于连接所在组织的 Matrix 网络',
    prompt: 'Matrix 服务器地址'
  },
  MATRIX_ACCESS_TOKEN: {
    description: 'Matrix 访问令牌，用于登录消息服务，优先于密码登录',
    prompt: 'Matrix 访问令牌'
  },
  MATRIX_USER_ID: {
    description: 'Matrix 用户编号，格式为 @用户名:服务器',
    prompt: 'Matrix 用户编号'
  },
  MATRIX_ALLOWED_USERS: {
    description: '允许使用机器人的 Matrix 用户编号，多个编号用英文逗号分隔',
    prompt: 'Matrix 允许用户'
  },
  MATRIX_REQUIRE_MENTION: {
    description: '是否仅在 Matrix 房间中被点名时回复；默认开启',
    prompt: 'Matrix 点名回复'
  },
  MATRIX_FREE_RESPONSE_ROOMS: {
    description: '无需点名即可回复的 Matrix 房间编号，多个编号用英文逗号分隔',
    prompt: 'Matrix 直回房间'
  },
  MATRIX_AUTO_THREAD: {
    description: '是否为 Matrix 房间中的消息自动创建话题；默认开启',
    prompt: 'Matrix 自动话题'
  },
  MATRIX_DM_AUTO_THREAD: {
    description: '是否为 Matrix 私聊消息自动创建话题；默认关闭',
    prompt: 'Matrix 私聊话题'
  },
  MATRIX_DEVICE_ID: {
    description: 'Matrix 设备编号，用于重启后继续使用同一套端到端加密身份',
    prompt: 'Matrix 设备编号'
  },
  MATRIX_RECOVERY_KEY: {
    description: 'Matrix 恢复密钥，用于设备密钥轮换后重新完成交叉签名验证',
    prompt: 'Matrix 恢复密钥'
  },
  BLUEBUBBLES_SERVER_URL: {
    description: 'BlueBubbles 服务器地址，用于连接 iMessage 消息服务',
    prompt: 'BlueBubbles 地址'
  },
  BLUEBUBBLES_PASSWORD: {
    description: 'BlueBubbles 服务器密码，用于登录消息服务',
    prompt: 'BlueBubbles 密码'
  },
  BLUEBUBBLES_ALLOWED_USERS: {
    description: '允许使用机器人的 iMessage 邮箱或手机号，多个地址用英文逗号分隔',
    prompt: 'iMessage 允许用户'
  },
  BLUEBUBBLES_ALLOW_ALL_USERS: {
    description: '是否允许所有 BlueBubbles 用户使用机器人而不检查白名单',
    prompt: '允许全部用户'
  },
  QQ_APP_ID: {
    description: 'QQ 机器人的应用编号，用于连接 QQ 开放平台',
    prompt: 'QQ 应用编号'
  },
  QQ_CLIENT_SECRET: {
    description: 'QQ 机器人的客户端密钥，与应用编号配合完成鉴权',
    prompt: 'QQ 客户端密钥'
  },
  QQ_ALLOWED_USERS: {
    description: '允许使用机器人的 QQ 用户编号，多个编号用英文逗号分隔',
    prompt: 'QQ 允许用户'
  },
  QQ_GROUP_ALLOWED_USERS: {
    description: '允许与机器人互动的 QQ 群编号，多个编号用英文逗号分隔',
    prompt: 'QQ 允许群组'
  },
  QQ_ALLOW_ALL_USERS: {
    description: '是否允许所有 QQ 用户使用机器人而不检查白名单',
    prompt: 'QQ 允许全部用户'
  },
  QQBOT_HOME_CHANNEL: {
    description: '定时任务和通知默认发送到的 QQ 频道或群组编号',
    prompt: 'QQ 默认频道'
  },
  QQBOT_HOME_CHANNEL_NAME: {
    description: 'QQ 默认频道或群组在界面中显示的名称',
    prompt: 'QQ 频道名称'
  },
  QQ_SANDBOX: {
    description: '是否启用 QQ 沙盒模式，用于开发和联调测试',
    prompt: 'QQ 沙盒模式'
  },
  IRC_SERVER: {
    description: 'IRC 服务器主机名，用于连接所在组织的 IRC 网络',
    prompt: 'IRC 服务器'
  },
  IRC_CHANNEL: {
    description: '机器人加入的 IRC 频道名称',
    prompt: 'IRC 频道'
  },
  IRC_NICKNAME: {
    description: '机器人在 IRC 网络中显示的昵称',
    prompt: 'IRC 机器人昵称'
  },
  IRC_SERVER_PASSWORD: {
    description: 'IRC 服务器要求鉴权时使用的连接密码',
    prompt: 'IRC 服务器密码'
  },
  IRC_NICKSERV_PASSWORD: {
    description: 'NickServ 身份服务密码，用于确认 IRC 昵称归属',
    prompt: 'NickServ 密码'
  },
  GATEWAY_ALLOW_ALL_USERS: {
    description: '是否允许所有用户与消息机器人互动；默认关闭',
    prompt: '网关允许全部用户'
  },
  API_SERVER_ENABLED: {
    description: '是否启用兼容 OpenAI 协议的接口服务，供其他客户端连接',
    prompt: '启用接口服务'
  },
  API_SERVER_KEY: {
    description: '接口服务的鉴权令牌；启用服务时必须填写，否则服务不会启动',
    prompt: '接口鉴权令牌'
  },
  API_SERVER_PORT: {
    description: '接口服务监听的端口号；留空时使用 8642',
    prompt: '接口服务端口'
  },
  API_SERVER_HOST: {
    description: '接口服务监听的主机或网卡地址；即使仅监听本机也需要鉴权令牌',
    prompt: '接口监听地址'
  },
  API_SERVER_MODEL_NAME: {
    description: '接口服务对外展示的模型名称，便于其他客户端识别当前配置',
    prompt: '对外模型名称'
  },
  GATEWAY_PROXY_URL: {
    description: '远程接口服务地址；代理模式只处理消息收发，智能体任务交给远程服务执行',
    prompt: '远程接口地址'
  },
  GATEWAY_PROXY_KEY: {
    description: '连接远程接口服务使用的鉴权令牌，必须与远程主机配置一致',
    prompt: '远程鉴权令牌'
  },
  WEBHOOK_ENABLED: {
    description: '是否启用 Webhook 消息入口，用于接收 GitHub、GitLab 等服务发送的事件',
    prompt: '启用 Webhook'
  },
  WEBHOOK_PORT: {
    description: 'Webhook 服务监听的端口号；留空时使用 8644',
    prompt: 'Webhook 端口'
  },
  WEBHOOK_SECRET: {
    description: 'Webhook 请求签名校验使用的全局 HMAC 密钥；单条路由可单独覆盖',
    prompt: 'Webhook 签名密钥'
  },
  SUDO_PASSWORD: {
    description: '终端命令需要管理员权限时使用的提权密码；密码为空时也可明确保存空值，避免再次询问',
    prompt: '终端提权密码'
  },
  HERMES_PREFILL_MESSAGES_FILE: {
    description: '临时预填消息的 JSON 文件路径，用于在每次模型调用前加入少量示例；不会写入会话记录',
    prompt: '预填消息文件'
  },
  HERMES_EPHEMERAL_SYSTEM_PROMPT: {
    description: '每次模型调用时临时加入的系统提示词；不会写入会话记录',
    prompt: '临时系统提示词'
  }
}

/** 中文仅覆盖已翻译字段；其他语言、未知键和空覆盖保留后端原文。 */
export function localizeEnvVar(locale: Locale, key: string, fallback: EnvVarText): EnvVarText {
  const override = locale === 'zh' ? ENV_VAR_ZH[key] : undefined
  const description = override?.description.trim() || fallback.description
  const prompt = override?.prompt?.trim() || fallback.prompt

  return prompt ? { description, prompt } : { description }
}
