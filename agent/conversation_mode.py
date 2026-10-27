"""Session-owned conversation policy; no process-wide switches or new model tools."""
from __future__ import annotations
import json

CHAT_TOOLS = frozenset({'web_search', 'web_extract'})
CHAT_COMMANDS = frozenset({'help', 'status'})
CHAT_GUIDANCE = ('Conversation mode: chat. Answer straightforward questions directly and promptly. '
    'Use web search/extraction only when the user requests it or current facts require it. '
    'Uploaded material may be understood through the existing attachment input. '
    'In chat mode you cannot access the user\'s computer, local files, running processes, or applications. '
    'Never claim that you have checked, are checking, or will later check the computer; '
    'never report local execution, scans, progress, or results without an actual permitted tool result. '
    'Do not ask the user to wait for a local operation or promise to post its results later. '
    'Code and commands may be explained when requested, but clearly label them as examples '
    'for the user to run manually; displaying code does not execute it. '
    'Do not plan or execute terminal, file changes, browser operations, skills, delegation, or deployment. '
    'If actions are needed, explain briefly and ask the user to start a related work conversation.')
BLOCK_MESSAGE = '聊天模式不执行此操作；请新建关联的工作会话。'


def normalize_mode(value=None):
    if value is None:
        return 'work'
    if not isinstance(value, str) or value not in {'chat', 'work'}:
        raise ValueError('会话模式必须为 chat 或 work')
    return value


def row_config(row):
    raw = (row or {}).get('model_config')
    if isinstance(raw, dict): return raw
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def row_mode(row):
    if 'mode' in (row or {}): return normalize_mode(row['mode'])
    return normalize_mode(row_config(row).get('conversation_mode'))


def session_mode(session):
    session = session or {}
    return normalize_mode(session.get('mode', getattr(session.get('agent'), 'conversation_mode', 'work')))


def tool_allowed(agent, name):
    if normalize_mode(getattr(agent, 'conversation_mode', 'work')) != 'chat':
        return True
    return name in getattr(agent, '_conversation_mode_allowed_tools', CHAT_TOOLS) and name in getattr(agent, 'valid_tool_names', CHAT_TOOLS)


def blocked_result():
    return json.dumps({'error': BLOCK_MESSAGE, 'reason': 'conversation_mode_restricted', 'mode': 'chat'}, ensure_ascii=False)


def preserve_mode(existing, incoming):
    """Partial settings writes preserve mode; explicitly changing it is rejected."""
    old = normalize_mode(existing.get('conversation_mode'))
    if 'conversation_mode' in incoming and normalize_mode(incoming['conversation_mode']) != old:
        raise ValueError('会话模式已冻结；切换模式请新建关联会话')
    result = dict(incoming)
    if 'conversation_mode' in existing:
        result['conversation_mode'] = old
    return result



def validate_chat_transport(mode, provider=None, api_mode=None, base_url=None, command=None):
    if normalize_mode(mode) != 'chat': return
    if api_mode == 'codex_app_server' or provider == 'copilot-acp' or str(base_url or '').startswith(('acp://', 'acp+tcp://')) or command:
        raise ValueError('此代理式模型接口不能保证聊天工具限制，请使用工作模式或标准聊天模型接口')


def configured_chat_tools(enabled_toolsets):
    if enabled_toolsets is None:
        return CHAT_TOOLS
    from toolsets import resolve_toolset
    allowed = set()
    for name in enabled_toolsets:
        try:
            allowed.update(resolve_toolset(name))
        except (KeyError, ValueError):
            continue
    return frozenset(allowed & CHAT_TOOLS)
