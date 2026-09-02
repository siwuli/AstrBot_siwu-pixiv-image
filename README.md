# P站找图（siwu-pixiv-image · AstrBot 插件）

让机器人访问 Pixiv 找图：关键词搜图、每日/每周排行榜、画师作品、指定作品详情。
拆成四个 Agent 工具，由 LLM 根据用户提问自动路由（与 siwu-image-search 同一套多工具架构）。

## 功能特性

- **多工具 + LLM 路由**：
  - `siwu_pixiv_search`：关键词搜图，默认按收藏热度降序
  - `siwu_pixiv_ranking`：每日/每周/每月排行榜（用户没指定目标时发热门榜）
  - `siwu_pixiv_artist`：画师名/IP 搜作品
  - `siwu_pixiv_detail`：pixiv 作品链接/ID 查详情
- **热门度门槛**：搜索按 `total_bookmarks` 降序，低于 `pixiv_min_bookmarks`（默认 1000）直接剔除
- **R18 三档**：`safe`（默认，仅一般向）/ `r18`（允许 R-18，拒绝 R-18G）/ `r18g`（全放行）
- **refresh_token 鉴权**：Pixiv App-API OAuth 自动续期，轮换后的 token 缓存到 AstrBot 数据目录
- **代理配置**：`pixiv_proxy` 支持 http/https 代理（Clash 混合端口 7890 亦可）
- **图片直发**：图片经代理下载到本地后用 `Image.fromFileSystem` 发送（i.pximg.net 需 Referer，QQ 客户端直连不可靠）
- **AI 作品过滤**：默认过滤 AI 生成作品（`pixiv_filter_ai`，可关闭）

## 配置项

| 配置 | 说明 | 默认 |
| --- | --- | --- |
| `pixiv_enabled` | 启用插件 | true |
| `pixiv_refresh_token` | Pixiv OAuth refresh_token（必填） | 空 |
| `pixiv_proxy` | HTTP 代理地址，如 `http://127.0.0.1:7890` | 空（直连） |
| `pixiv_r18_level` | 全局 R18 档位：safe / r18 / r18g | safe |
| `pixiv_r18_owners` | R18 白名单账号 QQ 号（逗号分隔），只有他们能用 `/pixiv r18 on` 开启 | 空 |
| `pixiv_r18_groups` | 允许开启 R18 的群号白名单（逗号分隔），空=任何群都不能开；私聊不受限 | 空 |
| `pixiv_min_bookmarks` | 最低收藏数（搜索/画师工具） | 1000 |
| `pixiv_max_results` | 返回候选条数 1~5 | 3 |
| `pixiv_send_images` | 每轮直发图片数 1~5 | 1 |
| `pixiv_rank_default` | 默认排行模式 | daily |
| `pixiv_filter_ai` | 过滤 AI 作品 | true |
| `pixiv_send_original` | 发送原图（关闭则发 1200px 大图） | false |

## 获取 refresh_token

1. 注册/登录 Pixiv 账号；
2. 使用开源工具（如 [gppt](https://github.com/eggplants/gppt)、浏览器控制台抓取 OAuth 回调）换取 refresh_token；
3. 填入插件配置 `pixiv_refresh_token`；
4. 插件首次调用会自动换取 access_token 并缓存（token 轮换后写 `data/pixiv_image/token.json`）。

> 提示：Pixiv 对中国大陆网络不可直连，请务必配置可达的代理；服务器场景建议在本机部署 Clash/mihomo 后填 `http://127.0.0.1:7890`。

## R18 分群管理

- 插件默认 `safe`（仅一般向）；**分群独立控制**：某群/会话的档位由白名单账号用指令切换，状态持久化（重启不丢）；
- 权限只认配置的 `pixiv_r18_owners` 白名单，**群主/管理员身份不生效**；群聊还需群号在 `pixiv_r18_groups` 内（配置为空则任何群都不能开），私聊只需账号白名单；
- 指令：`/pixiv r18 status`（查看当前会话档位）、`/pixiv r18 on`（开启 R-18，仍拒绝 R-18G）、`/pixiv r18 all`（R-18G 全放行）、`/pixiv r18 off`（关闭，回落到全局配置）；
- 群聊发 R18 图有平台风控风险，建议仅私聊/熟人小群使用。

## 使用示例

- 「P站今日排行前十」→ siwu_pixiv_ranking
- 「来几张 miku 的图」→ siwu_pixiv_search
- 「画师 望月けい 的作品」→ siwu_pixiv_artist
- 「https://www.pixiv.net/artworks/123456」→ siwu_pixiv_detail
- 兜底命令：`/pixiv miku`（搜图）、`/pixiv 排行`（排行榜）

## 发图机制

针对手机 QQ 图文不能同时发送的限制：先 `event.send(Image.fromFileSystem(...))` 直发图片，再 yield 结构化文本由主 LLM 组织最终回复；
`on_llm_request` 钩子在命中「pixiv/P站/找图/排行/画师」意图时注入路由指令并移除 `send_message_to_user`，避免重复发送。

## 模块结构

```text
siwu-pixiv-image-1_0/
├── main.py            # 插件入口：4 个 llm_tool + 意图钩子 + 命令兜底
├── pixiv_api.py       # Pixiv App-API 客户端（OAuth/搜索/排行/画师/详情/下载）
├── _conf_schema.json  # 配置 schema
├── metadata.yaml      # 插件元数据
├── requirements.txt   # aiohttp
├── build.py           # 打包脚本 → dist/siwu-pixiv-image-<version>.zip
└── tests/             # 单元测试（30 例，mock 网络层）
```

## 开发与测试

```bash
python -m unittest discover -s tests -v   # 30 例全绿，无网络依赖
ruff check .                              # lint 干净
python build.py                           # 产出 dist/siwu-pixiv-image-0.1.0.zip
```

## 版本历史

- v0.2.0：R18 分群管理（白名单账号指令开/关、群白名单、会话级持久化）；插件改名去个性化称呼（P站找图）。
- v0.1.0：首个版本。四工具 + LLM 路由、OAuth 自动续期、三档 R18、收藏门槛、代理、本地发图。

## 相关

- 同系列插件：[siwu-image-search（动漫识图三合一）](https://github.com/siwuli/AstrBot_siwu-image-search)