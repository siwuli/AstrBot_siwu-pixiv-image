# -*- coding: UTF-8 -*-
"""R18 会话策略：分群开关 + 账号白名单（纯逻辑，可单测，不依赖 astrbot）。

设计：
- 每个会话（群/私聊）可独立设置 R18 档位，持久化到 JSON；
- 只有 pixiv_r18_owners 白名单账号能开启；
- 群聊还需群号在 pixiv_r18_groups 白名单内（配置为空 = 任何群都不允许）；
- 群主/管理员身份不参与权限判定（只认配置白名单）。
"""

from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger("astrbot")

R18_ON_LEVEL = "r18"   # 开启指令落到 r18 档（safe/r18/r18g 三档中的中间档）
R18_OFF_LEVEL = "safe"  # 关闭指令回到 safe


def parse_id_list(raw: str | list | None) -> set[str]:
    """解析配置里的 QQ 号/群号列表：逗号、空格、换行分隔均可。"""
    if not raw:
        return set()
    if isinstance(raw, list):
        items = [str(x) for x in raw]
    else:
        items = str(raw).replace("，", ",").split(",")
    out = set()
    for it in items:
        for piece in it.replace("\n", " ").split():
            piece = piece.strip()
            if piece and piece.isdigit():
                out.add(piece)
    return out


def can_enable_r18(
    sender_qid: str | int | None,
    group_id: str | int | None,
    owners: set[str],
    allowed_groups: set[str],
) -> tuple[bool, str]:
    """判定某发送人能否开启当前会话的 R18。group_id=None 表示私聊。

    返回 (能否, 原因/说明)。规则：
    1. 发送人必须在 owners 白名单（群主/管理员身份不参与）；
    2. 群聊必须群号在 allowed_groups 内（空集合=任何群都不允许）；
    3. 私聊只要发送人是白名单即可（该会话只有本人）。
    """
    qid = str(sender_qid).strip() if sender_qid is not None else ""
    if not qid or qid not in owners:
        return False, "你没有权限开启 R-18（需要管理员在插件配置 pixiv_r18_owners 中把你的 QQ 加入白名单）"
    if group_id is None:
        return True, ""
    gid = str(group_id).strip()
    if not allowed_groups:
        return False, "未配置允许开启 R-18 的群（pixiv_r18_groups 为空）"
    if gid in allowed_groups:
        return True, ""
    return False, f"该群（{gid}）不在 pixiv_r18_groups 允许列表中"


class R18StateStore:
    """会话级 R18 档位持久化：origin -> level（safe/r18/r18g）。"""

    def __init__(self, path: str):
        self.path = path
        self._data: dict[str, str] = {}
        self.load()

    def load(self) -> None:
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                self._data = {str(k): str(v) for k, v in d.items() if str(v) in ("safe", "r18", "r18g")}
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[pixiv] r18 state load failed: {exc}")

    def save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=1)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[pixiv] r18 state save failed: {exc}")

    def get(self, origin: str) -> str | None:
        return self._data.get(str(origin))

    def set(self, origin: str, level: str) -> None:
        if level not in ("safe", "r18", "r18g"):
            return
        self._data[str(origin)] = level
        self.save()

    def clear(self, origin: str) -> None:
        self._data.pop(str(origin), None)
        self.save()

    def all(self) -> dict[str, str]:
        return dict(self._data)
