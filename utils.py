"""
utils.py - Funciones auxiliares: formateo, teclados, helpers de asyncio
"""
import asyncio
import io
import logging
import math
from typing import Callable, Awaitable
import qrcode
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import COMMUNITY_CHANNEL_URL

logger = logging.getLogger(__name__)


# ── Código QR de pago ──────────────────────────────────────────────────────

def generate_payment_qr(address: str, amount: float = None, network: str = None) -> bytes:
    """
    Genera un QR (PNG, en bytes) para que el usuario pague escaneando desde
    su wallet, en vez de copiar/pegar la dirección a mano.

    Por seguridad codificamos SOLO la dirección para cualquier chain/token
    (funciona en cualquier wallet: el usuario escanea y confirma el monto
    que ya le mostramos en el mensaje). La única excepción es Bitcoin, donde
    usamos el estándar BIP21 ("bitcoin:<address>?amount=<amount>"), que está
    ampliamente soportado y no tiene ambigüedad de unidades.

    Para chains EVM (BSC, ETH, POLYGON, etc.) existe el estándar EIP-681,
    pero varía según sea la moneda nativa o un token (requiere contrato +
    decimales del token) y distintas wallets lo interpretan distinto;
    incluir el monto ahí sería repetir el mismo tipo de bug de precisión
    que ya tuvimos con format_amount. Mejor no arriesgar money con eso:
    dirección sola + monto visible en texto es inequívoco en cualquier chain.
    """
    if network and network.upper() in ("BTC", "BITCOIN") and amount is not None:
        payload = f"bitcoin:{address}?amount={amount:.8f}"
    else:
        payload = address

    qr = qrcode.QRCode(border=2, box_size=8)
    qr.add_data(payload)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ── Formateo ──────────────────────────────────────────────────────────────────

def format_amount(amount: float, currency: str = "USD") -> str:
    """
    Formatea un monto cripto con precisión adecuada según la moneda.

    IMPORTANTE: 8 decimales para TODA cripto (no solo BTC/ETH). En
    ccpay_api.py, get_estimated_amount/create_order/refund_user redondean
    siempre a 8 decimales (round(x, 8)); si acá se mostraba con menos
    decimales (como pasaba antes con BNB, TRX, etc. a 4 decimales), el
    monto que el bot le pedía pagar al usuario NO coincidía con el monto
    real de la orden en CCPayment -> pagos de más, pagos de menos que
    nunca se completan, y órdenes expiradas sin motivo aparente.
    """
    decimals = 2 if currency == "USD" else 8
    return f"{amount:.{decimals}f} {currency}"


def floor_to_cents(amount: float) -> float:
    """
    Trunca (redondea SIEMPRE hacia abajo) un monto USD a 2 decimales.

    Por qué existe: format_amount() muestra USD con round-half-to-even a 2
    decimales (ej. un saldo real de 0.0951 se MUESTRA como "0.10", porque
    .2f redondea hacia arriba). Eso no es ruido de punto flotante (ver
    EPSILON en database.debit_balance) — es una pérdida real de hasta medio
    centavo por el redondeo de display. Si esa cifra redondeada-hacia-arriba
    se usa además como el máximo permitido para retirar (o el usuario
    simplemente copia el número que le mostramos), el bot deja pasar la
    validación inicial de "no más de tu saldo" y recién descubre el
    problema real al confirmar contra el saldo exacto en debit_balance,
    dando un "tu saldo cambió" confuso cuando en realidad no cambió nada:
    nunca hubo tanto saldo disponible como se mostró.
    Usar SIEMPRE floor_to_cents (nunca round) para: (a) el saldo "máximo
    retirable" que se muestra/valida, y (b) cualquier tope que el usuario
    pueda tomar literalmente como límite superior. Un pequeño epsilon evita
    que el propio ruido de punto flotante (ej. 0.1 representado como
    0.099999999999999996) trunque un centavo de más que sí es válido.
    """
    return math.floor(round(amount, 6) * 100 + 1e-9) / 100


def apply_markup(cost_usd: float, markup: float) -> float:
    """Aplica el margen de ganancia al costo de HeroSMS."""
    return round(cost_usd * markup, 4)


def apply_refund_fee(amount: float, fee_pct: float) -> float:
    """
    Calcula el monto a reembolsar cuando el usuario ya recibió un número y
    la operación no se completó (timeout de SMS o cancelación manual con
    número ya asignado). Retiene `fee_pct` como cargo de servicio no
    reembolsable (cubre la comisión de red y desincentiva pedir-y-no-usar).
    NO usar para timeout de pago ni "sin números disponibles": en esos
    casos el usuario nunca tuvo un número asignado y se reembolsa 100%.
    """
    fee_pct = max(0.0, min(fee_pct, 1.0))
    return round(amount * (1 - fee_pct), 8)


def format_cup(amount_cup: int) -> str:
    """
    Formatea un monto entero de CUP con separador de miles (espacio, no
    coma: es la convención local) y el sufijo "CUP". Usar SIEMPRE que se
    muestre saldo/monto de origen CUP al usuario en vez de format_amount
    con 'USD' — mostrar un monto en CUP con formato/etiqueta de USD es
    confuso (el usuario paga y retira en CUP real, no en dólares).
    """
    return f"{amount_cup:,}".replace(",", " ") + " CUP"


def usd_to_cup(amount_usd: float, rate: float) -> int:
    """
    Convierte USD a CUP a la tasa dada, redondeado a entero: nadie
    transfiere centavos de CUP y pedir un monto exacto con decimales solo
    genera fricción al momento de transferir.
    """
    return round(amount_usd * rate)


def effective_cup_rate(reference_rate: float, margin_pct: float) -> float:
    """
    Tasa que se le aplica al CLIENTE (más cara que la de referencia), para
    absorber el desfase entre acreditar el CUP y convertirlo realmente a
    USDT (ver config.MANUAL_DEPOSIT_CUP_MARGIN_PCT). El cliente sigue
    viendo un solo número en CUP, no un desglose de margen.
    """
    return round(reference_rate * (1 + max(0.0, margin_pct)), 2)


def effective_cup_rate_payout(reference_rate: float, margin_pct: float) -> float:
    """
    Tasa que se usa para un RETIRO en CUP (el operador entrega CUP al
    usuario), inversa a effective_cup_rate: en un depósito el margen sube
    la tasa para que el cliente entregue MÁS CUP por el mismo USD (protege
    al operador de que el CUP se deprecie antes de convertir); en un
    retiro el mismo riesgo se protege bajando la tasa para que el operador
    ENTREGUE MENOS CUP por ese USD. Usar la tasa "de depósito" acá por
    error regalaría margen doble en cada retiro.
    """
    return round(reference_rate * (1 - max(0.0, min(margin_pct, 1.0))), 2)


def apply_withdrawal_fee(amount: float, fee_pct: float) -> tuple[float, float]:
    """
    Divide un monto de retiro en (net_usd, fee_usd) reteniendo `fee_pct`
    como comisión de servicio.

    IMPORTANTE sobre el orden de redondeo: la comisión se calcula PRIMERO,
    redondeada a centavos (2 decimales, la misma precisión con la que se
    muestra en USD), y el neto se obtiene por RESTA exacta del monto
    original. Esto garantiza que fee_usd + net_usd == amount siempre,
    incluso mostrado a 2 decimales.

    Antes se calculaba net_usd = round(amount*(1-fee_pct), 4) y
    fee_usd = round(amount - net_usd, 4) por separado, y CADA UNO se
    redondeaba independientemente a 2 decimales solo al mostrarlo. Con
    montos chicos (ej. retirar $0.10 al 5% = comisión real de $0.005,
    medio centavo) los dos redondeos independientes podían subir cada
    uno "hacia arriba": comisión mostrada $0.01 Y "recibes" mostrado
    $0.10 (en vez de $0.09), dando la impresión de que no se cobró nada
    aunque el texto de arriba dijera lo contrario. Con comisión calculada
    primero y neto por resta, esa inconsistencia no puede pasar: en este
    mismo ejemplo da comisión $0.01 y recibes $0.09 (suman $0.10, exacto).
    """
    fee_pct = max(0.0, min(fee_pct, 1.0))
    fee_usd = round(amount * fee_pct, 2)
    net_usd = round(amount - fee_usd, 2)
    return net_usd, fee_usd


def is_wrapped_token(currency: str, network: str) -> bool:
    """
    Detecta si `currency` se está pagando en una red que NO es la suya
    nativa (ej. "TRX" símbolo pero network="BSC" -> TRX-BEP20, no Tron).
    Heurística simple: coincide si el símbolo de la moneda es igual al
    nombre de la red (ignorando mayúsculas). No es 100% infalible para
    todos los casos (ej. USDT no tiene "red nativa" propia), pero cubre
    el caso de riesgo real: coins con blockchain propia (TRX, ETH, BTC,
    SOL...) ofrecidos también envueltos en otras redes.
    """
    if not currency or not network:
        return False
    return currency.strip().upper() != network.strip().upper()


def format_phone(number: str) -> str:
    """Formatea el número telefónico para mostrar al usuario."""
    number = str(number).strip()
    if not number.startswith("+"):
        number = f"+{number}"
    return number


# ── Teclados inline ───────────────────────────────────────────────────────────

def services_keyboard(services: dict[str, str]) -> InlineKeyboardMarkup:
    """
    Genera teclado con botones para cada servicio.
    services: {"code": "Nombre", ...}
    """
    builder = InlineKeyboardBuilder()
    for code, name in services.items():
        builder.button(text=name, callback_data=f"svc:{code}")
    builder.adjust(2)  # 2 botones por fila
    return builder.as_markup()


def search_results_keyboard(results: list[dict]) -> InlineKeyboardMarkup:
    """
    Genera teclado con los servicios encontrados en una búsqueda.
    results: lista de dicts {"code": "tg", "name": "Telegram"} (normalizados
    por herosms_api.search_services / get_services).
    """
    builder = InlineKeyboardBuilder()
    for r in results:
        builder.button(text=r["name"], callback_data=f"svc:{r['code']}")
    builder.button(text="🔍 Nueva búsqueda", callback_data="new_purchase")
    builder.adjust(2)
    return builder.as_markup()


def countries_keyboard(countries: list[dict], markup: float, success_stats: dict = None) -> InlineKeyboardMarkup:
    """
    Genera teclado con países y precios para el usuario.
    Cada elemento de countries debe tener: country, name, price (en USD).
    El precio mostrado es en USD; la moneda de pago se elige en el paso siguiente.

    `success_stats` (opcional): {country_code: {"rate": float, "attempts": int}}
    de database.get_country_success_stats. Si se pasa, se ordenan los
    países por tasa de éxito (los que sí tienen datos primero, de mejor a
    peor) y se muestra un badge ✅/⚠️/❌ junto al precio, para que el
    usuario elija con información real de qué países entregan el código,
    en vez de descubrirlo después de pagar.
    """
    success_stats = success_stats or {}

    def _sort_key(c):
        code = c.get("country", c.get("code", "??"))
        stat = success_stats.get(code)
        # Sin datos suficientes -> al final, mezclados por precio (comportamiento previo)
        return (0, -stat["rate"]) if stat else (1, 0)

    ordered = sorted(countries[:20], key=_sort_key)  # Limitar a 20 para no saturar el chat

    builder = InlineKeyboardBuilder()
    for c in ordered:
        code        = c.get("country", c.get("code", "??"))
        name        = c.get("name", code.upper())
        cost_usd    = float(c.get("price", c.get("cost", 0)))
        price_usd   = apply_markup(cost_usd, markup)

        stat = success_stats.get(code)
        if stat:
            rate = stat["rate"]
            badge = "✅" if rate >= 80 else "⚠️" if rate >= 50 else "❌"
            suffix = f" · {badge} {rate:.0f}%"
        else:
            suffix = ""

        label = f"🌍 {name} — {format_amount(price_usd, 'USD')}{suffix}"
        builder.button(text=label, callback_data=f"cnt:{code}:{cost_usd}")
    builder.adjust(1)  # 1 país por fila para legibilidad
    return builder.as_markup()


def currencies_keyboard(
    options: list[dict], balance_usd: float = 0.0, price_usd: float = 0.0,
    manual_cup_available: bool = False,
) -> InlineKeyboardMarkup:
    """
    Genera teclado para elegir moneda/red de pago.
    options: lista de dicts {"currency", "network", "label", "amount",
    "low_fee"} donde "amount" ya es el equivalente cripto del precio en USD.
    Se asume que `options` ya viene ordenada (redes de comisión baja
    primero); acá solo se agrega la etiqueta visual.
    callback_data codifica índice para evitar problemas de longitud/caracteres.

    Si `balance_usd` alcanza para cubrir `price_usd`, se agrega arriba un
    botón para pagar con saldo interno (sin comisión de red, instantáneo).

    `manual_cup_available`: si hay al menos un método de pago manual CUP
    configurado (ver config.MANUAL_PAYMENT_METHODS), se agrega un botón para
    pagar directo en CUP para ESTA compra — sin que el cliente tenga que
    pasar por /saldo. El monto en CUP se calcula y muestra recién en el
    siguiente paso (ver handlers.cb_select_manual_purchase_method), acá solo
    el botón de entrada.
    """
    builder = InlineKeyboardBuilder()
    if balance_usd >= price_usd > 0:
        builder.button(
            text=f"💰 Pagar con saldo ({format_amount(balance_usd, 'USD')} disponible)",
            callback_data="pay_balance",
        )
    if manual_cup_available:
        builder.button(text="🇨🇺 Pagar con CUP", callback_data="pay_cup")
    for i, opt in enumerate(options):
        amount_str = format_amount(opt["amount"], opt["currency"])
        fee_tag = " · 💚 comisión baja" if opt.get("low_fee") else ""
        label = f"💰 {opt['label']} — {amount_str}{fee_tag}"
        builder.button(text=label, callback_data=f"cur:{i}")
    builder.adjust(1)
    return builder.as_markup()


def balance_menu_keyboard(can_withdraw: bool, can_withdraw_cup: bool = False) -> InlineKeyboardMarkup:
    """
    Botones de /saldo: siempre se puede agregar saldo (cripto o CUP);
    "Retirar a cripto" solo se muestra si hay saldo de ORIGEN cripto
    disponible (can_withdraw), y "Retirar en CUP" solo si hay saldo de
    origen CUP disponible (can_withdraw_cup) — son dos bolsas separadas,
    ver database.py Database.balances.
    """
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Agregar saldo (cripto)", callback_data="start_deposit")
    builder.button(text="🇨🇺 Agregar saldo (CUP)", callback_data="start_manual_deposit")
    if can_withdraw:
        builder.button(text="💸 Retirar a cripto", callback_data="start_withdraw")
    if can_withdraw_cup:
        builder.button(text="🇨🇺 Retirar en CUP", callback_data="start_cup_withdraw")
    builder.adjust(1)
    return builder.as_markup()


def manual_payment_methods_keyboard(methods: dict[str, dict]) -> InlineKeyboardMarkup:
    """Selección de método de pago manual (Transfermóvil, EnZona, ...)."""
    builder = InlineKeyboardBuilder()
    for code, info in methods.items():
        builder.button(text=f"🇨🇺 {info['name']}", callback_data=f"mmethod:{code}")
    builder.button(text="❌ Cancelar", callback_data="cancel_op")
    builder.adjust(1)
    return builder.as_markup()


def manual_deposit_review_keyboard(dep_id: int) -> InlineKeyboardMarkup:
    """Botones de aprobar/rechazar que ve el admin junto al comprobante."""
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Aprobar", callback_data=f"mdep_ok:{dep_id}")
    builder.button(text="❌ Rechazar", callback_data=f"mdep_no:{dep_id}")
    builder.adjust(2)
    return builder.as_markup()


def purchase_cup_review_keyboard(tx_id: int) -> InlineKeyboardMarkup:
    """
    Igual que manual_deposit_review_keyboard pero para un pago CUP ligado
    directo a una COMPRA (callback_data "ptx_*" en vez de "mdep_*", para no
    confundir este caso con un depósito de saldo — acá aprobar entrega un
    número, no acredita saldo, ver handlers.cb_admin_approve_purchase_cup).
    """
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Aprobar", callback_data=f"ptx_ok:{tx_id}")
    builder.button(text="❌ Rechazar", callback_data=f"ptx_no:{tx_id}")
    builder.adjust(2)
    return builder.as_markup()


def channel_invite_keyboard() -> InlineKeyboardMarkup:
    """Botón único para el nudge puntual (ver MSG_CHANNEL_INVITE). Solo se
    llama si config.COMMUNITY_CHANNEL_URL está seteado -mismo chequeo que
    main_menu_keyboard, ver handlers._maybe_prompt_channel_join."""
    builder = InlineKeyboardBuilder()
    builder.button(text="📢 Unirme al canal", url=COMMUNITY_CHANNEL_URL)
    builder.adjust(1)
    return builder.as_markup()


def refund_request_review_keyboard(request_id: int) -> InlineKeyboardMarkup:
    """
    Botones de aprobar/denegar una solicitud de reembolso post-entrega
    (callback_data "rfnd_*", keyed por refund_requests.id -NO por tx_id,
    porque una misma tx puede tener más de una solicitud a lo largo del
    tiempo si una denegada se vuelve a abrir después- ver
    handlers.cb_admin_approve_refund_request / cb_admin_deny_refund_request).
    """
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Aprobar reembolso", callback_data=f"rfnd_ok:{request_id}")
    builder.button(text="❌ Denegar", callback_data=f"rfnd_no:{request_id}")
    builder.adjust(2)
    return builder.as_markup()


def withdraw_start_keyboard() -> InlineKeyboardMarkup:
    """Botón para iniciar un retiro de saldo interno a cripto desde /saldo."""
    builder = InlineKeyboardBuilder()
    builder.button(text="💸 Retirar a cripto", callback_data="start_withdraw")
    builder.adjust(1)
    return builder.as_markup()


def deposit_currencies_keyboard(options: list[dict]) -> InlineKeyboardMarkup:
    """
    Igual que currencies_keyboard pero para el flujo de DEPÓSITO (agregar
    saldo): sin botón de "pagar con saldo" (no aplica, es el saldo mismo el
    que se está generando) y con prefijo de callback_data propio ("dcur:")
    para no compartir handler/estado con la selección de moneda de una
    compra o de un retiro.
    """
    builder = InlineKeyboardBuilder()
    for i, opt in enumerate(options):
        amount_str = format_amount(opt["amount"], opt["currency"])
        fee_tag = " · 💚 comisión baja" if opt.get("low_fee") else ""
        label = f"💰 {opt['label']} — {amount_str}{fee_tag}"
        builder.button(text=label, callback_data=f"dcur:{i}")
    builder.button(text="❌ Cancelar", callback_data="cancel_op")
    builder.adjust(1)
    return builder.as_markup()


def withdraw_currencies_keyboard(options: list[dict]) -> InlineKeyboardMarkup:
    """
    Igual que currencies_keyboard pero para el flujo de RETIRO de saldo:
    sin botón de "pagar con saldo" (no aplica acá) y con prefijo de
    callback_data propio ("wcur:") para no compartir handler/estado con la
    selección de moneda de una compra (misma callback_data "cur:i" en otro
    FSM state simplemente no matchearía, pero usar prefijo distinto es más
    claro de leer).
    """
    builder = InlineKeyboardBuilder()
    for i, opt in enumerate(options):
        amount_str = format_amount(opt["amount"], opt["currency"])
        fee_tag = " · 💚 comisión baja" if opt.get("low_fee") else ""
        # "deposited_in" es una pista (viene de db.get_last_completed_deposit),
        # no una garantía de liquidez real — ver docstring en database.py.
        dep_tag = " · 🔁 la que depositaste" if opt.get("deposited_in") else ""
        label = f"💰 {opt['label']} — {amount_str}{fee_tag}{dep_tag}"
        builder.button(text=label, callback_data=f"wcur:{i}")
    builder.button(text="❌ Cancelar", callback_data="cancel_op")
    builder.adjust(1)
    return builder.as_markup()


def withdraw_confirm_keyboard() -> InlineKeyboardMarkup:
    """Confirmación final antes de descontar saldo y llamar a CCPayment."""
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Confirmar retiro", callback_data="withdraw_confirm")
    builder.button(text="❌ Cancelar", callback_data="cancel_op")
    builder.adjust(1)
    return builder.as_markup()


def cup_withdraw_methods_keyboard(methods: dict[str, dict]) -> InlineKeyboardMarkup:
    """Selección de método para RECIBIR un retiro en CUP (mismo listado que los métodos de pago)."""
    builder = InlineKeyboardBuilder()
    for code, info in methods.items():
        builder.button(text=f"🇨🇺 {info['name']}", callback_data=f"wmethod:{code}")
    builder.button(text="❌ Cancelar", callback_data="cancel_op")
    builder.adjust(1)
    return builder.as_markup()


def cup_withdraw_confirm_keyboard() -> InlineKeyboardMarkup:
    """Confirmación final antes de descontar el saldo CUP y notificar al admin."""
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Confirmar retiro", callback_data="cup_withdraw_confirm")
    builder.button(text="❌ Cancelar", callback_data="cancel_op")
    builder.adjust(1)
    return builder.as_markup()


def manual_withdrawal_review_keyboard(wd_id: int) -> InlineKeyboardMarkup:
    """Botones de aprobar/rechazar que ve el admin para un retiro manual en CUP."""
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Ya transferí", callback_data=f"mwd_ok:{wd_id}")
    builder.button(text="❌ Rechazar", callback_data=f"mwd_no:{wd_id}")
    builder.adjust(2)
    return builder.as_markup()


def cancel_keyboard() -> InlineKeyboardMarkup:
    """Botón para cancelar la operación en curso."""
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Cancelar", callback_data="cancel_op")
    return builder.as_markup()


def main_menu_keyboard(is_admin: bool = False) -> InlineKeyboardMarkup:
    """
    Menú principal premium: todo lo que puede necesitar el usuario a un
    toque, sin escribir comandos. Solo incluye botones que hoy hacen algo
    REAL (nada de "Favoritos"/"Promociones" todavía — esos requieren
    sistemas que no existen aún, ver database.py; un botón que no hace
    nada es peor que no tenerlo).

    El botón "📢 Canal oficial" (si config.COMMUNITY_CHANNEL_URL está
    seteado) es la captación pasiva: queda siempre a la vista en TODOS
    lados donde se muestra este menú, sin mandar ningún mensaje extra ni
    requerir que un admin postee nada -es la opción de "poco esfuerzo".
    Es un botón de URL (abre Telegram directo), no callback_data, así que
    no hay forma de contar clicks desde acá -la confirmación real de quién
    se unió llega por separado vía el evento chat_member del canal (ver
    handlers.on_channel_member_update).

    Grilla de 3 columnas (en vez de 2) para que se vea más compacto,
    estilo "app menu" (ver referencia: teclado de Cwallet). Si `is_admin`
    es True, se agrega un botón extra que abre el panel de administrador
    (ver admin_menu_keyboard) en su propia fila.
    """
    builder = InlineKeyboardBuilder()
    builder.button(text="🛒 Comprar número",  callback_data="new_purchase")
    builder.button(text="💰 Mi saldo",         callback_data="my_balance")
    builder.button(text="👤 Mi cuenta",        callback_data="my_profile")
    builder.button(text="📦 Mis pedidos",      callback_data="my_txns")
    builder.button(text="🌍 Mi país",          callback_data="my_country")
    builder.button(text="🔗 Invitar amigos",   callback_data="my_referrals")
    builder.button(text="🆘 Soporte",          callback_data="support")
    rows = [3, 3, 1]
    if COMMUNITY_CHANNEL_URL:
        builder.button(text="📢 Canal oficial", url=COMMUNITY_CHANNEL_URL)
        rows = [3, 3, 2]
    if is_admin:
        builder.button(text="🛠️ Panel admin", callback_data="admin_panel")
        rows.append(1)
    builder.adjust(*rows)
    return builder.as_markup()


def admin_menu_keyboard() -> InlineKeyboardMarkup:
    """
    Panel de administrador en forma de grilla, mismo estilo que
    main_menu_keyboard: acceso rápido a los comandos administrativos más
    usados sin tener que escribirlos a mano.

    Los que requieren parámetros (/detalle <tx_id>, /convertido <ids>,
    /set_tipo, /set_pais) no pueden resolverse con un solo tap -> el botón
    simplemente muestra el formato de uso exacto (mismo texto que ya
    muestra el comando cuando se lo llama sin argumentos), para que el
    admin solo tenga que escribir el comando con el id/valor puntual.
    """
    builder = InlineKeyboardBuilder()
    builder.button(text="📊 Stats",            callback_data="adm_stats")
    builder.button(text="💵 Ventas",           callback_data="adm_ventas")
    builder.button(text="🔄 Pendientes",       callback_data="adm_pendientes")
    builder.button(text="🔎 Detalle tx",       callback_data="adm_detalle_help")
    builder.button(text="🇨🇺 Exposición CUP",  callback_data="adm_exposicion_cup")
    builder.button(text="✅ Convertido",       callback_data="adm_convertido_help")
    builder.button(text="🏷️ Set nivel",        callback_data="adm_set_tipo_help")
    builder.button(text="🌍 Set país",         callback_data="adm_set_pais_help")
    builder.button(text="🔙 Menú principal",   callback_data="back_to_user_menu")
    builder.adjust(3, 3, 2, 1)
    return builder.as_markup()


def top_services_keyboard(top_services: list[dict]) -> InlineKeyboardMarkup:
    """
    Teclado con los servicios más comprados (ranking real, no estático).
    top_services: lista de dicts {"code", "name", "count"} (ver
    database.Database.get_top_services). Reusa el mismo callback_data
    "svc:<code>" que el flujo normal de selección de servicio, para no
    duplicar lógica en los handlers.
    """
    builder = InlineKeyboardBuilder()
    medals = ["🥇", "🥈", "🥉"]
    for i, s in enumerate(top_services):
        prefix = medals[i] if i < len(medals) else "▪️"
        builder.button(
            text=f"{prefix} {s['name']} ({s['count']})",
            callback_data=f"svc:{s['code']}",
        )
    builder.button(text="🔍 Buscar otro servicio", callback_data="new_purchase")
    builder.adjust(1)
    return builder.as_markup()


# ── Helpers de asyncio ────────────────────────────────────────────────────────

async def poll_with_timeout(
    check_fn: Callable[[], Awaitable[bool]],
    interval: float,
    timeout: float,
    on_timeout: Callable[[], Awaitable[None]] = None,
) -> bool:
    """
    Ejecuta check_fn cada `interval` segundos durante `timeout` segundos.
    Si check_fn devuelve True, termina exitosamente.
    Si se agota el tiempo, llama on_timeout (si se proveyó) y devuelve False.
    """
    elapsed = 0.0
    while elapsed < timeout:
        try:
            if await check_fn():
                return True
        except Exception as exc:
            logger.warning("poll_with_timeout check_fn error: %s", exc)
        await asyncio.sleep(interval)
        elapsed += interval

    if on_timeout:
        try:
            await on_timeout()
        except Exception as exc:
            logger.error("poll_with_timeout on_timeout error: %s", exc)
    return False


# ── Mensajes predefinidos ─────────────────────────────────────────────────────

MSG_WELCOME = (
    "👋 <b>Bienvenido a OTPVirtual</b>\n\n"
    "Tu plataforma de números virtuales para verificación SMS/OTP: "
    "Telegram, WhatsApp, Discord, Gmail y muchos más.\n\n"
    "✅ Amplia cobertura de países y servicios\n"
    "⚡ Entrega inmediata tras confirmar el pago\n"
    "💳 Pago en cripto (USDT, BTC, ETH...) o CUP\n"
    "🛡 Soporte 24/7\n\n"
    "Selecciona una opción:"
)

MSG_SELECT_SERVICE = (
    "📲 <b>¿Para qué servicio necesitas el número?</b>\n\n"
    "✍️ Escribe el nombre del servicio que buscas "
    "(ej: <i>telegram</i>, <i>facebook</i>, <i>amazon</i>, <i>instagram</i>...)\n"
    "o escribe <b>todos</b> para ver algunos disponibles.\n\n"
    "Aquí tienes algunos populares para empezar:"
)

MSG_SEARCH_RESULTS = "🔎 Resultados para \"<b>{query}</b>\":\nSelecciona el servicio:"

MSG_SEARCH_NO_RESULTS = (
    "😕 No encontré servicios que coincidan con \"<b>{query}</b>\".\n"
    "Intenta con otro nombre, o escribe <b>todos</b> para ver algunos disponibles."
)

MSG_SELECT_COUNTRY = (
    "🌍 <b>Selecciona el país</b>\n"
    "Precio mostrado en USD (incluye nuestro margen de servicio).\n"
    "✅/⚠️/❌ = tasa histórica de códigos recibidos en ese país (solo se "
    "muestra con suficientes datos).\n"
    "La moneda de pago se elige en el siguiente paso:"
)

MSG_SELECT_CURRENCY = (
    "💰 <b>¿Con qué moneda quieres pagar?</b>\n\n"
    "Precio base: <b>{price_usd}</b>\n"
    "Selecciona la cripto/red — el monto ya está calculado al tipo de cambio actual:"
)

MSG_PAYMENT_INSTRUCTIONS = (
    "💰 <b>Instrucciones de pago</b>\n\n"
    "📦 <b>Servicio:</b> {service}\n"
    "🌍 <b>País:</b> {country}\n"
    "🪙 <b>Moneda:</b> {currency_label}\n"
    "💵 <b>Monto:</b> <code>{amount}</code>\n\n"
    "{wrapped_warning}"
    "📤 Envía exactamente ese monto a:\n"
    "<code>{address}</code>\n\n"
    "⏳ Tienes <b>15 minutos</b> para realizar el pago.\n"
    "El bot verificará automáticamente cuando reciba la transferencia.\n\n"
    "⚠️ Usa <b>únicamente la red indicada</b> ({network}). Envíos por otra red se perderán."
)

MSG_WRAPPED_TOKEN_WARNING = (
    "🚨 <b>Atención:</b> estás pagando con <b>{currency}</b> pero en la red "
    "<b>{network}</b>, que NO es la red nativa de {currency}.\n"
    "Verifica que tu wallet/exchange soporte enviar {currency} específicamente "
    "por la red {network} antes de pagar — si envías desde la red nativa de "
    "{currency} por error, el pago se pierde.\n\n"
)

MSG_PAYMENT_CONFIRMED = (
    "✅ <b>¡Pago confirmado!</b>\n"
    "Obteniendo tu número virtual... Un momento."
)

MSG_NUMBER_ASSIGNED = (
    "📱 <b>Tu número virtual:</b>\n"
    "<code>{number}</code>\n\n"
    "👆 Usa este número para verificar tu cuenta de <b>{service}</b>.\n"
    "⏳ Esperando el código SMS (máx. {timeout_min} minutos)...\n\n"
    "⚠️ <b>Importante:</b> abre {service} ahora mismo y solicita el código "
    "SIN cerrar esta conversación ni cambiar de pantalla. Si el código no "
    "llega dentro del tiempo indicado, el número expira y se te reembolsa "
    "tu pago (con el descuento de cargo de servicio habitual)."
)

MSG_CODE_RECEIVED = (
    "🎉 <b>¡Código recibido!</b>\n\n"
    "🔑 <b>Tu código OTP:</b> <code>{code}</code>\n\n"
    "✅ Activación completada. ¡Gracias por usar el servicio!"
)

MSG_PAYMENT_TIMEOUT = (
    "⏰ <b>Tiempo de pago agotado</b>\n"
    "No se recibió el pago en 15 minutos. La orden fue cancelada.\n"
    "Puedes iniciar una nueva compra cuando quieras."
)

MSG_SMS_TIMEOUT = (
    "😕 <b>No se recibió el código SMS</b>\n\n"
    "El número no recibió un código en 10 minutos.\n"
    "Se ha cancelado la activación y {refund_info}.\n\n"
    "Puedes intentarlo de nuevo con /start."
)

MSG_NO_NUMBERS = (
    "😔 <b>Sin números disponibles</b>\n\n"
    "No hay números disponibles para {service} en {country} en este momento.\n"
    "{refund_info}\n\n"
    "Intenta con otro país o servicio."
)

MSG_DEPOSIT_ASK_AMOUNT = (
    "➕ <b>Agregar saldo</b>\n\n"
    "Escribe el monto en USD que quieres depositar (mínimo <b>{min_usd}</b>).\n"
    "Se acredita el 100% del monto pagado a tu saldo interno, sin comisión."
)

MSG_DEPOSIT_SELECT_CURRENCY = (
    "💰 <b>¿Con qué moneda quieres depositar?</b>\n\n"
    "Monto a acreditar: <b>{amount_usd}</b> (100%, sin comisión)\n"
    "Selecciona la cripto/red — el monto ya está calculado al tipo de cambio actual:"
)

MSG_DEPOSIT_INSTRUCTIONS = (
    "💰 <b>Instrucciones de depósito</b>\n\n"
    "🪙 <b>Moneda:</b> {currency_label}\n"
    "💵 <b>Monto a enviar:</b> <code>{amount}</code>\n"
    "✅ <b>Se acreditará:</b> {amount_usd} a tu saldo interno\n\n"
    "📤 Envía exactamente ese monto a:\n"
    "<code>{address}</code>\n\n"
    "⏳ Tienes <b>15 minutos</b> para realizar el pago.\n"
    "El bot acreditará tu saldo automáticamente en cuanto reciba la transferencia.\n\n"
    "⚠️ Usa <b>únicamente la red indicada</b> ({network}). Envíos por otra red se perderán."
)

MSG_DEPOSIT_CONFIRMED = (
    "✅ <b>¡Depósito confirmado!</b>\n"
    "Se acreditaron {amount_usd} a tu saldo interno.\n"
    "Nuevo saldo: {new_balance}\n\n"
    "Puedes usarlo en tu próxima compra con /start, o consultar /saldo."
)

MSG_DEPOSIT_TIMEOUT = (
    "⏰ <b>Tiempo de depósito agotado</b>\n"
    "No se recibió el pago en 15 minutos. La orden fue cancelada.\n"
    "Puedes iniciar un nuevo depósito cuando quieras con /saldo."
)

MSG_MANUAL_DEPOSIT_SELECT_METHOD = (
    "🇨🇺 <b>Agregar saldo en CUP</b>\n\n"
    "Elige el método con el que vas a transferir:"
)

MSG_MANUAL_DEPOSIT_ASK_AMOUNT = (
    "🇨🇺 <b>{method_name}</b>\n\n"
    "Escribe el monto en USD que quieres depositar "
    "(mínimo <b>{min_usd}</b>, máximo <b>{max_usd}</b>).\n"
    "El monto en CUP se calcula al enviarte los datos de la cuenta."
)

MSG_MANUAL_DEPOSIT_INSTRUCTIONS = (
    "🇨🇺 <b>Instrucciones de pago — {method_name}</b>\n\n"
    "💵 <b>Monto a acreditar:</b> {amount_usd} (≈ {amount_cup} CUP)\n"
    "📤 <b>Transfiere exactamente {amount_cup} CUP a:</b>\n<code>{account}</code>\n\n"
    "⚠️ <b>Importante:</b> pon este código en el concepto/mensaje de la "
    "transferencia, es la única forma de identificar tu pago:\n"
    "<code>{reference_code}</code>\n\n"
    "Cuando termines, envía aquí una <b>captura de pantalla</b> del "
    "comprobante (o escribe el ID/número de la transacción si no puedes "
    "mandar foto)."
)

MSG_MANUAL_PURCHASE_INSTRUCTIONS = (
    "🇨🇺 <b>Pago en CUP — {method_name}</b>\n\n"
    "📤 <b>Transfiere exactamente {amount_cup} CUP a:</b>\n<code>{account}</code>\n\n"
    "⚠️ <b>Importante:</b> pon este código en el concepto/mensaje de la "
    "transferencia, es la única forma de identificar tu pago:\n"
    "<code>{reference_code}</code>\n\n"
    "Cuando termines, envía aquí una <b>captura de pantalla</b> del "
    "comprobante (o escribe el ID/número de la transacción si no puedes "
    "mandar foto). Apenas se confirme, recibirás tu número automáticamente."
)

MSG_MANUAL_DEPOSIT_PROOF_RECEIVED = (
    "✅ Comprobante recibido (código <code>{reference_code}</code>).\n"
    "Un administrador lo va a revisar en breve y se te avisará apenas se "
    "acredite tu saldo. Puedes seguir usando el bot mientras tanto."
)

MSG_MANUAL_DEPOSIT_APPROVED = (
    "✅ <b>¡Depósito aprobado!</b>\n"
    "Se acreditaron {amount_usd} a tu saldo interno.\n"
    "Nuevo saldo: {new_balance}\n\n"
    "Puedes usarlo en tu próxima compra con /start, o consultar /saldo."
)

MSG_MANUAL_DEPOSIT_REJECTED = (
    "❌ <b>Depósito rechazado</b> (código <code>{reference_code}</code>)\n"
    "No pudimos verificar el comprobante. Si crees que es un error, "
    "contacta al soporte indicando ese código."
)

MSG_MANUAL_DEPOSIT_ALREADY_PENDING = (
    "⚠️ Ya tienes una solicitud de depósito CUP en curso (código "
    "<code>{reference_code}</code>). Espera a que se resuelva antes de "
    "abrir otra."
)

MSG_WITHDRAW_ASK_AMOUNT = (
    "💸 <b>Retiro de saldo a cripto</b>\n\n"
    "Saldo disponible: <b>{balance}</b>\n"
    "¿Cuánto quieres retirar? Escribe el monto en USD (ej: <i>5.50</i>) "
    "o escribe <b>todo</b> para retirar el saldo completo."
)

MSG_WITHDRAW_SELECT_CURRENCY = (
    "💰 <b>¿En qué cripto/red quieres recibir el retiro?</b>\n\n"
    "💵 Retiras de tu saldo: <b>{amount_usd}</b>\n"
    "➖ Comisión de servicio ({fee_pct}): <b>{fee_usd}</b>\n"
    "✅ Recibes el equivalente a: <b>{net_usd}</b>\n\n"
    "El monto en cripto es una cotización aproximada al tipo de cambio "
    "actual — la red descuenta además su propia comisión al enviar, así "
    "que podrías recibir un poco menos:"
)

MSG_WITHDRAW_ASK_ADDRESS = (
    "📤 Envía la dirección de <b>{currency} ({network})</b> a la que "
    "quieres recibir el retiro.\n\n"
    "⚠️ Verifica que sea una dirección válida para esa red exacta — un "
    "envío a la dirección o red equivocada se pierde y no se puede recuperar."
)

MSG_WITHDRAW_CONFIRM = (
    "📋 <b>Confirma tu retiro</b>\n\n"
    "💵 Retiras de tu saldo: {amount_usd}\n"
    "➖ Comisión de servicio ({fee_pct}): {fee_usd}\n"
    "✅ Recibes el equivalente a: {net_usd} (≈ {amount_crypto})\n"
    "🌐 Red: {network}\n"
    "📤 Dirección: <code>{address}</code>\n\n"
    "El monto completo ({amount_usd}) se descuenta de tu saldo interno al "
    "confirmar. La red descontará además su propia comisión al enviar, así "
    "que el monto que llegue puede ser un poco menor al mostrado arriba."
)

MSG_WITHDRAW_SUCCESS = (
    "✅ <b>Retiro enviado</b>\n\n"
    "Se descontaron {amount_usd} de tu saldo interno.\n"
    "Nuevo saldo: {new_balance}\n\n"
    "La transferencia ya fue solicitada a la red; puede tardar unos "
    "minutos en confirmarse on-chain."
)

MSG_WITHDRAW_FAILED = (
    "❌ <b>No se pudo procesar el retiro</b>\n"
    "Hubo un error con el proveedor de pagos. No se descontó nada de tu "
    "saldo — intenta de nuevo en unos minutos con /saldo, o contacta al "
    "soporte si se repite."
)

MSG_CUP_WITHDRAW_SELECT_METHOD = (
    "🇨🇺 <b>Retiro de saldo en CUP</b>\n\n"
    "Saldo retirable en CUP: <b>{balance_cup}</b>\n"
    "Elige a través de qué método quieres recibirlo:"
)

MSG_CUP_WITHDRAW_ASK_AMOUNT = (
    "🇨🇺 <b>{method_name}</b>\n\n"
    "Saldo retirable en CUP: <b>{balance_cup}</b>\n"
    "¿Cuánto quieres retirar? Escribe el monto en CUP (ej: <i>5000</i>) "
    "o escribe <b>todo</b> para retirar el saldo CUP completo."
)

MSG_CUP_WITHDRAW_ASK_ACCOUNT = (
    "📤 Envía la cuenta/tarjeta de <b>{method_name}</b> donde quieres "
    "recibir el CUP.\n\n"
    "⚠️ Verifica que sea la cuenta correcta — un envío a una cuenta "
    "equivocada no se puede recuperar."
)

MSG_CUP_WITHDRAW_CONFIRM = (
    "📋 <b>Confirma tu retiro en CUP</b>\n\n"
    "💵 Retiras de tu saldo CUP: {amount_cup}\n"
    "➖ Comisión de servicio ({fee_pct}): {fee_cup}\n"
    "✅ Recibes: <b>{net_cup}</b>\n"
    "🏦 Método: {method_name}\n"
    "📤 Cuenta: <code>{destination}</code>\n\n"
    "El monto completo ({amount_cup}) se descuenta de tu saldo CUP al "
    "confirmar. Un administrador hace la transferencia a mano — puede "
    "tardar más que un retiro cripto."
)

MSG_CUP_WITHDRAW_SUBMITTED = (
    "✅ <b>Solicitud de retiro enviada</b> (código <code>{reference_code}</code>)\n"
    "Se descontaron {amount_usd} de tu saldo CUP.\n"
    "Un administrador va a transferir {amount_cup} CUP a tu cuenta en "
    "breve; se te avisará cuando esté hecho."
)

MSG_CUP_WITHDRAW_APPROVED = (
    "✅ <b>¡Retiro en CUP procesado!</b> (código <code>{reference_code}</code>)\n"
    "Ya deberías tener {amount_cup} CUP en tu cuenta. Si no lo ves, "
    "contacta al soporte indicando este código."
)

MSG_CUP_WITHDRAW_REJECTED = (
    "❌ <b>Retiro en CUP rechazado</b> (código <code>{reference_code}</code>)\n"
    "No se pudo procesar (ej. cuenta inválida). Se devolvieron "
    "{amount_usd} a tu saldo CUP interno — puedes intentar de nuevo con "
    "/saldo o contactar al soporte."
)

MSG_CUP_WITHDRAW_ALREADY_PENDING = (
    "⚠️ Ya tienes una solicitud de retiro CUP en curso (código "
    "<code>{reference_code}</code>). Espera a que se resuelva antes de "
    "abrir otra."
)

MSG_ERROR_GENERIC = (
    "❌ <b>Error interno</b>\n"
    "Algo salió mal. Por favor, intenta de nuevo con /start.\n"
    "Si el problema persiste, contacta al soporte."
)

MSG_REFERRAL_INFO = (
    "🔗 <b>Invita y gana</b>\n\n"
    "Comparte tu enlace. Cuando alguien lo use y complete su <b>primera "
    "compra</b>, ganas <b>{bonus_pct}</b> de esa compra en crédito para tu "
    "saldo (retirable, igual que una recarga).\n\n"
    "📤 Tu enlace:\n<code>{link}</code>\n\n"
    "👥 Invitados: {invited}\n"
    "💰 Bonos pagados: {paid} · Total ganado: {total_bonus}"
)

MSG_REFERRAL_NEW_SIGNUP = (
    "🔔 Alguien se registró con tu enlace de invitación.\n"
    "Cuando complete su primera compra, recibirás tu bono automáticamente."
)

MSG_REFERRAL_BONUS_EARNED = (
    "🎉 <b>¡Ganaste un bono de referido!</b>\n"
    "Tu invitado completó su primera compra.\n"
    "Bono acreditado: {bonus_usd}\n"
    "Saldo actual: {new_balance}\n\n"
    "Consulta /referidos o /saldo."
)

# ── Canal de comunidad ────────────────────────────────────────────────────────
# Nudge puntual (una sola vez, ver database.mark_channel_invite_sent) que se
# manda justo después de la primera compra completada -el momento de mayor
# confianza posible, recién tuvo una experiencia buena. El botón "📢 Canal
# oficial" del menú principal (ver utils.main_menu_keyboard) ya está siempre
# visible; esto es un empujón adicional en el mejor momento, no un reemplazo.
MSG_CHANNEL_INVITE = (
    "📢 Antes de que sigas, unite a nuestro canal -ahí vas a enterarte "
    "primero de lo nuevo (servicios, países, promos) sin tener que estar "
    "pendiente del bot.\n\n"
    "Es de solo lectura, nadie te va a escribir ni vas a recibir spam."
)

# ── Solicitud de reembolso post-entrega ──────────────────────────────────────
# A diferencia de un reembolso automático (timeout de SMS, cancelación antes
# de completar), esto cubre el caso donde el número YA se entregó y el
# código YA llegó, pero el cliente igual pide reembolso (ej. "no me sirvió
# en el servicio destino"). Política: solo se aprueba si HeroSMS confirma un
# problema real del número (cancelado/no entregado); si el código se
# entregó bien, el reclamo es contra el servicio destino, no contra
# nosotros -ver handlers.cmd_reembolso / cb_admin_approve_refund_request.

MSG_REFUND_REQUEST_RECEIVED = (
    "📨 <b>Solicitud de reembolso recibida</b> (tx <code>{tx_id}</code>).\n"
    "Un administrador la va a revisar. Te avisamos apenas se resuelva.\n\n"
    "Recuerda: solo se aprueban reembolsos cuando el número tuvo un "
    "problema real de nuestro lado (nunca llegó el código, activación "
    "cancelada, etc.). Si el número funcionó pero el servicio donde lo "
    "usaste lo rechazó, ese reclamo no es reembolsable aquí."
)

MSG_REFUND_ALREADY_OPEN = (
    "⚠️ Ya tienes una solicitud de reembolso abierta para la tx "
    "<code>{tx_id}</code>. Espera a que se resuelva antes de abrir otra."
)

MSG_REFUND_NOT_ELIGIBLE = (
    "❌ Esa transacción no es elegible para reembolso (o no existe / no es "
    "tuya). Solo se puede pedir reembolso de una compra ya <b>completada</b> "
    "(número entregado). Usa /historial para ver el estado de tus compras."
)

MSG_REFUND_APPROVED = (
    "💸 <b>Reembolso aprobado</b> (tx <code>{tx_id}</code>).\n"
    "Se confirmó un problema con el número de nuestro lado.\n"
    "Se acreditaron {credit_amount} a tu saldo interno "
    "(saldo total: {new_balance}; se retiene un {fee_pct} de cargo de "
    "servicio). Consulta /saldo."
)

MSG_REFUND_DENIED = (
    "❌ <b>Reembolso denegado</b> (tx <code>{tx_id}</code>).\n"
    "El número se entregó correctamente y el código llegó sin problemas "
    "de nuestro lado. Si el servicio donde lo usaste lo rechazó, ese es "
    "un problema de ese servicio, no del número -no es reembolsable aquí.\n\n"
    "Si crees que es un error, contacta al soporte indicando este ID."
)