# P站找图（siwu-pixiv-image · AstrBot 插件）

让机器人访问 Pixiv 找图：关键词搜图、每日/每周排行榜、画师作品、指定作品详情、相似图推荐。
拆成五个 Agent 工具，由 LLM 根据用户提问自动路由（与 siwu-image-search 同一套多工具架构）。

## 功能特性

- **多工具 + LLM 路由**：
  - `siwu_pixiv_search`：关键词搜图，默认「均衡混排」采样
  - `siwu_pixiv_ranking`：每日/每周/每月排行榜（用户没指定目标时发热门榜）
  - `siwu_pixiv_artist`：画师名/IP 搜作品
  - `siwu_pixiv_detail`：pixiv 作品链接/ID 查详情
  - `siwu_pixiv_related`：按作品 ID 找相似/相关插画
- **热门度门槛**：低于 `pixiv_min_bookmarks`（默认 1000）收藏的作品直接剔除（会员账号开启 `pixiv_premium` 后由后端热门排序保证；非会员自动拉取两页后客户端按收藏降序，并如实提示未达门槛）
- **均衡混排（默认开启，`pixiv_search_balanced`）**：热门搜索不再永远返回同一批顶级图，且**保证全部结果都是热门池里的高人气图**——首位保底、中段均匀步进采样，按小时轮换 30/60/90 深层页；仅当热门候选不足要补齐数量时，才用最新发布的新作兜底（不会在热门充足时混入低收藏新图）。关闭该配置则严格按收藏热度直取
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
| `pixiv_premium` | Pixiv 高级会员：开启后搜索使用后端热门排序（popular_desc）单页直取；关闭则拉两页按收藏降序兜底（非会员限制） | false |
| `pixiv_search_balanced` | 热门搜索均衡混排（热门头部+中段+最新新作，按小时轮换）；关闭则严格按收藏热度返回 | true |

## 获取 refresh_token（Pixiv App-API 登录凭证）

插件通过 refresh_token 换取 access_token（有效约 1 小时），并自动用服务端轮换的新 refresh_token 续期。
**建议注册专用小号**（注册后到 https://www.pixiv.net/settings 打开 R-18/R-18G 显示开关；插件默认 safe 档仍只发一般向，开启不影响）。

### 方式一：gppt 命令行自动登录（推荐，需要账号密码）

```bash
pip install gppt
gppt configure          # 交互式输入 Pixiv 邮箱/密码（可存 OTP 密钥，双向验证也能过）
gppt login --e2e        # 自动驱动浏览器登录并打印 refresh_token
```

说明：首次运行 `--e2e` 会自动下载 Chromium 内核（约 120MB，需能访问网络的机器）；
若出现图片验证码，gppt 会在浏览器窗口中等待人工填写（桌面环境直接看窗口）。

### 方式二：OAuth 授权码（免密码，适合浏览器已登录 Pixiv）

```bash
gppt login --oauth
```

gppt 会打印一个授权链接：用浏览器打开 → 登录（小号）→ 允许授权 → 浏览器地址栏出现 `pixiv://...?code=xxxx` → 把整段 code 粘贴回终端，得到 refresh_token。

### 方式三：从浏览器 Cookie 导入（旧版 gppt 提供 `gppt cookie`，5.x 已移除）

如使用旧版 gppt（如 4.x）：`gppt cookie --browser chrome` 可直接从已登录的浏览器读取；
注意导出的 token 属于**浏览器当前登录的账号**，需要小号就先小号登录。

> 拿到 refresh_token 后：填入插件配置 `pixiv_refresh_token`，插件数据目录会缓存轮换后的 token（`data/pixiv_image/token.json`）；
> token 一旦泄露可到 pixiv 设置页注销全部客户端授权使其失效。

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
├── main.py            # 插件入口：5 个 llm_tool + 意图钩子 + 命令兜底
├── pixiv_api.py       # Pixiv App-API 客户端（OAuth/搜索/排行/画师/详情/下载）
├── _conf_schema.json  # 配置 schema
├── metadata.yaml      # 插件元数据
├── requirements.txt   # aiohttp
├── build.py           # 打包脚本 → dist/siwu-pixiv-image-<version>.zip
└── tests/             # 单元测试（mock 网络层，无网络依赖）
```

## 开发与测试

```bash
python -m unittest discover -s tests -v   # 全绿，无网络依赖
ruff check .                              # lint 干净
python build.py                           # 产出 dist/siwu-pixiv-image-0.1.0.zip
```

## 版本历史

- v0.2.9：均衡混排改为热门池优先：结果全部来自热门池（首位保底+中段步进），不再混入低收藏最新新作；最新候选仅在热门不足时兜底补齐。
- v0.2.8：均衡混排自适应页数：第 0 页热门保底，仅当结果充足（next_url 存在）才按小时轮换追加深层页，冷门关键词不再越界拉空。
- v0.2.7：均衡混排搜索（默认开启）：热门头部+中段+最新新作混合采样、按小时轮换页起点，同一关键词多次搜索不重复，靠后的好图也能出现；新增 `pixiv_search_balanced` 配置可关闭。
- v0.2.5：会员模式 `pixiv_premium`：会员账号使用后端热门排序，非会员拉两页按收藏降序兜底。
- v0.2.2：搜索接口修正为 /v1/search/illust。
- v0.2.0：R18 分群管理（白名单账号指令开/关、群白名单、会话级持久化）；插件改名去个性化称呼（P站找图）。
- v0.1.0：首个版本。四工具 + LLM 路由、OAuth 自动续期、三档 R18、收藏门槛、代理、本地发图。

## 相关

- 同系列插件：[siwu-image-search（动漫识图三合一）](https://github.com/siwuli/AstrBot_siwu-image-search)