from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import html
import ipaddress
import json
import os
import re
import secrets
import socket
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from copy import deepcopy
from io import BytesIO
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlparse, urlunparse

import httpx
import uvicorn
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, ImageContent, TextContent, Tool as MCPTool
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Route

try:
    from PIL import Image, ImageOps, UnidentifiedImageError
except Exception:
    Image = None
    ImageOps = None
    UnidentifiedImageError = ValueError


ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")


def env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    if not value:
        return default
    path = Path(value).expanduser()
    return path if path.is_absolute() else ROOT / path


RUNTIME_DIR = env_path("AGENT_REACH_RUNTIME_DIR", ROOT / "runtime")
DEFAULT_VENV_BIN = env_path("AGENT_REACH_VENV", RUNTIME_DIR / "venv") / "bin"
DEFAULT_NODE_BIN = env_path("AGENT_REACH_NODE_BIN", RUNTIME_DIR / "node" / "bin")
DEFAULT_LOCAL_BIN = env_path("AGENT_REACH_BIN_DIR", RUNTIME_DIR / "bin")
DEFAULT_LOCAL_HOME = env_path("AGENT_REACH_HOME", RUNTIME_DIR / "home")

HOST = os.environ.get("AGENT_REACH_PROXY_HOST") or "127.0.0.1"
PORT = int(os.environ.get("AGENT_REACH_PROXY_PORT") or "18280")
PUBLIC_URL = (os.environ.get("AGENT_REACH_PROXY_PUBLIC_URL") or f"http://127.0.0.1:{PORT}").rstrip("/")
PASSWORD = os.environ.get("AGENT_REACH_PROXY_PASSWORD") or ""
if not PASSWORD:
    # An empty password would make every compare_digest("", "") check pass.
    raise SystemExit("AGENT_REACH_PROXY_PASSWORD is required")
MCP_TOKEN = os.environ.get("AGENT_REACH_MCP_TOKEN") or PASSWORD
MCP_AUTH_REQUIRED = (os.environ.get("AGENT_REACH_MCP_AUTH_REQUIRED") or "1").strip().lower() not in {"0", "false", "no", "off"}
MCP_PUBLIC_DISCOVERY = (os.environ.get("AGENT_REACH_MCP_PUBLIC_DISCOVERY") or "1").strip().lower() not in {"0", "false", "no", "off"}
MCP_OAUTH_ENABLED = (os.environ.get("AGENT_REACH_MCP_OAUTH_ENABLED") or "1").strip().lower() not in {"0", "false", "no", "off"}
MCP_OAUTH_SCOPE = os.environ.get("AGENT_REACH_MCP_OAUTH_SCOPE") or "agent_reach.read"
MCP_OAUTH_TOKEN_TTL = int(os.environ.get("AGENT_REACH_MCP_OAUTH_TOKEN_TTL") or "3600")
TURNSTILE_SITE_KEY = os.environ.get("AGENT_REACH_TURNSTILE_SITE_KEY", "").strip()
TURNSTILE_SECRET_KEY = os.environ.get("AGENT_REACH_TURNSTILE_SECRET_KEY", "").strip()
TURNSTILE_ENABLED = bool(TURNSTILE_SITE_KEY and TURNSTILE_SECRET_KEY)
SEARCH_LIMIT = int(os.environ.get("AGENT_REACH_PROXY_SEARCH_LIMIT") or "15")
COMMENT_LIMIT = int(os.environ.get("AGENT_REACH_PROXY_COMMENT_LIMIT") or "50")
MCP_IMAGE_MAX_BYTES = int(os.environ.get("AGENT_REACH_MCP_IMAGE_MAX_BYTES") or "4000000")
MCP_IMAGE_DOWNLOAD_MAX_BYTES = int(os.environ.get("AGENT_REACH_MCP_IMAGE_DOWNLOAD_MAX_BYTES") or "32000000")
XHS_INLINE_IMAGE_LIMIT = int(os.environ.get("AGENT_REACH_XHS_INLINE_IMAGE_LIMIT") or "30")
XHS_INLINE_COMMENT_IMAGE_LIMIT = int(os.environ.get("AGENT_REACH_XHS_INLINE_COMMENT_IMAGE_LIMIT") or "20")
XHS_INLINE_COMMENT_IMAGE_PER_COMMENT_LIMIT = int(os.environ.get("AGENT_REACH_XHS_INLINE_COMMENT_IMAGE_PER_COMMENT_LIMIT") or "4")
XHS_IMAGE_DEDUPE_DISTANCE = int(os.environ.get("AGENT_REACH_XHS_IMAGE_DEDUPE_DISTANCE") or "4")
X_INLINE_IMAGE_LIMIT = int(os.environ.get("AGENT_REACH_X_INLINE_IMAGE_LIMIT") or "10")
TG_API_ID = int(os.environ.get("AGENT_REACH_TG_API_ID") or "0")
TG_API_HASH = (os.environ.get("AGENT_REACH_TG_API_HASH") or "").strip()
TG_SESSION = env_path("AGENT_REACH_TG_SESSION", RUNTIME_DIR / "telegram" / "reach.session")
TG_RUN_GAP_SECONDS = int(os.environ.get("AGENT_REACH_TG_RUN_GAP_SECONDS") or "600")
TG_RUN_MAX = int(os.environ.get("AGENT_REACH_TG_RUN_MAX") or "30")

GENERIC_URL_RE = re.compile(r"https?://(?:[^\s，。；;、]+)")
XHS_BOILERPLATE_RE = re.compile(
    r"(拷走口令.*?阅读全文吧[!！]?|来【小红书】阅读全文吧[!！]?|复制.*?小红书.*?$)"
)
PLATFORM_ALIASES = {
    "xhs": "xhs",
    "xiaohongshu": "xhs",
    "rednote": "xhs",
    "x": "x",
    "twitter": "x",
    "web": "web",
    "youtube": "youtube",
    "yt": "youtube",
    "tg": "tg",
    "telegram": "tg",
}
SUPPORTED_PLATFORMS = ["xhs", "x", "web", "youtube", "tg"]
PUBLIC_HOST = urlparse(PUBLIC_URL).netloc
ALLOWED_IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif", "image/avif"}
PROXY_DNS_NETWORKS = [ipaddress.ip_network("198.18.0.0/15")]
READ_ONLY_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    openWorldHint=True,
    idempotentHint=True,
)
MCP_DISCOVERY_METHODS = {"initialize", "notifications/initialized", "tools/list", "ping"}
OAUTH_AUTH_CODES: dict[str, dict[str, Any]] = {}
OAUTH_CLIENTS: dict[str, dict[str, Any]] = {}
OAUTH_LOOPBACK_REDIRECT_HOSTS = {"127.0.0.1", "localhost"}
OAUTH_TRUSTED_REDIRECT_URIS = {
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
}


def mcp_security_schemes() -> list[dict[str, Any]]:
    raw = os.environ.get("AGENT_REACH_MCP_SECURITY_SCHEMES", "").strip()
    if raw:
        try:
            value = json.loads(raw)
            if isinstance(value, list) and all(isinstance(item, dict) for item in value):
                return value
        except json.JSONDecodeError:
            pass
    if MCP_AUTH_REQUIRED:
        return [{"type": "oauth2", "scopes": [MCP_OAUTH_SCOPE]}]
    return [{"type": "noauth"}]


MCP_TOOL_SECURITY_SCHEMES = mcp_security_schemes()


def oauth_resource_identifier() -> str:
    return f"{PUBLIC_URL}/mcp"


def oauth_resource_metadata_url() -> str:
    return f"{PUBLIC_URL}/.well-known/oauth-protected-resource/mcp"


def oauth_www_authenticate() -> str:
    return f'Bearer resource_metadata="{oauth_resource_metadata_url()}", scope="{MCP_OAUTH_SCOPE}"'


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode((data + padding).encode("ascii"))


def oauth_sign_payload(payload: dict[str, Any]) -> str:
    body = b64url_encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signature = hmac.new(MCP_TOKEN.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    return f"ar.{body}.{b64url_encode(signature)}"


def oauth_verify_token(token: str) -> bool:
    if not MCP_OAUTH_ENABLED or not token.startswith("ar."):
        return False
    try:
        prefix, body, signature = token.split(".", 2)
        if prefix != "ar":
            return False
        expected = hmac.new(MCP_TOKEN.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(b64url_decode(signature), expected):
            return False
        payload = json.loads(b64url_decode(body).decode("utf-8"))
    except Exception:
        return False

    now = int(time.time())
    scope = as_text(payload.get("scope") or "")
    return (
        payload.get("aud") in {oauth_resource_identifier(), PUBLIC_URL}
        and int(payload.get("exp") or 0) > now
        and MCP_OAUTH_SCOPE in scope.split()
    )


def oauth_challenge_s256(verifier: str) -> str:
    return b64url_encode(hashlib.sha256(verifier.encode("ascii")).digest())


async def request_form_data(request: Request) -> dict[str, str]:
    body = (await request.body()).decode("utf-8", errors="replace")
    parsed = parse_qs(body, keep_blank_values=True)
    return {key: values[-1] if values else "" for key, values in parsed.items()}


def oauth_error(error: str, description: str, status_code: int = 400) -> JSONResponse:
    return JSONResponse(
        {"error": error, "error_description": description},
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


def redirect_with_params(redirect_uri: str, params: dict[str, str]) -> RedirectResponse:
    separator = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(f"{redirect_uri}{separator}{urlencode(params)}", status_code=302)


class MCPSearchResult(BaseModel):
    id: str
    title: str
    url: str


class MCPSearchOutput(BaseModel):
    results: list[MCPSearchResult]


class MCPFetchOutput(BaseModel):
    id: str
    title: str
    text: str
    url: str
    metadata: dict[str, Any] | None = None


def mcp_token_candidates(request: Request) -> list[str]:
    values: list[str] = []
    authorization = request.headers.get("authorization", "").strip()
    if authorization:
        values.append(authorization)
        if authorization.lower().startswith("bearer "):
            values.append(authorization[7:].strip())
    for header in ("x-api-key", "api-key"):
        value = request.headers.get(header, "").strip()
        if value:
            values.append(value)
    for query_key in ("token", "key", "pwd"):
        value = request.query_params.get(query_key, "").strip()
        if value:
            values.append(value)
    return values


def mcp_authorized(request: Request) -> bool:
    if not MCP_AUTH_REQUIRED:
        return True
    if not MCP_TOKEN:
        return False
    return any(
        secrets.compare_digest(value, MCP_TOKEN) or oauth_verify_token(value)
        for value in mcp_token_candidates(request)
    )


async def mcp_request_methods(request: Request) -> set[str]:
    try:
        body = await request.body()
    except Exception:
        return set()
    if not body:
        return set()
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return set()
    messages = payload if isinstance(payload, list) else [payload]
    methods: set[str] = set()
    for message in messages:
        if isinstance(message, dict) and isinstance(message.get("method"), str):
            methods.add(message["method"])
    return methods


async def mcp_public_discovery_request(request: Request) -> bool:
    if not MCP_PUBLIC_DISCOVERY or request.method != "POST":
        return False
    methods = await mcp_request_methods(request)
    return bool(methods) and methods.issubset(MCP_DISCOVERY_METHODS)


class MCPAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if (
            request.url.path.rstrip("/") == "/mcp"
            and request.method != "OPTIONS"
            and not mcp_authorized(request)
            and not await mcp_public_discovery_request(request)
        ):
            return JSONResponse(
                {"ok": False, "error": "Unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": oauth_www_authenticate(), "Cache-Control": "no-store"},
            )
        return await call_next(request)


def api_response(payload: dict[str, Any], status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        payload,
        status_code=status_code,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Access-Control-Allow-Headers": "*",
            "Cache-Control": "no-store",
        },
    )


async def options(_: Request) -> JSONResponse:
    return api_response({"ok": True})


async def healthz(_: Request) -> JSONResponse:
    return api_response(
        {
            "ok": True,
            "service": "reach-proxy",
            "reach": "/http/?platform={platform}&query={query}&pwd={pwd}",
            "mcp": "/mcp",
            "public_url": PUBLIC_URL,
            "platforms": SUPPORTED_PLATFORMS,
            "mcp_auth_required": MCP_AUTH_REQUIRED,
            "mcp_public_discovery": MCP_PUBLIC_DISCOVERY,
            "mcp_oauth_token_ttl_seconds": MCP_OAUTH_TOKEN_TTL,
            "mcp_oauth_token_ttl_days": MCP_OAUTH_TOKEN_TTL / 86400,
            "search_limit": SEARCH_LIMIT,
            "comment_limit": COMMENT_LIMIT,
            "mcp_image_max_bytes": MCP_IMAGE_MAX_BYTES,
            "mcp_image_download_max_bytes": image_download_limit(),
            "image_compression": Image is not None,
            "xhs_image_dedupe_distance": XHS_IMAGE_DEDUPE_DISTANCE,
            "xhs_inline_comment_image_limit": XHS_INLINE_COMMENT_IMAGE_LIMIT,
            "xhs_inline_comment_image_per_comment_limit": XHS_INLINE_COMMENT_IMAGE_PER_COMMENT_LIMIT,
        }
    )


async def root(_: Request) -> JSONResponse:
    return api_response(
        {
            "ok": True,
            "service": "reach-proxy",
            "healthz": "/healthz",
            "reach": "/http/?platform={platform}&query={query}&pwd={pwd}",
            "mcp": "/mcp",
            "public_mcp": f"{PUBLIC_URL}/mcp",
            "mcp_auth_required": MCP_AUTH_REQUIRED,
            "mcp_public_discovery": MCP_PUBLIC_DISCOVERY,
        }
    )


async def oauth_protected_resource_metadata(_: Request) -> JSONResponse:
    if not MCP_OAUTH_ENABLED:
        return JSONResponse({"error": "oauth_disabled"}, status_code=404)
    return JSONResponse(
        {
            "resource": oauth_resource_identifier(),
            "authorization_servers": [PUBLIC_URL],
            "scopes_supported": [MCP_OAUTH_SCOPE],
            "resource_documentation": PUBLIC_URL,
        },
        headers={"Cache-Control": "no-store"},
    )


async def oauth_authorization_server_metadata(_: Request) -> JSONResponse:
    if not MCP_OAUTH_ENABLED:
        return JSONResponse({"error": "oauth_disabled"}, status_code=404)
    return JSONResponse(
        {
            "issuer": PUBLIC_URL,
            "authorization_endpoint": f"{PUBLIC_URL}/oauth/authorize",
            "token_endpoint": f"{PUBLIC_URL}/oauth/token",
            "registration_endpoint": f"{PUBLIC_URL}/oauth/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code"],
            "token_endpoint_auth_methods_supported": ["none"],
            "code_challenge_methods_supported": ["S256"],
            "scopes_supported": [MCP_OAUTH_SCOPE],
            # URL client_ids are accepted without registration; the redirect is
            # still limited to OAUTH_TRUSTED_REDIRECT_URIS (oauth_redirect_allowed).
            "client_id_metadata_document_supported": True,
        },
        headers={"Cache-Control": "no-store"},
    )


async def oauth_register(request: Request) -> JSONResponse:
    if not MCP_OAUTH_ENABLED:
        return oauth_error("oauth_disabled", "OAuth is disabled.", status_code=404)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    raw_redirect_uris = payload.get("redirect_uris") if isinstance(payload, dict) else []
    if not isinstance(raw_redirect_uris, list) or any(
        not isinstance(uri, str) or not oauth_redirect_uri_valid(uri) for uri in raw_redirect_uris
    ):
        return oauth_error(
            "invalid_client_metadata",
            "redirect_uris must be a list of valid HTTPS or localhost HTTP URIs.",
        )
    redirect_uris = list(dict.fromkeys(raw_redirect_uris))
    client_id = f"reach-{secrets.token_urlsafe(24)}"
    OAUTH_CLIENTS[client_id] = {
        "redirect_uris": redirect_uris,
        "created_at": int(time.time()),
    }
    return JSONResponse(
        {
            "client_id": client_id,
            "client_id_issued_at": OAUTH_CLIENTS[client_id]["created_at"],
            "token_endpoint_auth_method": "none",
            "redirect_uris": OAUTH_CLIENTS[client_id]["redirect_uris"],
            "grant_types": ["authorization_code"],
            "response_types": ["code"],
        },
        status_code=201,
        headers={"Cache-Control": "no-store"},
    )


async def verify_turnstile(token: str, remote_ip: str = "") -> bool:
    if not TURNSTILE_ENABLED:
        return True
    if not token:
        return False
    data = {"secret": TURNSTILE_SECRET_KEY, "response": token}
    if remote_ip:
        data["remoteip"] = remote_ip
    try:
        async with httpx.AsyncClient(timeout=10, trust_env=True) as client:
            response = await client.post("https://challenges.cloudflare.com/turnstile/v0/siteverify", data=data)
            payload = response.json()
    except Exception:
        return False
    return bool(payload.get("success"))


def oauth_redirect_uri_valid(redirect_uri: str) -> bool:
    parsed = urlparse(redirect_uri)
    if parsed.fragment or not parsed.scheme or not parsed.netloc:
        return False
    if parsed.scheme == "https":
        return True
    return parsed.scheme == "http" and parsed.hostname in OAUTH_LOOPBACK_REDIRECT_HOSTS


def oauth_client_redirect_uris(client_id: str) -> list[str]:
    client = OAUTH_CLIENTS.get(client_id)
    redirect_uris = client.get("redirect_uris") if isinstance(client, dict) else []
    if not isinstance(redirect_uris, list):
        return []
    return [uri for uri in redirect_uris if isinstance(uri, str)]


def oauth_trusted_redirect_allowed(redirect_uri: str) -> bool:
    if not oauth_redirect_uri_valid(redirect_uri):
        return False
    parsed = urlparse(redirect_uri)
    if parsed.scheme == "http":
        return True
    if redirect_uri in OAUTH_TRUSTED_REDIRECT_URIS:
        return True
    if parsed.hostname == "chatgpt.com" and parsed.path.startswith("/connector/oauth/"):
        return True
    return parsed.hostname == "chatgpt.com" and parsed.path == "/connector_platform_oauth_redirect"


def oauth_redirect_allowed(client_id: str, redirect_uri: str) -> bool:
    registered_redirect_uris = oauth_client_redirect_uris(client_id)
    if registered_redirect_uris:
        return redirect_uri in registered_redirect_uris and oauth_redirect_uri_valid(redirect_uri)
    if client_id in OAUTH_CLIENTS:
        return False
    return oauth_trusted_redirect_allowed(redirect_uri)


def oauth_authorize_form(params: dict[str, str], error: str = "") -> HTMLResponse:
    hidden_inputs = "\n".join(
        f'<input type="hidden" name="{html.escape(key)}" value="{html.escape(value)}">'
        for key, value in params.items()
        if key not in {"password", "cf-turnstile-response"}
    )
    error_html = f'<p class="error">{html.escape(error)}</p>' if error else ""
    turnstile_script = '<script src="https://challenges.cloudflare.com/turnstile/v0/api.js" async defer></script>' if TURNSTILE_SITE_KEY else ""
    turnstile_widget = (
        f'<div class="cf-turnstile" data-sitekey="{html.escape(TURNSTILE_SITE_KEY)}" data-action="reach-oauth"></div>'
        if TURNSTILE_SITE_KEY
        else ""
    )
    return HTMLResponse(
        f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Authorize Reach</title>
  {turnstile_script}
  <style>
    body {{ font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 0; background: #f6f7f9; color: #202123; }}
    main {{ max-width: 420px; margin: 12vh auto; padding: 28px; background: #fff; border: 1px solid #e5e7eb; border-radius: 8px; box-shadow: 0 20px 70px rgba(15, 23, 42, 0.12); }}
    h1 {{ font-size: 22px; margin: 0 0 8px; }}
    p {{ color: #5f6368; line-height: 1.5; }}
    label {{ display: block; margin: 22px 0 8px; font-weight: 600; }}
    input[type=password] {{ box-sizing: border-box; width: 100%; padding: 12px; border: 1px solid #cfd4dc; border-radius: 6px; font-size: 16px; }}
    button {{ width: 100%; margin-top: 18px; padding: 12px 14px; border: 0; border-radius: 6px; background: #111827; color: white; font-weight: 700; font-size: 15px; cursor: pointer; }}
    .cf-turnstile {{ margin-top: 18px; }}
    .error {{ color: #b42318; }}
  </style>
</head>
<body>
  <main>
    <h1>Authorize Reach</h1>
    <p>Enter the Reach password to let this MCP client call the server.</p>
    {error_html}
    <form method="post" action="/oauth/authorize">
      {hidden_inputs}
      <label for="password">Password</label>
      <input id="password" name="password" type="password" autocomplete="current-password" required autofocus>
      {turnstile_widget}
      <button type="submit">Authorize</button>
    </form>
  </main>
</body>
</html>""",
        headers={"Cache-Control": "no-store"},
    )


async def oauth_authorize(request: Request):
    if not MCP_OAUTH_ENABLED:
        return oauth_error("oauth_disabled", "OAuth is disabled.", status_code=404)
    params = dict(request.query_params) if request.method == "GET" else await request_form_data(request)

    required = ["client_id", "redirect_uri", "response_type", "code_challenge"]
    missing = [key for key in required if not params.get(key)]
    if missing:
        return oauth_error("invalid_request", f"Missing required parameter(s): {', '.join(missing)}")
    if params.get("response_type") != "code":
        return oauth_error("unsupported_response_type", "Only authorization code flow is supported.")
    if params.get("code_challenge_method", "S256") != "S256":
        return oauth_error("invalid_request", "Only S256 PKCE is supported.")
    if not oauth_redirect_allowed(params["client_id"], params["redirect_uri"]):
        return oauth_error("invalid_request", "Redirect URI is not allowed.")

    password = params.get("password") or params.get("pwd")
    if not password:
        return oauth_authorize_form(params)
    if TURNSTILE_ENABLED and not await verify_turnstile(
        params.get("cf-turnstile-response", ""), request.client.host if request.client else ""
    ):
        return oauth_authorize_form(params, error="Please complete the verification challenge.")
    if not (secrets.compare_digest(password, PASSWORD) or secrets.compare_digest(password, MCP_TOKEN)):
        return oauth_authorize_form(params, error="Incorrect password.")

    code = secrets.token_urlsafe(32)
    OAUTH_AUTH_CODES[code] = {
        "client_id": params["client_id"],
        "redirect_uri": params["redirect_uri"],
        "code_challenge": params["code_challenge"],
        "scope": params.get("scope") or MCP_OAUTH_SCOPE,
        "resource": params.get("resource") or oauth_resource_identifier(),
        "exp": int(time.time()) + 300,
    }
    redirect_params = {"code": code}
    if params.get("state"):
        redirect_params["state"] = params["state"]
    return redirect_with_params(params["redirect_uri"], redirect_params)


async def oauth_token(request: Request) -> JSONResponse:
    if not MCP_OAUTH_ENABLED:
        return oauth_error("oauth_disabled", "OAuth is disabled.", status_code=404)
    form = await request_form_data(request)
    if form.get("grant_type") != "authorization_code":
        return oauth_error("unsupported_grant_type", "Only authorization_code is supported.")

    code = form.get("code", "")
    code_data = OAUTH_AUTH_CODES.pop(code, None)
    if not code_data:
        return oauth_error("invalid_grant", "Authorization code is invalid or already used.")
    if int(code_data.get("exp") or 0) < int(time.time()):
        return oauth_error("invalid_grant", "Authorization code expired.")
    if form.get("client_id") and form["client_id"] != code_data.get("client_id"):
        return oauth_error("invalid_grant", "client_id does not match authorization code.")
    if form.get("redirect_uri") and form["redirect_uri"] != code_data.get("redirect_uri"):
        return oauth_error("invalid_grant", "redirect_uri does not match authorization code.")
    if form.get("resource") and form["resource"] != code_data.get("resource"):
        return oauth_error("invalid_target", "resource does not match authorization code.")

    verifier = form.get("code_verifier", "")
    if not verifier or oauth_challenge_s256(verifier) != code_data.get("code_challenge"):
        return oauth_error("invalid_grant", "PKCE verification failed.")

    now = int(time.time())
    token_scope = code_data.get("scope") or MCP_OAUTH_SCOPE
    if MCP_OAUTH_SCOPE not in as_text(token_scope).split():
        token_scope = MCP_OAUTH_SCOPE
    access_token = oauth_sign_payload(
        {
            "iss": PUBLIC_URL,
            "aud": oauth_resource_identifier(),
            "sub": as_text(code_data.get("client_id") or "mcp-client"),
            "scope": token_scope,
            "iat": now,
            "exp": now + MCP_OAUTH_TOKEN_TTL,
        }
    )
    return JSONResponse(
        {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": MCP_OAUTH_TOKEN_TTL,
            "scope": token_scope,
        },
        headers={"Cache-Control": "no-store"},
    )


def command_env() -> dict[str, str]:
    env = os.environ.copy()
    local_home = str(DEFAULT_LOCAL_HOME)
    Path(local_home).mkdir(parents=True, exist_ok=True)
    env["HOME"] = local_home
    env["XDG_CONFIG_HOME"] = str(DEFAULT_LOCAL_HOME / ".config")
    env["XDG_CACHE_HOME"] = str(DEFAULT_LOCAL_HOME / ".cache")
    env["XDG_DATA_HOME"] = str(DEFAULT_LOCAL_HOME / ".local" / "share")
    for key in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME"):
        Path(env[key]).mkdir(parents=True, exist_ok=True)
    path_parts = [
        str(DEFAULT_LOCAL_BIN),
        str(DEFAULT_VENV_BIN),
        str(DEFAULT_NODE_BIN),
        env.get("PATH", ""),
    ]
    env["PATH"] = ":".join(part for part in path_parts if part)
    return env


def trim(text: str, limit: int = 20000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[trimmed {len(text) - limit} chars]"


def unique_values(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


async def run_command(args: list[str], timeout: int = 60, max_chars: int = 20000) -> dict[str, Any]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(ROOT),
        env=command_env(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        stdout_b, stderr_b = await proc.communicate()
        return {
            "ok": False,
            "returncode": -1,
            "stdout": trim(stdout_b.decode("utf-8", errors="replace"), max_chars),
            "stderr": trim(stderr_b.decode("utf-8", errors="replace"), max_chars),
            "error": f"Command timed out after {timeout}s",
        }

    stdout = trim(stdout_b.decode("utf-8", errors="replace"), max_chars)
    stderr = trim(stderr_b.decode("utf-8", errors="replace"), max_chars)
    return {"ok": proc.returncode == 0, "returncode": proc.returncode, "stdout": stdout, "stderr": stderr}


def load_json_object(text: str) -> dict[str, Any]:
    value = load_json_any(text)
    if isinstance(value, dict):
        return value
    raise json.JSONDecodeError("JSON value is not an object", text, 0)


def load_json_any(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        for index, char in enumerate(text):
            if char not in "[{":
                continue
            try:
                value, _ = decoder.raw_decode(text[index:])
                return value
            except json.JSONDecodeError:
                continue
        raise


def load_json_lines(text: str, limit: int = SEARCH_LIMIT) -> list[Any]:
    values: list[Any] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            values.append(json.loads(line))
        except json.JSONDecodeError:
            continue
        if len(values) >= limit:
            break
    return values


def extract_url(text: str) -> str | None:
    match = GENERIC_URL_RE.search(text)
    if not match:
        return None
    return match.group(0).rstrip(".,;!?，。；！？）)]}>'\"")


def is_link_query(query: str) -> bool:
    return bool(extract_url(query.strip()))


def mcporter_expr(tool: str, args: dict[str, Any]) -> str:
    parts = []
    for key, value in args.items():
        if value is None:
            continue
        parts.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
    return f"{tool}({', '.join(parts)})"


def parse_exa_search_blocks(stdout: str, limit: int = SEARCH_LIMIT) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for block in re.split(r"\n---+\n", stdout.strip()):
        title_match = re.search(r"^Title:\s*(.+)$", block, flags=re.MULTILINE)
        url_match = re.search(r"^URL:\s*(\S+)$", block, flags=re.MULTILINE)
        published_match = re.search(r"^Published:\s*(.+)$", block, flags=re.MULTILINE)
        author_match = re.search(r"^Author:\s*(.+)$", block, flags=re.MULTILINE)
        if not url_match:
            continue
        url = url_match.group(1).strip()
        if not url.startswith(("http://", "https://")) or url in seen_urls:
            continue
        seen_urls.add(url)
        title = title_match.group(1).strip() if title_match else url
        highlights = block.split("Highlights:", 1)[1].strip() if "Highlights:" in block else ""
        results.append(
            {
                "id": url,
                "url": url,
                "title": title or url,
                "content": trim(highlights, 3000),
                "author": {"name": author_match.group(1).strip()} if author_match else {},
                "created_at": published_match.group(1).strip() if published_match else "",
                "images": [],
                "comments": [],
                "stats": {},
            }
        )
        if len(results) >= limit:
            break
    return results


def jina_title(text: str, url: str) -> str:
    match = re.search(r"^Title:\s*(.+)$", text, flags=re.MULTILINE)
    if match and match.group(1).strip():
        return match.group(1).strip()
    return url


def jina_content(text: str) -> str:
    marker = "Markdown Content:"
    marker_index = text.find(marker)
    if marker_index >= 0:
        return text[marker_index + len(marker) :].strip()
    return text.strip()


def walk_values(value: Any):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from walk_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_values(child)


def first_value(data: Any, keys: set[str]) -> Any:
    if isinstance(data, dict):
        for key, value in data.items():
            if str(key).lower() in keys and value not in (None, ""):
                return value
        for value in data.values():
            found = first_value(value, keys)
            if found not in (None, ""):
                return found
    elif isinstance(data, list):
        for value in data:
            found = first_value(value, keys)
            if found not in (None, ""):
                return found
    return None


def first_nested_dict(data: Any, keys: set[str]) -> dict[str, Any]:
    if isinstance(data, dict):
        for key, value in data.items():
            if str(key).lower() in keys and isinstance(value, dict):
                return value
        for value in data.values():
            found = first_nested_dict(value, keys)
            if found:
                return found
    elif isinstance(data, list):
        for value in data:
            found = first_nested_dict(value, keys)
            if found:
                return found
    return {}


def collect_lists_for_keys(data: Any, keys: set[str]) -> list[list[Any]]:
    lists: list[list[Any]] = []
    if isinstance(data, dict):
        for key, value in data.items():
            if str(key).lower() in keys and isinstance(value, list):
                lists.append(value)
            else:
                lists.extend(collect_lists_for_keys(value, keys))
    elif isinstance(data, list):
        for value in data:
            lists.extend(collect_lists_for_keys(value, keys))
    return lists


def best_list_for_keys(data: Any, keys: set[str]) -> list[Any]:
    lists = collect_lists_for_keys(data, keys)
    if lists:
        return max(lists, key=len)
    if isinstance(data, list):
        return data
    return []


def as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def to_iso8601(value: Any) -> str:
    """Normalize a timestamp to ISO 8601 (UTC, seconds precision).

    Accepts: epoch seconds/ms, ``YYYYMMDD``, Twitter's ``Wed Oct 10 20:19:24 +0000 2018``,
    already-ISO strings. Unknown shapes pass through unchanged so we never drop info.
    """
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    text = text.strip()
    if not text:
        return ""

    # Epoch seconds (10 digits) or milliseconds (13 digits).
    if text.isdigit() and len(text) in (10, 13):
        try:
            num = int(text)
            if len(text) == 13:
                num //= 1000
            return datetime.fromtimestamp(num, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except (ValueError, OSError, OverflowError):
            pass

    # yt-dlp upload_date / release_date: "YYYYMMDD".
    if len(text) == 8 and text.isdigit():
        try:
            return datetime.strptime(text, "%Y%m%d").strftime("%Y-%m-%dT00:00:00Z")
        except ValueError:
            pass

    # Already ISO 8601-ish (has date and time separators).
    if "-" in text and ":" in text:
        try:
            normalized = text.replace("Z", "+00:00")
            dt = datetime.fromisoformat(normalized)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            return text

    # Twitter CLI / API native format: "Wed Oct 10 20:19:24 +0000 2018".
    for fmt in ("%a %b %d %H:%M:%S %z %Y", "%a %b %d %H:%M:%S %Y"):
        try:
            dt = datetime.strptime(text, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue

    return text


def compact_author(data: Any) -> dict[str, Any]:
    author = first_nested_dict(data, {"author", "user", "user_info", "userinfo", "owner", "channel"})
    source = author or (data if isinstance(data, dict) else {})
    return {
        "id": first_value(source, {"id", "user_id", "userid", "rest_id", "author_id", "channel_id"}) or "",
        "name": first_value(source, {"name", "nickname", "nick_name", "screen_name", "screenname", "username", "display_name"}) or "",
        "handle": first_value(source, {"screen_name", "screenname", "username", "handle"}) or "",
        "url": first_value(source, {"url", "profile_url", "channel_url"}) or "",
        "avatar_url": first_value(source, {"avatar_url", "avatar", "profile_image_url", "profileimageurl", "profile_image", "thumb_url", "thumbnail_url"}) or "",
    }


def compact_stats(data: Any) -> dict[str, Any]:
    return {
        "likes": first_value(data, {"like_count", "likes", "liked_count", "favorite_count"}) or 0,
        "comments": first_value(data, {"comment_count", "comments_count", "reply_count", "replies"}) or 0,
        "reposts": first_value(data, {"retweet_count", "repost_count", "share_count", "shared_count", "rts", "retweets"}) or 0,
        "views": first_value(data, {"view_count", "views", "play_count"}) or 0,
        "collects": first_value(data, {"collected_count", "collect_count", "bookmark_count", "bookmarks"}) or 0,
    }


def collect_media_urls(data: Any, limit: int = 80) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    markers = (".jpg", ".jpeg", ".png", ".webp", ".gif", "xhscdn.com", "twimg.com", "ytimg.com")
    for value in walk_values(data):
        if not isinstance(value, str) or "http" not in value:
            continue
        candidates = GENERIC_URL_RE.findall(value)
        if value.startswith(("http://", "https://")):
            candidates.append(value)
        for candidate in candidates:
            candidate = candidate.rstrip(".,;!?，。；！？）)]}>'\"")
            lower = candidate.lower()
            if not any(marker in lower for marker in markers):
                continue
            if candidate in seen:
                continue
            seen.add(candidate)
            urls.append(candidate)
            if len(urls) >= limit:
                return urls
    return urls


def tweet_media_urls(data: Any, limit: int = 20) -> list[str]:
    sources: list[Any] = []
    if isinstance(data, dict):
        for key in ("media", "images", "photos", "videos"):
            value = data.get(key)
            if value:
                sources.append(value)
        for key in ("extended_entities", "entities", "attachments"):
            value = data.get(key)
            if isinstance(value, dict):
                for media_key in ("media", "images", "photos", "videos"):
                    nested = value.get(media_key)
                    if nested:
                        sources.append(nested)
    elif isinstance(data, list):
        sources = data

    urls: list[str] = []
    seen: set[str] = set()
    for value in walk_values(sources):
        if not isinstance(value, str) or "http" not in value:
            continue
        candidates = GENERIC_URL_RE.findall(value)
        if value.startswith(("http://", "https://")):
            candidates.append(value)
        for candidate in candidates:
            candidate = candidate.rstrip(".,;!?，。；！？）)]}>'\"")
            lower = candidate.lower()
            if "profile_images" in lower:
                continue
            if "pbs.twimg.com/media/" not in lower and "video.twimg.com" not in lower:
                continue
            if candidate in seen:
                continue
            seen.add(candidate)
            urls.append(candidate)
            if len(urls) >= limit:
                return urls
    return urls


def sniff_image_mime(data: bytes) -> str:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12] in {b"avif", b"avis"}:
        return "image/avif"
    return ""


def hamming_distance(left: int, right: int) -> int:
    return (left ^ right).bit_count()


def perceptual_image_hash(data: bytes) -> int | None:
    if Image is None:
        return None
    try:
        with Image.open(BytesIO(data)) as opened:
            image = ImageOps.exif_transpose(opened).convert("L").resize((9, 8), Image.Resampling.LANCZOS)
            pixels = list(image.getdata())
    except (UnidentifiedImageError, OSError):
        return None

    value = 0
    for row in range(8):
        offset = row * 9
        for col in range(8):
            value <<= 1
            if pixels[offset + col] > pixels[offset + col + 1]:
                value |= 1
    return value


def image_download_limit() -> int:
    return max(MCP_IMAGE_DOWNLOAD_MAX_BYTES, MCP_IMAGE_MAX_BYTES)


def flatten_for_jpeg(image: Any) -> Any:
    if Image is None:
        return image
    image = ImageOps.exif_transpose(image)
    if image.mode in {"RGBA", "LA"} or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        background.alpha_composite(rgba)
        return background.convert("RGB")
    if image.mode != "RGB":
        return image.convert("RGB")
    return image


def encode_pillow_image(image: Any, image_format: str, quality: int) -> tuple[bytes, str]:
    output = BytesIO()
    if image_format == "WEBP":
        image.save(output, format="WEBP", quality=quality, method=6)
        return output.getvalue(), "image/webp"
    image = flatten_for_jpeg(image)
    image.save(output, format="JPEG", quality=quality, optimize=True, progressive=True)
    return output.getvalue(), "image/jpeg"


def compressed_image_candidates(image: Any):
    formats = ["WEBP", "JPEG"]
    qualities = [88, 80, 72, 64, 56, 48, 40, 32]
    scale = 1.0
    working = image
    while True:
        for image_format in formats:
            for quality in qualities:
                yield working, image_format, quality
        if min(working.size) <= 512:
            break
        scale *= 0.82
        next_size = (
            max(512, int(image.size[0] * scale)),
            max(512, int(image.size[1] * scale)),
        )
        if next_size == working.size:
            break
        working = image.resize(next_size, Image.Resampling.LANCZOS)


def compress_image_to_limit(data: bytes, mime_type: str, max_bytes: int = MCP_IMAGE_MAX_BYTES) -> tuple[bytes, str]:
    if len(data) <= max_bytes:
        return data, mime_type
    if Image is None:
        raise ValueError(
            f"Image is too large ({len(data)} bytes) and Pillow is not available for compression. "
            f"Limit is {max_bytes} bytes."
        )

    try:
        with Image.open(BytesIO(data)) as opened:
            image = ImageOps.exif_transpose(opened)
            image.load()
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError(f"Image is too large and could not be opened for compression: {exc}") from exc

    if max(image.size) > 12000:
        ratio = 12000 / max(image.size)
        image = image.resize(
            (max(1, int(image.size[0] * ratio)), max(1, int(image.size[1] * ratio))),
            Image.Resampling.LANCZOS,
        )

    best_data: bytes | None = None
    best_mime_type = ""
    for candidate, image_format, quality in compressed_image_candidates(image):
        compressed, compressed_mime_type = encode_pillow_image(candidate, image_format, quality)
        if best_data is None or len(compressed) < len(best_data):
            best_data = compressed
            best_mime_type = compressed_mime_type
        if len(compressed) <= max_bytes:
            return compressed, compressed_mime_type

    if best_data is not None:
        raise ValueError(
            f"Image is too large after compression ({len(best_data)} bytes). Limit is {max_bytes} bytes."
        )
    raise ValueError(f"Image is too large ({len(data)} bytes). Limit is {max_bytes} bytes.")


def image_origin(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}/"
    return ""


def image_referer(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if host.endswith("xhscdn.com"):
        return "https://www.xiaohongshu.com/"
    return image_origin(url)


def public_ip_from_address(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    return ip.is_global


def allowed_resolved_ip_from_address(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    return ip.is_global or any(ip in network for network in PROXY_DNS_NETWORKS)


async def validate_public_http_url(url: str) -> str:
    url = url.strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Only public http(s) image URLs are supported.")
    if parsed.username or parsed.password:
        raise ValueError("Image URLs with embedded credentials are not supported.")

    host = parsed.hostname.strip("[]").rstrip(".").lower()
    if host == "localhost" or host.endswith(".localhost"):
        raise ValueError("Localhost image URLs are not supported.")
    if not host:
        raise ValueError("Image URL host is required.")

    if public_ip_from_address(host):
        return url
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError("Private, local, or reserved image URLs are not supported.")

    try:
        infos = await asyncio.wait_for(
            asyncio.to_thread(socket.getaddrinfo, host, None, type=socket.SOCK_STREAM),
            timeout=5,
        )
    except socket.gaierror as exc:
        raise ValueError(f"Could not resolve image host: {host}") from exc
    except asyncio.TimeoutError as exc:
        raise ValueError(f"Timed out resolving image host: {host}") from exc

    addresses = {info[4][0] for info in infos if info and len(info) >= 5 and info[4]}
    if not addresses or not all(allowed_resolved_ip_from_address(address) for address in addresses):
        raise ValueError("Private, local, or reserved image hosts are not supported.")
    return url


async def download_image_bytes(url: str) -> tuple[bytes, str, str]:
    current_url = url.strip()
    headers = {
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
    }
    timeout = httpx.Timeout(30.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        for _ in range(6):
            current_url = await validate_public_http_url(current_url)
            request_headers = {**headers, "Referer": image_referer(current_url)}
            async with client.stream("GET", current_url, headers=request_headers) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location", "").strip()
                    if not location:
                        raise ValueError("Image redirect did not include a Location header.")
                    current_url = urljoin(str(response.url), location)
                    continue

                response.raise_for_status()
                content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                content_length = response.headers.get("content-length")
                if content_length:
                    try:
                        declared_size = int(content_length)
                    except ValueError:
                        declared_size = 0
                    if declared_size > image_download_limit():
                        raise ValueError(f"Image is too large to download. Limit is {image_download_limit()} bytes.")

                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > image_download_limit():
                        raise ValueError(f"Image is too large to download. Limit is {image_download_limit()} bytes.")
                    chunks.append(chunk)

                data = b"".join(chunks)
                mime_type = content_type if content_type in ALLOWED_IMAGE_MIME_TYPES else sniff_image_mime(data)
                if mime_type not in ALLOWED_IMAGE_MIME_TYPES:
                    raise ValueError(f"URL did not return a supported image MIME type: {content_type or 'unknown'}")
                data, mime_type = compress_image_to_limit(data, mime_type)
                return data, mime_type, str(response.url)

        raise ValueError("Image URL redirected too many times.")


def is_xhs_post_image_url(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    return host.endswith("xhscdn.com") and ("webpic" in host or "notes_pre" in path or "notes_pre_post" in path)


def xhs_image_variant_key(url: str) -> str:
    path = urlparse(url).path
    match = re.search(r"/notes_pre(?:_post)?/([^!/?#]+)", path)
    if match:
        return match.group(1)
    return url.split("!", 1)[0]


def xhs_image_variant_score(url: str) -> int:
    lower = url.lower()
    score = 0
    if "!nd_dft" in lower:
        score += 20
    if "!nd_prv" in lower:
        score += 10
    if "webp" in lower:
        score += 3
    if url.startswith("https://"):
        score += 1
    return score


def xhs_post_image_urls(urls: list[Any]) -> list[str]:
    strings = [url for url in urls if isinstance(url, str) and url.startswith(("http://", "https://"))]
    post_urls = [url for url in strings if is_xhs_post_image_url(url)]
    deduped: dict[str, str] = {}
    order: list[str] = []
    for url in unique_values(post_urls or strings):
        key = xhs_image_variant_key(url)
        if key not in deduped:
            deduped[key] = url
            order.append(key)
            continue
        if xhs_image_variant_score(url) > xhs_image_variant_score(deduped[key]):
            deduped[key] = url
    return [deduped[key] for key in order]


def is_xhs_comment_item(item: dict[str, Any]) -> bool:
    return any(key in item for key in ("note_id", "pictures", "sub_comments", "sub_comment_count", "user_info"))


def xhs_comment_image_urls(item: dict[str, Any]) -> list[str]:
    sources: list[Any] = []
    for key in ("pictures", "image_list", "images"):
        value = item.get(key)
        if value:
            sources.append(value)
    return xhs_post_image_urls(collect_media_urls(sources, limit=20))


def inline_image_object(data: bytes, mime_type: str) -> dict[str, Any]:
    return {
        "type": "image",
        "mimeType": mime_type,
        "encoding": "base64",
        "data": base64.b64encode(data).decode("ascii"),
        "bytes": len(data),
    }


async def inline_xhs_item_images(item: dict[str, Any]) -> list[dict[str, Any]]:
    raw_images = item.get("images") or []
    raw_strings = [url for url in raw_images if isinstance(url, str) and url.startswith(("http://", "https://"))]
    raw_post_urls = [url for url in raw_strings if is_xhs_post_image_url(url)]
    raw_url_count = len(unique_values(raw_post_urls or raw_strings))
    deduped_urls = xhs_post_image_urls(raw_images)
    urls = deduped_urls[:XHS_INLINE_IMAGE_LIMIT]
    if not urls:
        item["images"] = []
        return []

    errors: list[dict[str, Any]] = []
    results: list[dict[str, Any] | None] = [None] * len(urls)
    semaphore = asyncio.Semaphore(4)

    async def fetch_one(index: int, url: str) -> None:
        async with semaphore:
            try:
                data, mime_type, _ = await download_image_bytes(url)
            except Exception as exc:
                errors.append({"stage": "xhs_inline_image", "index": index, "error": str(exc)})
                return
            results[index] = {
                "image": inline_image_object(data, mime_type),
                "sha256": hashlib.sha256(data).hexdigest(),
                "perceptual_hash": perceptual_image_hash(data),
            }

    await asyncio.gather(*(fetch_one(index, url) for index, url in enumerate(urls)))

    kept_images: list[dict[str, Any]] = []
    seen_sha256: set[str] = set()
    seen_perceptual_hashes: list[int] = []
    content_removed = 0
    for result in results:
        if not result:
            continue
        sha256 = as_text(result.get("sha256"))
        perceptual_hash = result.get("perceptual_hash")
        if sha256 in seen_sha256:
            content_removed += 1
            continue
        if isinstance(perceptual_hash, int) and any(
            hamming_distance(perceptual_hash, seen) <= XHS_IMAGE_DEDUPE_DISTANCE
            for seen in seen_perceptual_hashes
        ):
            content_removed += 1
            continue
        seen_sha256.add(sha256)
        if isinstance(perceptual_hash, int):
            seen_perceptual_hashes.append(perceptual_hash)
        kept_images.append(result["image"])

    item["images"] = kept_images
    metadata = item.setdefault("metadata", {})
    if isinstance(metadata, dict):
        variant_removed = max(0, raw_url_count - len(deduped_urls))
        limited = max(0, len(deduped_urls) - len(urls))
        metadata["image_dedupe"] = {
            "original_url_count": raw_url_count,
            "variant_key_count": len(deduped_urls),
            "candidate_count": len(urls),
            "returned_count": len(kept_images),
            "variant_removed_count": variant_removed,
            "content_removed_count": content_removed,
            "limited_count": limited,
            "removed_count": max(0, raw_url_count - len(kept_images)),
            "perceptual_hash_distance": XHS_IMAGE_DEDUPE_DISTANCE,
        }
    return errors


def iter_comment_items(comments: Any):
    if not isinstance(comments, list):
        return
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        yield comment
        yield from iter_comment_items(comment.get("replies"))


async def inline_xhs_comment_images(item: dict[str, Any]) -> list[dict[str, Any]]:
    comments = item.get("comments")
    if not isinstance(comments, list):
        return []

    errors: list[dict[str, Any]] = []
    remaining = XHS_INLINE_COMMENT_IMAGE_LIMIT
    comments_with_images = 0
    returned_count = 0
    original_url_count = 0
    limited_count = 0

    for comment in iter_comment_items(comments):
        raw_images = comment.get("images") if isinstance(comment.get("images"), list) else []
        raw_urls = [url for url in raw_images if isinstance(url, str) and url.startswith(("http://", "https://"))]
        deduped_urls = xhs_post_image_urls(raw_urls)
        original_url_count += len(unique_values(raw_urls))
        if not deduped_urls:
            comment["images"] = []
            continue
        comments_with_images += 1
        if remaining <= 0:
            limited_count += len(deduped_urls)
            comment["images"] = []
            continue

        per_comment_limit = min(XHS_INLINE_COMMENT_IMAGE_PER_COMMENT_LIMIT, remaining)
        candidate_count = min(len(deduped_urls), per_comment_limit)
        remaining -= candidate_count
        limited_count += max(0, len(deduped_urls) - candidate_count)
        comment_errors = await inline_item_image_urls(
            comment,
            deduped_urls,
            limit=per_comment_limit,
            stage="xhs_inline_comment_image",
            metadata_key="image_dedupe",
        )
        comment_id = as_text(comment.get("id") or "")
        errors.extend([{**error, "comment_id": comment_id} for error in comment_errors])
        returned_count += len(comment.get("images") or [])

    metadata = item.setdefault("metadata", {})
    if isinstance(metadata, dict):
        metadata["comment_images"] = {
            "original_url_count": original_url_count,
            "comments_with_images": comments_with_images,
            "returned_count": returned_count,
            "limited_count": limited_count,
            "total_limit": XHS_INLINE_COMMENT_IMAGE_LIMIT,
            "per_comment_limit": XHS_INLINE_COMMENT_IMAGE_PER_COMMENT_LIMIT,
        }
    return errors


def is_image_url(url: str) -> bool:
    """Heuristic: return True for likely-image URLs (path ends with image ext or known image CDN path)."""
    lower = url.lower().split("?")[0]
    if lower.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif")):
        return True
    # Twitter image CDN serves images under /media/; video under /amplify_video/, /ext_tw_video/, etc.
    if "pbs.twimg.com/media/" in lower:
        return True
    # XHS image CDN
    if "xhscdn.com" in lower or "sns-img.xhscdn.com" in lower:
        return True
    return False


async def inline_item_image_urls(
    item: dict[str, Any],
    urls: list[str],
    *,
    limit: int,
    stage: str,
    metadata_key: str,
) -> list[dict[str, Any]]:
    urls = unique_values([url for url in urls if url.startswith(("http://", "https://"))])[:limit]
    if not urls:
        item["images"] = []
        return []

    # Partition into image URLs (inline as base64) and non-image URLs (video/other — keep as-is).
    image_urls: list[str] = []
    non_image_urls: list[str] = []
    for url in urls:
        if is_image_url(url):
            image_urls.append(url)
        else:
            non_image_urls.append(url)

    errors: list[dict[str, Any]] = []
    results: list[dict[str, Any] | None] = [None] * len(image_urls)
    semaphore = asyncio.Semaphore(4)

    async def fetch_one(index: int, url: str) -> None:
        async with semaphore:
            try:
                data, mime_type, _ = await download_image_bytes(url)
            except Exception as exc:
                errors.append({"stage": stage, "index": index, "error": str(exc)})
                return
            results[index] = {
                "image": inline_image_object(data, mime_type),
                "sha256": hashlib.sha256(data).hexdigest(),
                "perceptual_hash": perceptual_image_hash(data),
            }

    await asyncio.gather(*(fetch_one(index, url) for index, url in enumerate(image_urls)))

    kept_images: list[dict[str, Any]] = []
    seen_sha256: set[str] = set()
    seen_perceptual_hashes: list[int] = []
    content_removed = 0
    for result in results:
        if not result:
            continue
        sha256 = as_text(result.get("sha256"))
        perceptual_hash = result.get("perceptual_hash")
        if sha256 in seen_sha256:
            content_removed += 1
            continue
        if isinstance(perceptual_hash, int) and any(
            hamming_distance(perceptual_hash, seen) <= XHS_IMAGE_DEDUPE_DISTANCE
            for seen in seen_perceptual_hashes
        ):
            content_removed += 1
            continue
        seen_sha256.add(sha256)
        if isinstance(perceptual_hash, int):
            seen_perceptual_hashes.append(perceptual_hash)
        kept_images.append(result["image"])

    # Non-image URLs (video, etc.) are kept as plain URL strings so callers still see them.
    for url in non_image_urls:
        kept_images.append({"type": "video", "url": url})

    item["images"] = kept_images
    metadata = item.setdefault("metadata", {})
    if isinstance(metadata, dict):
        metadata[metadata_key] = {
            "original_url_count": len(urls),
            "returned_count": len(kept_images),
            "content_removed_count": content_removed,
            "perceptual_hash_distance": XHS_IMAGE_DEDUPE_DISTANCE,
        }
    return errors


def compact_comment_item(item: Any, max_replies: int = 10) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {"id": "", "content": as_text(item), "author": {}, "created_at": "", "stats": {}, "images": [], "replies": []}
    reply_lists = collect_lists_for_keys(item, {"replies", "reply", "sub_comments", "sub_comment_list", "children"})
    replies_source = max(reply_lists, key=len) if reply_lists else []
    images = xhs_comment_image_urls(item) if is_xhs_comment_item(item) else collect_media_urls(item, limit=20)
    return {
        "id": first_value(item, {"id", "comment_id", "commentid"}) or "",
        "content": as_text(first_value(item, {"content", "text", "full_text", "desc", "description"}) or ""),
        "author": compact_author(item),
        "created_at": to_iso8601(first_value(item, {"created_at", "create_time", "created_time", "time", "date"}) or ""),
        "stats": compact_stats(item),
        "images": images,
        "replies": [compact_comment_item(reply, max_replies=0) for reply in replies_source[:max_replies]],
    }


def compact_comments(data: Any, limit: int = COMMENT_LIMIT) -> list[dict[str, Any]]:
    source = best_list_for_keys(
        data,
        {
            "comments",
            "comment_list",
            "comments_list",
            "commentlist",
            "items",
            "list",
            "replies",
        },
    )
    return [compact_comment_item(item) for item in source[:limit]]


def xhs_normalize_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.netloc.endswith("xhslink.com") and parsed.scheme == "http":
        return urlunparse(parsed._replace(scheme="https"))
    return url


def xhs_is_short_url(url: str) -> bool:
    return urlparse(url).netloc.endswith("xhslink.com")


def xhs_parse_note_url(url: str) -> dict[str, str | None]:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    note_id = None
    match = re.search(r"/(?:explore|discovery/item|item)/([^/?#]+)", parsed.path)
    if match:
        note_id = match.group(1)
    return {
        "url": url,
        "note_id": note_id,
        "xsec_token": (query.get("xsec_token") or [None])[0],
        "xsec_source": (query.get("xsec_source") or [None])[0],
    }


def xhs_query_from_share_text(text: str, max_chars: int = 120) -> str:
    text = GENERIC_URL_RE.sub(" ", text)
    text = XHS_BOILERPLATE_RE.sub(" ", text)
    text = text.replace("...", " ")
    text = re.sub(r"\s+", " ", text).strip(" \t\r\n:：-—!！")
    return text[:max_chars].strip()


def xhs_note_url(note_id: str | None, xsec_token: str | None = None) -> str:
    if not note_id:
        return ""
    url = f"https://www.xiaohongshu.com/explore/{note_id}"
    if xsec_token:
        url += f"?xsec_token={xsec_token}&xsec_source=pc_search"
    return url


def xhs_note_card(item: dict[str, Any]) -> dict[str, Any]:
    card = item.get("note_card")
    return card if isinstance(card, dict) else item


def xhs_note_id(item: dict[str, Any], card: dict[str, Any]) -> str:
    for source in (item, card):
        for key in ("id", "note_id", "noteId", "noteIdStr"):
            value = source.get(key)
            if value:
                return str(value)
    return ""


def xhs_xsec_token(item: dict[str, Any], card: dict[str, Any]) -> str:
    for source in (item, card, card.get("user") if isinstance(card.get("user"), dict) else {}):
        for key in ("xsec_token", "xsecToken"):
            value = source.get(key)
            if value:
                return str(value)
    return ""


def xhs_compact_search_item(item: dict[str, Any]) -> dict[str, Any]:
    card = xhs_note_card(item)
    note_id = xhs_note_id(item, card)
    token = xhs_xsec_token(item, card)
    return {
        "id": note_id,
        "url": xhs_note_url(note_id, token),
        "title": card.get("display_title") or card.get("title") or "",
        "content": card.get("desc") or card.get("description") or "",
        "author": compact_author(card),
        "created_at": to_iso8601(first_value(card, {"time", "create_time", "created_time", "last_update_time"}) or ""),
        "stats": compact_stats(card),
        "images": collect_media_urls(card),
        "comments": [],
        "xsec_token": token,
    }


def compact_item(data: Any, fallback: dict[str, Any] | None = None, comments: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    fallback = fallback or {}
    note_id = fallback.get("id") or fallback.get("note_id") or first_value(data, {"note_id", "noteid", "id"}) or ""
    url = fallback.get("url") or xhs_note_url(as_text(note_id), fallback.get("xsec_token")) if note_id else fallback.get("url", "")
    title = first_value(data, {"title", "display_title"}) or fallback.get("title") or ""
    content = first_value(data, {"desc", "description", "content", "text", "full_text"}) or fallback.get("content") or ""
    return {
        "id": as_text(note_id),
        "url": url,
        "title": as_text(title),
        "content": as_text(content),
        "author": compact_author(data) or fallback.get("author", {}),
        "created_at": to_iso8601(first_value(data, {"created_at", "create_time", "created_time", "time", "date", "upload_date"}) or fallback.get("created_at", "")),
        "stats": compact_stats(data) or fallback.get("stats", {}),
        "images": collect_media_urls(data) or fallback.get("images", []),
        "comments": comments or [],
    }


def xhs_json_error_text(value: Any) -> str:
    if isinstance(value, dict):
        error = value.get("error")
        if isinstance(error, dict):
            code = as_text(error.get("code") or "").strip()
            message = as_text(error.get("message") or "").strip()
            return f"{code}: {message}" if code and message else code or message
        for key in ("message", "error", "detail"):
            text = as_text(value.get(key) or "").strip()
            if text:
                return text
    return ""


async def xhs_run_json(args: list[str], timeout: int = 90) -> tuple[Any | None, dict[str, Any]]:
    result = await run_command(args, timeout=timeout, max_chars=5_000_000)
    parsed: Any | None = None
    if result.get("stdout", "").strip():
        try:
            parsed = load_json_any(result["stdout"])
        except json.JSONDecodeError as exc:
            if result["ok"]:
                result["ok"] = False
                result["error"] = f"Could not parse JSON output: {exc}"
                result["stdout"] = trim(result["stdout"], 4000)
    if not result["ok"]:
        if parsed is not None:
            result["json"] = parsed
            result["error"] = xhs_json_error_text(parsed) or trim(result.get("stdout", ""), 800)
        return None, result
    return parsed, result


async def xhs_expand_url_once(url: str) -> tuple[str | None, dict[str, Any]]:
    result = await run_command(
        [
            "curl",
            "--noproxy",
            "*",
            "-sS",
            "-L",
            "--max-redirs",
            "1",
            "-A",
            "Mozilla/5.0 AppleWebKit/537.36 Chrome/145 Safari/537.36",
            "-o",
            "/dev/null",
            "-w",
            "%{url_effective}",
            xhs_normalize_url(url),
        ],
        timeout=20,
        max_chars=4000,
    )
    expanded = result.get("stdout", "").strip()
    return (expanded if result["ok"] and expanded else None), result


async def xhs_comments(target: str, xsec_token: str = "") -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    errors: list[dict[str, Any]] = []
    cmd = ["xhs", "comments", target, "--json", "--all"]
    if xsec_token:
        cmd.extend(["--xsec-token", xsec_token])
    data, result = await xhs_run_json(cmd, timeout=120)
    if not data:
        errors.append({"stage": "xhs_comments", "error": result.get("error") or trim(result.get("stderr", ""), 800)})
        return [], errors
    return compact_comments(data, limit=COMMENT_LIMIT), errors


async def reach_xhs(query: str, *, inline_images: bool = True) -> dict[str, Any]:
    errors: list[dict[str, Any]] = []
    url = extract_url(query)
    if not url:
        search_query = xhs_query_from_share_text(query) or query
        results: list[dict[str, Any]] = []
        for page in range(1, 4):
            data, result = await xhs_run_json(
                ["xhs", "search", search_query, "--sort", "general", "--type", "all", "--page", str(page), "--json"],
                timeout=90,
            )
            if not data:
                errors.append({"stage": "xhs_search", "page": page, "error": result.get("error") or trim(result.get("stderr", ""), 800)})
                continue
            items = ((data.get("data") or {}).get("items") or []) if isinstance(data, dict) else []
            for item in items:
                if isinstance(item, dict):
                    results.append(xhs_compact_search_item(item))
                    if len(results) >= SEARCH_LIMIT:
                        break
            if len(results) >= SEARCH_LIMIT:
                break
        return {"type": "search", "source": "xhs_search", "results": results, "errors": errors}

    direct_target = xhs_normalize_url(url)
    parsed = xhs_parse_note_url(direct_target)
    resolved_url = direct_target
    if xhs_is_short_url(direct_target) or not parsed.get("note_id"):
        expanded_url, expand_result = await xhs_expand_url_once(direct_target)
        if expanded_url:
            resolved_url = expanded_url
            parsed = xhs_parse_note_url(expanded_url)
        else:
            errors.append({"stage": "xhs_expand_url", "error": expand_result.get("error") or trim(expand_result.get("stderr", ""), 800)})

    read_target = parsed.get("note_id") or resolved_url
    token = parsed.get("xsec_token") or ""
    cmd = ["xhs", "read", str(read_target), "--json"]
    if token:
        cmd.extend(["--xsec-token", token])
    detail, read_result = await xhs_run_json(cmd, timeout=90)
    if not detail:
        errors.append({"stage": "xhs_read", "error": read_result.get("error") or trim(read_result.get("stderr", ""), 800)})
        return {"type": "item", "source": "xhs_read", "item": {}, "errors": errors}

    comments, comment_errors = await xhs_comments(str(read_target), token)
    errors.extend(comment_errors)
    fallback = {
        "id": parsed.get("note_id") or "",
        "xsec_token": token,
        "url": xhs_note_url(parsed.get("note_id"), token) if parsed.get("note_id") else resolved_url,
    }
    item = compact_item(detail, fallback=fallback, comments=comments)
    if inline_images:
        errors.extend(await inline_xhs_item_images(item))
        errors.extend(await inline_xhs_comment_images(item))
    return {"type": "item", "source": "xhs_read", "item": item, "errors": errors}


async def read_url(url: str, max_characters: int = 80000) -> dict[str, Any]:
    reader_url = "https://r.jina.ai/" + url
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        response = await client.get(reader_url, headers={"User-Agent": "reach-proxy/1.0"})
    return {
        "ok": response.is_success,
        "status_code": response.status_code,
        "url": url,
        "reader_url": reader_url,
        "text": trim(response.text, max_characters),
    }


async def reach_web(query: str) -> dict[str, Any]:
    url = extract_url(query)
    if url:
        result = await read_url(url)
        text = result.get("text", "")
        item = {
            "id": url,
            "url": url,
            "title": jina_title(text, url),
            "content": jina_content(text),
            "author": {},
            "created_at": "",
            "stats": {},
            "images": collect_media_urls(text),
            "comments": [],
            "metadata": {"reader_url": result.get("reader_url"), "status_code": result.get("status_code")},
        }
        return {"type": "item", "source": "jina_reader", "item": item, "errors": [] if result["ok"] else [result]}

    expr = mcporter_expr("exa.web_search_exa", {"query": query, "numResults": SEARCH_LIMIT})
    result = await run_command(["mcporter", "call", expr], timeout=75, max_chars=100000)
    errors = [] if result["ok"] else [{"stage": "web_search", "error": result.get("error") or trim(result.get("stderr", ""), 1000)}]
    return {"type": "search", "source": "exa_web_search", "results": parse_exa_search_blocks(result.get("stdout", "")), "errors": errors}


def compact_tweet_author(item: dict[str, Any]) -> dict[str, Any]:
    raw_author = item.get("author")
    if isinstance(raw_author, dict):
        author = compact_author(raw_author)
        handle = as_text(author.get("handle") or author.get("name")).lstrip("@")
        if handle and not author.get("url"):
            author["url"] = f"https://x.com/{handle}"
        return author
    if isinstance(raw_author, str) and raw_author.strip():
        handle = raw_author.strip().lstrip("@")
        return {
            "id": "",
            "name": raw_author.strip(),
            "handle": handle,
            "url": f"https://x.com/{handle}" if handle else "",
        }
    return compact_author(item)


def compact_tweet_comment(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {"id": "", "content": as_text(item), "author": {}, "created_at": "", "stats": {}, "images": [], "replies": []}
    return {
        "id": as_text(first_value(item, {"id", "tweet_id", "rest_id", "conversation_id"}) or ""),
        "content": as_text(first_value(item, {"full_text", "text", "content"}) or ""),
        "author": compact_tweet_author(item),
        "created_at": to_iso8601(first_value(item, {"created_at", "createdat", "createdatiso", "createdatlocal", "created_time", "time", "date"}) or ""),
        "stats": compact_stats(item),
        "images": tweet_media_urls(item),
        "replies": [],
        # Id of the tweet this one answers (x_tweet.py adds it); None for a root tweet.
        "in_reply_to": as_text(item.get("inReplyToId") or "") or None,
    }


# twitter-cli (>= 0.8.6) writes an article's inline images as `![caption](url)`
# lines in place and its links as `[label](url)`.
ARTICLE_IMAGE_RE = re.compile(r"^!\[[^\]\n]*\]\((https?://[^)\s]+)\)$", re.MULTILINE)
MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[((?:[^\]\\\n]|\\.)*)\]\((?:https?://[^)\s]+)\)")


def tweet_article_image_urls(item: dict[str, Any]) -> list[str]:
    return ARTICLE_IMAGE_RE.findall(as_text(item.get("articleText")))


def tweet_article_content(item: dict[str, Any], text: str) -> tuple[str, str]:
    """Title/content for an X Article tweet (long-form post attached to a tweet).

    twitter-cli exposes the article as ``articleTitle`` + ``articleText`` (draft.js
    blocks flattened to Markdown). The tweet's own ``text`` is only the teaser plus
    a t.co link, so the article body becomes the content. Links are reduced to
    their label; heading lines keep their ``#`` markers and image lines stay,
    normalised to ``![](url)``, so consumers can render headings (and an outline)
    and place the images.
    """
    article_text = as_text(item.get("articleText")).strip()
    if not article_text:
        return trim(text, 100), text
    title = as_text(item.get("articleTitle")).strip() or trim(text, 100)
    body = MARKDOWN_LINK_RE.sub(lambda m: re.sub(r"\\([\[\]])", r"\1", m.group(1)), article_text)
    body = ARTICLE_IMAGE_RE.sub(r"![](\1)", body)
    return title, "\n\n".join(part for part in (text.strip(), title, body) if part)


def compact_tweet_item(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {"id": "", "url": "", "title": "", "content": as_text(item), "author": {}, "created_at": "", "stats": {}, "images": [], "comments": []}
    tweet_id = first_value(item, {"id", "tweet_id", "rest_id", "conversation_id"}) or ""
    # Top-level keys only: a recursive lookup would pick up media[].url.
    url = as_text(item.get("url") or item.get("tweet_url") or item.get("expanded_url") or "")
    author = compact_tweet_author(item)
    if tweet_id and not url:
        handle = as_text(author.get("handle") or author.get("name") or "i").lstrip("@")
        url = f"https://x.com/{handle}/status/{tweet_id}"
    text = as_text(first_value(item, {"full_text", "text", "content"}) or "")
    title, content = tweet_article_content(item, text)
    reply_lists = collect_lists_for_keys(item, {"replies", "reply", "comments"})
    replies = max(reply_lists, key=len) if reply_lists else []
    return {
        "id": as_text(tweet_id),
        "url": url,
        "title": title,
        "content": content,
        "author": author,
        "created_at": to_iso8601(first_value(item, {"created_at", "createdat", "createdatiso", "createdatlocal", "created_time", "time"}) or ""),
        "stats": compact_stats(item),
        "images": unique_values([*tweet_media_urls(item), *tweet_article_image_urls(item)]),
        "comments": [compact_tweet_comment(reply) for reply in replies[:COMMENT_LIMIT]],
        # An X Article (long-form post) rather than a plain tweet.
        "x_article": bool(as_text(item.get("articleText")).strip()),
    }


async def x_video_transcript(url: str) -> tuple[str, str, str]:
    """Best-effort subtitle extraction for a video tweet via yt-dlp.

    Most X/Twitter videos carry no subtitle track — returns ("", "", "") in
    that case or on any yt-dlp failure. Never raises.
    """
    result = await run_command(
        ["yt-dlp", "--dump-single-json", "--skip-download", "--no-warnings", "--no-playlist", url],
        timeout=60,
        max_chars=10_000_000,
    )
    if not result["ok"]:
        return "", "", ""
    try:
        data = load_json_object(result["stdout"])
    except Exception:
        return "", "", ""
    if not isinstance(data, dict):
        return "", "", ""
    return await transcript_from_info(data)


async def reach_x(query: str, *, inline_images: bool = True) -> dict[str, Any]:
    url = extract_url(query)
    if url:
        result = await run_command(
            [str(DEFAULT_VENV_BIN / "python"), "-I", str(ROOT / "scripts" / "x_tweet.py"), "tweet", url, "-n", str(COMMENT_LIMIT), "--json"],
            timeout=90,
            max_chars=5_000_000,
        )
        if not result["ok"]:
            return {"type": "item", "source": "twitter_tweet", "item": {}, "errors": [{"stage": "twitter_tweet", "error": result.get("error") or trim(result.get("stderr", ""), 1000)}]}
        data = load_json_any(result["stdout"])
        # `twitter tweet` returns the conversation around the focal tweet: the
        # tweets it replies to come first, then the focal tweet, then replies.
        # Pick the focal tweet by id so a reply is not mistaken for its parent.
        tweets = [tweet for tweet in best_list_for_keys(data, {"tweets", "results", "items", "data"}) if isinstance(tweet, dict)]
        if not tweets:
            return {"type": "item", "source": "twitter_tweet", "item": {}, "errors": []}
        focal_match = re.search(r"/status/(\d+)", url)
        focal_id = focal_match.group(1) if focal_match else ""
        focal_index = next((index for index, tweet in enumerate(tweets) if as_text(tweet.get("id")) == focal_id), 0)
        main_raw_item = tweets[focal_index]
        item = compact_tweet_item(main_raw_item)
        item["comments"] = [compact_tweet_comment(reply) for reply in tweets[focal_index + 1 : focal_index + 1 + COMMENT_LIMIT]]
        # Thread context: the tweets the focal one replies to, root first. X returns
        # the whole ancestor chain ahead of the focal tweet; `parent_missing` names
        # the next tweet up when the chain stops short of a root (deleted, hidden).
        by_id = {as_text(tweet.get("id")): tweet for tweet in tweets[:focal_index]}
        parents: list[dict[str, Any]] = []
        parent_id = as_text(main_raw_item.get("inReplyToId") or "")
        while parent_id and parent_id in by_id:
            parents.insert(0, compact_tweet_comment(by_id[parent_id]))
            parent_id = as_text(by_id[parent_id].get("inReplyToId") or "")
        item["in_reply_to"] = as_text(main_raw_item.get("inReplyToId") or "") or None
        item["parents"] = parents
        item["parent_missing"] = parent_id or None
        # Video tweets: try to pull a subtitle track (rarely present, but when
        # it is we surface it just like the YouTube transcript).
        if any("video.twimg.com" in as_text(media) for media in item.get("images") or []):
            transcript, transcript_lang, transcript_vtt = await x_video_transcript(url)
            if transcript:
                item["transcript"] = transcript
                item["transcript_lang"] = transcript_lang
                item["transcript_vtt"] = transcript_vtt
        if inline_images:
            errors = await inline_item_image_urls(
                item,
                item["images"][:X_INLINE_IMAGE_LIMIT],
                limit=X_INLINE_IMAGE_LIMIT,
                stage="x_inline_image",
                metadata_key="image_dedupe",
            )
        else:
            errors = []
        return {"type": "item", "source": "twitter_tweet", "item": item, "errors": errors}

    result = await run_command(
        ["twitter", "-c", "search", query, "--type", "latest", "-n", str(max(SEARCH_LIMIT, 20)), "--json"],
        timeout=90,
        max_chars=5_000_000,
    )
    if not result["ok"]:
        return {"type": "search", "source": "twitter_search", "results": [], "errors": [{"stage": "twitter_search", "error": result.get("error") or trim(result.get("stderr", ""), 1000)}]}
    data = load_json_any(result["stdout"])
    source = best_list_for_keys(data, {"tweets", "results", "items", "data"})
    return {"type": "search", "source": "twitter_search", "results": [compact_tweet_item(item) for item in source[: max(SEARCH_LIMIT, 20)]], "errors": []}


def clean_vtt(text: str, max_chars: int = 80000) -> str:
    lines: list[str] = []
    last = ""
    for line in text.splitlines():
        line = re.sub(r"<[^>]+>", "", line).strip()
        if not line or line == "WEBVTT" or line.startswith(("Kind:", "Language:", "NOTE")):
            continue
        if "-->" in line or line.isdigit():
            continue
        if line == last:
            continue
        lines.append(line)
        last = line
    return trim("\n".join(lines), max_chars)


TRANSCRIPT_ZH_LANGS = ("zh-Hans", "zh-CN", "zh", "zh-Hant", "zh-TW", "zh-HK")
TRANSCRIPT_EN_LANGS = ("en", "en-US", "en-GB", "en-orig")

TIMESTAMP_RE = re.compile(
    r"((?:\d{1,2}:)?\d{2}:\d{2}[.,]\d{3})\s*-->\s*((?:\d{1,2}:)?\d{2}:\d{2}[.,]\d{3})"
)


def normalize_vtt(text: str, max_chars: int = 300_000) -> str:
    """Reduce a raw VTT file to a minimal, player-ready WEBVTT.

    - Strips headers, cue ids, cue settings (align/position), and inline tags.
    - Removes rolling-window duplicate lines (YouTube auto captions repeat the
      previous cue's line in each new cue).
    Returns "" when no timed cues survive.
    """
    cues: list[tuple[str, list[str]]] = []
    current_ts: str | None = None
    current_lines: list[str] = []
    last_raw_lines: list[str] = []

    def flush() -> None:
        nonlocal current_ts, current_lines, last_raw_lines
        if current_ts and current_lines:
            fresh = [line for line in current_lines if line not in last_raw_lines]
            if fresh:
                cues.append((current_ts, fresh))
            last_raw_lines = list(current_lines)
        current_ts = None
        current_lines = []

    for raw_line in text.splitlines():
        match = TIMESTAMP_RE.search(raw_line)
        if match:
            flush()
            current_ts = f"{match.group(1).replace(',', '.')} --> {match.group(2).replace(',', '.')}"
            continue
        line = re.sub(r"<[^>]+>", "", raw_line).strip()
        if not line or line == "WEBVTT" or line.startswith(("Kind:", "Language:", "NOTE", "STYLE", "REGION")):
            continue
        if current_ts is None and (line.isdigit() or "-->" in line):
            continue
        if current_ts is not None:
            current_lines.append(line)
    flush()

    if not cues:
        return ""
    body = "\n\n".join(f"{ts}\n" + "\n".join(lines) for ts, lines in cues)
    return trim("WEBVTT\n\n" + body, max_chars)


async def transcript_from_info(data: dict[str, Any]) -> tuple[str, str, str]:
    """Extract the best subtitle track from a yt-dlp info dict.

    Preference order: manual Chinese > automatic Chinese > manual English >
    automatic English. Returns (transcript_text, language_code, vtt);
    ("", "", "") when no usable track exists. Works for any yt-dlp-supported
    site that exposes ``subtitles`` / ``automatic_captions`` (YouTube,
    X/Twitter, ...).
    """
    subs = data.get("subtitles") if isinstance(data.get("subtitles"), dict) else {}
    autos = data.get("automatic_captions") if isinstance(data.get("automatic_captions"), dict) else {}
    native_lang = as_text(data.get("language") or "")

    candidates: list[tuple[str, list[Any]]] = []
    # 1. Manual Chinese subs (uploader-provided, includes human translations).
    for language in TRANSCRIPT_ZH_LANGS:
        if subs.get(language):
            candidates.append((language, subs[language]))
    # 2. Auto Chinese captions — ONLY for Chinese-audio videos. YouTube lists
    #    auto-TRANSLATED tracks for every language in automatic_captions;
    #    those are near-garbage and must not shadow the English original.
    if native_lang.startswith("zh"):
        for language in TRANSCRIPT_ZH_LANGS:
            if autos.get(language):
                candidates.append((language, autos[language]))
    # 3. Manual English subs.
    for language in TRANSCRIPT_EN_LANGS:
        if subs.get(language):
            candidates.append((language, subs[language]))
    # 4. Auto English captions when the audio is English (or language unknown —
    #    the common case for en). "-orig" variants only exist for the native language.
    if not native_lang or native_lang.startswith("en"):
        for language in TRANSCRIPT_EN_LANGS:
            if autos.get(language):
                candidates.append((language, autos[language]))
    # 5. Other-language videos: fall back to the native-language track so a
    #    downstream translation engine can work from the real source text.
    if native_lang and not native_lang.startswith(("zh", "en")):
        for tracks_by_lang in (subs, autos):
            for language, tracks in tracks_by_lang.items():
                if language.split("-")[0] == native_lang.split("-")[0] and tracks:
                    candidates.append((language, tracks))
    for language, tracks in candidates:
        # Prefer real VTT so the timed track survives for in-player captions.
        track = next((item for item in tracks if item.get("ext") == "vtt"), None) \
            or next((item for item in tracks if item.get("ext") in {"srv3", "ttml"}), tracks[0])
        url = track.get("url")
        if not url:
            continue
        try:
            raw = await fetch_subtitle_text(url)
        except httpx.HTTPError:
            continue
        if raw.strip():
            vtt = normalize_vtt(raw) if track.get("ext") == "vtt" else ""
            return clean_vtt(vtt or raw), language, vtt
    return "", "", ""


async def fetch_subtitle_text(url: str) -> str:
    """Fetch a subtitle track, following one level of HLS indirection.

    X/Twitter's "vtt" subtitle URL actually returns an m3u8 playlist whose
    non-comment lines reference the real VTT segment files (relative paths).
    """
    headers = {"User-Agent": "reach-proxy/1.0"}
    async with httpx.AsyncClient(timeout=45, follow_redirects=True) as client:
        response = await client.get(url, headers=headers)
        if not response.is_success or not response.text.strip():
            return ""
        text = response.text
        if not text.lstrip().startswith("#EXTM3U"):
            return text
        segments = [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")]
        parts: list[str] = []
        for segment in segments[:50]:
            segment_url = urljoin(str(response.url), segment)
            try:
                seg_response = await client.get(segment_url, headers=headers)
            except httpx.HTTPError:
                continue
            if seg_response.is_success and seg_response.text.strip():
                parts.append(seg_response.text)
        return "\n\n".join(parts)


def compact_youtube_video_sources(data: dict[str, Any], limit: int = 12) -> list[dict[str, Any]]:
    """Summarize yt-dlp ``formats`` into compact downloadable source descriptors.

    Keeps progressive (video+audio) mp4/m4a/webm entries with a real URL, best-first.
    DASH-only (separate vcodec/acodec) streams are included only if no progressive
    source exists, since they need muxing before download.
    """
    formats = data.get("formats") or []
    if not isinstance(formats, list):
        return []
    progressive: list[dict[str, Any]] = []
    dash: list[dict[str, Any]] = []
    for fmt in formats:
        if not isinstance(fmt, dict):
            continue
        url = fmt.get("url")
        if not url:
            continue
        vcodec = fmt.get("vcodec")
        acodec = fmt.get("acodec")
        has_video = bool(vcodec) and vcodec != "none"
        has_audio = bool(acodec) and acodec != "none"
        height = fmt.get("height") or 0
        quality = f"{int(height)}p" if height else (fmt.get("format_note") or fmt.get("format_id") or "")
        entry = {
            "quality": quality,
            "url": url,
            "ext": fmt.get("ext") or "",
            "format_id": fmt.get("format_id") or "",
            "filesize": fmt.get("filesize") or fmt.get("filesize_approx") or 0,
            "has_video": has_video,
            "has_audio": has_audio,
        }
        if has_video and has_audio:
            progressive.append(entry)
        elif has_video:
            dash.append(entry)
    def _size(entry: dict[str, Any]) -> int:
        try:
            return int(entry.get("filesize") or 0)
        except (TypeError, ValueError):
            return 0

    progressive.sort(key=_size, reverse=True)
    dash.sort(key=_size, reverse=True)
    sources = progressive[:limit]
    if len(sources) < limit:
        sources.extend(dash[: limit - len(sources)])
    return sources


def compact_youtube_item(data: dict[str, Any], transcript: str = "", transcript_lang: str = "", transcript_vtt: str = "", channel_avatar_url: str = "") -> dict[str, Any]:
    video_id = data.get("id") or ""
    webpage_url = data.get("webpage_url") or data.get("original_url") or data.get("url") or ""
    return {
        "id": video_id,
        "url": webpage_url,
        "title": data.get("title") or "",
        "content": data.get("description") or "",
        "transcript": transcript,
        "transcript_lang": transcript_lang,
        "transcript_vtt": transcript_vtt,
        "author": {
            "id": data.get("channel_id") or data.get("uploader_id") or "",
            "name": data.get("channel") or data.get("uploader") or "",
            "handle": data.get("uploader_id") or "",
            "url": data.get("channel_url") or "",
            "avatar_url": channel_avatar_url,
        },
        "created_at": to_iso8601(data.get("upload_date") or data.get("release_date") or ""),
        "duration": data.get("duration"),
        "stats": compact_stats(data),
        "video_sources": compact_youtube_video_sources(data),
        "images": unique_values(
            [thumb.get("url") for thumb in data.get("thumbnails", []) if isinstance(thumb, dict) and thumb.get("url")]
            + ([data.get("thumbnail")] if data.get("thumbnail") else [])
        ),
        "comments": [compact_comment_item(comment) for comment in (data.get("comments") or [])[:COMMENT_LIMIT]],
    }


async def fetch_youtube_channel_avatar(channel_url: str) -> str:
    """Fetch channel avatar URL from YouTube channel page og:image meta tag.

    Returns empty string on any failure — avatar is best-effort, not critical.
    """
    if not channel_url:
        return ""
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            response = await client.get(channel_url, headers={"User-Agent": "reach-proxy/1.0"})
        if not response.is_success:
            return ""
        match = re.search(r'<meta\s+property="og:image"\s+content="([^"]+)"', response.text)
        if match:
            return match.group(1)
        match = re.search(r'<link\s+itemprop="thumbnailUrl"\s+href="([^"]+)"', response.text)
        if match:
            return match.group(1)
    except Exception:
        pass
    return ""


async def reach_youtube(query: str) -> dict[str, Any]:
    url = extract_url(query)
    if url:
        result = await run_command(
            [
                "yt-dlp",
                "--dump-single-json",
                "--skip-download",
                "--no-warnings",
                "--no-playlist",
                "--write-comments",
                "--extractor-args",
                f"youtube:max_comments={COMMENT_LIMIT}",
                url,
            ],
            timeout=150,
            max_chars=10_000_000,
        )
        if not result["ok"]:
            return {"type": "item", "source": "yt_dlp", "item": {}, "errors": [{"stage": "youtube_info", "error": result.get("error") or trim(result.get("stderr", ""), 1000)}]}
        data = load_json_object(result["stdout"])
        transcript, transcript_lang, transcript_vtt = await transcript_from_info(data)
        channel_url = data.get("channel_url") or ""
        avatar_url = await fetch_youtube_channel_avatar(channel_url)
        return {"type": "item", "source": "yt_dlp", "item": compact_youtube_item(data, transcript=transcript, transcript_lang=transcript_lang, transcript_vtt=transcript_vtt, channel_avatar_url=avatar_url), "errors": []}

    result = await run_command(
        ["yt-dlp", "--dump-json", "--skip-download", "--no-warnings", f"ytsearch{SEARCH_LIMIT}:{query}"],
        timeout=120,
        max_chars=10_000_000,
    )
    if not result["ok"]:
        return {"type": "search", "source": "yt_dlp_search", "results": [], "errors": [{"stage": "youtube_search", "error": result.get("error") or trim(result.get("stderr", ""), 1000)}]}
    items = load_json_lines(result["stdout"], limit=SEARCH_LIMIT)
    return {"type": "search", "source": "yt_dlp_search", "results": [compact_youtube_item(item) for item in items if isinstance(item, dict)], "errors": []}


# ── Telegram (tg) ────────────────────────────────────────────────
#
# A user-session MTProto reader. "Restrict saving content" is a client-side UI
# flag only: a logged-in user account sees the text, media and metadata like any
# member, so a private/no-forward channel this account belongs to is readable.
# Media has no public URL — it is streamed through this proxy's /tgfile route,
# so the Reach app downloads it exactly like any other image/video URL.

TG_LINK_RE = re.compile(
    r"t\.me/(?:c/(?P<cid>\d+)|(?P<user>[A-Za-z0-9_]{4,32}))/(?:\d+/)?(?P<msg>\d+)"
)

_tg_client: Any = None
_tg_lock = asyncio.Lock()


def parse_tg_link(query: str):
    """(peer, canonical_url, source_id, media_ref, msg_id) for a t.me message link, else None."""
    match = TG_LINK_RE.search(query or "")
    if not match:
        return None
    msg_id = int(match.group("msg"))
    cid = match.group("cid")
    if cid:
        from telethon.tl.types import PeerChannel

        peer = PeerChannel(int(cid))
        return peer, f"https://t.me/c/{cid}/{msg_id}", f"c{cid}_{msg_id}", f"priv={cid}", msg_id
    user = match.group("user")
    return user, f"https://t.me/{user}/{msg_id}", f"{user}_{msg_id}", f"user={user}", msg_id


async def get_tg_client() -> Any:
    """Lazily connect a single shared Telethon user client on the app event loop."""
    global _tg_client
    if _tg_client is not None and _tg_client.is_connected():
        return _tg_client
    async with _tg_lock:
        if _tg_client is not None and _tg_client.is_connected():
            return _tg_client
        try:
            from telethon import TelegramClient
        except Exception as exc:  # telethon optional, like Pillow
            raise RuntimeError("not_configured: telethon is not installed") from exc
        if not TG_API_ID or not TG_API_HASH:
            raise RuntimeError("not_configured: Telegram api_id/api_hash are not set")
        TG_SESSION.parent.mkdir(parents=True, exist_ok=True)
        client = TelegramClient(str(TG_SESSION), TG_API_ID, TG_API_HASH)
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            raise RuntimeError("auth_required: Telegram session is not authorized")
        _tg_client = client
        return _tg_client


def tg_media_url(media_ref: str, msg_id: int) -> str:
    return f"{PUBLIC_URL}/tgfile?{media_ref}&msg={msg_id}&pwd={quote(PASSWORD, safe='')}"


def tg_message_media(msg: Any, media_ref: str) -> list[Any]:
    """Media entries (image URL string / {type:video,url}) for one message, or []."""
    file = getattr(msg, "file", None)
    if getattr(msg, "media", None) is None or file is None:
        return []
    mime = as_text(getattr(file, "mime_type", "") or "")
    media_url = tg_media_url(media_ref, msg.id)
    if getattr(msg, "photo", None) is not None or mime.startswith("image/"):
        return [media_url]
    if getattr(msg, "voice", None) is None and getattr(msg, "audio", None) is None:
        # video or other downloadable document
        return [{"type": "video", "url": media_url}]
    return []


def tg_message_stats(msg: Any) -> dict[str, int]:
    reactions = 0
    reaction_obj = getattr(msg, "reactions", None)
    if reaction_obj is not None and getattr(reaction_obj, "results", None):
        for res in reaction_obj.results:
            reactions += int(getattr(res, "count", 0) or 0)
    replies_obj = getattr(msg, "replies", None)
    reply_count = int(getattr(replies_obj, "replies", 0) or 0) if replies_obj is not None else 0
    return {
        "likes": reactions,
        "comments": reply_count,
        "reposts": int(getattr(msg, "forwards", 0) or 0),
        "views": int(getattr(msg, "views", 0) or 0),
        "collects": 0,
    }


async def tg_album_messages(client: Any, entity: Any, focal: Any) -> list[Any]:
    """All messages of the album `focal` belongs to (shared grouped_id), id-ordered.

    Telegram albums are up to 10 consecutive messages sharing one grouped_id; the
    caption usually sits on a single member. Returns [focal] when it is not an album.
    """
    grouped_id = getattr(focal, "grouped_id", None)
    if not grouped_id:
        return [focal]
    ids = list(range(focal.id - 9, focal.id + 10))
    try:
        window = await client.get_messages(entity, ids=ids)
    except Exception:
        return [focal]
    members = [m for m in window if m is not None and getattr(m, "grouped_id", None) == grouped_id]
    if focal.id not in {m.id for m in members}:
        members.append(focal)
    members.sort(key=lambda m: m.id)
    return members or [focal]


async def tg_run_messages(client: Any, entity: Any, focal: Any) -> list[Any]:
    """The run of consecutive messages the focal shares with the SAME sender.

    In a group/supergroup a person often splits one thought across several
    messages in a row; a link to any of them should save the whole run. Bounded
    by a change of sender, a time gap, and a hard count. Disabled for broadcast
    channels, where every post carries the channel as its "sender" and posts are
    independent — there only real albums (grouped_id) are merged.
    """
    if getattr(entity, "broadcast", False):
        return [focal]
    sender_id = getattr(focal, "sender_id", None)
    if sender_id is None:
        return [focal]
    ids = list(range(focal.id - TG_RUN_MAX, focal.id + TG_RUN_MAX + 1))
    try:
        window = await client.get_messages(entity, ids=ids)
    except Exception:
        return [focal]
    present = {m.id: m for m in window if m is not None}
    present[focal.id] = focal
    ordered = [present[i] for i in sorted(present)]
    idx = next((i for i, m in enumerate(ordered) if m.id == focal.id), None)
    if idx is None:
        return [focal]

    def same_run(a: Any, b: Any) -> bool:
        if getattr(b, "sender_id", None) != sender_id:
            return False
        da, db = getattr(a, "date", None), getattr(b, "date", None)
        if da is None or db is None:
            return True
        return abs((db - da).total_seconds()) <= TG_RUN_GAP_SECONDS

    run = [ordered[idx]]
    left = idx
    while left - 1 >= 0 and same_run(ordered[left], ordered[left - 1]) and len(run) < TG_RUN_MAX:
        run.insert(0, ordered[left - 1])
        left -= 1
    right = idx
    while right + 1 < len(ordered) and same_run(ordered[right], ordered[right + 1]) and len(run) < TG_RUN_MAX:
        run.append(ordered[right + 1])
        right += 1
    return run


async def compact_telegram_message(
    messages: list[Any], focal: Any, chat: Any, url: str, source_id: str, media_ref: str
) -> dict[str, Any]:
    try:
        sender = await focal.get_sender()
    except Exception:
        sender = None

    chat_title = as_text(getattr(chat, "title", "") or "")
    chat_username = as_text(getattr(chat, "username", "") or "")
    sender_name = ""
    sender_handle = ""
    if sender is not None:
        first = as_text(getattr(sender, "first_name", "") or "")
        last = as_text(getattr(sender, "last_name", "") or "")
        sender_name = (first + " " + last).strip() or as_text(getattr(sender, "title", "") or "")
        sender_handle = as_text(getattr(sender, "username", "") or "")

    name = sender_name or chat_title or "Telegram"
    handle = sender_handle or chat_username
    author_url = f"https://t.me/{handle}" if handle else ""

    # Merge captions and media across every album member (id-ordered).
    texts: list[str] = []
    images: list[Any] = []
    for m in messages:
        t = as_text(getattr(m, "message", "") or "").strip()
        if t:
            texts.append(t)
        images.extend(tg_message_media(m, media_ref))
    text = "\n\n".join(texts)

    # Album stats live on one member; take the per-field maximum.
    merged = {"likes": 0, "comments": 0, "reposts": 0, "views": 0, "collects": 0}
    for m in messages:
        for key, value in tg_message_stats(m).items():
            if value > merged[key]:
                merged[key] = value

    first_line = text.strip().splitlines()[0] if text.strip() else ""
    title = trim(first_line, 100) if first_line else (f"{chat_title} 的消息" if chat_title else "Telegram 消息")
    date_msg = messages[0] if messages else focal

    return {
        "id": source_id,
        "url": url,
        "title": title,
        "content": text,
        "author": {
            "id": as_text(getattr(sender, "id", "") or getattr(chat, "id", "") or ""),
            "name": name,
            "handle": handle,
            "url": author_url,
            "avatar_url": "",
        },
        "created_at": to_iso8601(date_msg.date.isoformat()) if getattr(date_msg, "date", None) else "",
        "stats": merged,
        "images": images,
        "comments": [],
    }


async def reach_telegram(query: str, *, single: bool = False, **_: Any) -> dict[str, Any]:
    parsed = parse_tg_link(query)
    if not parsed:
        return {
            "type": "item",
            "source": "telegram",
            "item": {},
            "errors": [{"stage": "telegram", "error": "unsupported: not a t.me message link"}],
        }
    peer, url, source_id, media_ref, msg_id = parsed
    try:
        client = await get_tg_client()
    except RuntimeError as exc:
        return {"type": "item", "source": "telegram", "item": {}, "errors": [{"stage": "telegram", "error": str(exc)}]}
    try:
        entity = await client.get_entity(peer)
        msg = await client.get_messages(entity, ids=msg_id)
    except Exception as exc:
        return {"type": "item", "source": "telegram", "item": {}, "errors": [{"stage": "telegram_fetch", "error": str(exc)}]}
    if msg is None:
        return {
            "type": "item",
            "source": "telegram",
            "item": {},
            "errors": [{"stage": "telegram", "error": "not found: message unavailable"}],
        }
    # Save a whole batch under one link: a Telegram album (grouped_id), or —
    # in a group chat — a run of consecutive messages from the same sender.
    # `single` skips both merges and imports only the linked message's own media
    # (article media import wants just the one resource, not the group).
    if single:
        messages = [msg]
    elif getattr(msg, "grouped_id", None):
        messages = await tg_album_messages(client, entity, msg)
    else:
        messages = await tg_run_messages(client, entity, msg)
    item = await compact_telegram_message(messages, msg, entity, url, source_id, media_ref)
    return {"type": "item", "source": "telegram", "item": item, "errors": []}


async def tgfile(request: Request) -> Response:
    """Stream a Telegram message's media (photo/video/document) through the proxy."""
    pwd = request.query_params.get("pwd", "")
    if not secrets.compare_digest(pwd, PASSWORD):
        return api_response({"ok": False, "error": "Unauthorized"}, status_code=401)
    priv = request.query_params.get("priv", "").strip()
    user = request.query_params.get("user", "").strip()
    msg_raw = request.query_params.get("msg", "").strip()
    if not msg_raw.isdigit() or (not priv and not user):
        return api_response({"ok": False, "error": "priv/user and msg are required"}, status_code=400)
    try:
        client = await get_tg_client()
    except RuntimeError as exc:
        return api_response({"ok": False, "error": str(exc)}, status_code=503)
    if priv:
        from telethon.tl.types import PeerChannel

        peer: Any = PeerChannel(int(priv))
    else:
        peer = user
    try:
        entity = await client.get_entity(peer)
        msg = await client.get_messages(entity, ids=int(msg_raw))
    except Exception as exc:
        return api_response({"ok": False, "error": str(exc)}, status_code=502)
    if msg is None or getattr(msg, "media", None) is None or getattr(msg, "file", None) is None:
        return api_response({"ok": False, "error": "no downloadable media on this message"}, status_code=404)

    file = msg.file
    media_type = as_text(getattr(file, "mime_type", "") or "") or "application/octet-stream"
    size = getattr(file, "size", None)
    headers = {
        "Access-Control-Allow-Origin": "*",
        "Cache-Control": "public, max-age=86400",
    }
    if size:
        headers["Content-Length"] = str(size)

    async def stream():
        async for chunk in client.iter_download(msg):
            yield chunk

    return StreamingResponse(stream(), media_type=media_type, headers=headers)


def normalize_platform(platform: str) -> str:
    return PLATFORM_ALIASES.get(platform.strip().lower(), "")


def infer_platform_from_url(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if host.endswith("xhslink.com") or host.endswith("xiaohongshu.com"):
        return "xhs"
    if host.endswith("x.com") or host.endswith("twitter.com"):
        return "x"
    if host.endswith("youtube.com") or host.endswith("youtu.be"):
        return "youtube"
    if host == "t.me" or host.endswith(".t.me"):
        return "tg"
    return "web"


def parse_mcp_query(query: str) -> tuple[str, str]:
    query = query.strip()
    match = re.match(
        r"^(xhs|xiaohongshu|rednote|x|twitter|web|youtube|yt|tg|telegram)\s*[:：]\s*(.+)$",
        query,
        flags=re.IGNORECASE,
    )
    if match:
        return normalize_platform(match.group(1)), match.group(2).strip()

    url = extract_url(query)
    if url:
        return infer_platform_from_url(url), query

    return "web", query


def mcp_search_result_from_item(item: dict[str, Any]) -> MCPSearchResult | None:
    raw_url = as_text(item.get("url") or "")
    raw_id = as_text(item.get("id") or raw_url)
    url = raw_url or (raw_id if raw_id.startswith(("http://", "https://")) else "")
    if not url:
        return None
    title = as_text(item.get("title") or item.get("content") or url).strip() or url
    return MCPSearchResult(id=url, title=trim(title, 200), url=url)


def mcp_search_output_from_payload(payload: dict[str, Any]) -> MCPSearchOutput:
    source_items: list[Any]
    if payload.get("type") == "item" and isinstance(payload.get("item"), dict):
        source_items = [payload["item"]]
    else:
        source_items = payload.get("results") or []

    results: list[MCPSearchResult] = []
    seen_urls: set[str] = set()
    for item in source_items:
        if not isinstance(item, dict):
            continue
        result = mcp_search_result_from_item(item)
        if not result or result.url in seen_urls:
            continue
        seen_urls.add(result.url)
        results.append(result)
    return MCPSearchOutput(results=results)


def mcp_fetch_output_from_payload(payload: dict[str, Any], fallback_id: str) -> MCPFetchOutput:
    item = payload.get("item") if isinstance(payload.get("item"), dict) else {}
    if not item and payload.get("results"):
        first = payload["results"][0]
        item = first if isinstance(first, dict) else {}

    url = as_text(item.get("url") or fallback_id)
    title = as_text(item.get("title") or url or fallback_id)
    text_parts = [
        as_text(item.get("content") or ""),
        as_text(item.get("transcript") or ""),
    ]
    comments = item.get("comments") or []
    if comments:
        comment_lines = []
        for comment in comments[:COMMENT_LIMIT]:
            if not isinstance(comment, dict):
                continue
            content = as_text(comment.get("content") or "").strip()
            author = (comment.get("author") or {}).get("name") if isinstance(comment.get("author"), dict) else ""
            if content:
                comment_lines.append(f"{author}: {content}" if author else content)
        if comment_lines:
            text_parts.append("Comments:\n" + "\n".join(comment_lines))

    text = trim("\n\n".join(part for part in text_parts if part.strip()), 120000)
    metadata = {
        "platform": payload.get("platform"),
        "source": payload.get("source"),
        "type": payload.get("type"),
    }
    if item.get("metadata") and isinstance(item.get("metadata"), dict):
        metadata.update(item["metadata"])
    return MCPFetchOutput(
        id=url,
        title=title,
        text=text or title,
        url=url,
        metadata={key: value for key, value in metadata.items() if value not in (None, "")},
    )


def redact_inline_image_data(value: Any) -> Any:
    if isinstance(value, list):
        return [redact_inline_image_data(item) for item in value]
    if not isinstance(value, dict):
        return value
    if value.get("type") == "image" and isinstance(value.get("data"), str) and value.get("mimeType"):
        return {
            key: redact_inline_image_data(item)
            for key, item in value.items()
            if key != "data"
        } | {"data": "<inline image data returned as MCP image content block>"}
    return {key: redact_inline_image_data(item) for key, item in value.items()}


def inline_image_blocks_from_value(value: Any) -> list[ImageContent]:
    if isinstance(value, list):
        blocks: list[ImageContent] = []
        for item in value:
            blocks.extend(inline_image_blocks_from_value(item))
        return blocks
    if not isinstance(value, dict):
        return []
    data = value.get("data")
    mime_type = value.get("mimeType")
    if value.get("type") == "image" and isinstance(data, str) and isinstance(mime_type, str):
        return [ImageContent(type="image", data=data, mimeType=mime_type)]
    blocks: list[ImageContent] = []
    for item in value.values():
        blocks.extend(inline_image_blocks_from_value(item))
    return blocks


def mcp_image_blocks_from_payload(payload: dict[str, Any]) -> list[ImageContent]:
    item = payload.get("item") if isinstance(payload.get("item"), dict) else {}
    return inline_image_blocks_from_value(item)


def mcp_payload_result(payload: dict[str, Any]) -> CallToolResult:
    structured = redact_inline_image_data(payload)
    text = json.dumps(structured, ensure_ascii=False, separators=(",", ":"))
    return CallToolResult(
        content=[TextContent(type="text", text=text), *mcp_image_blocks_from_payload(payload)],
        structuredContent=structured,
        isError=not bool(payload.get("ok", True)),
    )


ERROR_KIND_TO_STATUS = {
    "rate_limited": 429,
    "not_found": 404,
    "auth_required": 401,
    "unsupported": 400,
    "not_configured": 503,
    "timeout": 504,
    "internal": 500,
}

ERROR_KIND_MESSAGES = {
    "rate_limited": "Upstream platform is rate-limiting requests; retry later.",
    "not_found": "Requested content was not found on the upstream platform.",
    "auth_required": "Upstream platform requires authentication; backend is not logged in.",
    "unsupported": "Platform or query is not supported.",
    "not_configured": "Backend command for this platform is not installed or not configured.",
    "timeout": "Upstream command timed out.",
    "internal": "Internal error while reaching the upstream platform.",
}


def classify_error(stage: str, error_text: str) -> tuple[str, str]:
    """Classify an upstream error string into a stable ``kind`` and a sanitized message.

    The raw stderr/exception text is never returned to the caller; only a short
    human-readable message plus the stage is kept for debugging.
    """
    text = (error_text or "").lower()
    if "no such file or directory" in text or "not found" in text and "command" in text:
        return "not_configured", ERROR_KIND_MESSAGES["not_configured"]
    if "timed out" in text or "command timed out" in text or "timeout" in text:
        return "timeout", ERROR_KIND_MESSAGES["timeout"]
    if "429" in text or "rate limit" in text or "too many requests" in text:
        return "rate_limited", ERROR_KIND_MESSAGES["rate_limited"]
    if "not_authenticated" in text or "missing credentials" in text or "unauthorized" in text or "auth_token" in text or "login" in text or "未登录" in text or "需要登录" in text or " 401" in text:
        return "auth_required", ERROR_KIND_MESSAGES["auth_required"]
    if "404" in text or "does not exist" in text or "no status found" in text or "not found" in text or "video unavailable" in text or "private video" in text or "资源不存在" in text or "不存在" in text:
        return "not_found", ERROR_KIND_MESSAGES["not_found"]
    return "internal", ERROR_KIND_MESSAGES["internal"]


def structured_error(stage: str, error_text: str) -> dict[str, Any]:
    kind, message = classify_error(stage, error_text)
    return {"stage": stage, "kind": kind, "message": message}


async def resolve_reach(platform: str, query: str, *, inline_images: bool = True, single: bool = False) -> tuple[dict[str, Any], int]:
    raw_platform = platform.strip()
    platform = normalize_platform(raw_platform)
    query = query.strip()
    if not raw_platform or not query:
        return {"ok": False, "error": "platform and query are required"}, 400
    if not platform:
        return {"ok": False, "error": "Unsupported platform. Use xhs, x, web, youtube, or tg."}, 400

    try:
        if platform == "xhs":
            payload = await reach_xhs(query, inline_images=inline_images)
        elif platform == "x":
            payload = await reach_x(query, inline_images=inline_images)
        elif platform == "web":
            payload = await reach_web(query)
        elif platform == "tg":
            payload = await reach_telegram(query, single=single)
        else:
            payload = await reach_youtube(query)
    except Exception as exc:
        err = structured_error(platform, str(exc))
        return {"ok": False, "platform": platform, "query": query, "error": err["message"], "errors": [err]}, ERROR_KIND_TO_STATUS[err["kind"]]

    raw_errors = payload.get("errors", []) or []
    errors = [structured_error(e.get("stage", platform), e.get("error", "")) for e in raw_errors if isinstance(e, dict)]
    ok = not errors or bool(payload.get("item") or payload.get("results"))
    response = {
        "ok": ok,
        "platform": platform,
        "query": query,
        "type": payload.get("type"),
        "source": payload.get("source"),
        "item": payload.get("item"),
        "results": payload.get("results"),
        "count": len(payload.get("results") or payload.get("item", {}).get("comments", []) or []),
        "errors": errors,
    }
    if ok:
        return response, 200
    primary_kind = errors[0]["kind"] if errors else "internal"
    return response, ERROR_KIND_TO_STATUS.get(primary_kind, 500)


async def reach(request: Request) -> JSONResponse:
    pwd = request.query_params.get("pwd", "")
    if not secrets.compare_digest(pwd, PASSWORD):
        return api_response({"ok": False, "error": "Unauthorized"}, status_code=401)

    single = request.query_params.get("single", "").strip().lower() in {"1", "true", "yes", "on"}
    payload, status_code = await resolve_reach(
        request.query_params.get("platform", ""),
        request.query_params.get("query", ""),
        inline_images=False,
        single=single,
    )
    return api_response(payload, status_code=status_code)


def sanitize_mcp_schema(schema: Any) -> Any:
    if not isinstance(schema, dict):
        return schema
    return _sanitize_mcp_schema(schema, schema.get("$defs") or schema.get("definitions") or {}, set())


def _sanitize_mcp_schema(value: Any, defs: dict[str, Any], seen_refs: set[str]) -> Any:
    if isinstance(value, list):
        return [_sanitize_mcp_schema(item, defs, seen_refs) for item in value]
    if not isinstance(value, dict):
        return value

    ref = value.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/$defs/") and ref not in seen_refs:
        name = ref.removeprefix("#/$defs/")
        target = defs.get(name)
        if isinstance(target, dict):
            merged = deepcopy(target)
            merged.update({key: item for key, item in value.items() if key != "$ref"})
            return _sanitize_mcp_schema(merged, defs, seen_refs | {ref})

    sanitized: dict[str, Any] = {}
    for key, item in value.items():
        if key in {"default", "$defs", "definitions"}:
            continue
        sanitized[key] = _sanitize_mcp_schema(item, defs, seen_refs)
    if sanitized.get("type") == "array" and "items" not in sanitized:
        sanitized["items"] = {}
    return sanitized


async def list_chatgpt_compatible_tools() -> list[MCPTool]:
    tools = await mcp_server.list_tools()
    compatible_tools: list[MCPTool] = []
    for tool in tools:
        payload = tool.model_dump(by_alias=True, exclude_none=True)
        for schema_key in ("inputSchema", "outputSchema"):
            if schema_key in payload:
                payload[schema_key] = sanitize_mcp_schema(payload[schema_key])
        meta = payload.get("_meta") if isinstance(payload.get("_meta"), dict) else {}
        meta = {**meta, "securitySchemes": MCP_TOOL_SECURITY_SCHEMES}
        payload["_meta"] = meta
        payload["securitySchemes"] = MCP_TOOL_SECURITY_SCHEMES
        compatible_tools.append(MCPTool(**payload))
    return compatible_tools


mcp_server = FastMCP(
    "Reach",
    instructions=(
        "Use Reach to read public URLs or search across Xiaohongshu, X/Twitter, "
        "the web, and YouTube. URL queries return one item; text queries return search results."
    ),
    website_url=PUBLIC_URL,
    host=HOST,
    port=PORT,
    streamable_http_path="/mcp",
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[
            "127.0.0.1:*",
            "localhost:*",
            "[::1]:*",
            PUBLIC_HOST,
        ],
        allowed_origins=[
            "http://127.0.0.1:*",
            "http://localhost:*",
            "http://[::1]:*",
            PUBLIC_URL,
            "https://chatgpt.com",
            "https://chat.openai.com",
        ],
    ),
)


@mcp_server.tool(
    name="agent_reach",
    title="Reach",
    description=(
        "Read a public URL/share link or search text on xhs, x, web, or youtube. "
        "Use URL queries for item details and text queries for search results. "
        "Xiaohongshu URL reads return post and comment images inline as image content, not image URL lists."
    ),
    annotations=READ_ONLY_ANNOTATIONS,
    structured_output=False,
)
async def mcp_agent_reach(
    platform: Annotated[
        str,
        Field(description="One of: xhs, x, web, youtube, tg. Aliases such as twitter, yt, telegram, rednote, and xiaohongshu are accepted."),
    ],
    query: Annotated[str, Field(description="A URL/share link to read, or search text to look up.")],
) -> CallToolResult:
    payload, _ = await resolve_reach(platform, query)
    return mcp_payload_result(payload)


@mcp_server.tool(
    name="reach",
    title="Reach",
    description=(
        "Unified Reach tool. Read a URL/share link or search text on xhs, x, web, or youtube. "
        "Xiaohongshu URL reads return post and comment images inline as image content, not image URL lists."
    ),
    annotations=READ_ONLY_ANNOTATIONS,
    structured_output=False,
)
async def mcp_reach(
    platform: Annotated[
        str,
        Field(description="One of: xhs, x, web, youtube, tg. Aliases such as twitter, yt, telegram, rednote, and xiaohongshu are accepted."),
    ],
    query: Annotated[str, Field(description="A URL/share link to read, or search text to look up.")],
) -> CallToolResult:
    payload, _ = await resolve_reach(platform, query)
    return mcp_payload_result(payload)


@mcp_server.tool(
    name="search",
    title="Search",
    description=(
        "Search Reach and return citation-ready results. Defaults to web search. "
        "Use prefixes like xhs:, x:, youtube:, or web: to choose a platform."
    ),
    annotations=READ_ONLY_ANNOTATIONS,
    structured_output=True,
)
async def mcp_search(
    query: Annotated[str, Field(description="Search text. Optional prefix: xhs:, x:, youtube:, or web:. URLs are fetched directly.")],
) -> MCPSearchOutput:
    platform, query = parse_mcp_query(query)
    payload, _ = await resolve_reach(platform, query)
    return mcp_search_output_from_payload(payload)


@mcp_server.tool(
    name="fetch",
    title="Fetch",
    description="Fetch/read a search result by id. Pass the id/url returned by the search tool.",
    annotations=READ_ONLY_ANNOTATIONS,
    structured_output=False,
)
async def mcp_fetch(
    id: Annotated[str, Field(description="The id or canonical URL returned by search.")],
) -> CallToolResult:
    platform, target = parse_mcp_query(id)
    url = extract_url(target) or target
    if extract_url(url):
        platform = infer_platform_from_url(extract_url(url) or url)
    payload, _ = await resolve_reach(platform, url)
    if platform == "xhs" and payload.get("type") == "item":
        return mcp_payload_result(payload)
    output = mcp_fetch_output_from_payload(payload, fallback_id=id)
    structured = output.model_dump(mode="json", exclude_none=True)
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(structured, ensure_ascii=False, separators=(",", ":")))],
        structuredContent=structured,
    )


@mcp_server.tool(
    name="agent_reach_health",
    title="Reach Health",
    description="Return Reach service metadata and supported platforms.",
    annotations=READ_ONLY_ANNOTATIONS,
    structured_output=True,
)
async def mcp_agent_reach_health() -> dict[str, Any]:
    return {
        "ok": True,
        "service": "reach-proxy",
        "platforms": SUPPORTED_PLATFORMS,
        "search_limit": SEARCH_LIMIT,
        "comment_limit": COMMENT_LIMIT,
        "mcp_oauth_token_ttl_seconds": MCP_OAUTH_TOKEN_TTL,
        "mcp_oauth_token_ttl_days": MCP_OAUTH_TOKEN_TTL / 86400,
        "mcp_image_max_bytes": MCP_IMAGE_MAX_BYTES,
        "mcp_image_download_max_bytes": image_download_limit(),
        "image_compression": Image is not None,
        "xhs_image_dedupe_distance": XHS_IMAGE_DEDUPE_DISTANCE,
        "xhs_inline_comment_image_limit": XHS_INLINE_COMMENT_IMAGE_LIMIT,
        "xhs_inline_comment_image_per_comment_limit": XHS_INLINE_COMMENT_IMAGE_PER_COMMENT_LIMIT,
        "public_url": PUBLIC_URL,
        "mcp": "/mcp",
        "mcp_auth_required": MCP_AUTH_REQUIRED,
        "mcp_public_discovery": MCP_PUBLIC_DISCOVERY,
    }


mcp_server._mcp_server.list_tools()(list_chatgpt_compatible_tools)

mcp_app = mcp_server.streamable_http_app()


@asynccontextmanager
async def lifespan(_: Starlette):
    async with mcp_server.session_manager.run():
        yield


app = Starlette(
    debug=False,
    lifespan=lifespan,
    routes=[
        Route("/", root, methods=["GET"]),
        Route("/healthz", healthz, methods=["GET"]),
        Route("/.well-known/oauth-protected-resource/mcp", oauth_protected_resource_metadata, methods=["GET"]),
        Route("/.well-known/oauth-protected-resource", oauth_protected_resource_metadata, methods=["GET"]),
        Route("/.well-known/oauth-authorization-server/mcp", oauth_authorization_server_metadata, methods=["GET"]),
        Route("/.well-known/oauth-authorization-server", oauth_authorization_server_metadata, methods=["GET"]),
        Route("/.well-known/openid-configuration", oauth_authorization_server_metadata, methods=["GET"]),
        Route("/oauth/register", oauth_register, methods=["POST"]),
        Route("/oauth/authorize", oauth_authorize, methods=["GET", "POST"]),
        Route("/oauth/token", oauth_token, methods=["POST"]),
        Route("/http", reach, methods=["GET"]),
        Route("/http/", reach, methods=["GET"]),
        Route("/http", options, methods=["OPTIONS"]),
        Route("/http/", options, methods=["OPTIONS"]),
        Route("/tgfile", tgfile, methods=["GET"]),
        Route("/tgfile", options, methods=["OPTIONS"]),
        *mcp_app.routes,
    ],
    middleware=[
        Middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
            allow_headers=["*"],
            expose_headers=["Mcp-Session-Id"],
        ),
        Middleware(MCPAuthMiddleware),
    ],
)


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT)
