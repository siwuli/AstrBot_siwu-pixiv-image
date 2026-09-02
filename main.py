# -*- coding: UTF-8 -*-
"""兔兔P站找图插件（pixiv_image，AstrBot）。

按「多工具 + LLM 路由」设计，拆成四个 Agent 工具，由 LLM 根据用户需求自动选择：

- siwu_pixiv_search    → 关键词搜图（默认按收藏热度降序 + 最低收藏门槛）
- siwu_pixiv_ranking   → 每日/每周/每月排行榜（用户没指定具体图时用热门榜）
- siwu_pixiv_artist    → 找指定画师的作品
- siwu_pixiv_detail    → 指定 pixiv 作品链接/ID 查详情

核心能力：
- refresh_token OAuth 自动续期（缓存轮换后的 token 到 AstrBot 数据目录）；
- HTTP 代理配置（pixiv_proxy，如 http://127.0.0.1:7890）；
- R18 三档：safe(默认，仅一般向) / r18(允许R-18) / r18g(全放行)，按 x_restrict 过滤；
- 热门度：搜索按 popular_desc 排序并过滤最低收藏数（默认 1000，可配置）；
- 发图：图片经代理下载到本地后用 Image.fromFileSystem 发送（i.pximg.net 需 Referer，
  QQ 客户端直连海外图片站不可靠，本地文件最稳），先发图后组织文字。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re

from astrbot.api import star
from astrbot.api.all import AstrBotConfig, AstrMessageEvent, llm_tool
from astrbot.api.event import MessageChain, filter
from astrbot.api.message_components import Image as ComponentImage
from astrbot.api.provider import ProviderRequest
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .pixiv_api import (
    R18_LEVELS,
    RANK_MODES,
    PixivClient,
    PixivError,
    extract_illust_id,
)

logger = logging.getLogger("astrbot")

DATA_DIR = os.path.join(get_astrbot_data_path(), "pixiv_image")
CACHE_DIR = os.path.join(DATA_DIR, "cache")
TOKEN_PATH = os.path.join(DATA_DIR, "token.json")

_client: PixivClient | None = None
_client_lock = asyncio.Lock()

INTENT_WORDS = (
    "pixiv", "P站", "p站", "找图", "搜图", "排行榜", "排行",
    "今日排行", "每日排行", "画师", "PIXIV", "图集", "插画",
)


def _sanitize_ext(url: str) -> str:
    m = re.search(r"\.(jpe?g|png|gif|webp)(?:\?|$)", url, re.IGNORECASE)
    return ("." + m.group(1).lower()) if m else ".jpg"


class PixivImagePlugin(star.Star):
    def __init__(self, context, config: AstrBotConfig = None):
        super().__init__(context, config)
        self.config = config or {}

    # ------------------------------------------------------------------
    # 配置辅助
    # ------------------------------------------------------------------
    def _cfg(self, key: str, default=None):
        value = self.config.get(key, default)
        return default if value is None else value

    def _enabled(self) -> bool:
        return bool(self._cfg("pixiv_enabled", True))

    def _r18_level(self) -> str:
        level = str(self._cfg("pixiv_r18_level", "safe") or "safe").strip().lower()
        return level if level in R18_LEVELS else "safe"

    def _min_bookmarks(self) -> int:
        return max(0, int(self._cfg("pixiv_min_bookmarks", 1000) or 0))

    def _max_results(self) -> int:
        return max(1, min(5, int(self._cfg("pixiv_max_results", 3) or 3)))

    def _max_send(self) -> int:
        return max(1, min(5, int(self._cfg("pixiv_send_images", 1) or 1)))

    def _send_original(self) -> bool:
        return bool(self._cfg("pixiv_send_original", False))

    async def _client_get(self) -> PixivClient:
        global _client
        async with _client_lock:
            if _client is None:
                _client = PixivClient(
                    refresh_token=str(self._cfg("pixiv_refresh_token", "") or ""),
                    proxy=str(self._cfg("pixiv_proxy", "") or ""),
                    token_path=TOKEN_PATH,
                )
            else:
                _client.tokens.refresh_token = str(self._cfg("pixiv_refresh_token", "") or "") or _client.tokens.refresh_token
                _client.proxy = (str(self._cfg("pixiv_proxy", "") or "").strip() or None)
            return _client

    # ------------------------------------------------------------------
    # 发图（先图后文：先直发图片，再 yield 数据文本给主 LLM 收尾）
    # ------------------------------------------------------------------
    async def _send_illust_images(
        self, event: AstrMessageEvent, client: PixivClient, results: list[dict],
    ) -> int:
        """下载 candidates 的图片并发给会话，返回成功发送张数。"""
        max_send = self._max_send()
        comps = []
        sent = 0
        for r in results:
            if sent >= max_send:
                break
            url = r.get("original_url") or r.get("image_url")
            if not url:
                continue
            dest = os.path.join(CACHE_DIR, f"{r.get('id')}_{sent}.{_sanitize_ext(url).lstrip('.')}")
            try:
                await client.download_image(url, dest)
                comps.append(ComponentImage.fromFileSystem(dest))
                sent += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[pixiv_image] download failed {r.get('id')}: {exc}")
        if comps:
            try:
                await event.send(MessageChain(comps))
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[pixiv_image] send images failed: {exc}")
                return 0
        return sent

    @staticmethod
    def _format_results(results: list[dict], engine: str, sent: int) -> str:
        lines = [f"引擎：{engine}（收藏数门槛过滤后，已直发 {sent} 张图）"]
        for i, r in enumerate(results, 1):
            tags = "、".join(r["tags"][:5]) if r.get("tags") else "无标签"
            lines.append(
                f"{i}. 《{r['title']}》 by {r['artist']}｜合集 {r['r18_label']}｜"
                f"{r['bookmarks']}收藏 {r['views']}浏览｜标签：{tags}",
            )
            lines.append(f"   {r['url']}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 工具 1：关键词搜图
    # ------------------------------------------------------------------
    @llm_tool(name="siwu_pixiv_search")
    async def pixiv_search(
        self, event: AstrMessageEvent, keyword: str = "", scope: str = "both",
        sort: str = "popular_desc", note: str = "",
    ):
        """在 Pixiv 按关键词找插画/美图（默认按收藏热度排序并过滤低人气作品）。适用于用户要求「找/搜/来几张 xx 的图」「xx 的插画」「miku/初音 图包」等，或用户没有具体要求只想看热门图时。关键词请用作品常用标签/标题词（中文或日文皆可）。scope 可选 tag(仅标签)/title(标题)/both(标签+标题，默认)；sort 可选 popular_desc(默认，收藏优先)/date_desc(最新优先)。会直发热门候选图，请基于返回数据组织最终回复并附上作品链接。
        
        Args:
            keyword(string): 搜索关键词，如 miku、明日方舟、风景
            scope(string): 搜索范围：tag/title/both，默认 both
            sort(string): 排序：popular_desc/date_desc，默认 popular_desc
            note(string): 用户补充说明，可为空
        """
        if not self._enabled():
            yield "P站找图功能已在配置中关闭。"
            return
        keyword = (keyword or "").strip()
        if not keyword:
            yield "请提供搜索关键词，例如：miku、明日方舟、风景。"
            return
        if scope not in ("tag", "title", "both"):
            scope = "both"
        if sort not in ("popular_desc", "date_desc", "date_asc"):
            sort = "popular_desc"
        client = await self._client_get()
        try:
            results = await client.search_illust(
                keyword, scope=scope, sort=sort,
                min_bookmarks=self._min_bookmarks(), r18_level=self._r18_level(),
                filter_ai=bool(self._cfg("pixiv_filter_ai", True)), limit=self._max_results(),
            )
        except PixivError as exc:
            logger.error(f"[pixiv_image] search failed: {exc}")
            yield f"Pixiv 搜索失败：{exc}"
            return
        if not results:
            yield f"关键词「{keyword}」没有找到满足条件的结果（R18 等级={self._r18_level()}，最低收藏 {self._min_bookmarks()}）。可尝试换关键词或放宽门槛。"
            return
        sent = await self._send_illust_images(event, client, results)
        yield self._format_results(results, f"Pixiv 搜索「{keyword}」", sent)

    # ------------------------------------------------------------------
    # 工具 2：排行榜
    # ------------------------------------------------------------------
    @llm_tool(name="siwu_pixiv_ranking")
    async def pixiv_ranking(self, event: AstrMessageEvent, mode: str = "", note: str = ""):
        """获取 Pixiv 排行榜的热门插画（每日/每周/每月等），用户没有指定具体搜索内容或想看「今天推荐/热门图」时使用。mode 可选：daily(每日，默认)/weekly(每周)/monthly(每月)/male(男性向)/female(女性向)/rookie(新人)/original(原创)；R-18 相关模式 daily_r18/weekly_r18/monthly_r18/r18g 仅当插件配置允许时可用。会直发热门候选图，请基于返回数据组织最终回复。
        
        Args:
            mode(string): 排行模式：daily/weekly/monthly/male/female/rookie/original/daily_r18/weekly_r18/monthly_r18/r18g，空则用默认
            note(string): 用户补充说明，可为空
        """
        if not self._enabled():
            yield "P站找图功能已在配置中关闭。"
            return
        mode = (mode or "").strip().lower() or str(self._cfg("pixiv_rank_default", "daily") or "daily")
        if mode not in RANK_MODES:
            yield f"不支持的排行模式：{mode}。可选：{', '.join(RANK_MODES)}"
            return
        level = self._r18_level()
        if "r18" in mode and level == "safe":
            yield "当前配置为 safe（仅一般向），无法查看 R-18 排行。可在插件配置 pixiv_r18_level 中调整为 r18 或 r18g。"
            return
        client = await self._client_get()
        try:
            results = await client.illust_ranking(
                mode, r18_level=level, min_bookmarks=0,
                filter_ai=bool(self._cfg("pixiv_filter_ai", True)), limit=self._max_results(),
            )
        except PixivError as exc:
            logger.error(f"[pixiv_image] ranking failed: {exc}")
            yield f"Pixiv 排行获取失败：{exc}"
            return
        if not results:
            yield f"{mode} 排行榜暂无结果。"
            return
        sent = await self._send_illust_images(event, client, results)
        yield self._format_results(results, f"Pixiv {mode} 排行榜", sent)

    # ------------------------------------------------------------------
    # 工具 3：画师作品
    # ------------------------------------------------------------------
    @llm_tool(name="siwu_pixiv_artist")
    async def pixiv_artist(self, event: AstrMessageEvent, query: str = "", note: str = ""):
        """查找某位 Pixiv 画师/插画师的作品。适用于用户说「找 xx 画师的作品」「这是谁画的还有哪些图」「画师 xx 的主页」等。query 填画师名称（中文/日文/账号名均可）或 Pixiv 用户 ID（纯数字，可带 https://www.pixiv.net/users/ 链接）。返回该画师作品列表（同样按收藏门槛过滤并直发热门图）。
        
        Args:
            query(string): 画师名称或 Pixiv 用户 ID/链接
            note(string): 用户补充说明，可为空
        """
        if not self._enabled():
            yield "P站找图功能已在配置中关闭。"
            return
        query = (query or "").strip()
        if not query:
            yield "请提供画师名称或 Pixiv 用户 ID。"
            return
        client = await self._client_get()
        try:
            user_id = extract_illust_id(query)
            artist_name = query
            if not user_id or _looks_like_name(query):
                users = await client.search_user(query, limit=3)
                if not users:
                    yield f"没有找到画师「{query}」，请确认名称拼写。"
                    return
                user_id = users[0]["id"]
                artist_name = users[0]["name"]
            results = await client.user_illusts(
                user_id, r18_level=self._r18_level(), min_bookmarks=self._min_bookmarks(),
                filter_ai=bool(self._cfg("pixiv_filter_ai", True)), limit=self._max_results(),
            )
        except PixivError as exc:
            logger.error(f"[pixiv_image] artist failed: {exc}")
            yield f"画师作品获取失败：{exc}"
            return
        if not results:
            yield f"画师「{artist_name}」（ID {user_id}）暂无满足条件的作品（R18={self._r18_level()}，最低收藏 {self._min_bookmarks()}）。"
            return
        sent = await self._send_illust_images(event, client, results)
        yield self._format_results(results, f"画师「{artist_name}」作品", sent)

    # ------------------------------------------------------------------
    # 工具 4：作品详情
    # ------------------------------------------------------------------
    @llm_tool(name="siwu_pixiv_detail")
    async def pixiv_detail(self, event: AstrMessageEvent, pixiv_id: str = "", note: str = ""):
        """查询/发送指定 Pixiv 作品（输入作品链接或 ID）。适用于用户发了 https://www.pixiv.net/artworks/数字 链接、或直接说「pixiv 123456」「看下这个作品」等。插件会获取该作品详情并直发图片。
        
        Args:
            pixiv_id(string): pixiv 作品链接或纯数字 ID，如 https://www.pixiv.net/artworks/123456 或 123456
            note(string): 用户补充说明，可为空
        """
        if not self._enabled():
            yield "P站找图功能已在配置中关闭。"
            return
        illust_id = extract_illust_id((pixiv_id or "").strip())
        if not illust_id:
            yield "请提供 pixiv 作品链接或数字 ID。"
            return
        client = await self._client_get()
        try:
            result = await client.illust_detail(illust_id)
        except PixivError as exc:
            logger.error(f"[pixiv_image] detail failed: {exc}")
            yield f"作品获取失败：{exc}"
            return
        if not result:
            yield f"作品 {illust_id} 不存在或已被删除。"
            return
        allowed = R18_LEVELS.get(self._r18_level(), R18_LEVELS["safe"])
        if result["x_restrict"] not in allowed:
            yield f"作品《{result['title']}》为 {result['r18_label']}，当前配置（{self._r18_level()}）不予展示。可在配置中调整 pixiv_r18_level。"
            return
        sent = await self._send_illust_images(event, client, [result])
        yield self._format_results([result], f"Pixiv 作品 {illust_id}", sent)

    # ------------------------------------------------------------------
    # 意图钩子：检测 P 站意图时注入路由指令并移除内置直发工具
    # ------------------------------------------------------------------
    @filter.on_llm_request()
    async def force_pixiv_tool(self, event: AstrMessageEvent, req: ProviderRequest):
        if not self._enabled():
            return
        text = event.get_message_str() or ""
        if not any(word in text for word in INTENT_WORDS):
            return
        req.system_prompt = (
            f"{req.system_prompt}\n\n"
            "[pixiv_image 指令] 用户请求与 Pixiv/P站 找图相关，请从以下工具中按语义选择其一调用：\n"
            "1. siwu_pixiv_search——用户给出具体关键词/题材（图、插画、特点标签）想找图；\n"
            "2. siwu_pixiv_ranking——用户没有具体目标，想看「今日/每周推荐、热门排行、排行榜」；\n"
            "3. siwu_pixiv_artist——用户想找某位画师的作品；\n"
            "4. siwu_pixiv_detail——用户提供了 pixiv 作品链接或 ID。\n"
            "工具会自行处理代理/鉴权/过滤并直发图片；工具只返回数据，最终回复由你组织输出，"
            "禁止调用 send_message_to_user。"
        )
        if req.func_tool is not None:
            try:
                req.func_tool.remove("send_message_to_user")
            except ValueError:
                pass
        req.user_instruction = text

    # ------------------------------------------------------------------
    # 命令兜底：/pixiv 关键词 或 /pixiv 排行
    # ------------------------------------------------------------------
    @filter.command("pixiv")
    async def pixiv_command(self, event: AstrMessageEvent, *args):
        parts = [str(a).strip() for a in args if str(a).strip()]
        text = (event.get_message_str() or "").strip()
        for w in ("pixiv", "P站", "p站"):
            if text.startswith(w):
                text = text[len(w):].lstrip(" :：,，")
                break
        if not text and parts:
            text = " ".join(parts)
        if not text or any(k in text for k in ("排行", "推荐", "热门")):
            async for chunk in self.pixiv_ranking(event, mode="", note=""):
                yield chunk
        else:
            async for chunk in self.pixiv_search(event, keyword=text, scope="both", sort="popular_desc", note=""):
                yield chunk


def _looks_like_name(query: str) -> bool:
    """纯数字或 /users/ 链接视为 ID，其余视为画师名。"""
    stripped = query.strip()
    if re.fullmatch(r"\d{4,}", stripped):
        return False
    return "pixiv.net/users" not in stripped