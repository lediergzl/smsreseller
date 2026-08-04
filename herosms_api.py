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
import unicodedata
from difflib import SequenceMatcher
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
        text = await resp.text()
        if resp.status >= 400:
            # Leemos el body ANTES de reventar: si HeroSMS devuelve algo
            # (BAD_ACTION, un mensaje de error, etc.) en el cuerpo de un
            # 4xx/5xx, raise_for_status() lo tiraba sin loguearlo -quedaba
            # solo el código HTTP, sin pista de la causa real.
            logger.error(
                "HeroSMS action=%s -> HTTP %s, body=%s", action, resp.status, text[:300]
            )
        resp.raise_for_status()
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


def _normalize(text: str) -> str:
    """
    Minúsculas + sin acentos/diacríticos (ej. "telegrám" -> "telegram"), para
    que la búsqueda no dependa de que el usuario tipee los acentos exactos
    -algo muy común escribiendo rápido desde el celular.
    """
    text = text.strip().lower()
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if not unicodedata.combining(c))


def _match_score(query_norm: str, query_words: list[str], name_norm: str, code_norm: str) -> Optional[float]:
    """
    Puntaje de relevancia (más BAJO = mejor match) para un servicio dado,
    o None si no matchea en absoluto. Antes esto era un único chequeo
    rígido `q in name.lower() or q in code.lower()` -si el usuario tenía
    una idea vaga o cometía un typo ("wasap", "watsap", "wp"), no
    encontraba nada aunque el servicio SÍ existiera en la lista de
    HeroSMS. Ahora se prueban, de más a menos estricto:

      0. Coincidencia exacta con el nombre o código.
      1. El nombre/código EMPIEZA con lo que escribió.
      2. Todas las palabras de la búsqueda aparecen en el nombre/código,
         en CUALQUIER orden (ej. "insta business" -> "Instagram Business").
      3. Lo que escribió aparece como substring en cualquier parte.
      4. Fuzzy: se PARECE lo suficiente al nombre (tolera errores de
         tipeo y variantes como "wasap" ~ "whatsapp"), via
         difflib.SequenceMatcher. Umbral 0.6 elegido para atrapar typos
         de 1-2 letras en nombres cortos sin generar demasiado ruido.
    """
    if query_norm == name_norm or query_norm == code_norm:
        return 0.0
    if name_norm.startswith(query_norm) or code_norm.startswith(query_norm):
        return 1.0
    if query_words and all(w in name_norm or w in code_norm for w in query_words):
        return 2.0
    if query_norm in name_norm or query_norm in code_norm:
        return 3.0

    ratio = max(
        SequenceMatcher(None, query_norm, name_norm).ratio(),
        SequenceMatcher(None, query_norm, code_norm).ratio(),
    )
    if ratio >= 0.6:
        return 4.0 + (1.0 - ratio)  # cuanto más se parece, mejor (más chico)
    return None


async def search_services(query: str, limit: int = 20) -> list[dict]:
    """
    Busca servicios por nombre o código, tolerando ideas vagas, errores de
    tipeo y orden de palabras distinto (ver _match_score). Si query está
    vacío o es "todos"/"all"/"populares", devuelve los primeros `limit`
    servicios tal cual los entrega la API.
    """
    services = await get_services()
    q = _normalize(query)

    if not q or q in ("todos", "all", "populares", "*"):
        return services[:limit]

    q_words = q.split()

    scored = []
    for s in services:
        name_n = _normalize(s["name"])
        code_n = _normalize(s["code"])
        score = _match_score(q, q_words, name_n, code_n)
        if score is not None:
            scored.append((score, s))

    scored.sort(key=lambda pair: pair[0])
    return [s for _, s in scored[:limit]]


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
        {"0": {"tg": {"cost": 0.10, "count": 123, "physicalCount": 45}}, ...}

    IMPORTANTE -físico vs virtual: "count" incluye TODO el stock (virtual +
    físico), mientras que "physicalCount" es solo el stock de SIMs físicas
    reales. Un país puede tener "count" enorme y "physicalCount": 0 -son
    números 100% virtuales (VOIP). HeroSMS mismo los marca "Solo virtuales"
    en su panel web (ver captura 2026-08-04, Canadá). Servicios como
    WhatsApp rechazan números VOIP para verificación con bastante
    frecuencia -es política de Meta, no algo que dependa de HeroSMS. Por
    eso se descartan acá directamente los países sin NINGÚN número físico:
    es la señal de confiabilidad más simple y directa que tenemos, y a
    diferencia de nuestro propio historial (stock_stats/success_stats) no
    sufre "cold start" -está disponible desde la primera consulta.

    Devuelve: [{"country": "0", "name": "Russia", "price": 0.10,
                "count": 123, "physical_count": 45}, ...]
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
            physical_count = int(info.get("physicalCount", 0) or 0)
            if count <= 0:
                continue  # sin stock, no ofrecer
            if physical_count <= 0:
                continue  # solo virtuales (VOIP) -alto riesgo de rechazo en verificación
            result.append({
                "country": str(country_id),
                "name": country_names.get(str(country_id), f"País {country_id}"),
                "price": float(info.get("cost", 0) or 0),
                "count": count,
                "physical_count": physical_count,
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


async def get_top_countries_quality(service: str, force_refresh: bool = False) -> dict:
    """
    HeroSMS NO expone esta acción para resellers vía API -confirmado en
    logs de producción 2026-08-04: action=getListOfTopCountriesByService
    devuelve HTTP 404 con body {"title":"BAD_ACTION","details":"Method
    Not Found"}. El endpoint del protocolo SMS-Activate que HeroSMS dice
    heredar simplemente no está implementado de su lado.

    Ante eso, esta función devuelve una SEMILLA ESTÁTICA cargada a mano
    con los valores reales del panel web de HeroSMS (Estadísticas -> Top
    10 países, ordenado "Por calidad", ventana de intervalo 24h, umbral
    >500 activaciones exitosas -ver captura 2026-08-04). Es la misma
    métrica que la API debía darnos ("% de activaciones exitosas"), solo
    que cargada manualmente en vez de por request.

    IMPORTANTE -mantenimiento manual:
    - Solo cubre "wa" (WhatsApp) por ahora; para cualquier otro service
      devuelve {} (mismo comportamiento que "sin datos", no penaliza).
    - Estos números se van a desactualizar -no hay forma de refrescarlos
      automáticamente mientras el endpoint siga caído. Revisar a mano en
      hero-sms.com/es/statistics/top10?service=wa cada tanto y actualizar
      _QUALITY_SEED_WA. Si HeroSMS habilita el endpoint real más
      adelante, reemplazar esta función por la llamada a _call_json de
      vuelta (ver implementación anterior en el historial de git).

    Devuelve: {"country_id": rate_0_a_100, ...}
    """
    if service != "wa":
        return {}
    return dict(_QUALITY_SEED_WA)


# Semilla manual -panel web HeroSMS, Estadísticas > Top 10 países > Whatsapp,
# ordenado por calidad, intervalo 24h, umbral >500 activaciones exitosas.
# Capturado 2026-08-04. IDs de país confirmados contra getPrices en logs
# de producción (mismo día). Actualizar a mano periódicamente -ver
# docstring de get_top_countries_quality.
_QUALITY_SEED_WA = {
    "187": 58.02,  # USA
    "73": 29.81,   # Brazil
    "36": 29.39,   # Canada
    "4": 25.02,    # Philippines
    "117": 21.81,  # Portugal
    "15": 17.62,   # Poland
    "52": 15.78,   # Thailand
    "6": 14.2,     # Indonesia
    "62": 13.52,   # Turkey
}


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

    Un HTTP 409 Conflict significa que HeroSMS ya NO considera esta
    activación cancelable -en la práctica, casi siempre porque ya fue
    cancelada antes por otra ruta del bot (ej. el usuario canceló manual
    con /cancelar mientras _poll_sms seguía esperando en background, o un
    redeploy solapó dos instancias sobre la misma tx). El resultado que
    nos importa -que el número quede liberado en HeroSMS- YA se cumplió
    en esos casos, así que se trata como éxito en vez de como fallo real;
    de lo contrario el llamador reintenta/alerta al admin por algo que en
    realidad ya está resuelto.
    """
    try:
        text = await _call("setStatus", {"id": activation_id, "status": 8})
        logger.info("cancel_number(%s) -> %s", activation_id, text)
        return text.startswith("ACCESS")
    except aiohttp.ClientResponseError as exc:
        if exc.status == 409:
            logger.info(
                "cancel_number(%s): HTTP 409 (ya estaba cancelada/cerrada en "
                "HeroSMS, se toma como éxito)", activation_id,
            )
            return True
        logger.error("cancel_number(%s) error: %s: %s", activation_id, type(exc).__name__, exc)
        return False
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