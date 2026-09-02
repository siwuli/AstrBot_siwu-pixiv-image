# -*- coding: UTF-8 -*-
"""P站找图插件（pixiv_image，AstrBot）。

按「多工具 + LLM 路由」设计，拆成四个 Agent 工具，由 LLM 根据用户需求自动选择：

- siwu_pixiv_search    → 关键词搜图（默认热门候选随机抽取 + 最低收藏门槛）
- siwu_pixiv_ranking   → 每日/每周/每月排行榜（用户没指定具体图时用热门榜）
- siwu_pixiv_artist    → 找指定画师的作品
- siwu_pixiv_detail    → 指定 pixiv 作品链接/ID 查详情
- siwu_pixiv_related   → 按作品 ID 找相似/相关插画

核心能力：
- refresh_token OAuth 自动续期（缓存轮换后的 token 到 AstrBot 数据目录）；
- HTTP 代理配置（pixiv_proxy，如 http://127.0.0.1:7890）；
- R18 五档：safe(默认，仅一般向) / r18(一般向+R-18) / r18only(只R-18) /
  r18gonly(只R-18G) / r18g(全放行)，按 x_restrict 过滤；
- 热门度：搜索按收藏热度过滤最低收藏数（默认 1000，可配置）；默认从热门候选
  随机抽取（结果不固定、不保底首位），关闭 pixiv_search_balanced 则严格取热度最高；
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
    strip_image_metadata,
)
from .r18_policy import (
    R18StateStore,
    can_enable_r18,
    migrate_legacy_keys,
    parse_id_list,
    session_key,
)

logger = logging.getLogger("astrbot")

DATA_DIR = os.path.join(get_astrbot_data_path(), "pixiv_image")
CACHE_DIR = os.path.join(DATA_DIR, "cache")
TOKEN_PATH = os.path.join(DATA_DIR, "token.json")
R18_STATE_PATH = os.path.join(DATA_DIR, "r18_state.json")

_client: PixivClient | None = None
_client_lock = asyncio.Lock()

_r18_store: R18StateStore | None = None
_r18_store_lock = asyncio.Lock()

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

    def _r18_owners(self) -> set:
        return parse_id_list(self._cfg("pixiv_r18_owners", ""))

    def _r18_groups(self) -> set:
        return parse_id_list(self._cfg("pixiv_r18_groups", ""))

    async def _r18_store_get(self) -> R18StateStore:
        global _r18_store
        async with _r18_store_lock:
            if _r18_store is None:
                _r18_store = R18StateStore(R18_STATE_PATH)
                try:
                    moved = migrate_legacy_keys(_r18_store)
                    if moved:
                        logger.info(f"[pixiv_image] migrated {moved} legacy r18 session keys")
                except Exception as exc:  # noqa: BLE001
                    logger.debug(f"[pixiv_image] r18 key migration failed: {exc}")
            return _r18_store

    async def _session_r18_level(self, event: AstrMessageEvent) -> str:
        """会话级 R18 档位：群聊按群维度共享，私聊按人；显式设置优先，否则回落全局。"""
        try:
            store = await self._r18_store_get()
            group_id = getattr(getattr(event, "message_obj", None), "group_id", None)
            lv = store.get(session_key(event.get_sender_id(), group_id))
            if lv in R18_LEVELS:
                return lv
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[pixiv_image] r18 state read failed: {exc}")
        return self._r18_level()

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
        """下载 candidates 的图片并发给会话，返回成功发送张数。

        发送策略：
        - 按 pixiv_send_original 选择原图（original_url）或 1200px 大图（image_url），
          所选档位在失败重试时不降级（保持原尺寸）；
        - pixiv_send_fallback=true 时下载/发送失败自动重试一次（同一 URL 与原尺寸）；
        - pixiv_send_rewrite_meta=true 时发送前改写文件元数据（删 EXIF/注释，
          纯字节级操作，像素与尺寸完全不变）。
        """
        max_send = self._max_send()
        prefer_original = self._send_original()
        retry = bool(self._cfg("pixiv_send_fallback", True))
        rewrite_meta = bool(self._cfg("pixiv_send_rewrite_meta", False))

        comps = []
        sent = 0
        for r in results:
            if sent >= max_send:
                break
            url = (
                str(r.get("original_url") or r.get("image_url") or "")
                if prefer_original
                else str(r.get("image_url") or r.get("original_url") or "")
            )
            if not url:
                continue
            dest = os.path.join(CACHE_DIR, f"{r.get('id')}_{sent}.{_sanitize_ext(url).lstrip('.')}")
            try:
                await client.download_image(url, dest)
            except Exception as exc:  # noqa: BLE001
                if retry:
                    try:
                        await client.download_image(url, dest)
                    except Exception as exc2:  # noqa: BLE001
                        logger.warning(f"[pixiv_image] download failed {r.get('id')}: {exc} / {exc2}")
                        continue
                else:
                    logger.warning(f"[pixiv_image] download failed {r.get('id')}: {exc}")
                    continue
            # 配置开启时规范化元数据（去 EXIF/注释，像素与尺寸零改动）
            if rewrite_meta:
                try:
                    strip_image_metadata(dest)
                except Exception as exc3:  # noqa: BLE001
                    logger.warning(f"[pixiv_image] strip metadata failed {r.get('id')}: {exc3}")
            comps.append(ComponentImage.fromFileSystem(dest))
            sent += 1
        if comps:
            try:
                await event.send(MessageChain(comps))
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[pixiv_image] send images failed: {exc}")
                if retry:
                    try:
                        await event.send(MessageChain(comps))
                        return sent
                    except Exception as exc2:  # noqa: BLE001
                        logger.warning(f"[pixiv_image] send retry failed: {exc2}")
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
        """在 Pixiv 按关键词找插画/美图（默认从满足收藏门槛的热门候选里随机抽取，过滤低人气作品，每轮结果不同、靠后的好图也有机会出现）。适用于用户要求「找/搜/来几张 xx 的图」「xx 的插画」「miku/初音 图包」等，或用户没有具体要求只想看热门图时。关键词请用作品常用标签/标题词（中文或日文皆可）。scope 可选 tag(仅标签)/title(标题)/both(标签+标题，默认)；sort 可选 popular_desc(默认，收藏优先)/date_desc(最新优先)。会直发热门候选图，请基于返回数据组织最终回复并附上作品链接。
        
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
                min_bookmarks=self._min_bookmarks(), r18_level=await self._session_r18_level(event),
                filter_ai=bool(self._cfg("pixiv_filter_ai", True)), limit=self._max_results(),
                premium=bool(self._cfg("pixiv_premium", False)),
                balanced=bool(self._cfg("pixiv_search_balanced", True)),
            )
        except PixivError as exc:
            logger.error(f"[pixiv_image] search failed: {exc}")
            yield f"Pixiv 搜索失败：{exc}"
            return
        if not results:
            yield f"关键词「{keyword}」没有找到满足条件的结果（R18 档位={await self._session_r18_level(event)}，最低收藏 {self._min_bookmarks()}）。可尝试换关键词或放宽门槛。"
            return
        sent = await self._send_illust_images(event, client, results)
        top_bm = results[0]["bookmarks"] if results else 0
        hint = ""
        if top_bm < self._min_bookmarks():
            premium_on = bool(self._cfg("pixiv_premium", False))
            hint = "（提示：收藏数未达门槛；非会员账号热门排序受限时仅为近期相对热门候选，可在配置中开启 pixiv_premium 使用高级排序）" if not premium_on else "（提示：收藏数未达门槛）"
        yield self._format_results(results, f"Pixiv 搜索「{keyword}」{hint}", sent)

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
        level = await self._session_r18_level(event)
        if "r18" in mode and level == "safe":
            yield "当前会话为 safe（仅一般向），无法查看 R-18 排行。可让白名单账号发送 /pixiv r18 on 开启，或在插件配置 pixiv_r18_level 调整全局档位。"
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
                user_id, r18_level=await self._session_r18_level(event), min_bookmarks=self._min_bookmarks(),
                filter_ai=bool(self._cfg("pixiv_filter_ai", True)), limit=self._max_results(),
            )
        except PixivError as exc:
            logger.error(f"[pixiv_image] artist failed: {exc}")
            yield f"画师作品获取失败：{exc}"
            return
        if not results:
            yield f"画师「{artist_name}」（ID {user_id}）暂无满足条件的作品（R18档位={await self._session_r18_level(event)}，最低收藏 {self._min_bookmarks()}）。"
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
        allowed = R18_LEVELS.get(await self._session_r18_level(event), R18_LEVELS["safe"])
        if result["x_restrict"] not in allowed:
            yield f"作品《{result['title']}》为 {result['r18_label']}，当前会话（{await self._session_r18_level(event)}）不予展示。"
            return
        sent = await self._send_illust_images(event, client, [result])
        yield self._format_results([result], f"Pixiv 作品 {illust_id}", sent)

    # ------------------------------------------------------------------
    # 工具 5：按作品 ID 找相似/相关插画
    # ------------------------------------------------------------------
    @llm_tool(name="siwu_pixiv_related")
    async def pixiv_related(self, event: AstrMessageEvent, illust_id: str = "", note: str = ""):
        """按 pixiv 作品 ID/链接找相似、相关风格的插画（Pixiv related 推荐）。适用于用户给出一个作品链接或 ID 后问「还有类似的图吗」「找同风格/同画师的其他图」「这些图相关推荐」等。会直发相关候选图并返回数据。
        
        Args:
            illust_id(string): pixiv 作品链接或纯数字 ID，如 https://www.pixiv.net/artworks/123456 或 123456
            note(string): 用户补充说明，可为空
        """
        if not self._enabled():
            yield "P站找图功能已在配置中关闭。"
            return
        illust_id = extract_illust_id((illust_id or "").strip())
        if not illust_id:
            yield "请提供 pixiv 作品链接或数字 ID，用于查找相似图。"
            return
        client = await self._client_get()
        try:
            origin = await client.illust_detail(illust_id)
            if not origin:
                yield f"作品 {illust_id} 不存在或已被删除。"
                return
            allowed = R18_LEVELS.get(await self._session_r18_level(event), R18_LEVELS["safe"])
            if origin["x_restrict"] not in allowed:
                yield f"参考作品《{origin['title']}》为 {origin['r18_label']}，当前会话（{await self._session_r18_level(event)}）不予展示。"
                return
            results = await client.related_illusts(
                illust_id, r18_level=await self._session_r18_level(event),
                min_bookmarks=self._min_bookmarks(),
                filter_ai=bool(self._cfg("pixiv_filter_ai", True)), limit=self._max_results(),
            )
        except PixivError as exc:
            logger.error(f"[pixiv_image] related failed: {exc}")
            yield f"相似图获取失败：{exc}"
            return
        if not results:
            yield f"作品 {illust_id}（《{origin['title']}》）暂无相关推荐（受 R18 档位/收藏门槛限制）。"
            return
        sent = await self._send_illust_images(event, client, results)
        yield self._format_results(results, f"与《{origin['title']}》相关的插画", sent)

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
            "4. siwu_pixiv_detail——用户提供了 pixiv 作品链接或 ID，想查看该作品本身；\n"
            "5. siwu_pixiv_related——用户有作品链接/ID 并想找「相似/相关/类似/同风格」的图。\n"
            "工具会自行处理代理/鉴权/过滤并直发图片；工具只返回数据，最终回复由你组织输出，"
            "禁止调用 send_message_to_user。"
        )
        if req.func_tool is not None:
            # ToolSet API（AstrBot 4.x）：移除内置直发工具，避免与插件工具重复；
            # 兼容旧版 list.remove 写法
            remove_tool = getattr(req.func_tool, "remove_tool", None)
            if callable(remove_tool):
                remove_tool("send_message_to_user")
            else:
                try:
                    req.func_tool.remove("send_message_to_user")
                except (AttributeError, ValueError):
                    pass
        req.user_instruction = text

    # ------------------------------------------------------------------
    # R18 会话开关：/pixiv r18 on|all|off|status（仅白名单账号，群聊需群白名单）
    # ------------------------------------------------------------------
    async def _cmd_r18(self, event: AstrMessageEvent, parts: list):
        sub = parts[1].lower() if len(parts) > 1 else "status"
        sender = str(event.get_sender_id() or "")
        group_id = getattr(getattr(event, "message_obj", None), "group_id", None)
        # 私聊事件 group_id 可能为 None/空串/"0"，归一化后再走群聊判断
        if group_id is not None and str(group_id).strip() in ("", "0"):
            group_id = None
        # 群聊按群维度共享状态（白名单账号开一次，该群全体生效），私聊按人
        key = session_key(sender, group_id)
        store = await self._r18_store_get()
        if sub == "status":
            cur = store.get(key) or self._r18_level()
            yield (
                f"当前会话 R-18 档位：{cur}（safe=仅一般向 / r18=一般向+R-18 / "
                "r18only=只R-18 / r18gonly=只R-18G / r18g=全放行）。\n"
                "白名单账号可用：/pixiv r18 on 开启、/pixiv r18 only 只发R-18、"
                "/pixiv r18 all 全放行、/pixiv r18 off 关闭"
            )
            return
        if sub not in ("on", "all", "off", "only", "gonly"):
            yield "用法：/pixiv r18 on（一般向+R-18）| only（只R-18）| gonly（只R-18G）| all（全放行）| off（关闭）| status（查看）"
            return
        if sub == "off":
            ok, reason = can_enable_r18(sender, group_id, self._r18_owners(), self._r18_groups())
            if not ok:
                yield f"无权关闭：{reason}"
                return
            store.clear(key)
            yield f"已关闭本会话 R-18（回落到全局配置 pixiv_r18_level={self._r18_level()}）。"
            return
        ok, reason = can_enable_r18(sender, group_id, self._r18_owners(), self._r18_groups())
        if not ok:
            yield f"无法开启 R-18：{reason}"
            return
        level = {
            "on": "r18",
            "all": "r18g",
            "only": "r18only",
            "gonly": "r18gonly",
        }[sub]
        label = {
            "r18": "R-18（含一般向，拒绝 R-18G）",
            "r18g": "R-18G 全放行",
            "r18only": "仅 R-18（不含一般向与 R-18G）",
            "r18gonly": "仅 R-18G",
        }[level]
        store.set(key, level)
        scope_txt = f"（{key}）" if group_id else "（私聊）"
        yield f"已开启本会话{scope_txt} {label}。关闭请发 /pixiv r18 off"

    # ------------------------------------------------------------------
    # 指令组：/pixiv r18 on|all|off|status ｜ /pixiv search 关键词 ｜ /pixiv rank ｜ /pixiv help
    # ------------------------------------------------------------------
    @filter.command_group("pixiv")
    def pixiv(self):
        """P站找图指令组：R18 会话开关、搜索、排行、帮助。"""
        return

    @pixiv.command(
        "r18",
        desc="会话级 R18 开关（仅白名单账号）：on=开启R-18 / all=R-18G全放行 / off=关闭 / status=查看",
    )
    async def pixiv_r18(self, event: AstrMessageEvent, args: str = ""):
        """子命令：/pixiv r18 status|on|all|off（按会话独立，权限走白名单）"""
        parts = ["r18"]
        if args:
            parts.extend(str(args).split())
        replies = []
        async for chunk in self._cmd_r18(event, parts):
            replies.append(chunk)
        if replies:
            event.stop_event()
            yield event.make_result().message("\n".join(replies))

    @pixiv.command("search", desc="关键词搜图，如 /pixiv search miku")
    async def pixiv_search_cmd(self, event: AstrMessageEvent, keyword: str = ""):
        if not (keyword or "").strip():
            event.stop_event()
            yield event.make_result().message("用法：/pixiv search 关键词（如 /pixiv search miku）")
            return
        replies = []
        async for chunk in self.pixiv_search(
            event, keyword=keyword.strip(), scope="both", sort="popular_desc", note="",
        ):
            replies.append(chunk)
        if replies:
            event.stop_event()
            yield event.make_result().message("\n".join(replies))

    @pixiv.command("rank", desc="今日排行榜")
    async def pixiv_rank_cmd(self, event: AstrMessageEvent):
        replies = []
        async for chunk in self.pixiv_ranking(event, mode="", note=""):
            replies.append(chunk)
        if replies:
            event.stop_event()
            yield event.make_result().message("\n".join(replies))

    @pixiv.command("help", desc="显示全部指令")
    async def pixiv_help(self, event: AstrMessageEvent):
        event.stop_event()
        yield event.make_result().message(
            "P站找图指令：\n"
            "/pixiv search 关键词 —— 搜图（如 /pixiv search miku）\n"
            "/pixiv rank —— 今日排行榜\n"
            "/pixiv r18 status —— 查看本会话 R-18 档位\n"
            "/pixiv r18 on —— 开启 R-18（含一般向，拒绝 R-18G，仅白名单账号）\n"
            "/pixiv r18 only —— 只发 R-18 作品（仅白名单账号）\n"
            "/pixiv r18 gonly —— 只发 R-18G 作品（仅白名单账号）\n"
            "/pixiv r18 all —— R-18G 全放行（仅白名单账号）\n"
            "/pixiv r18 off —— 关闭，回落到全局配置（仅白名单账号）\n"
            "R18 按会话独立：某群/私聊开关互不影响，重启后保留。"
        )


def _looks_like_name(query: str) -> bool:
    """纯数字或 /users/ 链接视为 ID，其余视为画师名。"""
    stripped = query.strip()
    if re.fullmatch(r"\d{4,}", stripped):
        return False
    return "pixiv.net/users" not in stripped