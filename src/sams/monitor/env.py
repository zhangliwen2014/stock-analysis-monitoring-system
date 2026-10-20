"""本机通道（桌面弹窗、语音朗读）能不能落地的判定。

这里要回答的不是「发出去了吗」，而是「这台机器上有没有一个可以弹给的人」。判断只看
环境变量与 `PATH`，**不起进程**：探测一次要 fork，而每条 strong 告警都要问一遍，
代价落在最不该慢的那条路径上。`GATED["subprocess"]` 也只放行 `monitor/notifiers`（T-2）。

探测不到 ⇒ **自动禁用且不算失败**：这个仓库日常跑在远程终端里，那里没有桌面也没有扬声器。
如果每次压制都记一条 `failed`，健康页就会永远写着「通知在失败」，而真坏掉的那个通道
藏在这堆噪音里。所以 router 拿到的是「不可用」，它写 `suppressed` 且不调 `mark_error`。
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass, field

#: 两个本机通道的名字，与 `core.notify.KINDS` 里的后两个一致
DESKTOP, VOICE = "desktop", "voice"

#: 判定「这是远程会话」的那几个变量。SSH_TTY 在没有图形转发的登录里也在，
#: 而它恰恰意味着「这里没有图形界面」；tmux/screen 同理——它们跑在服务器上，
#: 服务器前没有人。
_REMOTE_SIGNS = ("SSH_TTY", "SSH_CONNECTION", "TMUX", "STY")

#: 探测要看的那几个二进制。`open` 不在里面：它能打开 Finder，弹不出「一条通知」。
_PROBED = {DESKTOP: ("osascript",), VOICE: ("say", "osascript"), "linux": ("notify-send",)}

#: 每条原因都以一句可执行的建议收尾：屏幕上留一句只有运维看得懂的诊断等于没说。
_FIXES = {
    DESKTOP: "请改用企微 / 钉钉 / PushPlus 任一手机侧通道。",
    VOICE: "请给 strong 级也配一个手机侧通道。",
}


def _all_names() -> tuple[str, ...]:
    return tuple(dict.fromkeys(name for names in _PROBED.values() for name in names))


@dataclass(frozen=True)
class LocalEnv:
    """一次探测的快照（构造之后不再读世界）。

    快照而不是每次现探：一条告警发到一半重新判断环境，同一批投递里就会出现两种 outcome，
    而它们唯一的区别是「探测时恰好谁在」。
    """

    platform: str
    remote_sign: str = ""
    found: Mapping[str, str] = field(default_factory=dict)
    has_display: bool = False

    @classmethod
    def detect(
        cls,
        *,
        environ: Mapping[str, str] | None = None,
        which: Mapping[str, str] | None = None,
        platform: str | None = None,
    ) -> LocalEnv:
        """三个参数都只为测试存在：传了就整个用它，没传才走真实机器。

        `which` 给了就不碰 `shutil.which`——否则在 mac 上跑的那组 Linux 用例会被本机的
        PATH 污染，「这台开发机确实装了 osascript」会让断言失败得看不出原因。
        """
        env = os.environ if environ is None else environ
        resolved = (
            which
            if which is not None
            else {name: path for name in _all_names() if (path := shutil.which(name))}
        )
        return cls(
            platform=(platform or os.uname().sysname).lower(),
            remote_sign=next((key for key in _REMOTE_SIGNS if env.get(key)), ""),
            found=dict(resolved),
            has_display=bool(env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")),
        )

    def available(self, kind: str) -> bool:
        return self._gap(kind) is None

    def reason(self, kind: str) -> str:
        gap = self._gap(kind)
        return "" if gap is None else gap + _FIXES.get(kind, "")

    def _gap(self, kind: str) -> str | None:
        if kind not in (DESKTOP, VOICE):
            return f"本机通道只有 {DESKTOP} 与 {VOICE}，没有 {kind}。"
        if self.remote_sign:
            return f"当前是远程会话（{self.remote_sign}），这里没有一个坐在屏幕前的人。"
        return self._gap_mac(kind) if self.platform == "darwin" else self._gap_linux(kind)

    def _gap_mac(self, kind: str) -> str | None:
        if kind == DESKTOP:
            return None if self.found.get("osascript") else "本机没有 osascript，弹窗发不出去。"
        # 有 `say` 就用它，没有就退回 osascript 的 `say` 命令：mac 上这两条路都真能出声
        if self.found.get("say") or self.found.get("osascript"):
            return None
        return "本机没有 say 也没有 osascript，语音朗读发不出去。"

    def _gap_linux(self, kind: str) -> str | None:
        if kind == VOICE:
            return "语音朗读只在 macOS 上有落点（say / osascript），这台机器是 Linux。"
        if not self.found.get("notify-send"):
            return "本机没有 notify-send，弹窗发不出去。"
        if not self.has_display:
            return "这里没有图形会话（DISPLAY 为空），notify-send 无处可弹。"
        return None


def local_env() -> LocalEnv:
    """进程内一次探测，处处复用。

    缓存的理由不是「环境不会变」，而是「变了对这一批投递也没意义」：一条告警的两次通道
    投递必须用同一个答案。测试用 `LocalEnv.detect(...)` 直接构造，不经过这里。
    """
    return LocalEnv.detect()
