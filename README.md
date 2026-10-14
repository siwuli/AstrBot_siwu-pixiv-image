# P站找图（pixiv_image）

Pixiv 找图：关键词搜图、排行榜、画师作品、作品详情与相似图，收藏门槛过滤与 R18 分级控制。

## 功能
- 关键词搜图：默认从满足收藏门槛的热门候选里随机抽取
- 排行榜：每日/每周/每月，未指定时用配置的默认模式
- 画师作品、作品详情（链接或 ID）、相似图推荐
- 收藏门槛过滤，默认 ≥1000 收藏，排行榜不受限
- R18 五档：safe（默认）/ r18 / r18only / r18gonly / r18g
- 图片直发，支持 HTTP 代理与 AI 作品过滤

## 指令
- `/pixiv search 关键词`：搜图，如 `/pixiv search miku`
- `/pixiv rank`：今日排行榜
- `/pixiv r18 status|on|only|gonly|all|off`：会话级 R18 开关（仅白名单账号）
- `/pixiv help`：显示全部指令

## 配置
| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `pixiv_enabled` | true | 插件总开关 |
| `pixiv_refresh_token` | 空 | Pixiv 登录凭证，见下方小节 |
| `pixiv_proxy` | 空 | HTTP 代理地址，留空直连 |
| `pixiv_r18_level` | safe | R18 档位，默认仅一般向 |
| `pixiv_min_bookmarks` | 1000 | 搜索最低收藏数门槛 |
| `pixiv_max_results` | 3 | 搜索/排行最多返回条数 |
| `pixiv_send_images` | 1 | 每轮最多直发图片数 |
| `pixiv_rank_default` | daily | 默认排行模式 |
| `pixiv_filter_ai` | true | 搜索时过滤 AI 作品 |
| `pixiv_send_original` | false | 发送原图，关闭则发 1200px |
| `pixiv_send_fallback` | true | 发图失败自动重试一次 |
| `pixiv_send_rewrite_meta` | false | 发送前删除图片元数据 |
| `pixiv_r18_owners` | 空 | 可用 R18 指令的账号白名单 |
| `pixiv_r18_groups` | 空 | 允许开启 R18 的群白名单 |
| `pixiv_premium` | false | 会员账号，用后端热门排序 |
| `pixiv_search_balanced` | true | 从热门候选随机抽取 |

## 安装
1. 上传插件 zip 到 AstrBot 插件管理并启用
2. 依赖 `aiohttp`，安装插件时自动安装
3. 填入 `pixiv_refresh_token`，按需配置 `pixiv_proxy`

## 获取 refresh_token（使用必读）
1. `pip install gppt`
2. `gppt configure` 交互输入 Pixiv 邮箱与密码
3. `gppt login --e2e` 自动登录并打印 refresh_token（首次会下载 Chromium 内核）
4. 或 `gppt login --oauth` 浏览器授权后把 code 粘贴回终端
5. 把得到的 refresh_token 填入配置 `pixiv_refresh_token`，建议用专用小号
6. 更换账号后填入新 token 并重启，插件会自动作废旧缓存

## R18 说明
- 默认 safe（仅一般向）；群聊按群共享档位，私聊各人独立
- 权限只认 `pixiv_r18_owners` 白名单，群聊还需群号在 `pixiv_r18_groups` 内
- 群聊发 R18 图有平台风控风险，建议仅私聊或熟人小群使用

## 版本记录
| 版本 | 说明 |
| --- | --- |
| 0.2.24 | 精简文档 |
| 0.2.23 | R18 状态改为群维度共享 |
| 0.2.22 | 搜索改为热门候选随机抽取 |
| 0.2.20 | 发送前可删除图片元数据 |
| 0.2.19 | 发图失败自动重试一次 |

## 项目地址
<https://github.com/siwuli/AstrBot_siwu-pixiv-image>
