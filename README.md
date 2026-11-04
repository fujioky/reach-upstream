# reach-upstream — Reach's HTTP/MCP proxy

English · [简体中文](./README.zh-CN.md)

**所不及者，可达于人。** What people cannot reach, Reach brings to them.

[Reach](https://github.com/fujioky/reach) mirrors posts from platforms some readers cannot open into links anyone can, and this repository is the half that goes out and gets them. It runs the per-platform CLIs ([`twitter-cli`](https://github.com/jackwener/twitter-cli), [`xiaohongshu-cli`](https://github.com/jackwener/xiaohongshu-cli), [`yt-dlp`](https://github.com/yt-dlp/yt-dlp), [Telethon](https://github.com/LonamiWebs/Telethon), and Exa through [mcporter](https://github.com/steipete/mcporter)) and turns what they return into one item shape, served over a plain JSON endpoint and an MCP server with OAuth. Reach needs it; ChatGPT, Claude and other MCP clients can use it on their own.

## What it adds

**1. Parsing tailored for re-hosting.** Every platform answers with the same item:

- `id`, `url`, `title`, `content`, `author {id, name, handle, url, avatar_url}`, `created_at` (ISO 8601), `stats {likes, comments, reposts, views, collects}`, `images[]`, `comments[]`.
- **X**: the focal tweet picked by status id out of the conversation `twitter-cli` returns, then its replies (up to `COMMENT_LIMIT`) with their own images. X Articles become the article title and body, with inline images kept in place as Markdown image lines. Video URLs (`video.twimg.com`) are kept, and a subtitle track is extracted when the video has one. Search returns the same compact tweets.
- **YouTube**: every progressive `video_sources[]` (quality, url, ext, format_id, filesize, has_video/has_audio, DASH only as fallback), comments, channel avatar, `duration`, thumbnails.
- **Subtitles as data**: `transcript`, `transcript_lang`, and `transcript_vtt`, a normalised timed WebVTT ready for a `<track>` element. Priority: manual Chinese › auto Chinese (Chinese audio only) › manual English › auto English › native-language track. YouTube's auto-*translated* tracks are excluded because translating the original downstream is far better.
- **Telegram** (`t.me/<channel>/<id>` and private `t.me/c/<id>/<id>`): read with a user session, so groups with "restrict saving content" are readable by any member account. An album, or several messages in a row from the same sender in a group, comes back as one item. Media has no public URL and is streamed through `/tgfile`.
- **Xiaohongshu**: short-link expansion, note + every comment, images inlined as base64, de-duplicated by variant / bytes / perceptual hash and compressed to a size cap.
- **Web**: Jina Reader for URLs, Exa for search.
- **Structured errors**: every failure becomes `{stage, kind, message}` with `kind` ∈ `rate_limited | not_found | auth_required | unsupported | not_configured | timeout | internal` and a matching HTTP status. Raw stderr never leaves the box.

**2. Public access.**

- `GET /http/?platform=x|youtube|xhs|tg|web&query=<url or text>&pwd=<password>` + `GET /healthz`: the JSON contract Reach consumes, guarded by a shared password.
- **MCP server** at `/mcp` (Streamable HTTP, stateless JSON): `search` / `fetch` in the OpenAI-compatible shape plus `reach`, `agent_reach`, `agent_reach_health`. Bearer / API-key auth, **OAuth 2.1 authorization code + PKCE with dynamic client registration** (ChatGPT and Claude callbacks trusted), `.well-known` metadata, optional Cloudflare Turnstile on the authorize page, DNS-rebinding protection. Add `https://<your-host>/mcp` as a connector to use it from ChatGPT or Claude.

**3. Safety and isolation.** Every image download passes an SSRF guard (public IPs only, each redirect re-validated). `scripts/reach.sh` pins `HOME` and the XDG dirs under `runtime/home`, installs the CLIs into `runtime/venv`, puts shims in `runtime/bin`, and never touches your real home directory or global PATH.

## Run

Prerequisites: Python 3.10+ and [uv](https://docs.astral.sh/uv/). Web search also needs Node.js and `npm i -g mcporter` (Exa is configured in `config/mcporter.json`, no key needed).

```bash
git clone https://github.com/fujioky/reach-upstream.git
cd reach-upstream
cp .env.example .env            # set AGENT_REACH_PROXY_PASSWORD; the proxy refuses to start without it
./scripts/reach.sh setup        # runtime/venv: twitter-cli, xiaohongshu-cli, yt-dlp, telethon, mcp…
./scripts/reach.sh start
./scripts/reach.sh health
```

Platforms that need a login keep their credentials inside the isolated runtime:

- **X**: put `auth_token` and `ct0` from a logged-in x.com browser session into `TWITTER_AUTH_TOKEN` / `TWITTER_CT0` in `.env`. Check with `./scripts/reach.sh cmd twitter whoami`.
- **Xiaohongshu**: `runtime/bin/xhs login`.
- **Telegram** (optional): create an app at [my.telegram.org](https://my.telegram.org), set `AGENT_REACH_TG_API_ID` / `AGENT_REACH_TG_API_HASH`, then log in once with your phone number to create the session file:

  ```bash
  mkdir -p runtime/telegram
  ./scripts/reach.sh cmd runtime/venv/bin/python -c 'import os; from telethon.sync import TelegramClient; TelegramClient("runtime/telegram/reach", int(os.environ["AGENT_REACH_TG_API_ID"]), os.environ["AGENT_REACH_TG_API_HASH"]).start()'
  ```

  Only one process may open the session file at a time; stop the proxy before using it elsewhere.
- YouTube and web need nothing.

Expose it any way you like (Cloudflare Tunnel, nginx, Tailscale) and set `AGENT_REACH_PROXY_PUBLIC_URL` to that origin; it is used for MCP OAuth metadata and for the `/tgfile` media links. Then point Reach at it: `AGENT_REACH_BASE_URL=https://your-proxy.example.com`, `AGENT_REACH_PWD=<password>`, or fill the same two values under **后台 → 系统设置 → 抓取端接口**.

Other commands: `run` (foreground), `stop`, `restart`, `status`, `logs [n]`, `sync-commands`, `cmd <command…>` (run a command inside the isolated runtime). Under systemd, restart through `systemctl` rather than `reach.sh`, or systemd loses track of the process.

### Upgrading the CLIs

`./scripts/reach.sh setup` again. `requirements.txt` pins `twitter-cli` to a GitHub commit: PyPI stops at 0.8.5, whose `search` returns HTTP 404 since X's September 2026 rebuild. Check each platform with one URL and one search after an upgrade.

## Endpoints

```
GET /healthz                          → { "ok": true, "service": "reach-proxy", "platforms": [...], ... }
GET /http/?platform=<p>&query=<q>&pwd=<password>
    URL query  → { "ok": true, "type": "item",   "item": {...},   "errors": [] }
    text query → { "ok": true, "type": "search", "results": [...], "errors": [] }
    failure    → { "ok": false, "errors": [{ "stage": "youtube_info", "kind": "not_found", "message": "..." }] }  (4xx/5xx by kind)
    &single=1  → Telegram only: just the linked message, no album / run merging
GET /tgfile?priv=<id>|user=<name>&msg=<id>&pwd=<password>   Telegram media bytes (links come inside items)
POST /mcp                             Streamable HTTP MCP (bearer token or OAuth)
GET  /.well-known/oauth-*             OAuth metadata; /oauth/register, /oauth/authorize, /oauth/token
```

`platform` aliases: `twitter`→`x`, `xiaohongshu`/`rednote`→`xhs`, `yt`→`youtube`, `telegram`→`tg`.

## Configuration

All keys are optional except the password; see `.env.example`.

| Key | Default | Meaning |
| --- | --- | --- |
| `AGENT_REACH_PROXY_PASSWORD` | — | Required; the proxy exits at startup when it is empty. |
| `AGENT_REACH_PROXY_PUBLIC_URL` | `http://127.0.0.1:18280` | External origin, used for MCP OAuth metadata and `/tgfile` links. |
| `AGENT_REACH_MCP_TOKEN` | password | Static bearer for `/mcp`; `AGENT_REACH_MCP_AUTH_REQUIRED=0` disables auth (local only). |
| `AGENT_REACH_MCP_PUBLIC_DISCOVERY` | `1` | Let `initialize` / `tools/list` through without auth. |
| `AGENT_REACH_TURNSTILE_SITE_KEY` / `_SECRET_KEY` | — | Turnstile challenge on the OAuth authorize page. |
| `TWITTER_AUTH_TOKEN` / `TWITTER_CT0` | — | X session cookies for `twitter-cli`. |
| `AGENT_REACH_TG_API_ID` / `_API_HASH` / `_SESSION` | — / — / `runtime/telegram/reach.session` | Telegram app and user session. |
| `AGENT_REACH_TG_RUN_GAP_SECONDS` / `_RUN_MAX` | 600 / 30 | How consecutive same-sender group messages are merged. |
| `AGENT_REACH_PROXY_SEARCH_LIMIT` / `_COMMENT_LIMIT` | 15 / 50 | Result and comment caps. |
| `AGENT_REACH_MCP_IMAGE_MAX_BYTES` / `_DOWNLOAD_MAX_BYTES` | 4 MB / 32 MB | Inline image compression target and raw download cap. |
| `AGENT_REACH_RUNTIME_DIR`, `_VENV`, `_BIN_DIR`, `_HOME`, `_NODE_BIN` | under `runtime/` | Relocate the isolated runtime. |

The proxy is a single file, `agent_reach_proxy.py` (Starlette + uvicorn + the official `mcp` SDK). The CLIs run as subprocesses with the isolated environment from `command_env()`.

## License

MIT.
