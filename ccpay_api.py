"""
ccpay_api.py - Cliente asíncrono para la API v2 de CCPayment.

MIGRACIÓN v1 -> v2 (obligatoria: la cuenta del merchant quedó "solo v2",
error 225213 "This merchant account can only call API of Version 2"):

- Base URL: https://ccpayment.com/ccpayment/v2/  (antes: admin.ccpayment.com/ccpayment/v1)
- Firma: HMAC-SHA256(key=AppSecret, msg=Appid+Timestamp+body_json), YA NO es el
  SHA-256 plano de v1 (SHA256(appid+appsecret+timestamp+body)).
- Timestamp en MILISEGUNDOS (v1 usaba segundos, 10 dígitos).
- Ya no existe un "token_id" (UUID) único por moneda/red. Ahora un token se
  identifica con DOS campos: coinId (entero) + chain (string, ej. "TRX",
  "POLYGON", "ETH", "BTC"). Para no tocar handlers.py, este módulo sigue
  exponiendo un "token_id" de cara afuera, pero internamente es un string
  compuesto "coinId:chain" que se separa antes de llamar a la API.
- Endpoints usados (todos POST, ver /mnt/... ccpayment-sdk-skills/api/*.md):
    POST /getCoinList                    -> catálogo de monedas/redes + coinId
    POST /getCoinUSDTPrice                -> precio actual de un coinId en USDT
    POST /createAppOrderDepositAddress    -> crear orden de cobro (native checkout)
    POST /getAppDepositRecordList         -> consultar estado de depósitos por orderId
    POST /applyAppWithdrawToNetwork       -> retirar/reembolsar a una dirección externa

NOTA IMPORTANTE SOBRE LOS STATUS DE DEPÓSITO:
La documentación v2 no publica el enum exacto de valores del campo
"status" (string) en AppDepositRecordEntity. El mapeo de abajo asume que
sigue usando los mismos textos que v1 ("Pending", "Successful", "Expired",
etc.). La PRIMERA VEZ que llegue un pago real, revisa el log
"get_order_status raw status" para confirmar el texto exacto y ajusta
_STATUS_MAP si hace falta.
"""
import asyncio
import hashlib
import hmac
import json
import logging
import time
import uuid
from typing import Optional
import aiohttp
from config import CCPAY_API_URL, CCPAY_APP_ID, CCPAY_APP_SECRET

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=20)

# Estados de orden CCPayment (ver campo "status" en AppDepositRecordEntity).
# NO CONFIRMADO 1:1 contra v2 -> verificar con un pago real (ver nota arriba).
ORDER_STATUS_PENDING   = 0
ORDER_STATUS_COMPLETED = 1
ORDER_STATUS_EXPIRED   = 2
ORDER_STATUS_CANCELLED = 3

# Código de error de CCPayment cuando el MERCHANT (no el usuario) no tiene
# saldo suficiente de esa moneda/red puntual para cubrir el retiro. No es un
# error del usuario ni de su saldo interno -> ver refund_user() y su uso en
# handlers.cb_withdraw_confirm, donde se usa para reintentar automáticamente
# con otra moneda de las permitidas en vez de simplemente fallar.
CCPAY_ERR_INSUFFICIENT_MERCHANT_BALANCE = 14000

_STATUS_MAP = {
    "Pending":       ORDER_STATUS_PENDING,
    "Processing":    ORDER_STATUS_PENDING,
    "Success":    ORDER_STATUS_COMPLETED,
    "Overpaid":      ORDER_STATUS_COMPLETED,
    "Expired":       ORDER_STATUS_EXPIRED,
    "Failed":        ORDER_STATUS_CANCELLED,
    "Underpaid":     ORDER_STATUS_PENDING,   # esperar el resto del pago
    "Overdue paid":  ORDER_STATUS_EXPIRED,
    "Multiple paid": ORDER_STATUS_COMPLETED,
}

# Sesión HTTP compartida y reutilizable (evita handshake TCP/TLS por request).
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


def _sign(timestamp: str, body_str: str) -> str:
    """
    v2: HMAC-SHA256(key=AppSecret, msg=Appid + Timestamp + body_json_string).
    (v1 usaba SHA-256 plano sobre appid+appsecret+timestamp+body; ya no aplica.)
    """
    sign_text = f"{CCPAY_APP_ID}{timestamp}{body_str}"
    return hmac.new(
        CCPAY_APP_SECRET.encode("utf-8"),
        sign_text.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


async def _post(path: str, payload: Optional[dict] = None) -> dict:
    """Envía POST firmado a CCPayment v2 y retorna el JSON de respuesta."""
    body_str = json.dumps(payload or {}, separators=(",", ":"))
    timestamp = str(int(time.time() * 1000))  # v2: milisegundos (v1 era segundos)
    headers = {
        "Content-Type": "application/json",
        "Appid": CCPAY_APP_ID,
        "Timestamp": timestamp,
        "Sign": _sign(timestamp, body_str),
    }

    url = f"{CCPAY_API_URL.rstrip('/')}{path}"
    session = await _get_session()
    async with session.post(url, data=body_str, headers=headers) as resp:
        resp.raise_for_status()
        # IMPORTANTE: CCPayment a veces devuelve el body en JSON válido pero
        # con Content-Type: text/html (bug conocido de su lado). aiohttp por
        # defecto valida el Content-Type antes de parsear y tira
        # "Attempt to decode JSON with unexpected mimetype" si no coincide.
        # content_type=None desactiva esa validación y parsea el body igual.
        data = await resp.json(content_type=None)
        logger.debug("CCPayment POST %s -> %s", path, data)
        return data


# Precisión on-chain por red. CCPayment rechaza price/amount con más
# decimales de los que la red soporta nativamente ("Exceed token precision
# limit" / "La precisión del monto del retiro no puede ser superior a N").
# TRX/TRC20 = 6 decimales (SUN), no 8 -> antes se mandaba round(x, 8) fijo
# para TODAS las redes, lo que rompía órdenes y reembolsos en TRX.
_CHAIN_DECIMALS = {
    "TRX": 6,
    "TRC20": 6,
    "BTC": 8,
    "BSC": 18,
    "ETH": 18,
    "POLYGON": 18,
    "SOL": 9,
}
_DEFAULT_DECIMALS = 8  # fallback conservador si aparece una red no listada


def _chain_precision(chain: str) -> int:
    """Decimales máximos aceptados por CCPayment para esta red."""
    return min(_CHAIN_DECIMALS.get(chain.upper(), _DEFAULT_DECIMALS), 8)


# Redes con comisión de red (gas) típicamente baja para el usuario al enviar
# el pago desde su wallet/exchange. Es una tabla ESTÁTICA basada en costos
# de gas conocidos on-chain -> getCoinList no expone de forma confiable un
# campo de fee real por red, así que no hay forma 100% dinámica de saberlo.
# Se usa solo para ORDENAR/ETIQUETAR el listado de monedas, nunca para
# calcular montos.
_LOW_FEE_CHAINS = {
    "TRX", "TRC20", "POLYGON", "BSC", "BEP20",
    "SOL", "SOLANA", "ARBITRUM", "OPTIMISM", "BASE", "TON",
}


def _is_low_fee_chain(chain: str) -> bool:
    return chain.strip().upper() in _LOW_FEE_CHAINS


def _pack_token_id(coin_id: int, chain: str) -> str:
    """Empaqueta coinId+chain en el 'token_id' compuesto que usa el resto del bot."""
    return f"{coin_id}:{chain}"


def _unpack_token_id(token_id: str) -> tuple[int, str]:
    """Separa el 'token_id' compuesto de vuelta en (coin_id, chain)."""
    coin_id_str, chain = token_id.split(":", 1)
    return int(coin_id_str), chain


# ── Monedas soportadas ───────────────────────────────────────────────────────

async def get_supported_currencies(
    max_coins: int = 12, max_networks_per_coin: int = 2,
) -> list[dict]:
    """
    Consulta /getCoinList: catálogo de monedas/redes habilitadas para el
    merchant, con su coinId + chain (reemplaza al token_id UUID de v1).

    Devuelve una lista de dicts:
        {"currency": "USDT", "network": "TRC20", "label": "...",
         "token_id": "<coinId>:<chain>"}

    `max_coins` limita cuántas monedas DISTINTAS (por orden de aparición)
    se consideran. OJO: eso por sí solo NO limita el tamaño del teclado,
    porque cada moneda puede traer varias redes (ej. USDT con 6+ redes) y
    todas se agregaban igual -> con solo 5-6 monedas el teclado terminaba
    con 20+ botones, obligando a scrollear un montón para llegar al botón
    real que el usuario quería.

    Por eso acá también se limita `max_networks_per_coin`: de las redes de
    cada moneda, se quedan solo las `max_networks_per_coin` de comisión más
    baja (ver _LOW_FEE_CHAINS) primero; si la moneda no tiene ninguna red
    de comisión baja, se toman las primeras que haya. Menos redes por
    moneda, pero las más relevantes/baratas para el usuario.
    """
    try:
        resp = await _post("/getCoinList")
        if resp.get("code") != 10000:
            logger.error("get_supported_currencies error: %s", resp)
            return []

        coins = (resp.get("data") or {}).get("coins", [])
        result = []
        for coin in coins[:max_coins]:
            status = str(coin.get("status", "")).lower()
            if status in ("maintain", "delisted", "disabled"):
                continue  # solo excluimos estados claramente inhabilitados
            symbol = coin.get("symbol")
            coin_id = coin.get("coinId")
            if not symbol or not coin_id:
                continue
            networks = coin.get("networks") or {}

            coin_options = []
            for chain, net_info in networks.items():
                net_info = net_info or {}
                network_label = net_info.get("network") or chain
                coin_options.append({
                    "currency": symbol,
                    "network":  network_label,
                    "label":    f"{symbol} ({network_label})",
                    "token_id": _pack_token_id(coin_id, chain),
                    "low_fee":  _is_low_fee_chain(chain),
                })

            # Comisión baja primero (sort estable), y solo nos quedamos con
            # las primeras `max_networks_per_coin` de esta moneda.
            coin_options.sort(key=lambda o: not o["low_fee"])
            result.extend(coin_options[:max_networks_per_coin])
        return result
    except Exception as exc:
        logger.error("get_supported_currencies exception: %s: %s", type(exc).__name__, exc)
        return []


async def get_estimated_amount(amount_usd: float, token_id: str) -> Optional[float]:
    """
    Consulta /getCoinUSDTPrice: precio actual del coin en USDT, y calcula
    cuánto equivale `amount_usd` en esa cripto (se asume USD ~= USDT 1:1,
    igual que hacía la versión v1 de este bot).

    Amount en USD = Amount en la cripto * price  ->  amount_cripto = usd / price
    """
    coin_id, chain = _unpack_token_id(token_id)
    payload = {"coinIds": [coin_id]}
    try:
        resp = await _post("/getCoinUSDTPrice", payload)
        if resp.get("code") != 10000:
            logger.warning("get_estimated_amount bad code (%s): %s", token_id, resp)
            return None
        prices = (resp.get("data") or {}).get("prices", {})
        # Las claves del mapa pueden volver como string aunque coinId sea int
        price = prices.get(coin_id) or prices.get(str(coin_id))
        try:
            price_f = float(price) if price is not None else 0.0
        except (TypeError, ValueError):
            price_f = 0.0
        if price_f <= 0:
            # Coins de testnet (ej. ETH_SEPOLIA) u otros sin precio real
            # devuelven "0" -> no se puede cotizar, no ofrecer esta opción.
            logger.debug("get_estimated_amount(%s): precio no disponible (%r)", token_id, price)
            return None
        return round(amount_usd / price_f, _chain_precision(chain))
    except Exception as exc:
        logger.error("get_estimated_amount(%s) exception: %s: %s", token_id, type(exc).__name__, exc)
        return None


async def get_estimated_amounts_batch(
    amount_usd: float, token_ids: list[str]
) -> dict[str, Optional[float]]:
    """
    Versión en lote de get_estimated_amount: cotiza TODOS los token_id en
    UNA sola llamada a /getCoinUSDTPrice (el endpoint acepta coinIds como
    array), en vez de una llamada por moneda/red.

    Esto reemplaza al patrón anterior (asyncio.gather de N llamadas
    individuales), que disparaba decenas de requests simultáneos a
    CCPayment y provocaba "11004 Request too fast / rate limit" apenas
    el usuario tenía más de ~10 monedas soportadas.

    Devuelve: {token_id: monto_en_esa_cripto_o_None, ...}
    (None si no se pudo cotizar esa moneda, ej. precio 0 en coins de testnet)
    """
    # coinId puede repetirse entre varios token_id (mismo coin, distintas
    # redes) -> solo pedimos precios de los coinId únicos.
    unique_coin_ids = sorted({_unpack_token_id(tid)[0] for tid in token_ids})
    if not unique_coin_ids:
        return {}

    try:
        resp = await _post("/getCoinUSDTPrice", {"coinIds": unique_coin_ids})
        if resp.get("code") != 10000:
            logger.warning("get_estimated_amounts_batch bad code: %s", resp)
            return {tid: None for tid in token_ids}

        prices = (resp.get("data") or {}).get("prices", {})
        result: dict[str, Optional[float]] = {}
        for tid in token_ids:
            coin_id, chain = _unpack_token_id(tid)
            price = prices.get(coin_id) or prices.get(str(coin_id))
            try:
                price_f = float(price) if price is not None else 0.0
            except (TypeError, ValueError):
                price_f = 0.0
            result[tid] = (
                round(amount_usd / price_f, _chain_precision(chain))
                if price_f > 0 else None
            )
        return result
    except Exception as exc:
        logger.error("get_estimated_amounts_batch exception: %s: %s", type(exc).__name__, exc)
        return {tid: None for tid in token_ids}


# ── Órdenes ───────────────────────────────────────────────────────────────────

async def create_order(
    amount_token: float,
    token_id: str,
    memo: str = "",
) -> Optional[dict]:
    """
    Crea una orden de cobro vía /createAppOrderDepositAddress, en la cripto/red
    exacta para que el monto a pagar coincida con el ya mostrado al usuario
    (evita reconversión/redondeo). No se pasa fiatId, así que "price" se
    interpreta en la unidad del propio coin.

    Devuelve dict con:
        orderId      Nuestro orderId (usarlo para consultar estado)
        payAddress   Dirección a la que el usuario debe enviar el pago
        payAmount    Monto exacto a enviar
    o None si falla.
    """
    coin_id, chain = _unpack_token_id(token_id)
    order_id = str(uuid.uuid4()).replace("-", "")[:24]
    price_rounded = round(amount_token, _chain_precision(chain))
    payload = {
        "orderId":  order_id,
        "coinId":   coin_id,
        "chain":    chain,
        "price":    str(price_rounded),
        # v2 "expiredAt" documentado como marca de tiempo, no duración:
        "expiredAt": int(time.time()) + 900,  # 15 min, igual a PAYMENT_TIMEOUT_SECONDS
        "generateCheckoutURL": False,
        "product": memo[:120] if memo else "",
    }
    try:
        resp = await _post("/createAppOrderDepositAddress", payload)
        if resp.get("code") != 10000:
            logger.error("create_order error response: %s", resp)
            return None
        data = resp.get("data", {})
        return {
            "orderId":    order_id,
            "payAddress": data.get("address"),
            "payAmount":  float(data.get("amount") or price_rounded),
            "memo":       data.get("memo"),
            "currency":   None,   # no viene en la respuesta; ya lo sabemos por token_id
            "network":    chain,
        }
    except Exception as exc:
        logger.error("create_order exception: %s: %s", type(exc).__name__, exc)
        return None


async def get_order_status(order_id: str) -> int:
    """
    Consulta /getAppDepositRecordList con nuestro orderId (el mismo que
    generamos en create_order) y mapea el status de texto a los enteros
    ORDER_STATUS_* usados por el resto del bot.
    """
    try:
        resp = await _post("/getAppDepositRecordList", {"orderId": order_id, "limit": 1})
        if resp.get("code") != 10000:
            logger.warning("get_order_status bad code: %s", resp)
            return -1
        records = (resp.get("data") or {}).get("records") or []
        if not records:
            return ORDER_STATUS_PENDING  # aún no hay ni intento de pago
        status_text = records[0].get("status", "")
        logger.debug("get_order_status raw status for %s: %r", order_id, status_text)
        mapped = _STATUS_MAP.get(status_text, -1)
        if mapped == -1:
            logger.warning(
                "get_order_status: status v2 no reconocido (%r) para orden %s; "
                "revisar _STATUS_MAP en ccpay_api.py", status_text, order_id,
            )
        return mapped
    except Exception as exc:
        logger.error("get_order_status(%s) exception: %s: %s", order_id, type(exc).__name__, exc)
        return -1


async def refund_user(
    to_address: str,
    amount: float,
    token_id: str,
    memo: str = "",
) -> tuple[bool, Optional[int]]:
    """
    Transfiere cripto al usuario como reembolso vía /applyAppWithdrawToNetwork,
    en el mismo coinId/chain que usó para pagar.

    Devuelve (ok, error_code):
      - (True, None) si se pudo solicitar la transferencia.
      - (False, code) si falló, con el código de error de CCPayment cuando
        se pudo determinar (None si fue una excepción local, ej. timeout).
        Distinguir el código importa porque CCPAY_ERR_INSUFFICIENT_MERCHANT_BALANCE
        (14000) significa "esta moneda/red puntual se quedó sin fondos del
        lado del merchant" -> es un caso RETRYABLE con otra moneda permitida
        (ver handlers.cb_withdraw_confirm), a diferencia de una dirección
        inválida u otro error que fallaría igual sin importar la moneda.
    """
    if not to_address:
        logger.error(
            "refund_user: sin dirección de destino. Reembolso manual pendiente: "
            "%.8f (token_id=%s)", amount, token_id,
        )
        return False, None
    coin_id, chain = _unpack_token_id(token_id)
    amount_rounded = round(amount, _chain_precision(chain))
    payload = {
        "orderId":  str(uuid.uuid4()).replace("-", "")[:24],
        "coinId":   coin_id,
        "chain":    chain,
        "address":  to_address,
        "amount":   str(amount_rounded),
        "memo":     memo[:64] if memo else "Reembolso automático",
        # False = la comisión de red se descuenta del monto retirado (la
        # paga el usuario). Antes estaba en True (la pagábamos nosotros),
        # lo que significaba perder la comisión de red en CADA reembolso,
        # sin importar el motivo. Con muchos timeouts de SMS eso vacía el
        # saldo disponible para retiros (ver error 14000 "not enough
        # balance for withdrawal"). El usuario recibe un poco menos, pero
        # nunca pierde su dinero.
        "merchantPayNetworkFee": False,
    }
    try:
        resp = await _post("/applyAppWithdrawToNetwork", payload)
        code = resp.get("code")
        if code != 10000:
            logger.error("refund_user error: %s", resp)
            return False, code
        logger.info(
            "Reembolso solicitado: %.8f (coinId=%s chain=%s) -> %s (recordId=%s)",
            amount_rounded, coin_id, chain, to_address, (resp.get("data") or {}).get("recordId"),
        )
        return True, None
    except Exception as exc:
        logger.error("refund_user exception: %s: %s", type(exc).__name__, exc)
        return False, None
