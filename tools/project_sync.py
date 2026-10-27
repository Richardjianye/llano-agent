"""项目群聊的恢复协议 —— 阶段 27-B

桌面端拿群聊消息的路是 `after_seq` 增量拉取（阶段 27 实施决议 1：P0 先不做 WS，
网关目前零 WebSocket，新增它要连带做鉴权与多 worker 扇出）。这个模块是那条路上
**唯一**的状态：水位、去重、退避、以及「这份响应还算不算数」。

写成独立的纯逻辑模块，是因为这四件事错了都不会报错，只会表现成「群里少了一条
消息」「同一条出现两次」「切了项目还在刷上一个项目的内容」——都要等人发现。

## 四条规则

1. **水位只前进。** 旧响应晚到不得把 `after_seq` 拉回去，否则下一轮会把已经
   显示过的消息再拉一遍。
2. **去重集合有界。** 不设上限的话它随会话时长单调增长，长期挂着的客户端最后
   会把内存吃掉 —— 而这类泄漏在测试里永远看不见。
3. **补拉与实时可重叠，靠 id 去重收尾。** 用 `project_id:seq`，不是全局自增 id：
   项目 A 的游标不该因为项目 B 的写入而跳号。
4. **身份变了就整体清场。** 账号退出、切换、网关地址变更之后，上一个账号的水位
   和缓存都必须消失 —— 否则新账号会从旧游标开始拉，看起来像「消息丢了」。

## 失权与归档是**终止条件**，不是错误

项目被归档、agent 被移除、账号停用时网关回 404/403。那不是网络抖动，重试没有
意义：这里把它记成 `revoked`，调用方据此停止轮询。不区分的话就会留下一个后台
死循环，对着一个自己已经没资格看的项目每秒敲一次网关。
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any, Dict, Iterable, List, Optional, Tuple

# 去重集合上限。512 条足够覆盖一次补拉窗口与实时流的重叠区间，
# 而重叠区间本来就只有几十条 —— 上限存在的意义是**有上限**，不是这个具体数字。
MAX_DEDUPE_KEYS = 512

# 轮询退避。空闲时慢下来，出错时更慢，但都有上限：上限之外的等待对用户来说
# 与「坏了」没有区别。
POLL_MIN_SECONDS = 2.0
POLL_IDLE_SECONDS = 8.0
POLL_ERROR_MAX_SECONDS = 60.0


def next_delay(*, idle_rounds: int = 0, error_rounds: int = 0) -> float:
    """下一次拉取等多久。

    出错优先：连不上网关时慢退避，而不是继续按空闲节奏敲。
    """
    if error_rounds > 0:
        delay = POLL_MIN_SECONDS * (2 ** min(int(error_rounds), 8))
        return min(POLL_ERROR_MAX_SECONDS, delay)
    if idle_rounds <= 0:
        return POLL_MIN_SECONDS
    # 空闲：线性爬到 POLL_IDLE_SECONDS 就不再增加。群聊不是日志，
    # 停在 8 秒是「有人说话时不会觉得卡」与「没人说话时别刷网关」的折中。
    return min(POLL_IDLE_SECONDS, POLL_MIN_SECONDS + float(idle_rounds))


def message_key(project_id: Any, seq: Any) -> str:
    """去重键。与网关侧 services/project_runtime.event_id_of 同一套形状。"""
    return f"{project_id}:{seq}"


class ProjectCursor:
    """一个项目的读游标。

    `seq` 是水位，`_seen` 是最近见过的消息键。两者都需要：水位决定下次从哪拉，
    去重决定这一批里哪几条是新的 —— 补拉窗口与实时流允许重叠，重叠部分正是靠
    去重收尾的。
    """

    __slots__ = ("code", "seq", "_seen")

    def __init__(self, code: str, *, seq: int = 0):
        self.code = code
        self.seq = max(0, int(seq or 0))
        # OrderedDict 当有界 LRU 用：popitem(last=False) 丢最旧的。
        self._seen: "OrderedDict[str, None]" = OrderedDict()

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"ProjectCursor({self.code!r}, seq={self.seq}, seen={len(self._seen)})"

    @property
    def seen_count(self) -> int:
        return len(self._seen)

    def _remember(self, key: str) -> None:
        self._seen[key] = None
        self._seen.move_to_end(key)
        while len(self._seen) > MAX_DEDUPE_KEYS:
            self._seen.popitem(last=False)

    def admit(self, messages: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """筛出这一批里**没见过**的消息，并推进水位。

        缺 seq 的行直接丢：没有 seq 就没有水位，留着它只会让去重靠内容猜。
        """
        fresh: List[Dict[str, Any]] = []
        for message in messages or []:
            seq = message.get("seq")
            if seq is None:
                continue
            try:
                seq = int(seq)
            except (TypeError, ValueError):
                continue

            key = message_key(message.get("project_id", self.code), seq)
            if key in self._seen:
                # 重叠区间里的老消息。见过就不再往上抛，但水位照常前进。
                self.seq = max(self.seq, seq)
                continue
            self._remember(key)
            fresh.append(message)
            # 水位只前进：乱序到达的旧消息不会把它拉回去。
            self.seq = max(self.seq, seq)
        return fresh


class ProjectSync:
    """一个账号下所有项目的游标集合。

    「一个账号」是关键：`identity` 变了（退出、切号、换网关地址）就整体清场。
    留着上一个账号的水位，新账号会从一个凭空的 seq 开始拉，界面上表现为
    「刚登录进来，历史消息少了一截」——而没有任何一处会报错。
    """

    def __init__(self) -> None:
        self._identity: Optional[str] = None
        self._cursors: Dict[str, ProjectCursor] = {}
        self._revoked: Dict[str, str] = {}
        # 每次清场 +1。发出去的请求带上它，回来时对不上就丢弃 ——
        # 这就是「旧项目请求晚到不会覆盖用户刚切过去的新项目」那一条。
        self._epoch = 0

    @property
    def epoch(self) -> int:
        return self._epoch

    @property
    def identity(self) -> Optional[str]:
        return self._identity

    def note_identity(self, identity: Optional[str]) -> bool:
        """记录当前身份。变了就清场，返回是否清过。

        由调用方在**每次**请求时调用，而不是等界面记得调退出接口：客户端忘了调
        的那一次，正是账号已经换了的那一次。
        """
        key = (identity or "").strip() or None
        if key == self._identity:
            return False
        self.reset(reason="identity-changed")
        self._identity = key
        return True

    def reset(self, *, reason: str = "") -> None:
        self._cursors.clear()
        self._revoked.clear()
        self._identity = None
        self._epoch += 1

    def forget(self, code: str, *, reason: str = "") -> None:
        """停止跟踪一个项目：归档、被移除、失权。

        记进 `_revoked` 而不是只删游标 —— 调用方要能区分「还没拉过」和
        「不该再拉了」，后者是轮询的终止条件。
        """
        self._cursors.pop(code, None)
        if reason:
            self._revoked[code] = reason

    def resume(self, code: str) -> None:
        """A fresh authorized project listing permits a previously revoked project."""
        self._revoked.pop(code, None)

    def revoked_reason(self, code: str) -> str:
        return self._revoked.get(code, "")

    def is_revoked(self, code: str) -> bool:
        return code in self._revoked

    def cursor(self, code: str) -> ProjectCursor:
        cursor = self._cursors.get(code)
        if cursor is None:
            cursor = ProjectCursor(code)
            self._cursors[code] = cursor
        return cursor

    def accepts(self, epoch: Any) -> bool:
        """这份响应还算不算数。

        调用方发请求前取 `epoch`，拿到响应后带回来问一次。清场之后回来的旧响应
        在这里被丢掉，不会污染新账号 / 新项目的状态。
        """
        try:
            return int(epoch) == self._epoch
        except (TypeError, ValueError):
            return False

    def admit(self, code: str, messages: Iterable[Dict[str, Any]],
              *, epoch: Any = None) -> Tuple[List[Dict[str, Any]], int]:
        """把一批拉回来的消息喂进游标，返回 (新消息, 新水位)。

        `epoch` 对不上直接丢弃 —— 返回空批而不是抛异常：一份过期的响应不是错误，
        它只是不再有人要了。
        """
        if epoch is not None and not self.accepts(epoch):
            return [], self._cursors[code].seq if code in self._cursors else 0
        if self.is_revoked(code):
            return [], 0
        cursor = self.cursor(code)
        return cursor.admit(messages), cursor.seq
