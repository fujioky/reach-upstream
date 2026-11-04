# reach-upstream：Reach 的 HTTP/MCP 代理

[English](./README.md) · 简体中文

**所不及者，可达于人。** 别人够不着的内容，由 Reach 送到他面前。

[Reach](https://github.com/fujioky/reach) 把有些读者打不开的平台上的帖子，镜像成谁都能打开的链接；本仓库就是其中「够过去、取回来」的那一半。它调用各平台的命令行工具——[`twitter-cli`](https://github.com/jackwener/twitter-cli)、[`xiaohongshu-cli`](https://github.com/jackwener/xiaohongshu-cli)、[`yt-dlp`](https://github.com/yt-dlp/yt-dlp)、[Telethon](https://github.com/LonamiWebs/Telethon)，以及经 [mcporter](https://github.com/steipete/mcporter) 调用的 Exa——把返回结果整理成统一结构，通过一个朴素 JSON 接口和一个带 OAuth 的 MCP 服务提供出去。Reach 必须先部署它；ChatGPT、Claude 等 MCP 客户端也能脱离 Reach 单独使用。

## 代理补了什么

**1. 针对转存场景定制的解析。** 所有平台都返回同一种结构：

- `id`、`url`、`title`、`content`、`author {id, name, handle, url, avatar_url}`、`created_at`（ISO 8601）、`stats {likes, comments, reposts, views, collects}`、`images[]`、`comments[]`。
- **X**：从 `twitter-cli` 返回的整段会话里按 status id 选出目标推文，其后的回复作为评论（至多 `COMMENT_LIMIT` 条），回复里的图片一并带上。每条推文都带 `in_reply_to`，回复的回复可以按层级嵌套；item 还带 `parents`（它所回复的推文，从根帖开始）和 `parent_missing`（链条没到根帖时下一条的 id）。`twitter-cli` 会丢掉回复对象字段，所以由 `scripts/x_tweet.py` 代跑它的 `tweet` 命令并把这个字段补回来。X 长文（`x_article: true`）取文章标题和正文，内嵌图片以 Markdown 图片行留在原位置。保留 `video.twimg.com` 视频地址，视频带字幕轨时一并提取。搜索返回同样精简的推文。
- **YouTube**：全部渐进式 `video_sources[]`（清晰度、url、ext、format_id、大小、有无音视频，DASH 仅作兜底）、评论、频道头像、`duration`、缩略图。
- **字幕是数据**：`transcript`、`transcript_lang`、`transcript_vtt`（规范化的带时间轴 WebVTT，可直接喂给 `<track>`）。优先级：人工中文 › 自动中文（仅中文音频）› 人工英文 › 自动英文 › 原生语言轨。YouTube 的自动*翻译*轨有意排除，下游拿原文翻译效果好得多。
- **Telegram**（`t.me/<频道>/<id>` 与私有的 `t.me/c/<id>/<id>`）：用用户账号会话读取，开了「禁止保存内容」的群，成员账号照样能读。一组相册、或群里同一人连发的几条，合并成一个 item 返回。媒体没有公网地址，经 `/tgfile` 流式转发。
- **小红书**：短链展开、笔记加全部评论，图片 base64 内联，按变体 / 字节 / 感知哈希去重，超限压缩。
- **网页**：链接走 Jina Reader，搜索走 Exa。
- **结构化错误**：任何失败都变成 `{stage, kind, message}`，`kind` ∈ `rate_limited | not_found | auth_required | unsupported | not_configured | timeout | internal`，并映射到对应 HTTP 状态。原始 stderr 不出机器。

**2. 公开访问。**

- `GET /http/?platform=x|youtube|xhs|tg|web&query=<链接或文本>&pwd=<口令>` + `GET /healthz`：Reach 所用的 JSON 契约，口令保护。
- **MCP 服务** `/mcp`（Streamable HTTP，无状态 JSON）：OpenAI 兼容形态的 `search` / `fetch`，外加 `reach`、`agent_reach`、`agent_reach_health`。支持 Bearer / API-key 鉴权、**OAuth 2.1 授权码 + PKCE 与动态客户端注册**（内置信任 ChatGPT 与 Claude 回调）、`.well-known` 元数据、授权页可选 Cloudflare Turnstile、DNS 重绑定防护。把 `https://<你的域名>/mcp` 加为连接器，就能在 ChatGPT / Claude 里直接用。

**3. 安全与隔离。** 每次图片下载都过 SSRF 防护（仅公网 IP，跳转逐跳复验）。`scripts/reach.sh` 把 `HOME` 和 XDG 目录钉在 `runtime/home`，CLI 全部装进 `runtime/venv`，命令壳在 `runtime/bin`，不碰真实家目录、不注册全局命令。

## 运行

前置：Python 3.10+、[uv](https://docs.astral.sh/uv/)。网页搜索另需 Node.js 和 `npm i -g mcporter`（Exa 配置在 `config/mcporter.json`，不需要 key）。

```bash
git clone https://github.com/fujioky/reach-upstream.git
cd reach-upstream
cp .env.example .env            # 填 AGENT_REACH_PROXY_PASSWORD；不填代理拒绝启动
./scripts/reach.sh setup        # runtime/venv：twitter-cli、xiaohongshu-cli、yt-dlp、telethon、mcp……
./scripts/reach.sh start
./scripts/reach.sh health
```

需要登录态的平台，凭据都放在隔离运行时里：

- **X**：从已登录 x.com 的浏览器里取 `auth_token` 和 `ct0`，填进 `.env` 的 `TWITTER_AUTH_TOKEN` / `TWITTER_CT0`。用 `./scripts/reach.sh cmd twitter whoami` 检查。
- **小红书**：`runtime/bin/xhs login`。
- **Telegram**（可选）：在 [my.telegram.org](https://my.telegram.org) 建一个应用，填 `AGENT_REACH_TG_API_ID` / `AGENT_REACH_TG_API_HASH`，再用手机号登录一次生成会话文件：

  ```bash
  mkdir -p runtime/telegram
  ./scripts/reach.sh cmd runtime/venv/bin/python -c 'import os; from telethon.sync import TelegramClient; TelegramClient("runtime/telegram/reach", int(os.environ["AGENT_REACH_TG_API_ID"]), os.environ["AGENT_REACH_TG_API_HASH"]).start()'
  ```

  会话文件同一时间只能被一个进程打开，别处要用时先停代理。
- YouTube 与网页不需要任何配置。

对外暴露方式随意（Cloudflare Tunnel、nginx、Tailscale），并把 `AGENT_REACH_PROXY_PUBLIC_URL` 设成对外地址：MCP OAuth 元数据和 `/tgfile` 媒体链接都用它。然后把 Reach 指过来：`AGENT_REACH_BASE_URL=https://your-proxy.example.com`、`AGENT_REACH_PWD=<口令>`，或在 **后台 → 系统设置 → 抓取端接口** 填同样两项。

其它命令：`run`（前台）、`stop`、`restart`、`status`、`logs [n]`、`sync-commands`、`cmd <命令…>`（在隔离运行时里执行命令）。用 systemd 托管时要用 `systemctl` 重启，别直接跑 `reach.sh`，否则 systemd 会跟丢进程。

### 升级 CLI

重新跑 `./scripts/reach.sh setup`。`requirements.txt` 把 `twitter-cli` 钉在一个 GitHub 提交上：PyPI 停在 0.8.5，X 在 2026 年 9 月改版后它的 `search` 会返回 HTTP 404。升级后每个平台用一个链接和一次搜索各测一遍。

## 接口

```
GET /healthz                          → { "ok": true, "service": "reach-proxy", "platforms": [...], ... }
GET /http/?platform=<p>&query=<q>&pwd=<口令>
    链接查询 → { "ok": true, "type": "item",   "item": {...},   "errors": [] }
    文本查询 → { "ok": true, "type": "search", "results": [...], "errors": [] }
    失败     → { "ok": false, "errors": [{ "stage": "youtube_info", "kind": "not_found", "message": "..." }] }（按 kind 返回 4xx/5xx）
    &single=1 → 仅 Telegram：只取链接那一条，不做相册 / 连发合并
GET /tgfile?priv=<id>|user=<名>&msg=<id>&pwd=<口令>   Telegram 媒体原始字节（链接已包含在 item 里）
POST /mcp                             Streamable HTTP MCP（Bearer 令牌或 OAuth）
GET  /.well-known/oauth-*             OAuth 元数据；/oauth/register、/oauth/authorize、/oauth/token
```

`platform` 别名：`twitter`→`x`、`xiaohongshu`/`rednote`→`xhs`、`yt`→`youtube`、`telegram`→`tg`。

## 配置

除口令外都是可选项，见 `.env.example`。

| 键 | 默认 | 含义 |
| --- | --- | --- |
| `AGENT_REACH_PROXY_PASSWORD` | — | 必填；为空时代理启动即退出。 |
| `AGENT_REACH_PROXY_PUBLIC_URL` | `http://127.0.0.1:18280` | 对外地址，用于 MCP OAuth 元数据和 `/tgfile` 链接。 |
| `AGENT_REACH_MCP_TOKEN` | 口令 | `/mcp` 的静态 Bearer；`AGENT_REACH_MCP_AUTH_REQUIRED=0` 关闭鉴权（仅限本机）。 |
| `AGENT_REACH_MCP_PUBLIC_DISCOVERY` | `1` | `initialize` / `tools/list` 免鉴权。 |
| `AGENT_REACH_TURNSTILE_SITE_KEY` / `_SECRET_KEY` | — | OAuth 授权页的 Turnstile 校验。 |
| `TWITTER_AUTH_TOKEN` / `TWITTER_CT0` | — | 给 `twitter-cli` 用的 X 会话 Cookie。 |
| `AGENT_REACH_TG_API_ID` / `_API_HASH` / `_SESSION` | — / — / `runtime/telegram/reach.session` | Telegram 应用与用户会话。 |
| `AGENT_REACH_TG_RUN_GAP_SECONDS` / `_RUN_MAX` | 600 / 30 | 群里同一人连发多条时的合并规则。 |
| `AGENT_REACH_PROXY_SEARCH_LIMIT` / `_COMMENT_LIMIT` | 15 / 50 | 结果数与评论数上限。 |
| `AGENT_REACH_MCP_IMAGE_MAX_BYTES` / `_DOWNLOAD_MAX_BYTES` | 4 MB / 32 MB | 内联图片压缩目标与原图下载上限。 |
| `AGENT_REACH_RUNTIME_DIR`、`_VENV`、`_BIN_DIR`、`_HOME`、`_NODE_BIN` | `runtime/` 下 | 迁移隔离运行时的位置。 |

代理就是一个文件 `agent_reach_proxy.py`（Starlette + uvicorn + 官方 `mcp` SDK）。各 CLI 以子进程方式运行，环境来自 `command_env()`。

## 许可

MIT。
