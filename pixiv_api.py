# -*- coding: UTF-8 -*-
"""Pixiv App-API 客户端：OAuth 鉴权 / 搜索 / 排行榜 / 画师 / 详情 / 图片下载。

设计约束（与 image_search 一致）：
- 不依赖 astrbot，纯 Python 模块，网络层用 aiohttp（导入失败时降级，便于本地单测）；
- 所有解析/过滤/参数构造均为纯函数，可独立单测；
- 代理通过 aiohttp 的 proxy 参数支持（http/https 代理，Clash 混合端口亦可）；
- R18 三档：safe(0) / r18(0,1) / r18g(0,1,2)，按作品的 x_restrict 字段过滤。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import re
import time
from datetime import datetime, timezone
from typing import Any

try:
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None

logger = logging.getLogger("astrbot")

# ---------------------------------------------------------------------------
# 公开常量（pixivpy 同源 OAuth 客户端凭证，社区公开）
# ---------------------------------------------------------------------------
CLIENT_ID = "MOBrBDS8blbauoSck0ZfDbtuzpyT"
CLIENT_SECRET = "lsACyCD94FhDUtGTXi3QzcFE2uU1hqtDaKeqrdwj"
HASH_SECRET = "28c1fdd170a5204386cb1313c7077b34f83e4aaf4aa829ce78c231e05b0bae2c"
APP_API_HOST = "https://app-api.pixiv.net"
OAUTH_HOST = "https://oauth.secure.pixiv.net"
IMAGE_HOST = "https://i.pximg.net"
APP_UA = "PixivIOSApp/7.13.3 (iOS 14.6; iPhone13,2)"
IMAGE_REFERER = "https://app-api.pixiv.net/"

# x_restrict: 0=一般向 1=R-18 2=R-18G
# safe=仅一般向；r18=一般向+R-18；r18g=一般向+R18+R18G；
# r18only=只R-18（不含一般向、不含R-18G）；r18gonly=只R-18G
R18_LEVELS: dict[str, set[int]] = {
    "safe": {0},
    "r18": {0, 1},
    "r18g": {0, 1, 2},
    "r18only": {1},
    "r18gonly": {2},
}
R18_LABELS = {0: "一般向", 1: "R-18", 2: "R-18G"}

# 用户可读排行模式 -> app-api mode 参数
RANK_MODES: dict[str, str] = {
    "daily": "day",
    "weekly": "week",
    "monthly": "month",
    "male": "day_male",
    "female": "day_female",
    "rookie": "week_rookie",
    "original": "week_original",
    "daily_r18": "day_r18",
    "weekly_r18": "week_r18",
    "monthly_r18": "month_r18",
    "r18g": "week_r18g",
}

# 搜索范围 -> search_target 参数
SEARCH_TARGETS: dict[str, str] = {
    "tag": "partial_match_for_tags",
    "title": "title_and_caption",
    "both": "partial_match_for_tags",
}

_ARTWORK_RE = re.compile(r"pixiv\.net/(?:en/)?(?:artworks|i)/?(\d+)", re.IGNORECASE)
_PURE_ID_RE = re.compile(r"^\d{6,9}$")
_AI_TAG_RE = re.compile(r"AI(?:[-_ ]?generated)?$|AI生成|人工知能", re.IGNORECASE)


class PixivError(RuntimeError):
    """Pixiv 请求/解析错误（message 可直接面向用户展示）。"""


def extract_illust_id(text: str) -> int | None:
    """从用户消息中提取 pixiv 作品 ID（支持链接/纯数字/「pixiv 123456」口语）。"""
    if not text:
        return None
    m = _ARTWORK_RE.search(text)
    if m:
        return int(m.group(1))
    stripped = text.strip()
    if _PURE_ID_RE.fullmatch(stripped):
        return int(stripped)
    if re.search(r"pixiv|P站|p站|artwork|id\b|ID|作品", text, re.IGNORECASE):
        m = re.search(r"(?<!\d)(\d{6,9})(?!\d)", text)
        if m:
            return int(m.group(1))
    return None


def oauth_headers(now_ts: float | None = None) -> dict[str, str]:
    """构造 OAuth token 端点所需的 x-client-time / x-client-hash 头。"""
    now = datetime.fromtimestamp(now_ts or time.time(), tz=timezone.utc)
    local_time = now.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    digest = hashlib.md5((local_time + HASH_SECRET).encode("utf-8")).hexdigest()
    return {
        "x-client-time": local_time,
        "x-client-hash": digest,
        "app-os": "ios",
        "app-os-version": "14.6",
        "user-agent": APP_UA,
    }


def parse_illust(item: dict[str, Any]) -> dict[str, Any]:
    """把 app-api 的作品条目转为 LLM/发图友好的结构化数据（中文标签优先）。"""
    user = item.get("user") or {}
    tags_raw = item.get("tags") or []
    tags: list[str] = []
    for tag in tags_raw:
        if isinstance(tag, dict):
            name = tag.get("translated_name") or tag.get("name") or ""
        else:
            name = str(tag)
        if name and name not in tags:
            tags.append(name)
    x_restrict = int(item.get("x_restrict", 0) or 0)
    images = item.get("image_urls") or {}
    single = item.get("meta_single_page") or {}
    pages = item.get("meta_pages") or []
    image_url = images.get("large") or images.get("medium") or ""
    original_url = single.get("original_image_url") or ""
    page_urls = []
    for p in pages:
        u = (p.get("image_urls") or {}).get("original") or (p.get("image_urls") or {}).get("large") or ""
        if u:
            page_urls.append(u)
    if not original_url and page_urls:
        original_url = page_urls[0]
    elif not page_urls and original_url:
        page_urls = [original_url]
    illust_id = int(item.get("id") or 0)
    return {
        "id": illust_id,
        "title": str(item.get("title") or ""),
        "artist": str(user.get("name") or "未知画师"),
        "artist_id": int(user.get("id") or 0),
        "bookmarks": int(item.get("total_bookmarks") or 0),
        "views": int(item.get("total_view") or 0),
        "x_restrict": x_restrict,
        "r18_label": R18_LABELS.get(x_restrict, "一般向"),
        "tags": tags,
        "page_count": int(item.get("page_count") or 1),
        "image_url": image_url,
        "original_url": original_url,
        "pages": page_urls,
        "url": f"https://www.pixiv.net/artworks/{illust_id}" if illust_id else "",
        "ai": any(_AI_TAG_RE.search(t) for t in tags),
    }


def filter_illusts(
    items: list[dict[str, Any]],
    r18_level: str = "safe",
    min_bookmarks: int = 0,
    filter_ai: bool = False,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """按 R18 等级 / 收藏门槛 / AI 标记过滤 raw 条目，返回 parse_illust 结果。"""
    allowed = R18_LEVELS.get(r18_level, R18_LEVELS["safe"])
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            xr = int(item.get("x_restrict", 0) or 0)
        except (TypeError, ValueError):
            xr = 0
        if xr not in allowed:
            continue
        if min_bookmarks > 0 and int(item.get("total_bookmarks") or 0) < min_bookmarks:
            continue
        parsed = parse_illust(item)
        if filter_ai and parsed["ai"]:
            continue
        out.append(parsed)
    if limit is not None:
        out = out[: max(0, int(limit))]
    return out


def sample_random(
    items: list[dict[str, Any]],
    limit: int = 3,
    rng: Any = None,
) -> list[dict[str, Any]]:
    """从候选池中随机抽取（保持池内原有顺序的子序列），候选不足则全取。

    - items：已按热度/收藏过滤后的候选池；
    - rng：可注入的随机源（测试用），默认 random 模块；
    - 不再固定保底第一名，也不会总是同一批图。
    """
    pool = list(items or [])
    need = max(1, int(limit or 1))
    if len(pool) <= need:
        return pool
    picker = rng or random
    # 随机抽取位置，但输出保持池内原有顺序（热度降序的子序列）
    idxs = sorted(picker.sample(range(len(pool)), need))
    return [pool[i] for i in idxs]


def search_params(word: str, scope: str = "tag", sort: str = "popular_desc",
                  filter_ai: bool = True, offset: int = 0) -> dict[str, Any]:
    """构造 /v1/illust/search 的查询参数。"""
    target = SEARCH_TARGETS.get(scope, SEARCH_TARGETS["both"])
    params: dict[str, Any] = {
        "word": word,
        "search_target": target,
        "sort": sort if sort in ("popular_desc", "date_desc", "date_asc") else "popular_desc",
        "filter": "for_android",
    }
    # search_ai_type 只接受 1（包含 AI 作品）；0 会触发 400，省略即走服务器默认（过滤 AI）
    if not filter_ai:
        params["search_ai_type"] = 1
    if offset > 0:
        params["offset"] = offset
    return params


def ranking_params(mode: str = "daily", date: str | None = None) -> dict[str, Any]:
    """构造 /v1/illust/ranking 的查询参数（mode 支持 RANK_MODES 键）。"""
    api_mode = RANK_MODES.get(mode, RANK_MODES.get("daily", "day"))
    params: dict[str, Any] = {"mode": api_mode, "filter": "for_android"}
    if date:
        params["date"] = date
    return params


def user_illusts_params(user_id: int | str, offset: int = 0) -> dict[str, Any]:
    params: dict[str, Any] = {"user_id": str(user_id), "type": "illust"}
    if offset > 0:
        params["offset"] = offset
    return params


class TokenStore:
    """access_token 内存缓存 + refresh_token 持久化（每次刷新轮换后写回数据目录）。"""

    def __init__(self, path: str | None = None, fallback_refresh: str = ""):
        self.path = path
        self.access_token = ""
        self.refresh_token = fallback_refresh or ""
        self.expires_at = 0.0
        # 缓存来源指纹：生成缓存时使用的配置 refresh_token 前缀，
        # 用于重启后判断「同账号轮换（保留缓存）」还是「换了账号（作废缓存）」。
        self.source_refresh = ""
        self.load()

    def load(self) -> None:
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            cached_refresh = str(d.get("refresh_token") or "")
            source = str(d.get("source_refresh") or "")
            cfg = (self.refresh_token or "").strip()
            if cfg and (not source or source[:16] != cfg[:16]):
                # 配置的 refresh_token 已变化（换了账号/更新配置），或者缓存是
                # 无来源标记的旧格式：缓存作废，以配置为准重新登录。
                # 同一账号的正常轮换不会走到这里（前缀指纹一致，缓存续用）。
                self.access_token = ""
                self.refresh_token = cfg
                self.expires_at = 0.0
                logger.info(
                    "[pixiv] 配置 refresh_token 与缓存账号不一致，已重置缓存"
                    "（将用新配置重新登录；同一账号正常轮换不受影响）"
                )
                return
            self.access_token = str(d.get("access_token") or "")
            self.refresh_token = cached_refresh or cfg
            self.expires_at = float(d.get("expires_at") or 0)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[pixiv] token load failed: {exc}")

    def save(self) -> None:
        if not self.path:
            return
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump({
                    "access_token": self.access_token,
                    "refresh_token": self.refresh_token,
                    "source_refresh": self.source_refresh,
                    "expires_at": self.expires_at,
                }, f, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[pixiv] token save failed: {exc}")

    def usable(self, now: float | None = None) -> bool:
        now = now or time.time()
        return bool(self.access_token) and self.expires_at > now

    def update(
        self,
        access_token: str,
        refresh_token: str,
        expires_in: int,
        source_refresh: str = "",
    ) -> None:
        self.access_token = access_token
        if refresh_token:
            self.refresh_token = refresh_token
        self.source_refresh = (source_refresh or self.refresh_token)[:16]
        self.expires_at = time.time() + max(60, int(expires_in or 3600)) - 60
        self.save()


class PixivClient:
    """异步 Pixiv App-API 客户端。所有网络方法可被单测替换 _request。"""

    def __init__(
        self,
        refresh_token: str = "",
        proxy: str = "",
        token_path: str | None = None,
        timeout: float = 30.0,
        session: Any = None,
    ):
        self.tokens = TokenStore(token_path, refresh_token)
        self.proxy = (proxy or "").strip() or None
        self.timeout = timeout
        self._session = session
        self._owns_session = session is None

    async def _session_get(self) -> Any:
        if self._session is None:
            if aiohttp is None:
                raise PixivError("缺少 aiohttp，无法发起 Pixiv 请求")
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout),
                headers={"User-Agent": APP_UA},
            )
        return self._session

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            try:
                await self._session.close()
            except Exception:  # noqa: BLE001, S110
                pass
        self._session = None

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        auth: bool = True,
    ) -> dict[str, Any]:
        """执行请求并解析 JSON；非 2xx 抛 PixivError（含 Pixiv 的 error 文案）。"""
        session = await self._session_get()
        req_headers = dict(headers or {})
        if auth:
            req_headers["Authorization"] = f"Bearer {self.tokens.access_token}"
        try:
            async with session.request(
                method, url, params=params, data=data, headers=req_headers,
                proxy=self.proxy if self.proxy else None,
            ) as resp:
                body = await resp.text()
        except PixivError:
            raise
        except Exception as exc:
            raise PixivError(f"Pixiv 请求失败（网络/代理）：{exc}") from exc
        if resp.status < 200 or resp.status >= 300:
            hint = _extract_api_error(body, resp.status)
            raise PixivError(f"Pixiv API HTTP {resp.status}：{hint}")
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise PixivError(f"Pixiv 响应解析失败：{body[:120]!r}") from exc

    async def refresh_auth(self) -> None:
        """用 refresh_token 换取 access_token（自动轮换并持久化）。"""
        if not self.tokens.refresh_token:
            raise PixivError("未配置 Pixiv refresh_token：请在插件配置 pixiv_refresh_token 中填写")
        used_refresh = self.tokens.refresh_token
        data = {
            "get_secure_url": 1,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": used_refresh,
        }
        resp = await self._request(
            "POST", f"{OAUTH_HOST}/auth/token", data=data,
            headers=oauth_headers(), auth=False,
        )
        token = resp.get("response") or {}
        if not token.get("access_token"):
            raise PixivError("Pixiv 鉴权失败：未返回 access_token（refresh_token 可能已失效）")
        self.tokens.update(
            str(token["access_token"]),
            str(token.get("refresh_token") or ""),
            int(token.get("expires_in") or 3600),
            source_refresh=used_refresh[:16],
        )

    async def ensure_auth(self) -> None:
        if not self.tokens.usable():
            await self.refresh_auth()

    async def search_illust(
        self,
        word: str,
        scope: str = "both",
        sort: str = "popular_desc",
        min_bookmarks: int = 0,
        r18_level: str = "safe",
        filter_ai: bool = True,
        limit: int = 5,
        offset: int = 0,
        premium: bool = False,
        balanced: bool = True,
    ) -> list[dict[str, Any]]:
        await self.ensure_auth()
        limit_n = max(1, int(limit))
        # 会员（premium=True）+ 热门排序：popular_desc 后端直接生效
        if premium and sort == "popular_desc":
            params = search_params(word, scope=scope, sort=sort, filter_ai=filter_ai, offset=offset)
            resp = await self._request("GET", f"{APP_API_HOST}/v1/search/illust", params=params)
            items = resp.get("illusts") or []
            pool = filter_illusts(items, r18_level, min_bookmarks, filter_ai, None)
            pool.sort(key=lambda x: x["bookmarks"], reverse=True)
            if balanced:
                # 随机抽取：从「满足门槛且按收藏降序」的热门候选里随机取 limit 张，
                # 不固定保底第一名，同一关键词每次结果不同，靠后的好图也有机会。
                return sample_random(pool, limit_n)
            if not pool and min_bookmarks > 0:
                pool = filter_illusts(items, r18_level, 0, filter_ai, None)
                pool.sort(key=lambda x: x["bookmarks"], reverse=True)
            return pool[:limit_n]
        # 非会员（或 date 排序）：popular_desc 会被静默降级为最新，
        # 拉两页后由客户端按收藏数降序兜底；date_desc 保持 API 顺序（最新）。
        items: list[dict] = []
        for off in (offset, offset + 30):
            params = search_params(word, scope=scope, sort=sort, filter_ai=filter_ai, offset=off)
            resp = await self._request("GET", f"{APP_API_HOST}/v1/search/illust", params=params)
            page = resp.get("illusts") or []
            items.extend(page)
            if not resp.get("next_url"):
                break
        out = filter_illusts(items, r18_level, min_bookmarks, filter_ai, None)
        if sort == "popular_desc":
            out.sort(key=lambda x: x["bookmarks"], reverse=True)
            if balanced:
                out = sample_random(out, limit_n)
            else:
                out = out[:limit_n]
        if not out and min_bookmarks > 0:
            # 软降级：门槛过严/排序受限时，退而展示相对最热候选
            out = filter_illusts(items, r18_level, 0, filter_ai, None)
            if sort == "popular_desc":
                out.sort(key=lambda x: x["bookmarks"], reverse=True)
                if balanced:
                    out = sample_random(out, limit_n)
                else:
                    out = out[:limit_n]
        return out[:limit_n]

    async def illust_ranking(
        self,
        mode: str = "daily",
        r18_level: str = "safe",
        min_bookmarks: int = 0,
        filter_ai: bool = True,
        limit: int = 5,
        date: str | None = None,
    ) -> list[dict[str, Any]]:
        await self.ensure_auth()
        params = ranking_params(mode, date)
        resp = await self._request("GET", f"{APP_API_HOST}/v1/illust/ranking", params=params)
        items = resp.get("illusts") or []
        return filter_illusts(items, r18_level, min_bookmarks, filter_ai, limit)

    async def search_user(self, word: str, limit: int = 5) -> list[dict[str, Any]]:
        await self.ensure_auth()
        params = {"word": word, "filter": "for_android"}
        resp = await self._request("GET", f"{APP_API_HOST}/v1/search/user", params=params)
        users = resp.get("user_previews") or []
        out = []
        for u in users[: max(1, int(limit))]:
            user = u.get("user") or {}
            out.append({
                "id": int(user.get("id") or 0),
                "name": str(user.get("name") or "未知画师"),
                "account": str(user.get("account") or ""),
                "comment": str(user.get("comment") or ""),
                "url": f"https://www.pixiv.net/users/{user.get('id') or ''}",
            })
        return out

    async def user_illusts(
        self,
        user_id: int | str,
        r18_level: str = "safe",
        min_bookmarks: int = 0,
        filter_ai: bool = True,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        await self.ensure_auth()
        params = user_illusts_params(user_id)
        resp = await self._request("GET", f"{APP_API_HOST}/v1/user/illusts", params=params)
        items = resp.get("illusts") or []
        return filter_illusts(items, r18_level, min_bookmarks, filter_ai, limit)

    async def related_illusts(
        self,
        illust_id: int | str,
        r18_level: str = "safe",
        min_bookmarks: int = 0,
        filter_ai: bool = True,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """按作品 ID 获取相关/相似插画（保持 API 相关度顺序，门槛软降级）。"""
        await self.ensure_auth()
        params = {"illust_id": str(illust_id), "filter": "for_android"}
        resp = await self._request("GET", f"{APP_API_HOST}/v2/illust/related", params=params)
        items = resp.get("illusts") or []
        out = filter_illusts(items, r18_level, min_bookmarks, filter_ai, None)
        if not out and min_bookmarks > 0:
            out = filter_illusts(items, r18_level, 0, filter_ai, None)
        return out[: max(1, int(limit))]

    async def illust_detail(self, illust_id: int | str) -> dict[str, Any] | None:
        await self.ensure_auth()
        params = {"illust_id": str(illust_id)}
        resp = await self._request("GET", f"{APP_API_HOST}/v1/illust/detail", params=params)
        item = (resp.get("illust") or {}).get("illust") or {}
        if not item:
            return None
        return parse_illust(item)

    async def download_image(
        self,
        url: str,
        dest_path: str,
        override_referer: str = "",
    ) -> str:
        """下载图片到本地（i.pximg.net 需要 Referer/UA），返回保存路径。"""
        if not url:
            raise PixivError("图片 URL 为空")
        session = await self._session_get()
        headers = {
            "Referer": override_referer or IMAGE_REFERER,
            "User-Agent": APP_UA,
        }
        try:
            async with session.get(url, headers=headers,
                                   proxy=self.proxy if self.proxy else None) as resp:
                if resp.status < 200 or resp.status >= 300:
                    raise PixivError(f"图片下载失败 HTTP {resp.status}")
                os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                tmp = dest_path + ".part"
                with open(tmp, "wb") as f:  # noqa: ASYNC230
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        f.write(chunk)
                os.replace(tmp, dest_path)
        except PixivError:
            raise
        except Exception as exc:
            raise PixivError(f"图片下载失败：{exc}") from exc
        return dest_path


def _extract_api_error(body: str, status: int) -> str:
    """从 Pixiv 错误响应提取可读文案。"""
    try:
        d = json.loads(body)
        err = d.get("error") or {}
        if isinstance(err, dict):
            msg = err.get("user_message") or err.get("message") or str(err)
        else:
            msg = str(d.get("error") or body[:120])
        return str(msg)[:200]
    except Exception:  # noqa: BLE001
        return body[:120]


# ---------------------------------------------------------------------------
# 图片元数据规范化：仅改写容器元数据（EXIF/注释/文本块），像素数据零改动
# ---------------------------------------------------------------------------
# JPEG: 移除 APP1(EXIF)/APP2(ICC/XMP)/COM 等段，SOF/SOS/熵编码数据原样保留；
# PNG : 移除 tEXt/iTXt/zTXt/tIME/eXIf 等辅助块，IDAT 像素块原样保留；
# 其他格式（GIF/WebP 等）原样返回。输出与输入同路径（原地覆盖）。
_JPEG_SKIP_MARKERS = {0xE1, 0xE2, 0xED, 0xFE}  # APP1/APP2/XMP/COM


def strip_image_metadata(path: str) -> str:
    """原地规范化图片元数据（像素与尺寸不变），返回处理后的路径。"""
    with open(path, "rb") as f:
        data = f.read()
    if data[:3] == b"\xff\xd8\xff":
        rewritten = _strip_jpeg(data)
    elif data[:8] == b"\x89PNG\r\n\x1a\n":
        rewritten = _strip_png(data)
    else:
        return path
    if rewritten and rewritten != data:
        with open(path, "wb") as f:
            f.write(rewritten)
    return path


def _strip_jpeg(data: bytes) -> bytes:
    out = bytearray(b"\xff\xd8")
    i = 2
    n = len(data)
    while i < n:
        if data[i] != 0xFF:
            # 熵编码数据段（SOS 之后）：原样拷贝到结尾
            out += data[i:]
            break
        marker = data[i + 1]
        if marker == 0xD9:  # EOI
            out += b"\xff\xd9"
            break
        if marker == 0xDA:  # SOS：段头 + 剩余熵数据
            seg_len = int.from_bytes(data[i + 2:i + 4], "big")
            out += data[i:i + 2 + seg_len]
            out += data[i + 2 + seg_len:]
            break
        seg_len = int.from_bytes(data[i + 2:i + 4], "big")
        if marker in _JPEG_SKIP_MARKERS:
            i += 2 + seg_len  # 丢弃元数据段
            continue
        out += data[i:i + 2 + seg_len]
        i += 2 + seg_len
    return bytes(out)


_PNG_TEXT_TYPES = {b"tEXt", b"iTXt", b"zTXt", b"tIME", b"eXIf"}


def _strip_png(data: bytes) -> bytes:
    if len(data) < 33:
        return data
    out = bytearray(data[:8])  # 签名原样
    i = 8
    n = len(data)
    while i + 12 <= n:
        chunk_len = int.from_bytes(data[i:i + 4], "big")
        chunk_type = data[i + 4:i + 8]
        total = 12 + chunk_len
        if i + total > n:
            break
        if chunk_type in _PNG_TEXT_TYPES:
            i += total
            continue
        out += data[i:i + total]
        i += total
    return bytes(out)