"""
admin_dashboard.py - Panel de control web para el admin, montado sobre el
mismo servidor aiohttp que ya usa el bot en modo webhook (ver main.py).

QUÉ ES Y QUÉ NO ES (primera versión):
  - Es un panel de SOLO LECTURA: muestra compras, depósitos, retiros,
    referidos y errores en vivo (auto-refresh cada 5s), para poder ver
    "quién está comprando, qué pasó" sin entrar a Telegram.
  - NO aprueba/rechaza nada todavía (eso sigue funcionando igual que
    siempre, con los botones de Telegram). Se agrega como paso 2 más
    adelante, reusando la misma lógica de crédito/débito que ya funciona,
    en vez de duplicarla acá a las apuradas.

AUTENTICACIÓN: Telegram Login Widget.
  - El navegador manda tus datos de Telegram (id, first_name, username,
    auth_date) firmados por Telegram con HMAC-SHA256 usando el BOT_TOKEN
    como secreto. El servidor reproduce esa firma y la compara: si no
    coincide, o el campo `hash` fue alterado, o el login es muy viejo
    (>1 día), se rechaza. Esto es exactamente el mecanismo que documenta
    Telegram para "Login with Telegram" - no es una firma inventada acá.
  - Además de la firma válida, se exige que el `id` esté en config.ADMIN_IDS
    -si alguien más loguea con SU cuenta de Telegram, la firma es válida
    (es su cuenta real) pero no es admin, así que se lo rechaza igual.
  - REQUISITO EN BOTFATHER: correr /setdomain en @BotFather y setear el
    dominio público del bot (ej. smsreseller.onrender.com) - sin esto el
    widget de Telegram no se muestra (Telegram lo bloquea por seguridad
    hasta que el dueño del bot confirma el dominio donde va a vivir).

SESIÓN: cookie firmada con HMAC (no hace falta ninguna librería nueva:
  solo hmac/hashlib/base64/json de la stdlib). Contiene {user_id, exp} y
  su firma; se verifica en cada request a /admin/*. Nada de estado en el
  servidor (no hay tabla de sesiones), así que sobrevive perfecto a un
  restart del proceso.
"""
import hashlib
import hmac
import json
import base64
import logging
import pathlib
import time
from datetime import datetime

from aiohttp import web

import config
from database import db

logger = logging.getLogger(__name__)

_STATIC_DIR = pathlib.Path(__file__).parent / "admin_static"


SESSION_COOKIE = "admin_session"
SESSION_MAX_AGE_SECONDS = 7 * 24 * 3600   # 7 días
TELEGRAM_AUTH_MAX_AGE_SECONDS = 24 * 3600  # el login de Telegram no puede ser viejo


def _session_secret() -> bytes:
    # Reutiliza BOT_TOKEN como semilla del secreto de sesión: es un valor
    # ya secreto que rotarías si se filtrara, y evita pedir una variable de
    # entorno más solo para esto. Se hashea para no usar el token crudo
    # como clave HMAC.
    return hashlib.sha256(config.BOT_TOKEN.encode()).digest()


def _sign(payload: bytes) -> str:
    return hmac.new(_session_secret(), payload, hashlib.sha256).hexdigest()


def _make_session_cookie(user_id: int) -> str:
    data = {"user_id": user_id, "exp": int(time.time()) + SESSION_MAX_AGE_SECONDS}
    raw = json.dumps(data, separators=(",", ":")).encode()
    b64 = base64.urlsafe_b64encode(raw).decode()
    sig = _sign(raw)
    return f"{b64}.{sig}"


def _verify_session_cookie(cookie_value: str) -> int | None:
    """Devuelve el user_id si la cookie es válida y no expiró, si no None."""
    try:
        b64, sig = cookie_value.split(".", 1)
        raw = base64.urlsafe_b64decode(b64.encode())
        if not hmac.compare_digest(sig, _sign(raw)):
            return None
        data = json.loads(raw)
        if data["exp"] < time.time():
            return None
        return int(data["user_id"])
    except Exception:
        return None


def _verify_telegram_login(params: dict) -> int | None:
    """
    Verifica la firma del Telegram Login Widget. `params` son los query
    params que Telegram agrega al redirigir de vuelta (id, first_name,
    username, photo_url, auth_date, hash). Devuelve el `id` de Telegram si
    la firma es válida y no es demasiado vieja, si no None.
    """
    data = dict(params)
    received_hash = data.pop("hash", None)
    if not received_hash:
        return None

    check_string = "\n".join(f"{k}={data[k]}" for k in sorted(data.keys()))
    secret_key = hashlib.sha256(config.BOT_TOKEN.encode()).digest()
    computed_hash = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        logger.warning("Login de Telegram con firma inválida (posible intento de falsificación).")
        return None

    auth_date = int(data.get("auth_date", 0))
    if time.time() - auth_date > TELEGRAM_AUTH_MAX_AGE_SECONDS:
        logger.warning("Login de Telegram expirado (auth_date muy viejo).")
        return None

    return int(data["id"])


def _is_admin(user_id: int) -> bool:
    return user_id in config.ADMIN_IDS


@web.middleware
async def _require_admin_session(request: web.Request, handler):
    # Rutas públicas: la página de login y el callback de Telegram.
    if request.path in ("/admin/login", "/admin/auth/telegram"):
        return await handler(request)
    if not request.path.startswith("/admin"):
        return await handler(request)

    cookie = request.cookies.get(SESSION_COOKIE)
    user_id = _verify_session_cookie(cookie) if cookie else None
    if not user_id or not _is_admin(user_id):
        if request.path.startswith("/admin/api/"):
            return web.json_response({"error": "no autenticado"}, status=401)
        raise web.HTTPFound("/admin/login")

    request["admin_user_id"] = user_id
    return await handler(request)


async def _login_page(request: web.Request) -> web.Response:
    bot_username = getattr(config, "BOT_USERNAME", "") or ""
    if not bot_username:
        return web.Response(
            text=(
                "Falta configurar config.BOT_USERNAME (el @username de tu bot, "
                "sin el @) para poder mostrar el botón de login de Telegram."
            ),
            status=500,
        )
    html = f"""<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8">
<title>Admin · Login</title>
<style>
  body {{ background:#0f1115; color:#e6e8eb; font-family:system-ui,sans-serif;
         display:flex; align-items:center; justify-content:center; height:100vh; margin:0; }}
  .card {{ background:#181b21; padding:40px; border-radius:12px; text-align:center;
           box-shadow:0 4px 24px rgba(0,0,0,.4); }}
  h1 {{ font-size:1.2rem; font-weight:600; margin-bottom:24px; }}
</style></head>
<body>
  <div class="card">
    <h1>Panel de administración</h1>
    <script async src="https://telegram.org/js/telegram-widget.js?22"
      data-telegram-login="{bot_username}"
      data-size="large"
      data-auth-url="/admin/auth/telegram"
      data-request-access="write"></script>
  </div>
</body></html>"""
    return web.Response(text=html, content_type="text/html")


async def _auth_telegram(request: web.Request) -> web.Response:
    user_id = _verify_telegram_login(dict(request.query))
    if not user_id:
        return web.Response(text="Login inválido o expirado.", status=403)
    if not _is_admin(user_id):
        logger.warning("Intento de login al dashboard de un usuario NO admin: %s", user_id)
        return web.Response(text="Tu cuenta no tiene permiso de administrador.", status=403)

    resp = web.HTTPFound("/admin")
    resp.set_cookie(
        SESSION_COOKIE, _make_session_cookie(user_id),
        max_age=SESSION_MAX_AGE_SECONDS, httponly=True, samesite="Lax",
    )
    raise resp


async def _dashboard_page(request: web.Request) -> web.Response:
    # Todo el HTML/CSS/JS vive en un único archivo estático servido acá
    # mismo -no hay build step ni framework de frontend, es intencional
    # para que este panel no dependa de nada más que Python+aiohttp.
    return web.FileResponse(_STATIC_DIR / "dashboard.html")


def _json_default(obj):
    if isinstance(obj, datetime):
        return obj.isoformat()
    return str(obj)


async def _api_summary(request: web.Request) -> web.Response:
    data = await db.get_dashboard_summary()
    return web.json_response(data, dumps=lambda d: json.dumps(d, default=_json_default))


async def _api_transactions(request: web.Request) -> web.Response:
    status = request.query.get("status") or None
    limit = int(request.query.get("limit", 50))
    data = await db.get_recent_transactions_admin(limit=limit, status=status)
    return web.json_response(data, dumps=lambda d: json.dumps(d, default=_json_default))


async def _api_ledger(request: web.Request) -> web.Response:
    limit = int(request.query.get("limit", 50))
    data = await db.get_recent_ledger_admin(limit=limit)
    return web.json_response(data, dumps=lambda d: json.dumps(d, default=_json_default))


async def _api_referrals(request: web.Request) -> web.Response:
    limit = int(request.query.get("limit", 50))
    data = await db.get_recent_referrals_admin(limit=limit)
    return web.json_response(data, dumps=lambda d: json.dumps(d, default=_json_default))


async def _api_manual_deposits(request: web.Request) -> web.Response:
    limit = int(request.query.get("limit", 50))
    data = await db.get_recent_manual_deposits_admin(limit=limit)
    return web.json_response(data, dumps=lambda d: json.dumps(d, default=_json_default))


async def _api_manual_withdrawals(request: web.Request) -> web.Response:
    limit = int(request.query.get("limit", 50))
    data = await db.get_recent_manual_withdrawals_admin(limit=limit)
    return web.json_response(data, dumps=lambda d: json.dumps(d, default=_json_default))


async def _api_refunds(request: web.Request) -> web.Response:
    limit = int(request.query.get("limit", 50))
    data = await db.get_recent_refund_requests_admin(limit=limit)
    return web.json_response(data, dumps=lambda d: json.dumps(d, default=_json_default))


async def _api_errors(request: web.Request) -> web.Response:
    limit = int(request.query.get("limit", 50))
    data = await db.get_recent_errors_admin(limit=limit)
    return web.json_response(data, dumps=lambda d: json.dumps(d, default=_json_default))


def setup_admin_dashboard(app: web.Application) -> None:
    """Llamar una vez desde main.py, pasándole el mismo `app` de aiohttp
    que ya usa el webhook del bot."""
    app.middlewares.append(_require_admin_session)
    app.router.add_get("/admin/login", _login_page)
    app.router.add_get("/admin/auth/telegram", _auth_telegram)
    app.router.add_get("/admin", _dashboard_page)
    app.router.add_get("/admin/api/summary", _api_summary)
    app.router.add_get("/admin/api/transactions", _api_transactions)
    app.router.add_get("/admin/api/ledger", _api_ledger)
    app.router.add_get("/admin/api/referrals", _api_referrals)
    app.router.add_get("/admin/api/manual_deposits", _api_manual_deposits)
    app.router.add_get("/admin/api/manual_withdrawals", _api_manual_withdrawals)
    app.router.add_get("/admin/api/refunds", _api_refunds)
    app.router.add_get("/admin/api/errors", _api_errors)
    logger.info("Dashboard de admin montado en /admin")
