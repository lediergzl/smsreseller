"""
herosms_api.py - Cliente asíncrono para la API de HeroSMS.

IMPORTANTE: HeroSMS es el sucesor de SMS-Activate y usa su MISMO protocolo:
un único endpoint `stubs/handler_api.php`, con la acción indicada por el
parámetro `action=` (no rutas REST separadas como /getServices, /getNumber).
Muchas respuestas son TEXTO PLANO con formato "CODIGO:valor1:valor2" en vez
de JSON (ej: "ACCESS_NUMBER:1234567:34987654321", "STATUS_OK:987654").
Otras acciones (getServicesList, getCountries, getPrices) sí devuelven JSON.

Documentación: https://hero-sms.com/api
"""
import asyncio
import json
import logging
import time
from typing import Optional
import aiohttp
from config import HEROSMS_API_KEY, HEROSMS_API_URL

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)

# Sesión HTTP compartida y reutilizable (ver mismo motivo en ccpay_api.py).
_session: Optional[aiohttp.ClientSession] = None
_session_lock = asyncio.Lock()


async def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        async with _session_lock:
            if _session is None or _session.closed:
                connector = aiohttp.TCPConnector(limit=50, ttl_dns_cache=300)
                _session = aiohttp.ClientSession(timeout=REQUEST_TIMEOUT, connector=connector)
    return _session


async def close_session():
    """Llamar al apagar el bot (main.py) para cerrar limpiamente la sesión."""
    global _session
    if _session and not _session.closed:
        await _session.close()

# ── Caché en memoria ────────────────────────────────────────────────────────
SERVICES_CACHE_TTL = 600     # 10 minutos (los servicios cambian poco)
COUNTRIES_CACHE_TTL = 3600   # 1 hora (los países casi no cambian)
FAILURE_BACKOFF_TTL = 60     # si falla la consulta, no reintentar antes de 60s
                              # (evita que CADA compra pague un timeout de 15s
                              # completo mientras el endpoint esté caído/lento)

_services_cache: dict = {"data": [], "ts": 0.0}
_countries_cache: dict = {"data": {}, "ts": 0.0}  # {country_id: name}


# ── Llamada base al endpoint único ─────────────────────────────────────────

async def _call(action: str, params: dict = None) -> str:
    """
    Llama a handler_api.php?action=<action>&... y devuelve el cuerpo
    de la respuesta como texto crudo (puede ser texto plano o JSON).

    Se fuerza Accept-Encoding a "gzip, deflate" (sin "br") para evitar
    depender de que la librería Brotli esté correctamente instalada en
    el sistema; hero-sms.com respeta este header y responde sin Brotli.
    """
    if params is None:
        params = {}
    params = {**params, "api_key": HEROSMS_API_KEY, "action": action}

    url = f"{HEROSMS_API_URL.rstrip('/')}/stubs/handler_api.php"
    headers = {"Accept-Encoding": "gzip, deflate"}
    session = await _get_session()
    async with session.get(url, params=params, headers=headers) as resp:
        resp.raise_for_status()
        text = await resp.text()
        logger.debug("HeroSMS action=%s -> %s", action, text[:300])
        return text.strip()


async def _call_json(action: str, params: dict = None):
    """Como _call pero intenta parsear la respuesta como JSON."""
    text = await _call(action, params)
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        logger.warning("Respuesta no-JSON para action=%s: %s", action, text[:200])
        return {}


# ── Servicios ────────────────────────────────────────────────────────────────

async def get_services(force_refresh: bool = False) -> list[dict]:
    """
    action=getServicesList
    Devuelve TODOS los servicios disponibles, normalizados y cacheados.
    Cada item: {"code": "tg", "name": "Telegram"}
    """
    now = time.time()
    if (
        not force_refresh
        and _services_cache["data"]
        and (now - _services_cache["ts"] < SERVICES_CACHE_TTL)
    ):
        return _services_cache["data"]

    try:
        data = await _call_json("getServicesList")
        raw = data.get("services", []) if isinstance(data, dict) else []

        normalized = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            code = item.get("code")
            if not code:
                continue
            normalized.append({
                "code": str(code),
                "name": str(item.get("name") or code.upper()),
            })

        if normalized:
            _services_cache["data"] = normalized
            _services_cache["ts"] = now
            return normalized

        logger.warning("get_services devolvió vacío, usando caché anterior si existe.")
        _services_cache["ts"] = now - SERVICES_CACHE_TTL + FAILURE_BACKOFF_TTL
        return _services_cache["data"]
    except Exception as exc:
        logger.error("get_services error: %s: %s", type(exc).__name__, exc)
        _services_cache["ts"] = now - SERVICES_CACHE_TTL + FAILURE_BACKOFF_TTL
        return _services_cache["data"]


async def search_services(query: str, limit: int = 20) -> list[dict]:
    """
    Busca servicios por nombre o código (case-insensitive, substring match).
    Si query está vacío o es "todos"/"all"/"populares", devuelve los primeros
    `limit` servicios tal cual los entrega la API.
    """
    services = await get_services()
    q = query.strip().lower()

    if not q or q in ("todos", "all", "populares", "*"):
        return services[:limit]

    matches = [
        s for s in services
        if q in s["name"].lower() or q in s["code"].lower()
    ]
    return matches[:limit]


async def get_service_name(code: str) -> str:
    """Devuelve el nombre legible de un servicio a partir de su código."""
    services = await get_services()
    for s in services:
        if s["code"] == code:
            return s["name"]
    return code.upper()


# ── Países ───────────────────────────────────────────────────────────────────

async def _get_countries_map() -> dict:
    """Cachea id_pais -> nombre (en inglés) usando action=getCountries."""
    now = time.time()
    if _countries_cache["data"] and (now - _countries_cache["ts"] < COUNTRIES_CACHE_TTL):
        return _countries_cache["data"]

    try:
        data = await _call_json("getCountries")
        items = data.values() if isinstance(data, dict) else (data or [])

        mapping = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            cid = item.get("id")
            name = item.get("eng") or item.get("name")
            if cid is not None and name:
                mapping[str(cid)] = str(name)

        if mapping:
            _countries_cache["data"] = mapping
            _countries_cache["ts"] = now
            return mapping

        return _countries_cache["data"]
    except Exception as exc:
        logger.error("get_countries (mapa) error: %s: %s", type(exc).__name__, exc)
        # Backoff corto para no repetir un timeout de 15s en cada compra
        # mientras el endpoint siga fallando.
        _countries_cache["ts"] = now - COUNTRIES_CACHE_TTL + FAILURE_BACKOFF_TTL
        return _countries_cache["data"]


async def get_countries(service: str) -> list[dict]:
    """
    Devuelve países disponibles CON precio (en USD) y stock para el servicio
    dado, usando action=getPrices (trae precio+cantidad por país en UNA sola
    llamada, en vez de consultar país por país).

    Respuesta de ejemplo esperada de getPrices:
        {"0": {"tg": {"cost": 0.10, "count": 123}}, "6": {"tg": {...}}, ...}

    Devuelve: [{"country": "0", "name": "Russia", "price": 0.10, "count": 123}, ...]
    """
    try:
        try:
            prices, country_names = await asyncio.gather(
                _call_json("getPrices", {"service": service}),
                _get_countries_map(),
            )
        except asyncio.TimeoutError:
            # Un solo reintento: HeroSMS a veces tiene picos puntuales de
            # lentitud; no vale la pena rendirse tras el primer timeout.
            logger.warning(
                "getPrices(%s) timeout, reintentando una vez...", service
            )
            prices, country_names = await asyncio.gather(
                _call_json("getPrices", {"service": service}),
                _get_countries_map(),
            )
        if not isinstance(prices, dict):
            logger.warning("getPrices(%s) devolvió formato inesperado: %r", service, prices)
            return []

        result = []
        for country_id, services_at_country in prices.items():
            if not isinstance(services_at_country, dict):
                continue
            info = services_at_country.get(service)
            if not info:
                continue
            count = int(info.get("count", 0) or 0)
            if count <= 0:
                continue  # sin stock, no ofrecer
            result.append({
                "country": str(country_id),
                "name": country_names.get(str(country_id), f"País {country_id}"),
                "price": float(info.get("cost", 0) or 0),
                "count": count,
            })

        result.sort(key=lambda c: c["price"])
        return result
    except asyncio.TimeoutError:
        logger.error(
            "get_countries(%s) timeout: HeroSMS no respondió en %ds",
            service, REQUEST_TIMEOUT.total,
        )
        return []
    except Exception as exc:
        logger.error("get_countries(%s) error: %s: %s", service, type(exc).__name__, exc)
        return []


# ── Números / activaciones ────────────────────────────────────────────────────

async def get_number(service: str, country: str) -> Optional[dict]:
    """
    action=getNumber
    Solicita un número virtual. Descuenta el costo del saldo de HeroSMS.

    Respuesta esperada: texto "ACCESS_NUMBER:<id>:<numero>"
    Errores comunes: "NO_NUMBERS", "NO_BALANCE", "BAD_SERVICE", "BAD_KEY"

    Devuelve: {"id": "1234567", "number": "34987654321"} o None si falla.
    """
    try:
        text = await _call("getNumber", {"service": service, "country": country})
        parts = text.split(":")
        if parts[0] == "ACCESS_NUMBER" and len(parts) >= 3:
            return {"id": parts[1], "number": parts[2]}
        logger.warning("get_number(%s, %s) sin número disponible: %s", service, country, text)
        return None
    except Exception as exc:
        logger.error("get_number(%s, %s) error: %s: %s", service, country, type(exc).__name__, exc)
        return None


async def get_status(activation_id: str) -> dict:
    """
    action=getStatus
    Verifica si ya llegó el código SMS.

    Respuestas posibles (texto plano):
        "STATUS_OK:123456"    -> código recibido
        "STATUS_WAIT_CODE"    -> esperando código
        "STATUS_WAIT_RETRY:x" -> esperando reintento
        "STATUS_CANCEL"       -> activación cancelada
    """
    try:
        text = await _call("getStatus", {"id": activation_id})

        if text.startswith("STATUS_OK"):
            code = text.split(":", 1)[1] if ":" in text else ""
            return {"status": "ready", "code": code}
        if text.startswith("STATUS_WAIT"):
            return {"status": "pending"}
        if text.startswith("STATUS_CANCEL"):
            return {"status": "cancelled"}

        return {"status": text}
    except Exception as exc:
        logger.error("get_status(%s) error: %s: %s", activation_id, type(exc).__name__, exc)
        return {"status": "error", "error": str(exc)}


async def set_status_done(activation_id: str) -> bool:
    """
    action=setStatus&status=6
    Confirma que recibimos el código (estado 6 = completado).
    Respuesta esperada: "ACCESS_ACTIVATION"
    """
    try:
        text = await _call("setStatus", {"id": activation_id, "status": 6})
        logger.info("set_status_done(%s) -> %s", activation_id, text)
        return text.startswith("ACCESS")
    except Exception as exc:
        logger.error("set_status_done(%s) error: %s: %s", activation_id, type(exc).__name__, exc)
        return False


async def cancel_number(activation_id: str) -> bool:
    """
    action=setStatus&status=8
    Cancela la activación y reembolsa el costo al saldo de HeroSMS.
    Respuesta esperada: "ACCESS_CANCEL"
    """
    try:
        text = await _call("setStatus", {"id": activation_id, "status": 8})
        logger.info("cancel_number(%s) -> %s", activation_id, text)
        return text.startswith("ACCESS")
    except Exception as exc:
        logger.error("cancel_number(%s) error: %s: %s", activation_id, type(exc).__name__, exc)
        return False


# ── Precalentamiento de caché ─────────────────────────────────────────────────

async def warm_cache():
    """
    Precarga servicios y mapa de países al arrancar el bot, en paralelo,
    para que el primer usuario real no pague el costo de un timeout lento
    (ej. getCountries tardando 15s) durante su compra.
    Se llama como tarea en background desde main.py, sin bloquear el polling.
    """
    results = await asyncio.gather(
        get_services(), _get_countries_map(), return_exceptions=True
    )
    for r in results:
        if isinstance(r, Exception):
            logger.warning("warm_cache: fallo parcial precargando caché: %s", r)
    logger.info("warm_cache: caché de servicios/países precargada.")