"""
handlers.py - Controladores de comandos y callbacks con FSM de aiogram 3.x
"""
import asyncio
import logging
from datetime import datetime, timezone
from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import Message, CallbackQuery, BufferedInputFile
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove

import herosms_api as hero
import ccpay_api as ccpay
import telegram_sender
import outbox
from config import MARKUP, SERVICES, PAYMENT_TIMEOUT_SECONDS, SMS_TIMEOUT_SECONDS
from config import PAYMENT_POLL_INTERVAL, SMS_POLL_INTERVAL
from config import ADMIN_CHAT_ID, ADMIN_IDS
from config import REFUND_FEE_PCT, ABUSE_MAX_STRIKES, ABUSE_WINDOW_HOURS, ABUSE_BLOCK_HOURS
from config import WITHDRAWAL_FEE_PCT, WITHDRAWAL_ALLOWED_CURRENCIES, DEPOSIT_MIN_USD, WITHDRAWAL_MIN_USD, CUP_WITHDRAWAL_MIN_USD
from config import MANUAL_DEPOSIT_MIN_USD, MANUAL_DEPOSIT_MAX_USD, MANUAL_DEPOSIT_CUP_RATE
from config import MANUAL_DEPOSIT_CUP_MARGIN_PCT, MANUAL_DEPOSIT_CUP_EXPOSURE_ALERT_USD, MANUAL_PURCHASE_MIN_USD
from config import ACCOUNT_TYPE_LABELS
from config import REFERRAL_BONUS_PCT, REFERRAL_MIN_PURCHASE_USD
from database import db
from utils import (
    format_amount, format_cup, apply_markup, apply_refund_fee, apply_withdrawal_fee, floor_to_cents, format_phone,
    usd_to_cup, effective_cup_rate, effective_cup_rate_payout,
    generate_payment_qr,
    is_wrapped_token,
    services_keyboard, countries_keyboard, currencies_keyboard,
    cancel_keyboard, main_menu_keyboard, admin_menu_keyboard, search_results_keyboard,
    top_services_keyboard,
    balance_menu_keyboard,
    withdraw_start_keyboard, withdraw_currencies_keyboard, withdraw_confirm_keyboard,
    deposit_currencies_keyboard,
    manual_payment_methods_keyboard, manual_deposit_review_keyboard,
    purchase_cup_review_keyboard,
    cup_withdraw_methods_keyboard, cup_withdraw_confirm_keyboard, manual_withdrawal_review_keyboard,
    MSG_WELCOME, MSG_SELECT_SERVICE, MSG_SELECT_COUNTRY, MSG_SELECT_CURRENCY,
    MSG_PAYMENT_INSTRUCTIONS, MSG_WRAPPED_TOKEN_WARNING,
    MSG_PAYMENT_CONFIRMED, MSG_NUMBER_ASSIGNED,
    MSG_CODE_RECEIVED, MSG_PAYMENT_TIMEOUT, MSG_SMS_TIMEOUT,
    MSG_NO_NUMBERS, MSG_ERROR_GENERIC, MSG_SEARCH_RESULTS, MSG_SEARCH_NO_RESULTS,
    MSG_WITHDRAW_ASK_AMOUNT, MSG_WITHDRAW_SELECT_CURRENCY, MSG_WITHDRAW_ASK_ADDRESS,
    MSG_WITHDRAW_CONFIRM, MSG_WITHDRAW_SUCCESS, MSG_WITHDRAW_FAILED,
    MSG_DEPOSIT_ASK_AMOUNT, MSG_DEPOSIT_SELECT_CURRENCY, MSG_DEPOSIT_INSTRUCTIONS,
    MSG_DEPOSIT_CONFIRMED, MSG_DEPOSIT_TIMEOUT,
    MSG_MANUAL_DEPOSIT_SELECT_METHOD, MSG_MANUAL_DEPOSIT_ASK_AMOUNT,
    MSG_MANUAL_DEPOSIT_INSTRUCTIONS, MSG_MANUAL_DEPOSIT_PROOF_RECEIVED,
    MSG_MANUAL_DEPOSIT_APPROVED, MSG_MANUAL_DEPOSIT_REJECTED,
    MSG_MANUAL_DEPOSIT_ALREADY_PENDING,
    MSG_MANUAL_PURCHASE_INSTRUCTIONS,
    MSG_CUP_WITHDRAW_SELECT_METHOD, MSG_CUP_WITHDRAW_ASK_AMOUNT, MSG_CUP_WITHDRAW_ASK_ACCOUNT,
    MSG_CUP_WITHDRAW_CONFIRM, MSG_CUP_WITHDRAW_SUBMITTED, MSG_CUP_WITHDRAW_APPROVED,
    MSG_CUP_WITHDRAW_REJECTED, MSG_CUP_WITHDRAW_ALREADY_PENDING,
    MSG_REFERRAL_INFO, MSG_REFERRAL_NEW_SIGNUP, MSG_REFERRAL_BONUS_EARNED,
)

logger = logging.getLogger(__name__)
router = Router()


# ── Estados FSM ───────────────────────────────────────────────────────────────

class PurchaseFlow(StatesGroup):
    selecting_service  = State()   # Usuario viendo menú de servicios
    selecting_country  = State()   # Usuario viendo lista de países
    selecting_currency = State()   # Usuario eligiendo moneda/red de pago
    awaiting_payment   = State()   # Bot esperando confirmación de pago CCPay
    awaiting_sms       = State()   # Bot esperando código OTP de HeroSMS

    # Pago CUP ligado a ESTA compra (botón "🇨🇺 Pagar con CUP" en la
    # pantalla de moneda), distinto del depósito manual de /saldo: acá el
    # monto ya está fijado por el precio del número, no se pregunta cuánto
    # depositar, y no se acredita saldo — se marca la tx como pagada y se
    # entrega el número directo apenas un admin aprueba el comprobante.
    selecting_manual_method = State()  # Usuario eligiendo Transfermóvil/EnZona
    awaiting_manual_review  = State()  # Comprobante enviado, esperando aprobación del admin


class WithdrawFlow(StatesGroup):
    awaiting_amount    = State()   # Usuario escribiendo cuánto quiere retirar (USD)
    selecting_currency = State()   # Usuario eligiendo cripto/red de destino
    awaiting_address    = State()  # Usuario escribiendo la dirección de destino
    confirming          = State()  # Usuario confirmando antes de ejecutar el retiro


class DepositFlow(StatesGroup):
    awaiting_amount    = State()   # Usuario escribiendo cuánto quiere depositar (USD)
    selecting_currency = State()   # Usuario eligiendo cripto/red de pago
    awaiting_payment   = State()   # Bot esperando confirmación de pago CCPay


class ManualDepositFlow(StatesGroup):
    selecting_method = State()   # Usuario eligiendo Transfermóvil/EnZona
    awaiting_amount  = State()   # Usuario escribiendo cuánto quiere depositar (USD)
    awaiting_proof   = State()   # Bot esperando foto/texto del comprobante


class CupWithdrawFlow(StatesGroup):
    """
    Retiro de saldo CUP a CUP real (contraparte de ManualDepositFlow). A
    diferencia de WithdrawFlow (cripto, instantáneo vía CCPayment), acá
    SIEMPRE hace falta que un admin transfiera a mano y confirme -no hay
    forma de automatizarlo- así que el estado final no es "enviado" sino
    "pendiente de revisión" (ver manual_withdrawals en database.py).
    """
    selecting_method = State()   # Usuario eligiendo Transfermóvil/EnZona (por dónde RECIBE)
    awaiting_amount  = State()   # Usuario escribiendo cuánto quiere retirar (USD, de su saldo CUP)
    awaiting_account = State()   # Usuario escribiendo su cuenta/tarjeta de destino
    confirming       = State()   # Usuario confirmando antes de descontar saldo y avisar al admin


# HeroSMS exige un mínimo de ~2 minutos desde que se asigna un número antes
# de poder cancelarlo (ver panel web: el botón de cancelar queda inactivo
# hasta entonces). Si el usuario cancela antes, igual se le acredita el
# saldo de inmediato (ver cb_cancel) pero la llamada real a
# hero.cancel_number se retrasa hasta cumplir este mínimo.
HEROSMS_MIN_CANCEL_WAIT_SECONDS = 120


# ── Datos que guardamos en FSM (en memoria entre pasos) ───────────────────────
# Claves usadas: service_code, service_name, country_code, country_name,
#                cost_herosms, price_usd, tx_id, currency_options,
#                currency, network, order_id, pay_address, pay_amount,
#                activation_id, phone_number, refund_address


# ── Helpers internos ──────────────────────────────────────────────────────────

async def _safe_call_answer(call: CallbackQuery, *args, **kwargs):
    """
    Como _safe_answer/_safe_send pero para call.answer(). Con conexión
    inestable (ver bot.log: timeouts de red al arrancar, "Run polling"
    tardando >20s en conectar), Telegram puede rechazar un callback ya
    entregado con "query is too old" si pasó demasiado tiempo. Sin este
    wrapper, esa excepción cortaba TODO el handler antes de llegar a la
    lógica real (ej. cb_new_purchase nunca llegaba a mostrar el menú de
    servicios ni a limpiar el estado) — el usuario clickeaba y no pasaba
    nada, sin ningún mensaje de error. El resto del handler no depende de
    que el "reloj" visual de Telegram se apague, así que seguimos de largo.
    """
    try:
        await call.answer(*args, **kwargs)
    except Exception as exc:
        logger.warning("call.answer() falló (callback_data=%s): %s", call.data, exc)


async def _safe_answer(message: Message, text: str, **kwargs):
    """
    Envía mensaje capturando errores de Telegram. Si el envío directo
    falla, lo encola en el outbox (ver outbox.py) para reintento
    automático con backoff en vez de perderlo con solo un log.error() —
    así avisos importantes (reembolsos, confirmaciones) no dependen de
    que la red esté bien justo en ese instante.
    """
    try:
        await message.answer(text, parse_mode="HTML", **kwargs)
    except Exception as exc:
        logger.error("Error enviando mensaje a %s: %s", message.chat.id, exc)
        await outbox.notify(
            message.bot, message.chat.id, text,
            reply_markup=kwargs.get("reply_markup"),
        )


async def _clear_reply_keyboard(bot, chat_id: int):
    """
    Cierra cualquier ReplyKeyboardMarkup que haya quedado pegado en pantalla
    (hoy el único que existe es el de /verificar, ver cmd_verificar) enviando
    un mensaje descartable con ReplyKeyboardRemove y borrándolo enseguida.

    Por qué hace falta: los ReplyKeyboardMarkup son una capa DISTINTA a los
    InlineKeyboardMarkup que usa el resto del bot -mostrar un menú inline NO
    cierra un ReplyKeyboardMarkup que haya quedado abierto de antes-, así
    que si el usuario ignoraba /verificar y seguía navegando por los
    botones normales, el teclado "Compartir mi contacto" se quedaba tapando
    la pantalla (ver: se llamaba desde el panel admin y bloqueaba los
    botones). Se llama en los puntos de entrada principales a los menús
    (cmd_start, panel admin) como red de seguridad, además del botón
    "❌ Cancelar" que ya cierra el teclado directamente.
    """
    try:
        msg = await bot.send_message(chat_id, "🔄", reply_markup=ReplyKeyboardRemove())
        await msg.delete()
    except Exception as exc:
        logger.debug("No se pudo limpiar el teclado de respuesta en %s: %s", chat_id, exc)


async def _safe_send(bot, chat_id: int, text: str, **kwargs):
    """Envía mensaje a un chat_id específico; si falla, lo encola en el
    outbox para reintento automático (ver _safe_answer)."""
    try:
        await bot.send_message(chat_id, text, parse_mode="HTML", **kwargs)
    except Exception as exc:
        logger.error("Error send_message a %s: %s", chat_id, exc)
        await outbox.notify(bot, chat_id, text, reply_markup=kwargs.get("reply_markup"))


async def _collapse_selection(call: CallbackQuery, text: str):
    """
    Colapsa el mensaje que tenía el teclado de opciones (monedas, redes,
    métodos de pago, etc.) a una sola línea de confirmación apenas el
    usuario elige una opción, en vez de dejarlo intacto con TODOS los
    botones y mandar la siguiente pregunta como mensaje nuevo debajo.

    Antes, listas largas (ej. 20+ redes de pago) se quedaban ocupando toda
    la pantalla y el usuario tenía que scrollear para ver el siguiente
    paso del flujo. Editando el mismo mensaje a un texto corto sin
    teclado, el paso siguiente queda visible sin necesidad de scroll.

    Si falla (mensaje ya editado/borrado, es una foto sin caption editable,
    etc.) no es crítico para el flujo: solo no se colapsa el mensaje viejo.
    """
    try:
        await call.message.edit_text(text, parse_mode="HTML")
    except Exception as exc:
        logger.debug("No se pudo colapsar mensaje de selección: %s", exc)


def _reused_proof_warning(matches: list[dict]) -> str:
    """
    Arma la línea de alerta que se antepone al mensaje de revisión del
    admin cuando la MISMA captura ya se usó en otra orden (ver
    database.find_reused_proof). No bloquea nada automáticamente -la
    decisión de aprobar/rechazar sigue siendo 100% del admin-, pero antes
    no existía ninguna señal de esto: cualquier captura se mandaba a
    revisión igual, sin importar si ya se había usado para "pagar" otra
    compra/depósito distinto.
    """
    if not matches:
        return ""
    detail = ", ".join(f"{m['kind']} #{m['id']} ({m['status']})" for m in matches)
    return (
        "🚨 <b>ALERTA: esta misma captura ya se usó antes en otra orden</b>\n"
        f"Coincide con: {detail}\n"
        "Verifica con cuidado antes de aprobar — puede ser un intento de "
        "pagar varias órdenes con un solo comprobante.\n\n"
    )


async def _notify_admin(bot, text: str):
    """
    Envía una alerta al canal/grupo de admin (ADMIN_CHAT_ID). No-op si no
    está configurado, y nunca debe tumbar el flujo del usuario si falla.
    """
    if not ADMIN_CHAT_ID:
        return
    try:
        await bot.send_message(ADMIN_CHAT_ID, text, parse_mode="HTML")
    except Exception as exc:
        logger.error("No se pudo notificar al canal de admin: %s", exc)


def _user_label(user_id: int, username: str = None) -> str:
    """Identificación corta y clickeable-ish para mensajes de admin."""
    return f"@{username}" if username else f"<code>{user_id}</code>"


def _is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def _is_admin_dm(message: Message) -> bool:
    """
    Como _is_admin, pero además exige que el comando se haya ejecutado en
    chat PRIVADO con el bot. Sin esto, un admin corriendo /stats, /ventas o
    /pendientes dentro de un grupo (ej. el mismo grupo de ADMIN_CHAT_ID, que
    puede tener más de un miembro) expone margen/costo/operaciones a
    cualquiera que esté en ese grupo, no solo a admins. Igual que con un
    usuario sin permisos, si esto falla no respondemos nada.
    """
    return _is_admin(message.from_user.id) and message.chat.type == "private"


async def _mark_admin_message_resolved(message: Message, suffix: str):
    """
    Tacha visualmente el mensaje de revisión (foto o texto) del admin tras
    aprobar/rechazar, y le quita los botones para que no se pueda volver a
    pulsar sobre una solicitud ya resuelta.
    """
    try:
        if message.caption is not None:
            await message.edit_caption(caption=message.caption + suffix, reply_markup=None)
        else:
            await message.edit_text((message.text or "") + suffix, reply_markup=None)
    except Exception:
        pass


def _tx_summary_line(tx: dict) -> str:
    """Línea compacta reusada en varias alertas/reportes de admin."""
    currency_disp = tx.get("currency") or "—"
    return (
        f"TX #{tx['id']} · <b>{tx['service_name']}</b> ({tx['country_name']})\n"
        f"👤 <code>{tx['user_id']}</code> · "
        f"{format_amount(tx.get('amount_usd') or 0, 'USD')} "
        f"(pagado en {currency_disp})"
    )


async def _maybe_credit_referral_bonus(bot, tx_id: int):
    """
    Si la compra recién marcada 'completed' (tx_id) es la PRIMERA compra
    completada de quien la hizo, y esa persona llegó invitada por un
    referidor (users.referrer_id), acredita el bono (config.
    REFERRAL_BONUS_PCT del monto de esta compra, sujeto a
    REFERRAL_MIN_PURCHASE_USD) al saldo del referidor y se lo notifica.

    Se llama justo después de db.set_status(tx_id, "completed") en los dos
    lugares donde eso pasa (_poll_sms y el chequeo de último momento en
    cb_new_purchase_cancel). Cualquier error queda solo logueado: la
    entrega del número al comprador ya fue exitosa en este punto y esta
    lógica no debe poder romperla ni duplicarla (register_referral_bonus
    solo se llama una vez por tx, ya que count_completed_orders == 1 solo
    es cierto en la tx que acaba de completarse por primera vez).
    """
    try:
        tx = await db.get_by_id(tx_id)
        if not tx:
            return
        buyer_id = tx["user_id"]
        if await db.count_completed_orders(buyer_id) != 1:
            return  # no es la primera compra completada de este comprador

        buyer = await db.get_user(buyer_id)
        referrer_id = buyer.get("referrer_id") if buyer else None
        if not referrer_id:
            return

        amount_usd = float(tx.get("amount_usd") or 0)
        if amount_usd < REFERRAL_MIN_PURCHASE_USD:
            return

        bonus = round(amount_usd * REFERRAL_BONUS_PCT, 4)
        if bonus <= 0:
            return

        new_balance = await db.register_referral_bonus(referrer_id, buyer_id, tx_id, bonus)
        await _safe_send(
            bot, referrer_id,
            MSG_REFERRAL_BONUS_EARNED.format(
                bonus_usd=format_amount(bonus, "USD"),
                new_balance=format_amount(new_balance, "USD"),
            ),
        )
    except Exception as exc:
        logger.error("No se pudo acreditar bono de referido para tx=%s: %s", tx_id, exc)


async def _credit_refund_for_tx(user_id: int, tx: dict, amount_usd: float, tx_id: int, reason: str) -> float:
    """
    Acredita un reembolso ligado a `tx` respetando su origen (cripto/CUP/
    mixto, ver database.Database.get_purchase_origin_ratios) en vez de
    asumir siempre origen cripto. Sin esto, reembolsar a saldo interno una
    compra pagada en CUP dejaba esa plata marcada como "origen cripto":
    técnicamente gastable en el bot, pero el usuario -que eligió CUP
    justamente porque no tiene wallet cripto- no podría retirarla nunca
    como dinero real.

    Devuelve el saldo TOTAL (cripto + cup) después de acreditar.
    """
    ratios = await db.get_purchase_origin_ratios(tx)
    new_balance = None
    for origin, frac in ratios.items():
        portion = round(amount_usd * frac, 4)
        if portion <= 0:
            continue
        new_balance = await db.credit_balance(
            user_id, portion, tx_id, reason=f"{reason} ({origin})", origin=origin,
        )
    if new_balance is None:
        # ratios vacío o todo redondeó a 0 (montos muy chicos): fallback
        # simple, no debería pasar en la práctica.
        new_balance = await db.credit_balance(user_id, amount_usd, tx_id, reason=reason)
    return new_balance


# ── /start ────────────────────────────────────────────────────────────────────

async def _capture_referral(message: Message):
    """
    Si /start vino con el parámetro ref_<código> (deep link armado en
    _send_referral_info), vincula a quien corre /start con el dueño de ese
    código (ver database.set_referrer). No hace nada si el link no trae
    parámetro, si el código no existe, si es un intento de autoreferirse,
    o si este usuario ya tenía un referidor asignado de antes (set_referrer
    ya protege eso a nivel de DB, acá solo se evita la notificación de
    "nuevo registrado" en ese caso). Nunca debe romper /start.
    """
    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2 or not args[1].startswith("ref_"):
        return
    code = args[1][len("ref_"):].strip()
    if not code:
        return
    try:
        referrer = await db.get_user_by_referral_code(code)
        if not referrer or referrer["user_id"] == message.from_user.id:
            return
        linked = await db.set_referrer(message.from_user.id, referrer["user_id"])
        if linked:
            await _safe_send(message.bot, referrer["user_id"], MSG_REFERRAL_NEW_SIGNUP)
    except Exception as exc:
        logger.error("No se pudo procesar referido para %s: %s", message.from_user.id, exc)


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    """Punto de entrada principal."""
    await state.clear()
    await db.register_user(
        message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
        language_code=message.from_user.language_code,
        is_premium=message.from_user.is_premium,
    )
    await _capture_referral(message)

    # Si había quedado un teclado de /verificar abierto de una sesión
    # anterior, se cierra acá (ver _clear_reply_keyboard) — /start ya se
    # documenta como la forma de cancelar esa verificación.
    await _clear_reply_keyboard(message.bot, message.chat.id)

    # Si quien corre /start es un admin (ver config.ADMIN_IDS), el menú
    # principal lleva un botón extra "🛠️ Panel admin" (ver
    # utils.main_menu_keyboard) que abre el panel administrativo — el admin
    # sigue viendo el mismo menú de cliente normal (puede comprar para sí
    # mismo, consultar su saldo, etc.), solo que con acceso rápido extra.
    is_admin = _is_admin(message.from_user.id)

    # Tarjeta de bienvenida personalizada (foto de perfil real + datos de la
    # cuenta), con el menú principal ya adjunto. Si algo falla (Pillow,
    # fuente, foto corrupta, etc.) no debe tumbar el /start: se cae al
    # mensaje de texto normal de siempre.
    sent_card = await telegram_sender.send_welcome_card(
        message.bot, message, caption=MSG_WELCOME, parse_mode="HTML",
        reply_markup=main_menu_keyboard(is_admin=is_admin),
    )
    if not sent_card:
        await message.answer(
            MSG_WELCOME,
            parse_mode="HTML",
            reply_markup=main_menu_keyboard(is_admin=is_admin),
        )


async def _send_referral_info(bot, chat_id: int, user_id: int):
    """Arma y envía el mensaje de /referidos (comando y botón del menú)."""
    code = await db.ensure_referral_code(user_id)
    bot_username = (await bot.get_me()).username
    link = f"https://t.me/{bot_username}?start=ref_{code}"
    stats = await db.get_referral_stats(user_id)
    await bot.send_message(
        chat_id,
        MSG_REFERRAL_INFO.format(
            bonus_pct=f"{REFERRAL_BONUS_PCT:.0%}",
            link=link,
            invited=stats["invited"],
            paid=stats["paid"],
            total_bonus=format_amount(stats["total_bonus"], "USD"),
        ),
        parse_mode="HTML",
    )


@router.message(Command("referidos"))
async def cmd_referidos(message: Message):
    await _send_referral_info(message.bot, message.chat.id, message.from_user.id)


@router.callback_query(F.data == "my_referrals")
async def cb_my_referrals(call: CallbackQuery):
    await _safe_call_answer(call)
    await _send_referral_info(call.bot, call.message.chat.id, call.from_user.id)


# ── Panel de administrador (callbacks del botón "🛠️ Panel admin") ──────────────
# Reusa las mismas funciones _send_* que ya usan los comandos /stats, /ventas,
# /pendientes y /exposicion_cup (ver definiciones más abajo) para no duplicar
# lógica. Los que requieren parámetros (detalle/convertido/set_tipo/set_pais)
# no pueden resolverse con un solo tap: el botón muestra el mismo texto de
# uso que ya se ve al llamar el comando sin argumentos.

@router.callback_query(F.data == "admin_panel")
async def cb_admin_panel(call: CallbackQuery):
    if not _is_admin(call.from_user.id):
        await _safe_call_answer(call, "No autorizado.", show_alert=True)
        return
    await _safe_call_answer(call)
    await _clear_reply_keyboard(call.bot, call.message.chat.id)
    await call.message.answer(
        "🛠️ <b>Panel de administrador</b>\nElige qué querés consultar:",
        parse_mode="HTML",
        reply_markup=admin_menu_keyboard(),
    )


@router.callback_query(F.data == "back_to_user_menu")
async def cb_back_to_user_menu(call: CallbackQuery):
    await _safe_call_answer(call)
    await _clear_reply_keyboard(call.bot, call.message.chat.id)
    is_admin = _is_admin(call.from_user.id)
    await call.message.answer(
        "Menú principal:", reply_markup=main_menu_keyboard(is_admin=is_admin),
    )


@router.callback_query(F.data == "adm_stats")
async def cb_admin_stats(call: CallbackQuery):
    if not _is_admin(call.from_user.id):
        await _safe_call_answer(call, "No autorizado.", show_alert=True)
        return
    await _safe_call_answer(call)
    await _send_stats(None, call.message.answer)


@router.callback_query(F.data == "adm_ventas")
async def cb_admin_ventas(call: CallbackQuery):
    if not _is_admin(call.from_user.id):
        await _safe_call_answer(call, "No autorizado.", show_alert=True)
        return
    await _safe_call_answer(call)
    await _send_ventas(10, call.message.answer)


@router.callback_query(F.data == "adm_pendientes")
async def cb_admin_pendientes(call: CallbackQuery):
    if not _is_admin(call.from_user.id):
        await _safe_call_answer(call, "No autorizado.", show_alert=True)
        return
    await _safe_call_answer(call)
    await _send_pendientes(call.message.answer)


@router.callback_query(F.data == "adm_exposicion_cup")
async def cb_admin_exposicion_cup(call: CallbackQuery):
    if not _is_admin(call.from_user.id):
        await _safe_call_answer(call, "No autorizado.", show_alert=True)
        return
    await _safe_call_answer(call)
    await _send_exposicion_cup(call.message.answer)


@router.callback_query(F.data == "adm_detalle_help")
async def cb_admin_detalle_help(call: CallbackQuery):
    if not _is_admin(call.from_user.id):
        await _safe_call_answer(call, "No autorizado.", show_alert=True)
        return
    await _safe_call_answer(call)
    await call.message.answer(
        "Escribe: <code>/detalle &lt;tx_id&gt;</code>\n"
        "(el id lo ves en /pendientes o /ventas)",
        parse_mode="HTML",
    )


@router.callback_query(F.data == "adm_convertido_help")
async def cb_admin_convertido_help(call: CallbackQuery):
    if not _is_admin(call.from_user.id):
        await _safe_call_answer(call, "No autorizado.", show_alert=True)
        return
    await _safe_call_answer(call)
    await call.message.answer(
        "Escribe: <code>/convertido id1 id2 ...</code>\n"
        "(ids de la lista de /exposicion_cup)",
        parse_mode="HTML",
    )


@router.callback_query(F.data == "adm_set_tipo_help")
async def cb_admin_set_tipo_help(call: CallbackQuery):
    if not _is_admin(call.from_user.id):
        await _safe_call_answer(call, "No autorizado.", show_alert=True)
        return
    await _safe_call_answer(call)
    tipos = ", ".join(ACCOUNT_TYPE_LABELS.keys())
    await call.message.answer(
        "Escribe: <code>/set_tipo user_id tipo</code>\n"
        f"Tipos válidos: <code>{tipos}</code> o <code>ninguno</code> (para quitarlo)",
        parse_mode="HTML",
    )


@router.callback_query(F.data == "adm_set_pais_help")
async def cb_admin_set_pais_help(call: CallbackQuery):
    if not _is_admin(call.from_user.id):
        await _safe_call_answer(call, "No autorizado.", show_alert=True)
        return
    await _safe_call_answer(call)
    await call.message.answer(
        "Escribe: <code>/set_pais user_id país</code>\n"
        "Ej: <code>/set_pais 123456789 Cuba</code>\n"
        "Para quitarlo: <code>/set_pais 123456789 ninguno</code>",
        parse_mode="HTML",
    )


# ── /historial ────────────────────────────────────────────────────────────────

async def _send_historial(user_id: int, answer_func):
    """Lógica compartida entre /historial (mensaje) y el botón 'Mis pedidos'
    (callback). Recibe el user_id explícito y la función para responder en
    vez de un `Message`, porque en un callback `call.message.from_user` es
    el BOT (dueño del mensaje con el teclado), no el usuario real -> usar
    `message.from_user.id` ahí daría siempre resultados vacíos/incorrectos."""
    txns = await db.get_user_transactions(user_id, limit=5)
    if not txns:
        await answer_func("📋 No tienes transacciones registradas.")
        return

    lines = ["📋 <b>Tus últimas transacciones:</b>\n"]
    for t in txns:
        status_icon = {
            "completed": "✅", "refunded": "💸", "expired": "⏰",
            "sms_timeout": "😕", "paid": "💳", "pending": "⏳",
        }.get(t["status"], "❓")
        currency_disp = t.get("currency") or "—"
        lines.append(
            f"{status_icon} <b>{t['service_name']}</b> — {t['country_name']}\n"
            f"   Monto: {format_amount(t['amount_usd'] or 0, 'USD')} "
            f"(pagado en {currency_disp}) | Estado: {t['status']}\n"
            f"   Fecha: {str(t['created_at'] or '')[:16]}\n"
        )
    await answer_func("\n".join(lines), parse_mode="HTML")


# ── Verificación opcional de contacto real ──────────────────────────────────
# Nunca es obligatorio para usar el bot: solo le da al usuario una forma de
# dejar su teléfono real asociado a su cuenta (ej. para resolver una disputa
# de soporte más rápido), sin que el admin tenga que confiar únicamente en
# el user_id numérico + first_name que puede poner cualquier cosa.

@router.message(Command("verificar"))
async def cmd_verificar(message: Message):
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📱 Compartir mi contacto", request_contact=True)],
            [KeyboardButton(text="❌ Cancelar")],
        ],
        resize_keyboard=True, one_time_keyboard=True,
    )
    await message.answer(
        "Si querés, podés compartir tu contacto de Telegram para que quede "
        "asociado a tu cuenta (útil solo para soporte, ej. si hay que "
        "resolver una disputa). Es totalmente opcional.\n\n"
        "Pulsa el botón de abajo, o toca ❌ Cancelar.",
        reply_markup=kb,
    )


@router.message(F.text == "❌ Cancelar")
async def msg_cancel_verificar(message: Message):
    """
    Botón de escape del teclado de /verificar (ver cmd_verificar). Sin esto,
    la única forma de cerrar ese ReplyKeyboardMarkup era acordarse de
    escribir /start -y ese teclado NO se cierra solo al usar los botones
    inline de los menús (son capas distintas de Telegram), así que se
    quedaba pegado en pantalla tapando otros botones si el usuario
    navegaba a otro lado en vez de completar/cancelar la verificación.
    """
    await message.answer("Verificación cancelada.", reply_markup=ReplyKeyboardRemove())


@router.message(F.contact)
async def msg_contact_shared(message: Message):
    contact = message.contact
    # Un usuario podría reenviar el contacto de OTRA persona en vez de tocar
    # el botón -> validar que sea el suyo propio antes de guardarlo, o
    # terminaríamos asociando el teléfono de un tercero a esta cuenta.
    if contact.user_id != message.from_user.id:
        await message.answer(
            "Ese contacto no es el tuyo, no lo guardé. Si querés compartir "
            "tu propio número, usa /verificar de nuevo y toca el botón.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    await db.set_phone_number(message.from_user.id, contact.phone_number)
    await message.answer(
        "✅ Listo, tu contacto quedó asociado a tu cuenta.",
        reply_markup=ReplyKeyboardRemove(),
    )



@router.message(Command("historial"))
async def cmd_historial(message: Message):
    """Muestra las últimas 5 transacciones del usuario."""
    await _send_historial(message.from_user.id, message.answer)


# ── /saldo ────────────────────────────────────────────────────────────────────

async def _send_saldo(user_id: int, answer_func):
    """Lógica compartida entre /saldo (mensaje) y el botón 'Mi saldo'
    (callback) — mismo motivo que _send_historial: evita depender de
    `message.from_user` cuando el llamador es un callback."""
    breakdown = await db.get_balance_breakdown(user_id)
    total_available  = floor_to_cents(breakdown["total"])
    crypto_available = floor_to_cents(breakdown["crypto"])
    cup_available    = floor_to_cents(breakdown["cup"])

    can_withdraw_crypto = crypto_available >= WITHDRAWAL_MIN_USD
    # cup_available > 0 además del mínimo: con CUP_WITHDRAWAL_MIN_USD en 0
    # (default), ">= mínimo" solo no alcanza, o un saldo CUP de $0.00
    # mostraría igual el botón "Retirar en CUP".
    can_withdraw_cup    = cup_available > 0 and cup_available >= CUP_WITHDRAWAL_MIN_USD

    lines = [f"💰 <b>Tu saldo interno:</b> {format_amount(total_available, 'USD')}"]
    if crypto_available > 0 or cup_available > 0:
        lines.append(
            f"   ↳ Origen cripto (retirable a cripto): {format_amount(crypto_available, 'USD')}\n"
            f"   ↳ Origen CUP (retirable en CUP): {format_amount(cup_available, 'USD')}"
        )
    lines.append(
        "\nSe aplica automáticamente como opción de pago en tu próxima compra "
        "(botón \"Pagar con saldo\" al elegir moneda), sin importar de qué "
        "origen venga. También puedes agregar más saldo, o retirar cada "
        "bolsa por su propio camino (cripto asume comisión de red; CUP lo "
        "transfiere un admin a tu cuenta)."
    )
    if not can_withdraw_crypto and crypto_available > 0:
        lines.append(
            f"\n(Te faltan {format_amount(WITHDRAWAL_MIN_USD - crypto_available, 'USD')} "
            "de origen cripto para poder retirar a cripto.)"
        )
    if not can_withdraw_cup and cup_available > 0:
        lines.append(
            f"\n(Te faltan {format_amount(CUP_WITHDRAWAL_MIN_USD - cup_available, 'USD')} "
            "de origen CUP para poder retirar en CUP.)"
        )

    # Se muestra floor_to_cents (nunca el saldo redondeado hacia arriba de
    # format_amount) para no prometer más de lo que el usuario realmente
    # puede usar/retirar (ver docstring de floor_to_cents en utils.py).
    await answer_func(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=balance_menu_keyboard(
            can_withdraw=can_withdraw_crypto, can_withdraw_cup=can_withdraw_cup,
        ),
    )


@router.message(Command("saldo"))
async def cmd_saldo(message: Message):
    """
    Muestra el saldo interno del usuario (créditos de reembolsos previos,
    ver database.credit_balance) y cómo usarlo.

    El saldo está partido en dos "bolsas" según su ORIGEN (cripto/CUP, ver
    database.py Database.balances): ambas se pueden gastar juntas en una
    compra, pero cada una solo se puede RETIRAR de su propia forma (cripto
    o CUP real) — por eso se muestran y habilitan por separado.
    """
    await _send_saldo(message.from_user.id, message.answer)


# ── Comandos de admin (/stats, /ventas, /pendientes) ──────────────────────────
# Requieren que el user_id esté en config.ADMIN_IDS. Si alguien sin permisos
# los ejecuta, no respondemos nada (no delatamos que el comando existe).

@router.message(Command("stats"))
async def cmd_stats(message: Message):
    if not _is_admin_dm(message):
        return

    args = (message.text or "").split()
    days = int(args[1]) if len(args) > 1 and args[1].isdigit() else None
    await _send_stats(days, message.answer)


async def _send_stats(days: int | None, answer_func):
    """Lógica compartida entre /stats [días] y el botón '📊 Stats' del panel
    admin (ver cb_admin_stats). Sin días -> histórico completo."""
    period_label = f"últimos {days} días" if days else "histórico completo"

    stats = await db.get_stats(days=days)
    by_status = stats["by_status"]
    user_count = await db.get_user_count()

    lines = [
        f"📊 <b>Estadísticas</b> ({period_label})\n",
        f"👥 Usuarios registrados (histórico, todo período): {user_count}",
        f"💰 Ingresos (completadas): {format_amount(stats['revenue_usd'], 'USD')}",
        f"💸 Costo HeroSMS: {format_amount(stats['cost_usd'], 'USD')}",
        f"📈 Margen: {format_amount(stats['revenue_usd'] - stats['cost_usd'], 'USD')}",
        f"🧾 Ticket promedio: {format_amount(stats['avg_ticket_usd'], 'USD')}\n",
        "<b>Por estado:</b>",
    ]
    if not by_status:
        lines.append("  (sin operaciones en este período)")
    else:
        for status, count in sorted(by_status.items(), key=lambda x: -x[1]):
            lines.append(f"  • {status}: {count}")

    await answer_func("\n".join(lines), parse_mode="HTML")


@router.message(Command("ventas"))
async def cmd_ventas(message: Message):
    if not _is_admin_dm(message):
        return

    args = (message.text or "").split()
    limit = int(args[1]) if len(args) > 1 and args[1].isdigit() else 10
    await _send_ventas(limit, message.answer)


async def _send_ventas(limit: int, answer_func):
    """Lógica compartida entre /ventas [n] y el botón '💵 Ventas' del panel admin."""
    sales = await db.get_recent_sales(limit=limit)
    if not sales:
        await answer_func("📋 Todavía no hay ventas completadas.")
        return

    lines = [f"🧾 <b>Últimas {len(sales)} ventas completadas</b>\n"]
    for t in sales:
        lines.append(
            f"✅ {_tx_summary_line(t)}\n"
            f"   📱 {t.get('phone_number') or '—'} · {str(t['updated_at'] or '')[:16]}\n"
        )
    await answer_func("\n".join(lines), parse_mode="HTML")


@router.message(Command("pendientes"))
async def cmd_pendientes(message: Message):
    if not _is_admin_dm(message):
        return

    await _send_pendientes(message.answer)


async def _send_pendientes(answer_func):
    """Lógica compartida entre /pendientes y el botón '🔄 Pendientes' del panel admin."""
    pending = await db.get_pending_transactions()
    if not pending:
        await answer_func("✅ No hay operaciones activas en este momento.")
        return

    status_icon = {"pending": "⏳", "paid": "💳", "number_assigned": "📱"}
    lines = [f"🔄 <b>{len(pending)} operaciones activas</b>\n"]
    for t in pending:
        icon = status_icon.get(t["status"], "❓")
        lines.append(f"{icon} {_tx_summary_line(t)} · estado: {t['status']}\n")

    await answer_func("\n".join(lines), parse_mode="HTML")


@router.message(Command("detalle"))
async def cmd_detalle(message: Message):
    """
    /detalle <tx_id> - Ficha completa de UNA transacción para el admin:
    todos los campos guardados en `transactions` + el usuario (username/
    first_name) + un chequeo EN VIVO contra CCPay (¿ya llegó el pago?) y
    contra HeroSMS (¿ya llegó el código, o sigue esperando?), en vez de
    quedarnos solo con el `status` que guardamos nosotros (que puede estar
    desactualizado si el bot se reinició o un poll todavía no corrió).
    """
    if not _is_admin_dm(message):
        return

    args = (message.text or "").split()
    if len(args) != 2 or not args[1].isdigit():
        await message.answer("Uso: <code>/detalle &lt;tx_id&gt;</code>", parse_mode="HTML")
        return

    tx = await db.get_by_id(int(args[1]))
    if not tx:
        await message.answer(f"No existe ninguna transacción con id {args[1]}.")
        return

    user = await db.get_user(tx["user_id"])
    user_line = f"<code>{tx['user_id']}</code>"
    if user:
        uname = f"@{user['username']}" if user.get("username") else "(sin username)"
        full_name = " ".join(
            p for p in (user.get("first_name"), user.get("last_name")) if p
        ) or "(sin nombre)"
        user_line += f" · {uname} · {full_name}"
        extra_bits = []
        if user.get("language_code"):
            extra_bits.append(f"idioma: {user['language_code']}")
        if user.get("is_premium"):
            extra_bits.append("Telegram Premium")
        if user.get("phone_number"):
            extra_bits.append(f"tel. verificado: <code>{user['phone_number']}</code>")
        if extra_bits:
            user_line += " · " + " · ".join(extra_bits)
        # Deep link que abre el perfil/chat de esta persona directo en
        # Telegram. Funciona en los clientes oficiales SIEMPRE QUE haya
        # habido interacción previa (este bot ya la tuvo, así que sirve);
        # con solo un user_id numérico antes no había forma de ver más que
        # eso -> esto da acceso a foto de perfil, bio, y poder escribirle.
        user_line += f'\n🔗 <a href="tg://user?id={tx["user_id"]}">Abrir perfil en Telegram</a>'
    else:
        user_line += " · (nunca corrió /start, no hay más datos)"

    lines = [
        f"🧾 <b>TX #{tx['id']}</b> · estado guardado: <b>{tx['status']}</b>\n",
        f"👤 Usuario: {user_line}",
        f"📦 Servicio: {tx.get('service_name')} ({tx.get('service')})",
        f"🌎 País: {tx.get('country_name')} ({tx.get('country')})",
        f"📱 Número: {tx.get('phone_number') or '—'}",
        f"🔑 Código SMS: {tx.get('sms_code') or '—'}",
        f"🆔 activation_id (HeroSMS): {tx.get('activation_id') or '—'}\n",
        f"💵 Precio cliente: {format_amount(tx.get('amount_usd') or 0, 'USD')}",
        f"💸 Costo HeroSMS: {format_amount(tx.get('cost_herosms') or 0, 'USD')}",
        f"💱 Moneda/red de pago: {tx.get('currency') or '—'} / {tx.get('network') or '—'}",
        f"🔢 Monto a pagar: {tx.get('pay_amount')}",
        f"🏦 Dirección de pago: <code>{tx.get('pay_address') or '—'}</code>",
        f"↩️ Dirección de reembolso: <code>{tx.get('refund_address') or '—'}</code>",
        f"🆔 order_id (CCPay): <code>{tx.get('order_id') or '—'}</code>",
        f"🔗 token_id (coinId:chain): {tx.get('token_id') or '—'}\n",
        f"🕒 Creada: {tx.get('created_at')}",
        f"🕒 Actualizada: {tx.get('updated_at')}\n",
    ]

    # ── Chequeo en vivo del pago contra CCPay ──────────────────────────────
    # order_id puede ser una orden real de CCPay, o uno de los dos prefijos
    # "sintéticos" que usa el bot para identificar la tx sin haber creado
    # ninguna orden real: "cup-<id>" (pago manual en CUP, ver
    # _start_manual_purchase_payment) o "balance-<id>" (pago con saldo
    # interno). Consultar CCPay con esos ids no tiene sentido -> devuelve
    # "Invalid order id" (ver bot.log) en vez de un estado real.
    order_id = tx.get("order_id") or ""
    if order_id.startswith("cup-"):
        lines.append(
            "💳 <b>CCPay:</b> no aplica, esta compra se pagó manualmente en CUP "
            "(Transfermóvil/EnZona) — revisar el comprobante en el canal de admin, "
            "no hay orden en CCPay."
        )
    elif order_id.startswith("balance-"):
        lines.append("💳 <b>CCPay:</b> esta compra se pagó con saldo interno, no hay orden en CCPay.")
    elif order_id:
        ccpay_status_map = {
            ccpay.ORDER_STATUS_PENDING: "⏳ Pendiente (CCPay no ve el pago todavía)",
            ccpay.ORDER_STATUS_COMPLETED: "✅ Pago recibido y confirmado por CCPay",
            ccpay.ORDER_STATUS_EXPIRED: "⌛ Expirada (nunca llegó el pago a tiempo)",
            ccpay.ORDER_STATUS_CANCELLED: "❌ Cancelada/fallida",
            -1: "⚠️ CCPay devolvió un estado no reconocido (ver logs)",
        }
        try:
            live_status = await ccpay.get_order_status(order_id)
            lines.append(
                f"💳 <b>CCPay ahora mismo:</b> "
                f"{ccpay_status_map.get(live_status, f'código {live_status}')}"
            )
        except Exception as exc:
            lines.append(f"💳 <b>CCPay ahora mismo:</b> ⚠️ error al consultar ({exc})")
    else:
        if tx["status"] == "error" and tx.get("pay_amount") is None:
            lines.append(
                "💳 <b>CCPay:</b> nunca se generó una orden de pago para esta "
                "compra (se reinició el bot o se canceló antes de llegar a "
                "elegir moneda)."
            )
        else:
            lines.append("💳 <b>CCPay:</b> sin order_id registrado.")

    # ── Chequeo en vivo del SMS contra HeroSMS ─────────────────────────────
    if tx.get("activation_id"):
        try:
            hero_status = await hero.get_status(tx["activation_id"])
            hero_desc = {
                "ready": f"✅ Código ya disponible: {hero_status.get('code')}",
                "pending": "⏳ Esperando que llegue el SMS",
                "cancelled": "❌ Activación cancelada en HeroSMS",
                "error": f"⚠️ Error al consultar: {hero_status.get('error')}",
            }.get(hero_status.get("status"), f"❓ {hero_status.get('status')}")
            lines.append(f"📟 <b>HeroSMS ahora mismo:</b> {hero_desc}")
        except Exception as exc:
            lines.append(f"📟 <b>HeroSMS ahora mismo:</b> ⚠️ error al consultar ({exc})")
    else:
        lines.append("📟 <b>HeroSMS:</b> todavía no se pidió número (activation_id vacío).")

    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("exposicion_cup"))
async def cmd_exposicion_cup(message: Message):
    """
    Cuánto CUP aprobado (ya acreditado al cliente) sigue SIN convertirse a
    USDT real: es el riesgo abierto a la tasa informal moviéndose en
    contra (ver config.MANUAL_DEPOSIT_CUP_MARGIN_PCT). Alerta cuando supera
    MANUAL_DEPOSIT_CUP_EXPOSURE_ALERT_USD, pero no bloquea nada solo — el
    admin decide cuándo convertir.
    """
    if not _is_admin_dm(message):
        return

    await _send_exposicion_cup(message.answer)


async def _send_exposicion_cup(answer_func):
    """Lógica compartida entre /exposicion_cup y el botón '🇨🇺 Exposición CUP' del panel admin."""
    exposure = await db.get_cup_exposure()
    if exposure["count"] == 0:
        await answer_func("✅ Sin exposición: todo el CUP aprobado ya está marcado como convertido.")
        return

    alert = "🚨" if exposure["total_usd"] >= MANUAL_DEPOSIT_CUP_EXPOSURE_ALERT_USD else "🟡"
    lines = [
        f"{alert} <b>Exposición CUP sin convertir</b>\n",
        f"📄 {exposure['count']} depósito(s) aprobado(s), sin convertir",
        f"🇨🇺 Total: {exposure['total_cup']:,} CUP".replace(",", " "),
        f"💵 Equivalente: {format_amount(exposure['total_usd'], 'USD')}\n",
    ]
    if exposure["total_usd"] >= MANUAL_DEPOSIT_CUP_EXPOSURE_ALERT_USD:
        lines.append(
            f"⚠️ Supera el umbral de alerta "
            f"({format_amount(MANUAL_DEPOSIT_CUP_EXPOSURE_ALERT_USD, 'USD')}). "
            "Considera convertir pronto."
        )

    details = await db.get_unconverted_manual_deposits()
    lines.append("\n<b>Detalle:</b>")
    for d in details[:20]:
        cup_str = f"{d['amount_cup']:,}".replace(",", " ") if d.get("amount_cup") else "?"
        lines.append(f"  • #{d['id']} ({d['reference_code']}) — {cup_str} CUP")
    if len(details) > 20:
        lines.append(f"  … y {len(details) - 20} más")
    lines.append(
        "\nUsa <code>/convertido id1 id2 ...</code> para marcarlos como "
        "convertidos y sacarlos de la exposición."
    )

    await answer_func("\n".join(lines), parse_mode="HTML")


@router.message(Command("convertido"))
async def cmd_convertido(message: Message):
    """Marca uno o más depósitos manuales aprobados como ya convertidos a USDT real."""
    if not _is_admin_dm(message):
        return

    args = (message.text or "").split()[1:]
    ids = [int(a) for a in args if a.isdigit()]
    if not ids:
        await message.answer(
            "Uso: <code>/convertido id1 id2 ...</code> (ids de /exposicion_cup)",
            parse_mode="HTML",
        )
        return

    await db.mark_manual_deposits_converted(ids)
    await message.answer(f"✅ Marcados como convertidos: {', '.join(str(i) for i in ids)}")


@router.message(Command("set_tipo"))
async def cmd_set_tipo(message: Message):
    """Asigna el 'Nivel' (cliente/reseller/vip) que se muestra en la tarjeta
    de bienvenida del usuario. No requiere que el usuario esté online: solo
    que ya haya corrido /start alguna vez (existe una fila en `users`)."""
    if not _is_admin_dm(message):
        return

    args = (message.text or "").split()[1:]
    if len(args) != 2 or not args[0].isdigit():
        tipos = ", ".join(ACCOUNT_TYPE_LABELS.keys())
        await message.answer(
            "Uso: <code>/set_tipo user_id tipo</code>\n"
            f"Tipos válidos: <code>{tipos}</code> o <code>ninguno</code> (para quitarlo)",
            parse_mode="HTML",
        )
        return

    user_id = int(args[0])
    tipo_raw = args[1].lower()
    if tipo_raw == "ninguno":
        tipo = None
    elif tipo_raw in ACCOUNT_TYPE_LABELS:
        tipo = tipo_raw
    else:
        tipos = ", ".join(ACCOUNT_TYPE_LABELS.keys())
        await message.answer(
            f"Tipo inválido: <code>{tipo_raw}</code>. Válidos: <code>{tipos}</code> "
            "o <code>ninguno</code>.",
            parse_mode="HTML",
        )
        return

    if not await db.set_account_type(user_id, tipo):
        await message.answer(
            f"⚠️ El usuario <code>{user_id}</code> nunca corrió /start, no se pudo asignar.",
            parse_mode="HTML",
        )
        return

    label = ACCOUNT_TYPE_LABELS.get(tipo, "sin nivel") if tipo else "sin nivel"
    await message.answer(f"✅ Usuario <code>{user_id}</code> ahora es: <b>{label}</b>", parse_mode="HTML")


@router.message(Command("set_pais"))
async def cmd_set_pais(message: Message):
    """Asigna el país (texto libre) que se muestra en la tarjeta de
    bienvenida del usuario. Igual que /set_tipo: solo requiere que exista
    una fila en `users` (ya haya corrido /start alguna vez)."""
    if not _is_admin_dm(message):
        return

    args = (message.text or "").split(maxsplit=2)
    if len(args) != 3 or not args[1].isdigit():
        await message.answer(
            "Uso: <code>/set_pais user_id país</code>\n"
            "Ej: <code>/set_pais 123456789 Cuba</code>\n"
            "Para quitarlo: <code>/set_pais 123456789 ninguno</code>",
            parse_mode="HTML",
        )
        return

    user_id = int(args[1])
    pais_raw = args[2].strip()
    pais = None if pais_raw.lower() == "ninguno" else pais_raw

    if not await db.set_country(user_id, pais):
        await message.answer(
            f"⚠️ El usuario <code>{user_id}</code> nunca corrió /start, no se pudo asignar.",
            parse_mode="HTML",
        )
        return

    await message.answer(
        f"✅ Usuario <code>{user_id}</code> ahora tiene país: <b>{pais or 'sin país'}</b>",
        parse_mode="HTML",
    )


@router.message(Command("metodos"))
async def cmd_metodos(message: Message):
    """Lista los métodos de pago manual (CUP) configurados, activos e inactivos."""
    if not _is_admin_dm(message):
        return

    methods = await db.get_payment_methods(active_only=False)
    if not methods:
        await message.answer(
            "No hay métodos de pago configurados todavía.\n"
            "Usa <code>/set_metodo code | nombre | cuenta</code> para agregar uno.",
            parse_mode="HTML",
        )
        return

    lines = ["🇨🇺 <b>Métodos de pago manual (CUP)</b>\n"]
    for code, m in methods.items():
        estado = "🟢" if m["active"] else "🔴"
        lines.append(
            f"{estado} <code>{code}</code> · {m['name']}\n"
            f"    Cuenta: <code>{m['account']}</code>"
        )
    lines.append(
        "\nEditar/crear: <code>/set_metodo code | nombre | cuenta</code>\n"
        "Desactivar: <code>/quitar_metodo code</code>"
    )
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("set_metodo"))
async def cmd_set_metodo(message: Message):
    """
    Crea o actualiza un método de pago manual (tarjeta/cuenta CUP). Antes
    esto vivía hardcodeado en config.MANUAL_PAYMENT_METHODS y requería
    editar código y redeployar para cambiar una cuenta; ahora es una fila
    en la tabla `payment_methods` (ver database.upsert_payment_method) y
    el cambio se aplica al instante, sin tocar código.
    """
    if not _is_admin_dm(message):
        return

    # code | nombre | cuenta  (el nombre puede tener espacios, por eso "|"
    # como separador en vez de split() a secas)
    raw = (message.text or "").split(maxsplit=1)[1:]
    parts = [p.strip() for p in (raw[0].split("|") if raw else [])]
    if len(parts) != 3 or not all(parts):
        await message.answer(
            "Uso: <code>/set_metodo code | nombre | cuenta</code>\n"
            "Ej: <code>/set_metodo transfermovil | Transferencia (CUP) | "
            "Tarjeta 9234 XXXX XXXX 1234</code>\n\n"
            "Si <code>code</code> ya existe, actualiza nombre/cuenta y lo "
            "reactiva si estaba desactivado. Ve /metodos para ver los que hay.",
            parse_mode="HTML",
        )
        return

    code, name, account = parts
    code = code.lower().replace(" ", "_")

    await db.upsert_payment_method(code, name, account, updated_by=message.from_user.id)
    await message.answer(
        f"✅ Método <code>{code}</code> guardado:\n"
        f"Nombre: {name}\n"
        f"Cuenta: <code>{account}</code>",
        parse_mode="HTML",
    )


@router.message(Command("quitar_metodo"))
async def cmd_quitar_metodo(message: Message):
    """
    Desactiva un método de pago (no lo borra: transacciones y retiros
    viejos que ya lo usaron siguen mostrando su nombre correctamente, ver
    handlers._find_manual_method_name). Deja de ofrecerse a usuarios
    nuevos hasta que se reactive con /set_metodo.
    """
    if not _is_admin_dm(message):
        return

    args = (message.text or "").split()[1:]
    if len(args) != 1:
        await message.answer(
            "Uso: <code>/quitar_metodo code</code>\nVe /metodos para ver los códigos.",
            parse_mode="HTML",
        )
        return

    code = args[0].lower().replace(" ", "_")
    if not await db.set_payment_method_active(code, active=False):
        await message.answer(f"⚠️ No existe el método <code>{code}</code>.", parse_mode="HTML")
        return

    await message.answer(
        f"✅ Método <code>{code}</code> desactivado. Ya no se ofrece a usuarios nuevos "
        "(reactivalo con /set_metodo si te equivocaste).",
        parse_mode="HTML",
    )


# ── Menú principal (callback) ─────────────────────────────────────────────────

@router.callback_query(F.data == "new_purchase")
async def cb_new_purchase(call: CallbackQuery, state: FSMContext):
    await _safe_call_answer(call)
    await state.clear()

    # Antiabuso: bloquear temporalmente a quien acumula muchos "número
    # asignado y nunca completado" (ver database.get_abuse_strikes). Se
    # revisa acá, ANTES de dejarlo elegir servicio, para no gastar ni una
    # llamada a HeroSMS/CCPayment en un intento que probablemente se
    # abandonará de nuevo.
    strikes = await db.get_abuse_strikes(call.from_user.id, ABUSE_WINDOW_HOURS)
    if strikes >= ABUSE_MAX_STRIKES:
        await call.message.answer(
            f"⏳ <b>Compras temporalmente restringidas</b>\n\n"
            f"Detectamos {strikes} números solicitados y no completados en "
            f"las últimas {ABUSE_WINDOW_HOURS}h.\n"
            f"Por seguridad, espera {ABUSE_BLOCK_HOURS}h o contacta al "
            "soporte si crees que es un error.",
            parse_mode="HTML",
        )
        return

    # Mostramos el ranking real de más comprados si ya hay historial;
    # si el bot es nuevo (sin compras completadas todavía) caemos al
    # listado estático de config.SERVICES para no mostrar nada vacío.
    top = await db.get_top_services(limit=8)
    keyboard = top_services_keyboard(top) if top else services_keyboard(SERVICES)

    await call.message.answer(
        MSG_SELECT_SERVICE,
        parse_mode="HTML",
        reply_markup=keyboard,
    )
    await state.set_state(PurchaseFlow.selecting_service)


@router.callback_query(F.data == "my_txns")
async def cb_my_txns(call: CallbackQuery):
    await _safe_call_answer(call)
    await _send_historial(call.from_user.id, call.message.answer)


@router.callback_query(F.data == "my_balance")
async def cb_my_balance(call: CallbackQuery):
    await _safe_call_answer(call)
    await _send_saldo(call.from_user.id, call.message.answer)


@router.callback_query(F.data == "my_profile")
async def cb_my_profile(call: CallbackQuery):
    """Botón 'Mi cuenta': ficha básica del usuario. 'Nivel' y 'País' solo
    se muestran si un admin ya los asignó (ver /set_tipo, /set_pais);
    mientras tanto se omiten en vez de mostrar un valor inventado (mismo
    criterio que welcome_card.generate_welcome_card)."""
    await _safe_call_answer(call)
    user = call.from_user
    user_row = await db.get_user(user.id)
    orders_count = await db.count_completed_orders(user.id)

    lines = ["👤 <b>Tu cuenta</b>\n", f"ID: <code>{user.id}</code>"]
    if user.username:
        lines.append(f"Usuario: @{user.username}")
    if user_row and user_row.get("account_type"):
        label = ACCOUNT_TYPE_LABELS.get(user_row["account_type"].lower(), user_row["account_type"])
        lines.append(f"Nivel: {label}")
    if user_row and user_row.get("country"):
        lines.append(f"País: {user_row['country']}")
    lines.append(f"Pedidos completados: {orders_count}")
    if user_row and user_row.get("first_seen"):
        lines.append(f"Miembro desde: {str(user_row['first_seen'])[:10]}")

    await call.message.answer("\n".join(lines), parse_mode="HTML")


@router.callback_query(F.data == "my_country")
async def cb_my_country(call: CallbackQuery):
    """Botón 'Mi país': el país es texto libre asignado por un admin
    (ver database.set_country / /set_pais), no algo que el usuario elige
    aquí mismo -> solo se muestra el valor actual (o se avisa que aún no
    fue asignado)."""
    await _safe_call_answer(call)
    user_row = await db.get_user(call.from_user.id)
    country = user_row.get("country") if user_row else None
    if country:
        await call.message.answer(f"🌍 Tu país registrado: <b>{country}</b>", parse_mode="HTML")
    else:
        await call.message.answer(
            "🌍 Todavía no tienes un país asignado en tu cuenta. "
            "Contacta a soporte si crees que debería tener uno."
        )


@router.callback_query(F.data == "support")
async def cb_support(call: CallbackQuery):
    await _safe_call_answer(call)
    await call.message.answer(
        "🆘 <b>Soporte</b>\n\n"
        "Escríbenos directamente a @yode86 con tu consulta "
        "(incluye el ID de tu pedido si aplica) y te responderemos a la "
        "brevedad."
    )


# ── Selección de servicio ─────────────────────────────────────────────────────

@router.callback_query(PurchaseFlow.selecting_service, F.data.startswith("svc:"))
async def cb_select_service(call: CallbackQuery, state: FSMContext):
    await _safe_call_answer(call)
    service_code = call.data.split(":", 1)[1]
    service_name = SERVICES.get(service_code, service_code.upper())

    await call.message.answer("🔍 Consultando países disponibles...")

    countries = await hero.get_countries(service_code)

    if not countries:
        # El código de este botón puede venir de config.SERVICES (tabla
        # armada a mano) o de db.get_top_services (histórico), y puede
        # haber quedado desactualizado frente al catálogo REAL de HeroSMS
        # -> ej. botón "TikTok" con código "tt" que HeroSMS ya no reconoce
        # y por eso get_countries da vacío, aunque el servicio SÍ existe.
        # La búsqueda por texto (msg_search_service) nunca sufre esto
        # porque usa hero.search_services(), que consulta el catálogo real
        # en vez de la tabla fija. Antes de rendirnos, intentamos lo mismo
        # acá: buscar por NOMBRE en el catálogo real y reintentar con el
        # código correcto si aparece uno distinto.
        live_matches = await hero.search_services(service_name, limit=1)
        if live_matches and live_matches[0]["code"] != service_code:
            logger.warning(
                "Código de servicio desactualizado: '%s' (%s) sin países; "
                "reintentando con código real del catálogo HeroSMS: '%s'.",
                service_code, service_name, live_matches[0]["code"],
            )
            service_code = live_matches[0]["code"]
            service_name = live_matches[0]["name"]
            countries = await hero.get_countries(service_code)

    if not countries:
        await call.message.answer(
            f"😔 No hay países disponibles para <b>{service_name}</b> en este momento.\n"
            "Intenta con otro servicio.",
            parse_mode="HTML",
            reply_markup=services_keyboard(SERVICES),
        )
        return

    await state.update_data(
        service_code=service_code,
        service_name=service_name,
        countries=countries,
    )
    success_stats = await db.get_country_success_stats(service_code)
    await call.message.answer(
        MSG_SELECT_COUNTRY,
        parse_mode="HTML",
        reply_markup=countries_keyboard(countries, MARKUP, success_stats),
    )
    await state.set_state(PurchaseFlow.selecting_country)


@router.message(PurchaseFlow.selecting_service)
async def msg_search_service(message: Message, state: FSMContext):
    """Búsqueda de servicio por texto libre (ver MSG_SELECT_SERVICE)."""
    query = (message.text or "").strip()
    if not query:
        return

    results = await hero.search_services(query)

    if not results:
        await message.answer(
            MSG_SEARCH_NO_RESULTS.format(query=query),
            parse_mode="HTML",
            reply_markup=services_keyboard(SERVICES),
        )
        return

    await message.answer(
        MSG_SEARCH_RESULTS.format(query=query),
        parse_mode="HTML",
        reply_markup=search_results_keyboard(results),
    )


# ── Selección de país → mostrar opciones de moneda ────────────────────────────

@router.callback_query(PurchaseFlow.selecting_country, F.data.startswith("cnt:"))
async def cb_select_country(call: CallbackQuery, state: FSMContext):
    await _safe_call_answer(call)
    _, country_code, cost_str = call.data.split(":", 2)
    cost_herosms = float(cost_str)
    price_usd    = apply_markup(cost_herosms, MARKUP)

    data = await state.get_data()
    service_code = data["service_code"]
    service_name = data["service_name"]

    country_name = country_code.upper()
    for c in data.get("countries", []):
        if c.get("country", c.get("code")) == country_code:
            country_name = c.get("name", country_code.upper())
            break

    await _collapse_selection(call, f"✅ País elegido: {country_name}")

    # Crear registro en BD con el precio base en USD
    tx_id = await db.create_transaction(
        user_id      = call.from_user.id,
        service      = service_code,
        service_name = service_name,
        country      = country_code,
        country_name = country_name,
        cost_herosms = cost_herosms,
        amount_usd   = price_usd,
    )

    await state.update_data(
        country_code = country_code,
        country_name = country_name,
        cost_herosms = cost_herosms,
        price_usd    = price_usd,
        tx_id        = tx_id,
    )

    await _quote_and_show_currency_menu(call.message, state, tx_id, call.from_user.id, price_usd)


async def _quote_and_show_currency_menu(
    message: Message, state: FSMContext, tx_id: int, user_id: int, price_usd: float,
) -> bool:
    """
    Consulta monedas soportadas + cotización actual y muestra el menú de
    selección de moneda (PurchaseFlow.selecting_currency) para `tx_id`.

    Extraído de cb_select_country para poder reusarlo también al reanudar
    una transacción tras un reinicio del bot (ver resume_transaction, caso
    "servicio/país ya elegidos pero sin orden de pago aún") sin duplicar la
    lógica de cotización.

    Devuelve True si se mostró el menú, False si no se pudo cotizar (en
    cuyo caso ya se marcó la tx como 'error' y se avisó al usuario).
    """
    await message.answer("🔍 Consultando monedas disponibles y cotización actual...")

    # Consultar monedas soportadas dinámicamente y calcular el equivalente
    # en cada una para el precio en USD ya calculado. Se piden todas las
    # cotizaciones EN PARALELO (antes era secuencial: 5-6 llamadas x ~1-2s
    # cada una sumaban directo a la latencia percibida por el usuario).
    supported = await ccpay.get_supported_currencies()

    # Una sola llamada batch para TODAS las monedas (antes: una llamada por
    # cada una vía asyncio.gather, lo que disparaba 20-50 requests
    # simultáneos y provocaba "11004 Request too fast" del lado de CCPayment).
    token_ids = [cur["token_id"] for cur in supported]
    estimates_by_token = await ccpay.get_estimated_amounts_batch(price_usd, token_ids)

    options = []
    for cur in supported:
        amount = estimates_by_token.get(cur["token_id"])
        if amount is None:
            continue  # si no se pudo cotizar, no la ofrecemos
        options.append({
            "currency": cur["currency"],
            "network":  cur["network"],
            "label":    cur["label"],
            "amount":   amount,
            "token_id": cur["token_id"],
            "low_fee":  cur.get("low_fee", False),
        })

    # Redes de comisión baja primero (ver ccpay_api._LOW_FEE_CHAINS), para
    # que el usuario vea de entrada la opción que menos le va a costar en
    # gas al enviar el pago. sort() es estable: dentro de cada grupo se
    # mantiene el orden original.
    options.sort(key=lambda o: not o["low_fee"])

    if not options:
        await db.set_status(tx_id, "error")
        await message.answer(
            "😔 No pudimos obtener cotizaciones de pago en este momento. "
            "Intenta de nuevo en unos minutos con /start.",
        )
        await state.clear()
        return False

    await state.update_data(currency_options=options)
    await state.set_state(PurchaseFlow.selecting_currency)

    balance_usd = await db.get_balance(user_id)
    await message.answer(
        MSG_SELECT_CURRENCY.format(price_usd=format_amount(price_usd, "USD")),
        parse_mode="HTML",
        reply_markup=currencies_keyboard(
            options, balance_usd=balance_usd, price_usd=price_usd,
            manual_cup_available=bool(await db.get_payment_methods()),
        ),
    )
    return True


# ── Pagar con saldo interno ─────────────────────────────────────────────────

@router.callback_query(PurchaseFlow.selecting_currency, F.data == "pay_balance")
async def cb_pay_balance(call: CallbackQuery, state: FSMContext):
    """
    Paga la compra con saldo interno (créditos acumulados de reembolsos
    previos, ver database.credit_balance). Sin comisión de red, sin espera
    de confirmación: se descuenta y se pide el número al toque.
    """
    await _safe_call_answer(call)
    data = await state.get_data()
    tx_id     = data["tx_id"]
    price_usd = data["price_usd"]
    user_id   = call.from_user.id

    ok = await db.debit_balance(user_id, price_usd, tx_id, reason=f"Pago con saldo tx={tx_id}")
    if not ok:
        balance = await db.get_balance(user_id)
        await call.message.answer(
            f"😕 Tu saldo actual ({format_amount(balance, 'USD')}) ya no alcanza "
            f"para esta compra ({format_amount(price_usd, 'USD')}). Elige otra "
            "moneda de pago.",
        )
        return

    await _collapse_selection(call, "✅ Pagando con saldo interno...")

    order_id = f"balance-{tx_id}"
    await db.set_order_info(
        tx_id, order_id, pay_address="", currency="BALANCE", network="BALANCE",
        pay_amount=price_usd, token_id="BALANCE",
    )
    await db.set_status(tx_id, "paid")

    await state.update_data(
        refund_address = "",
        pay_amount     = price_usd,
        currency       = "BALANCE",
        network        = "BALANCE",
        token_id       = "BALANCE",
    )
    await state.set_state(PurchaseFlow.awaiting_payment)

    await call.message.answer(
        f"✅ Pagado con saldo interno ({format_amount(price_usd, 'USD')}). "
        f"Saldo restante: {format_amount(await db.get_balance(user_id), 'USD')}.\n"
        "Obteniendo tu número virtual..."
    )
    if tx := await db.get_by_id(tx_id):
        await _notify_admin(bot=call.bot, text=f"💰 <b>Pago con saldo</b>\n{_tx_summary_line(tx)}")

    await _handle_after_payment(call.bot, call.message.chat.id, state, tx_id)


# ── Selección de moneda → crear orden de pago ─────────────────────────────────

@router.callback_query(PurchaseFlow.selecting_currency, F.data.startswith("cur:"))
async def cb_select_currency(call: CallbackQuery, state: FSMContext):
    await _safe_call_answer(call)
    idx = int(call.data.split(":", 1)[1])

    data = await state.get_data()
    options = data.get("currency_options", [])
    if idx < 0 or idx >= len(options):
        await call.message.answer(MSG_ERROR_GENERIC, parse_mode="HTML")
        await state.clear()
        return

    chosen       = options[idx]
    currency     = chosen["currency"]
    network      = chosen["network"]
    currency_label = chosen["label"]
    token_id     = chosen["token_id"]

    tx_id        = data["tx_id"]
    price_usd    = data["price_usd"]
    service_name = data["service_name"]
    country_name = data["country_name"]

    memo  = f"{service_name}-{country_name}"
    order = await ccpay.create_order(chosen["amount"], token_id, memo=memo)

    if not order or not order.get("orderId") or not order.get("payAddress"):
        await db.set_status(tx_id, "error")
        await call.message.answer(MSG_ERROR_GENERIC, parse_mode="HTML")
        await state.clear()
        return

    order_id    = order["orderId"]
    pay_address = order["payAddress"]
    pay_amount  = order["payAmount"] or chosen["amount"]

    await db.set_order_info(tx_id, order_id, pay_address, currency, network, pay_amount, token_id=token_id)

    await _collapse_selection(call, f"✅ Método de pago elegido: {currency_label} (red {network})")

    # Si esta moneda tiene una opción "nativa" (currency == network, ej.
    # TRX en la red TRX) entre lo que se le ofreció al usuario, pero eligió
    # una red DISTINTA (ej. TRX en BSC), es un token envuelto/bridged:
    # avisamos para que no confunda la red al mandar el pago desde su
    # wallet/exchange (ver conversación: TRX(BSC) != TRX nativo de Tron).
    has_native_option = any(
        opt["currency"] == currency and opt["network"].upper() == currency.upper()
        for opt in options
    )
    wrapped_warning = ""
    if has_native_option and is_wrapped_token(currency, network):
        wrapped_warning = MSG_WRAPPED_TOKEN_WARNING.format(currency=currency, network=network)

    await state.update_data(
        order_id        = order_id,
        pay_address     = pay_address,
        pay_amount      = pay_amount,
        currency        = currency,
        network         = network,
        currency_label  = currency_label,
        token_id        = token_id,
        wrapped_warning = wrapped_warning,
    )
    await state.set_state(PurchaseFlow.awaiting_payment)

    # Pedir dirección de reembolso EN LA MISMA RED elegida
    await call.message.answer(
        f"📨 Para poder reembolsarte si ocurre algún problema, "
        f"por favor envía tu dirección de <b>{currency_label}</b> "
        f"(red {network}):\n\n"
        "(Escribe la dirección o envía /skip si prefieres no indicarla)",
        parse_mode="HTML",
        reply_markup=cancel_keyboard(),
    )
    await state.update_data(waiting_refund_address=True)


# ── Pagar ESTA compra directo en CUP (sin pasar por /saldo) ──────────────────
# A diferencia del depósito manual de /saldo (que pregunta "¿cuánto quieres
# depositar?" y acredita saldo para gastar después), acá el monto en CUP ya
# está fijado por el precio del número elegido: se le muestra directo
# "transfiere X CUP" (ver MSG_MANUAL_PURCHASE_INSTRUCTIONS, que a propósito
# NO menciona el equivalente en USD ni la tasa aplicada — solo el monto en
# CUP, igual que con cripto solo se muestra el monto en esa moneda). Al
# aprobar el admin, NO se acredita saldo: se marca la tx como pagada y se
# reanuda el flujo normal de compra (pedir número a HeroSMS), reusando
# resume_transaction (ver más abajo) tal cual se usa para recuperar
# transacciones tras un reinicio del bot.

@router.callback_query(PurchaseFlow.selecting_currency, F.data == "pay_cup")
async def cb_pay_cup(call: CallbackQuery, state: FSMContext):
    await _safe_call_answer(call)

    methods = await db.get_payment_methods()
    if not methods:
        await call.message.answer(MSG_ERROR_GENERIC, parse_mode="HTML")
        return

    await _collapse_selection(call, "✅ Pagando en CUP")

    if len(methods) == 1:
        method_code = next(iter(methods))
        await _start_manual_purchase_payment(call.message, state, method_code)
        return

    await state.set_state(PurchaseFlow.selecting_manual_method)
    await call.message.answer(
        "🇨🇺 Elige con qué vas a transferir:",
        reply_markup=manual_payment_methods_keyboard(methods),
    )


@router.callback_query(PurchaseFlow.selecting_manual_method, F.data.startswith("mmethod:"))
async def cb_select_purchase_manual_method(call: CallbackQuery, state: FSMContext):
    await _safe_call_answer(call)
    method_code = call.data.split(":", 1)[1]
    methods = await db.get_payment_methods()
    method_name = methods.get(method_code, {}).get("name", method_code)
    await _collapse_selection(call, f"✅ Transferencia elegida: {method_name}")
    await _start_manual_purchase_payment(call.message, state, method_code)


async def _start_manual_purchase_payment(message: Message, state: FSMContext, method_code: str):
    method = (await db.get_payment_methods()).get(method_code)
    if not method:
        await message.answer(MSG_ERROR_GENERIC, parse_mode="HTML")
        await state.clear()
        return

    data      = await state.get_data()
    tx_id     = data["tx_id"]
    price_usd = data["price_usd"]

    # Piso mínimo (ver config.MANUAL_PURCHASE_MIN_USD): si el precio del
    # número es más barato que esto, se cobra el mínimo igual. Sin esto,
    # un número de $0.04 termina pidiendo unos pocos CUP, monto que no
    # justifica el tiempo del operador en revisar el comprobante y luego
    # convertir ese CUP a USDT real.
    billed_usd = max(price_usd, MANUAL_PURCHASE_MIN_USD)

    # Tasa efectiva (con margen) pero NUNCA se le muestra al cliente el
    # desglose USD->CUP, solo el monto final en CUP (ver docstring arriba).
    effective_rate = effective_cup_rate(MANUAL_DEPOSIT_CUP_RATE, MANUAL_DEPOSIT_CUP_MARGIN_PCT)
    amount_cup = usd_to_cup(billed_usd, effective_rate)
    reference_code = f"REF-{tx_id:06d}"

    # Se guarda la "orden" directo en la transacción (igual que con cripto:
    # order_id/pay_address/pay_amount/currency), sin crear ningún registro
    # nuevo — el tx_id YA identifica la operación de punta a punta.
    await db.set_order_info(
        tx_id, order_id=f"cup-{tx_id}", pay_address=method["account"],
        currency="CUP", network="MANUAL", pay_amount=amount_cup, token_id="CUP_MANUAL",
    )

    # Aviso inmediato al admin de que hay un pago CUP en camino, ANTES de
    # que llegue el comprobante (que puede tardar minutos u horas — ver
    # msg_purchase_manual_proof, que manda el mensaje "de verdad" con foto
    # y botones para aprobar/rechazar). Este es solo un heads-up liviano
    # para que quien tiene Telegram en el móvil sepa que hay que estar
    # pendiente; no requiere ninguna acción todavía.
    if tx := await db.get_by_id(tx_id):
        buyer = await db.get_user(tx["user_id"])
        await _notify_admin(
            message.bot,
            f"🔔 <b>Pago CUP iniciado</b> (compra)\n"
            f"{_user_label(tx['user_id'], buyer.get('username') if buyer else None)} · "
            f"{format_amount(billed_usd, 'USD')} ({f'{amount_cup:,}'.replace(',', ' ')} CUP)\n"
            f"Método: {method['name']} · Código: <code>{reference_code}</code>\n"
            f"{_tx_summary_line(tx)}\n"
            "Aún sin comprobante — avisamos apenas llegue.",
        )

    await state.update_data(
        manual_method_code = method_code,
        manual_amount_cup  = amount_cup,
        manual_reference_code = reference_code,
    )
    await state.set_state(PurchaseFlow.awaiting_manual_review)

    await message.answer(
        MSG_MANUAL_PURCHASE_INSTRUCTIONS.format(
            method_name    = method["name"],
            amount_cup     = f"{amount_cup:,}".replace(",", " "),
            account        = method["account"],
            reference_code = reference_code,
        ),
        parse_mode="HTML",
        reply_markup=cancel_keyboard(),
    )


@router.message(PurchaseFlow.awaiting_manual_review)
async def msg_purchase_manual_proof(message: Message, state: FSMContext):
    """Comprobante de un pago CUP ligado a una compra (el número aún no se entregó)."""
    data = await state.get_data()
    tx_id = data.get("tx_id")
    reference_code = data.get("manual_reference_code")
    if not tx_id:
        await state.clear()
        await message.answer(MSG_ERROR_GENERIC, parse_mode="HTML")
        return

    if not message.photo:
        await message.answer(
            "⚠️ Necesito una <b>captura de pantalla</b> del comprobante de la "
            "transferencia (no alcanza con escribir un ID/número).",
            parse_mode="HTML",
            reply_markup=cancel_keyboard(),
        )
        return

    proof_file_id = message.photo[-1].file_id
    proof_file_unique_id = message.photo[-1].file_unique_id
    proof_text = None

    # Antes esto no se guardaba en ningún lado (ver docstring de
    # db.set_purchase_proof): quedaba solo como mensaje en el canal de
    # admin, sin poder cruzarlo con otras órdenes.
    await db.set_purchase_proof(
        tx_id, proof_file_id=proof_file_id,
        proof_file_unique_id=proof_file_unique_id, proof_text=proof_text,
    )
    reused = await db.find_reused_proof(proof_file_unique_id, exclude_tx_id=tx_id)

    await message.answer(
        f"✅ Comprobante recibido (código <code>{reference_code}</code>).\n"
        "Un administrador lo va a revisar en breve; apenas se confirme, "
        "recibirás tu número automáticamente.",
        parse_mode="HTML",
    )

    tx = await db.get_by_id(tx_id)
    methods = await db.get_payment_methods()
    method_name = methods.get(data.get("manual_method_code"), {}).get("name", "CUP")
    amount_cup_str = f"{data.get('manual_amount_cup', 0):,}".replace(",", " ")
    caption = (
        _reused_proof_warning(reused) +
        f"🇨🇺 <b>Pago CUP de una compra a revisar</b>\n"
        f"Código: <code>{reference_code}</code>\n"
        f"Método: {method_name}\n"
        f"Monto: {amount_cup_str} CUP\n"
        f"{_tx_summary_line(tx) if tx else f'tx: {tx_id}'}"
    )
    if proof_text:
        caption += f"\n📝 Comprobante (texto): <code>{proof_text}</code>"

    try:
        if proof_file_id:
            await message.bot.send_photo(
                ADMIN_CHAT_ID, proof_file_id, caption=caption, parse_mode="HTML",
                reply_markup=purchase_cup_review_keyboard(tx_id),
            )
        else:
            await message.bot.send_message(
                ADMIN_CHAT_ID, caption, parse_mode="HTML",
                reply_markup=purchase_cup_review_keyboard(tx_id),
            )
    except Exception as exc:
        logger.error("No se pudo notificar al admin el pago CUP de compra tx=%s: %s", tx_id, exc)

    # El estado se limpia acá: la entrega del número no depende de la FSM
    # en memoria (que se perdería si el admin tarda y el bot se reinicia
    # mientras tanto) sino de resume_transaction, reconstruida a partir de
    # lo que ya quedó en SQLite (ver cb_admin_approve_purchase_cup).
    await state.clear()


@router.callback_query(F.data.startswith("ptx_ok:"))
async def cb_admin_approve_purchase_cup(call: CallbackQuery, fsm_storage):
    if not _is_admin(call.from_user.id):
        await _safe_call_answer(call, "No autorizado.", show_alert=True)
        return

    tx_id = int(call.data.split(":", 1)[1])
    tx = await db.get_by_id(tx_id)
    if not tx:
        await _safe_call_answer(call, "No encontrada.", show_alert=True)
        return
    if tx["status"] != "pending":
        await _safe_call_answer(call, f"Ya estaba en estado '{tx['status']}'.", show_alert=True)
        return

    await db.set_status(tx_id, "paid")
    await _safe_call_answer(call, "Aprobado ✅")
    await _mark_admin_message_resolved(call.message, f"\n\n✅ Aprobado por {call.from_user.id}")

    tx = await db.get_by_id(tx_id)
    await resume_transaction(call.bot, fsm_storage, tx)


@router.callback_query(F.data.startswith("ptx_no:"))
async def cb_admin_reject_purchase_cup(call: CallbackQuery):
    if not _is_admin(call.from_user.id):
        await _safe_call_answer(call, "No autorizado.", show_alert=True)
        return

    tx_id = int(call.data.split(":", 1)[1])
    tx = await db.get_by_id(tx_id)
    if not tx:
        await _safe_call_answer(call, "No encontrada.", show_alert=True)
        return
    if tx["status"] != "pending":
        await _safe_call_answer(call, f"Ya estaba en estado '{tx['status']}'.", show_alert=True)
        return

    await db.set_status(tx_id, "error")
    await _safe_call_answer(call, "Rechazado ❌")
    await _mark_admin_message_resolved(call.message, f"\n\n❌ Rechazado por {call.from_user.id}")

    await _safe_send(
        call.bot, tx["user_id"],
        f"❌ No pudimos confirmar tu pago en CUP para <code>{tx['service_name']}</code> "
        f"({tx['country_name']}). Si crees que es un error contacta al soporte indicando "
        f"este ID: <code>{tx_id}</code>. No se generó ningún cargo.",
    )


# ── Capturar dirección de reembolso ──────────────────────────────────────────

@router.message(PurchaseFlow.awaiting_payment)
async def msg_refund_address(message: Message, state: FSMContext):
    """Recibe la dirección de reembolso (en la red elegida) del usuario."""
    data = await state.get_data()

    if not data.get("waiting_refund_address"):
        # Ya tenemos la dirección; ignorar mensajes extra
        return

    refund_address = ""
    if message.text and message.text.strip().lower() != "/skip":
        refund_address = message.text.strip()

    await db.set_refund_address(data["tx_id"], refund_address)

    await state.update_data(
        refund_address          = refund_address,
        waiting_refund_address  = False,
    )

    # Ahora sí mostramos las instrucciones de pago, con QR para que el
    # usuario pueda escanear desde su wallet en vez de copiar la dirección
    # a mano (utils.generate_payment_qr).
    instructions_text = MSG_PAYMENT_INSTRUCTIONS.format(
        service         = data["service_name"],
        country         = data["country_name"],
        currency_label  = data["currency_label"],
        amount          = format_amount(data["pay_amount"], data["currency"]),
        address         = data["pay_address"],
        network         = data["network"],
        wrapped_warning = data.get("wrapped_warning", ""),
    )

    try:
        qr_bytes = generate_payment_qr(
            address = data["pay_address"],
            amount  = data["pay_amount"],
            network = data["network"],
        )
        await message.answer_photo(
            photo=BufferedInputFile(qr_bytes, filename="payment_qr.png"),
            caption=instructions_text,
            parse_mode="HTML",
            reply_markup=cancel_keyboard(),
        )
    except Exception as exc:
        # Si algo falla generando/enviando el QR, no bloqueamos el pago:
        # caemos al mensaje de texto plano de siempre.
        logger.error("No se pudo generar/enviar el QR de pago (tx=%s): %s", data["tx_id"], exc)
        await message.answer(
            instructions_text,
            parse_mode="HTML",
            reply_markup=cancel_keyboard(),
        )

    # Iniciar polling de pago en background
    asyncio.create_task(
        _poll_payment(
            bot      = message.bot,
            chat_id  = message.chat.id,
            state    = state,
            tx_id    = data["tx_id"],
            order_id = data["order_id"],
        )
    )


async def _poll_payment(
    bot, chat_id: int, state: FSMContext, tx_id: int, order_id: str,
    elapsed_start: int = 0,
):
    """
    Tarea asyncio: verifica el pago cada PAYMENT_POLL_INTERVAL segundos
    durante un máximo de PAYMENT_TIMEOUT_SECONDS.

    `elapsed_start`: segundos ya transcurridos antes de esta llamada. Se usa
    al RECUPERAR una transacción tras un reinicio del bot (ver main.py),
    para no reiniciar el timeout de pago desde cero.
    """
    elapsed = elapsed_start
    while elapsed < PAYMENT_TIMEOUT_SECONDS:
        await asyncio.sleep(PAYMENT_POLL_INTERVAL)
        elapsed += PAYMENT_POLL_INTERVAL

        # Si el usuario canceló mientras esperábamos (botón "Cancelar"),
        # dejamos de bloquear su flujo. CCPayment no tiene un endpoint para
        # cerrar la orden de depósito (es solo una dirección con su propio
        # expiredAt) así que lo único que controlamos es nuestro lado: si
        # para cuando canceló YA había enviado el pago, lo reembolsamos
        # automáticamente en vez de dejarlo sin resolver.
        tx = await db.get_by_id(tx_id)
        if tx and tx["status"] == "cancelled":
            await _handle_cancelled_order(bot, chat_id, tx_id, order_id)
            return

        status = await ccpay.get_order_status(order_id)

        if status == ccpay.ORDER_STATUS_COMPLETED:
            await db.set_status(tx_id, "paid")
            await _safe_send(bot, chat_id, MSG_PAYMENT_CONFIRMED)
            if tx := await db.get_by_id(tx_id):
                await _notify_admin(bot, f"💰 <b>Pago confirmado</b>\n{_tx_summary_line(tx)}")
            await _handle_after_payment(bot, chat_id, state, tx_id)
            return

        if status in (ccpay.ORDER_STATUS_EXPIRED, ccpay.ORDER_STATUS_CANCELLED):
            await db.set_status(tx_id, "expired")
            await _safe_send(bot, chat_id, MSG_PAYMENT_TIMEOUT)
            if tx := await db.get_by_id(tx_id):
                await _notify_admin(bot, f"⏰ <b>Pago expirado/cancelado</b>\n{_tx_summary_line(tx)}")
            await state.clear()
            return

        # Estado pendiente → seguimos esperando
        logger.debug("Pago pendiente (tx=%s, elapsed=%ds)", tx_id, elapsed)

    # Tiempo agotado
    await db.set_status(tx_id, "expired")
    await _safe_send(bot, chat_id, MSG_PAYMENT_TIMEOUT)
    if tx := await db.get_by_id(tx_id):
        await _notify_admin(bot, f"⏰ <b>Pago expirado (timeout)</b>\n{_tx_summary_line(tx)}")
    await state.clear()


async def _handle_cancelled_order(bot, chat_id: int, tx_id: int, order_id: str):
    """
    Se llama cuando _poll_payment detecta que el usuario canceló (botón
    "Cancelar") mientras esperábamos su pago.

    CCPayment no ofrece forma de cancelar/cerrar la orden de depósito en sí
    (ver nota en _poll_payment) -> lo que hacemos es chequear UNA vez más si
    ya había un pago acreditado antes de la cancelación. Si lo hay, se
    reembolsa automáticamente; si no, simplemente dejamos de esperar.
    """
    status = await ccpay.get_order_status(order_id)
    if status != ccpay.ORDER_STATUS_COMPLETED:
        logger.info("Orden %s (tx=%s) cancelada por el usuario, sin pago recibido.", order_id, tx_id)
        return

    tx = await db.get_by_id(tx_id)
    if not tx:
        return

    new_balance = await db.credit_balance(
        tx["user_id"], tx["amount_usd"], tx_id, reason=f"Cancelación con pago tx={tx_id}",
    )
    await db.set_status(tx_id, "refunded")

    await _safe_send(
        bot, chat_id,
        "💸 Detectamos que tu pago ya había llegado cuando cancelaste la operación.\n"
        f"Se acreditaron {format_amount(tx['amount_usd'], 'USD')} a tu saldo interno "
        f"(saldo total: {format_amount(new_balance, 'USD')}). Úsalo en tu próxima "
        "compra con /start, o consulta /saldo.",
    )
    await _notify_admin(
        bot, f"💸 <b>Orden cancelada con pago detectado</b> (acreditado a saldo)\n{_tx_summary_line(tx)}"
    )


async def _handle_after_payment(bot, chat_id: int, state: FSMContext, tx_id: int):
    """
    Lógica post-pago: obtener número de HeroSMS y esperar SMS.
    Se ejecuta dentro de la tarea asyncio de polling de pago.
    """
    data = await state.get_data()
    service_code   = data["service_code"]
    service_name   = data["service_name"]
    country_code   = data["country_code"]
    country_name   = data["country_name"]
    refund_address = data.get("refund_address", "")
    pay_amount     = data["pay_amount"]
    currency       = data["currency"]
    network        = data["network"]
    token_id       = data["token_id"]

    # Solicitar número a HeroSMS
    number_info = await hero.get_number(service_code, country_code)

    if not number_info:
        # Sin números disponibles → esto NO es un caso de posible abuso (el
        # usuario nunca llegó a tener un número asignado), así que se
        # acredita el 100% como saldo interno: no hay comisión de red que
        # pagar porque no es una transferencia on-chain, se acredita al
        # instante y sin fricción.
        price_usd = data.get("price_usd", pay_amount)
        tx_for_origin = await db.get_by_id(tx_id) or {}
        new_balance = await _credit_refund_for_tx(
            chat_id, tx_for_origin, price_usd, tx_id, reason=f"Sin números disponibles tx={tx_id}",
        )
        await db.set_status(tx_id, "refunded")
        refund_info = (
            f"Se acreditaron {format_amount(price_usd, 'USD')} a tu saldo interno "
            f"(saldo total: {format_amount(new_balance, 'USD')}). Úsalo en tu "
            "próxima compra con /start, o pide retirarlo con /saldo."
        )
        refund_ok = True
        await _safe_send(
            bot, chat_id,
            MSG_NO_NUMBERS.format(
                service     = service_name,
                country     = country_name,
                refund_info = refund_info,
            ),
        )
        if tx := await db.get_by_id(tx_id):
            icon = "💸" if refund_ok else "🚨"
            note = "reembolso OK" if refund_ok else "REEMBOLSO FALLÓ - revisar manualmente"
            await _notify_admin(
                bot, f"{icon} <b>Sin números disponibles</b> ({note})\n{_tx_summary_line(tx)}"
            )
        await state.clear()
        return

    activation_id = str(number_info["id"])
    phone_number  = format_phone(number_info["number"])

    await db.set_activation(tx_id, activation_id, phone_number)
    await db.set_status(tx_id, "number_assigned")

    await state.update_data(
        activation_id = activation_id,
        phone_number  = phone_number,
    )
    await state.set_state(PurchaseFlow.awaiting_sms)

    await _safe_send(
        bot, chat_id,
        MSG_NUMBER_ASSIGNED.format(
            number=phone_number, service=service_name,
            timeout_min=SMS_TIMEOUT_SECONDS // 60,
        ),
    )

    # Iniciar polling de SMS
    asyncio.create_task(
        _poll_sms(
            bot            = bot,
            chat_id        = chat_id,
            state          = state,
            tx_id          = tx_id,
            activation_id  = activation_id,
            pay_amount     = pay_amount,
            token_id       = token_id,
            refund_address = refund_address,
        )
    )


async def _poll_sms(
    bot, chat_id: int, state: FSMContext,
    tx_id: int, activation_id: str,
    pay_amount: float, token_id: str, refund_address: str,
    elapsed_start: int = 0,
):
    """
    Tarea asyncio: verifica el código OTP cada SMS_POLL_INTERVAL segundos
    durante un máximo de SMS_TIMEOUT_SECONDS.

    `elapsed_start`: igual que en _poll_payment, para reanudar tras reinicio
    sin resetear el timeout.
    """
    elapsed = elapsed_start
    while elapsed < SMS_TIMEOUT_SECONDS:
        await asyncio.sleep(SMS_POLL_INTERVAL)
        elapsed += SMS_POLL_INTERVAL

        result = await hero.get_status(activation_id)
        status = result.get("status", "")

        if status == "ready" and result.get("code"):
            code = result["code"]
            await db.set_sms_code(tx_id, code)
            await db.set_status(tx_id, "completed")

            # Confirmar recepción a HeroSMS
            await hero.set_status_done(activation_id)

            await _safe_send(bot, chat_id, MSG_CODE_RECEIVED.format(code=code))
            if tx := await db.get_by_id(tx_id):
                await _notify_admin(
                    bot,
                    f"✅ <b>Venta completada</b>\n{_tx_summary_line(tx)}\n"
                    f"📱 {tx.get('phone_number') or '—'}",
                )
            await _maybe_credit_referral_bonus(bot, tx_id)
            await state.clear()
            return

        if status == "cancelled":
            # El número fue cancelado del lado del proveedor (ej. manualmente
            # en el panel de hero-sms.com), NO desde el bot -> a diferencia
            # de un timeout normal o de una cancelación manual del usuario,
            # acá no hubo "abandono" del usuario, así que no tiene sentido
            # retener el cargo de servicio antiabuso (REFUND_FEE_PCT). Antes
            # este caso caía en el else de abajo y el bot seguía "esperando"
            # en silencio hasta agotar todo SMS_TIMEOUT_SECONDS, sin avisar
            # a nadie de que el número ya estaba muerto.
            tx = await db.get_by_id(tx_id)
            user_id = tx["user_id"] if tx else chat_id
            price_usd = float(tx["amount_usd"]) if tx else pay_amount
            new_balance = await _credit_refund_for_tx(
                user_id, tx or {}, price_usd, tx_id,
                reason=f"Cancelado externamente en HeroSMS tx={tx_id}",
            )
            await db.set_status(tx_id, "cancelled")

            await _safe_send(
                bot, chat_id,
                "⚠️ Tu número fue cancelado del lado del proveedor antes de "
                "recibir el código.\n"
                f"Se te reembolsó el monto completo: {format_amount(price_usd, 'USD')} "
                f"a tu saldo interno (saldo total: {format_amount(new_balance, 'USD')}), "
                "sin cargo de servicio.\nÚsalo en tu próxima compra con /start, "
                "o consulta /saldo.",
            )
            if tx := await db.get_by_id(tx_id):
                await _notify_admin(
                    bot,
                    f"⚠️ <b>Número cancelado externamente en HeroSMS</b> "
                    f"(detectado durante el polling, reembolso completo)\n{_tx_summary_line(tx)}",
                )
            await state.clear()
            return

        logger.debug("Esperando SMS (tx=%s, elapsed=%ds, status=%s)", tx_id, elapsed, status)

    # Tiempo agotado → cancelar en HeroSMS y acreditar como saldo interno.
    # Reembolso COMPLETO (sin REFUND_FEE_PCT): a diferencia de una
    # cancelación manual voluntaria (ver cb_cancel_active_purchase más
    # abajo), acá no hay forma de saber si el SMS de verdad nunca llegó
    # (falla del servicio, no culpa del usuario) o si el usuario
    # simplemente no lo usó a tiempo — y penalizar el primer caso con un
    # cargo antiabuso es peor que el abuso ocasional que el fee buscaba
    # evitar. Se acredita como saldo interno en vez de mandar una
    # transacción on-chain: no hay comisión de red que pagar, es
    # instantáneo, y si el usuario de verdad quiere el dinero fuera del
    # bot puede pedir un retiro con /saldo (ahí sí asume él la comisión
    # de red).
    await hero.cancel_number(activation_id)

    tx = await db.get_by_id(tx_id)
    user_id = tx["user_id"] if tx else chat_id
    price_usd = tx["amount_usd"] if tx else pay_amount
    credit_amount = price_usd
    new_balance = await _credit_refund_for_tx(
        user_id, tx or {}, credit_amount, tx_id, reason=f"SMS timeout tx={tx_id}",
    )
    refund_info = (
        f"se acreditaron {format_amount(credit_amount, 'USD')} a tu saldo interno "
        f"(saldo total: {format_amount(new_balance, 'USD')}; reembolso completo, "
        "el número no llegó a usarse). "
        "Úsalo en tu próxima compra con /start, o consulta /saldo"
    )
    await db.set_status(tx_id, "sms_timeout")

    await _safe_send(
        bot, chat_id,
        MSG_SMS_TIMEOUT.format(refund_info=refund_info),
    )
    if tx := await db.get_by_id(tx_id):
        await _notify_admin(
            bot, f"💸 <b>Timeout de SMS</b> (acreditado a saldo)\n{_tx_summary_line(tx)}"
        )
    await state.clear()


async def _find_manual_method_name(account: str | None) -> str | None:
    """
    Busca entre los métodos de pago (ver database.get_payment_methods, antes
    era el dict estático config.MANUAL_PAYMENT_METHODS) cuál tiene esta
    cuenta/tarjeta (guardada como pay_address en la tx, ver
    _start_manual_purchase_payment) para recuperar su nombre legible (ej.
    'Transfermóvil (CUP)') al reanudar una compra CUP tras un reinicio, ya
    que el método_code en sí no se persiste en la tabla `transactions`.
    None si no hay match — incluye métodos inactivos (active_only=False),
    para que una tx vieja que usó una tarjeta ya dada de baja siga
    mostrando su nombre en vez de "CUP" genérico.
    """
    if not account:
        return None
    methods = await db.get_payment_methods(active_only=False)
    for method in methods.values():
        if method.get("account") == account:
            return method.get("name")
    return None


# ── Recuperación tras reinicio del bot ────────────────────────────────────────

def _elapsed_seconds(timestamp) -> int:
    """
    Calcula cuántos segundos pasaron desde `timestamp` (columna updated_at,
    en UTC) hasta ahora. Se usa para reanudar un polling sin resetear su
    timeout desde cero. Si el timestamp falta o es inválido, asume 0
    (peor caso: se le da al usuario el timeout completo de nuevo).

    Con Neon/psycopg2, columnas TIMESTAMPTZ llegan ya como datetime.datetime,
    no como string (a diferencia de la versión vieja con SQLite) — de ahí el
    isinstance() antes de intentar parsear un string con fromisoformat.
    """
    if not timestamp:
        return 0
    try:
        dt = timestamp if isinstance(timestamp, datetime) else datetime.fromisoformat(timestamp)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))
    except (ValueError, TypeError):
        return 0


async def resume_transaction(bot, storage, tx: dict) -> str:
    """
    Reanuda automáticamente una transacción que quedó en un estado activo
    (pending/paid/number_assigned) cuando el bot se reinició, sin que el
    operador tenga que revisar la base de datos a mano.

    Se llama desde main.py al arrancar, una vez por cada fila "colgada".
    Reconstruye el FSMContext del usuario (se perdió al reiniciar, ya que
    usamos MemoryStorage) a partir de lo que sí quedó guardado en SQLite,
    y relanza la tarea de polling correspondiente con el timeout restante
    (no desde cero).

    Devuelve una etiqueta corta (para loguear) describiendo qué se hizo.
    """
    tx_id   = tx["id"]
    user_id = tx["user_id"]
    chat_id = user_id  # este bot solo opera en chats privados 1:1

    key   = StorageKey(bot_id=bot.id, chat_id=chat_id, user_id=user_id)
    state = FSMContext(storage=storage, key=key)

    base_data = dict(
        service_code   = tx["service"],
        service_name   = tx["service_name"],
        country_code   = tx["country"],
        country_name   = tx["country_name"],
        cost_herosms   = tx["cost_herosms"],
        price_usd      = tx["amount_usd"],
        tx_id          = tx_id,
        refund_address = tx.get("refund_address") or "",
        pay_amount     = tx.get("pay_amount"),
        currency       = tx.get("currency"),
        network        = tx.get("network"),
        token_id       = tx.get("token_id"),
        order_id       = tx.get("order_id"),
    )
    elapsed = _elapsed_seconds(tx.get("updated_at"))

    # ── Caso 1: ya tenía número asignado, solo faltaba el SMS ────────────────
    if tx["status"] == "number_assigned" and tx.get("activation_id"):
        await state.set_data({
            **base_data,
            "activation_id": tx["activation_id"],
            "phone_number":  tx.get("phone_number"),
        })
        await state.set_state(PurchaseFlow.awaiting_sms)
        await _safe_send(
            bot, chat_id,
            "🔄 El bot se reinició, pero tu operación sigue en curso.\n"
            "Retomando la espera de tu código SMS...",
        )
        asyncio.create_task(_poll_sms(
            bot=bot, chat_id=chat_id, state=state, tx_id=tx_id,
            activation_id=tx["activation_id"], pay_amount=tx["pay_amount"],
            token_id=tx["token_id"], refund_address=base_data["refund_address"],
            elapsed_start=elapsed,
        ))
        return "sms_polling_resumed"

    # ── Caso 2: el pago ya estaba confirmado, faltaba pedir el número ────────
    if tx["status"] == "paid":
        await state.set_data(base_data)
        await state.set_state(PurchaseFlow.awaiting_payment)
        await _safe_send(
            bot, chat_id,
            "✅ Tu pago fue confirmado.\nObteniendo tu número virtual...",
        )
        asyncio.create_task(_handle_after_payment(bot, chat_id, state, tx_id))
        return "number_request_resumed"

    # ── Caso 3: ya se había generado una orden de pago CRIPTO (vía CCPay),
    # esperando el pago. Excluye explícitamente las órdenes de pago manual
    # en CUP (order_id con prefijo "cup-", ver Caso 3.5 abajo): esas nunca
    # pasan por CCPay, así que lanzar _poll_payment para ellas es incorrecto
    # (consulta un orderId que no existe en CCPay, siempre da "pendiente" y
    # termina marcando la tx como 'expired' por timeout aunque el usuario sí
    # haya pagado y esté esperando revisión del admin).
    if (
        tx["status"] == "pending" and tx.get("order_id") and tx.get("pay_address")
        and not str(tx["order_id"]).startswith("cup-")
    ):
        await state.set_data(base_data)
        await state.set_state(PurchaseFlow.awaiting_payment)
        await _safe_send(
            bot, chat_id,
            "🔄 El bot se reinició. Seguimos esperando tu pago — "
            "si ya lo enviaste, se confirmará automáticamente en breve.",
        )
        asyncio.create_task(_poll_payment(
            bot=bot, chat_id=chat_id, state=state, tx_id=tx_id,
            order_id=tx["order_id"], elapsed_start=elapsed,
        ))
        return "payment_polling_resumed"

    # ── Caso 3.5: orden de pago manual en CUP ligada a esta compra (ver
    # _start_manual_purchase_payment: order_id = f"cup-{tx_id}", pay_address =
    # cuenta del método elegido, pay_amount = monto en CUP, reference_code
    # es determinístico a partir de tx_id así que no hace falta guardarlo
    # aparte). Se distingue si ya mandó el comprobante (columnas
    # proof_file_id/proof_text ya persistidas, ver database.set_purchase_proof)
    # para no pedírselo de nuevo.
    if tx["status"] == "pending" and str(tx.get("order_id") or "").startswith("cup-"):
        await state.set_data(base_data)
        await state.set_state(PurchaseFlow.awaiting_manual_review)

        if tx.get("proof_file_id") or tx.get("proof_text"):
            await _safe_send(
                bot, chat_id,
                "🔄 El bot se reinició, pero tu comprobante ya está enviado.\n"
                "Seguimos esperando que un administrador lo revise; en cuanto "
                "se confirme, recibirás tu número automáticamente.",
            )
            return "manual_review_resumed_awaiting_admin"

        reference_code = f"REF-{tx_id:06d}"
        method_name = await _find_manual_method_name(tx.get("pay_address")) or "CUP"
        await state.update_data(
            manual_amount_cup      = tx.get("pay_amount"),
            manual_reference_code  = reference_code,
        )
        await _safe_send(
            bot, chat_id,
            "🔄 El bot se reinició, pero ya tenías generada tu orden de pago en "
            "CUP. No hace falta que vuelvas a empezar:",
        )
        await _safe_send(
            bot, chat_id,
            MSG_MANUAL_PURCHASE_INSTRUCTIONS.format(
                method_name    = method_name,
                amount_cup     = f"{tx.get('pay_amount', 0):,.0f}".replace(",", " "),
                account        = tx.get("pay_address") or "",
                reference_code = reference_code,
            ),
            reply_markup=cancel_keyboard(),
        )
        return "manual_instructions_resent"

    # ── Caso 4: ya se habían elegido servicio y país (la fila en `transactions`
    # solo existe desde cb_select_country en adelante, ver database.create_transaction),
    # pero el reinicio ocurrió mientras se cotizaban/elegían las monedas de pago,
    # antes de generar una orden (order_id vacío). No hace falta que el usuario
    # vuelva a elegir servicio ni país: se recotiza para la MISMA tx y se le
    # muestra de nuevo el menú de monedas.
    if tx["status"] == "pending" and not tx.get("order_id"):
        await state.set_data(base_data)
        await _safe_send(
            bot, chat_id,
            f"🔄 El bot se reinició, pero ya tenías elegido "
            f"{tx['service_name']} / {tx['country_name']}. No se te cobró nada.\n"
            "Recotizando los métodos de pago disponibles...",
        )
        placeholder = await bot.send_message(chat_id, "⏳")
        shown = await _quote_and_show_currency_menu(
            placeholder, state, tx_id, user_id, tx["amount_usd"],
        )
        return "currency_selection_resumed" if shown else "currency_quote_failed"

    # ── Caso 5: cualquier otro estado no contemplado arriba (por ejemplo la
    # fila fue creada pero ni siquiera tiene país todavía, lo cual en la
    # práctica no debería pasar dado cómo se crea la fila) → no hay nada
    # seguro que recuperar automáticamente.
    await db.set_status(tx_id, "error")
    await _safe_send(
        bot, chat_id,
        "⚠️ El bot se reinició mientras tenías una compra en curso, "
        "en un punto que no pudimos reanudar automáticamente. No se te cobró nada.\n"
        "Puedes intentarlo de nuevo ahora mismo, sin ningún problema 👇",
        reply_markup=outbox.retry_keyboard(),
    )
    return "no_charge_notified"


async def resume_deposit(bot, storage, dep: dict) -> str:
    """
    Análogo a resume_transaction, pero para depósitos pendientes (orden ya
    generada, esperando pago). Se llama desde main.py una vez por cada
    depósito "colgado" tras un reinicio.
    """
    dep_id  = dep["id"]
    user_id = dep["user_id"]
    chat_id = user_id  # este bot solo opera en chats privados 1:1

    key   = StorageKey(bot_id=bot.id, chat_id=chat_id, user_id=user_id)
    state = FSMContext(storage=storage, key=key)

    await state.set_data({
        "deposit_id":     dep_id,
        "deposit_amount": dep["amount_usd"],
        "order_id":       dep["order_id"],
        "pay_address":    dep.get("pay_address"),
        "pay_amount":     dep.get("pay_amount"),
        "currency":       dep.get("currency"),
        "network":        dep.get("network"),
        "token_id":       dep.get("token_id"),
    })
    await state.set_state(DepositFlow.awaiting_payment)

    elapsed = _elapsed_seconds(dep.get("updated_at"))
    await _safe_send(
        bot, chat_id,
        "🔄 El bot se reinició. Seguimos esperando tu depósito — "
        "si ya lo enviaste, se acreditará automáticamente en breve.",
    )
    asyncio.create_task(_poll_deposit(
        bot=bot, chat_id=chat_id, state=state, deposit_id=dep_id,
        order_id=dep["order_id"], elapsed_start=elapsed,
    ))
    return "deposit_polling_resumed"


# ── Retiro de saldo interno a cripto ──────────────────────────────────────────
# Flujo: /saldo -> "Retirar a cripto" -> monto (USD) -> moneda/red -> dirección
# -> confirmar. Reusa ccpay.get_supported_currencies/get_estimated_amounts_batch
# (mismo patrón que cb_select_country para cotizar una compra) y
# ccpay.refund_user (ya genérico: dirección + monto + token_id) para ejecutar
# la transferencia. El saldo se descuenta ANTES de llamar a CCPayment y se
# re-acredita si la llamada falla, para no perder dinero por un error de red.

@router.callback_query(F.data == "start_withdraw")
async def cb_start_withdraw(call: CallbackQuery, state: FSMContext):
    await _safe_call_answer(call)
    # Solo el saldo de ORIGEN cripto es retirable a cripto (ver
    # database.Database.balances) — el origen CUP tiene su propio flujo
    # (cb_start_cup_withdraw).
    balance = (await db.get_balance_breakdown(call.from_user.id))["crypto"]
    balance_available = floor_to_cents(balance)
    if balance_available < WITHDRAWAL_MIN_USD:
        await call.message.answer(
            f"💰 El mínimo para retirar es {format_amount(WITHDRAWAL_MIN_USD, 'USD')}. "
            f"Tu saldo disponible es {format_amount(balance_available, 'USD')}."
        )
        return

    await state.set_data({"withdraw_balance": balance})
    await state.set_state(WithdrawFlow.awaiting_amount)
    # Se muestra floor_to_cents: el usuario suele escribir de vuelta
    # exactamente lo que ve acá, así que nunca debe ver un número mayor al
    # que realmente puede retirar (ver docstring de floor_to_cents).
    await call.message.answer(
        MSG_WITHDRAW_ASK_AMOUNT.format(balance=format_amount(floor_to_cents(balance), "USD")),
        parse_mode="HTML",
        reply_markup=cancel_keyboard(),
    )


@router.message(WithdrawFlow.awaiting_amount)
async def msg_withdraw_amount(message: Message, state: FSMContext):
    data = await state.get_data()
    balance = data.get("withdraw_balance", 0.0)
    text = (message.text or "").strip().lower()

    if text in ("todo", "all", "todos"):
        amount = balance
    else:
        try:
            amount = round(float(text.replace(",", ".")), 2)
        except ValueError:
            await message.answer(
                "⚠️ Escribe un número válido (ej: 5.50) o <b>todo</b> para "
                "retirar el saldo completo.",
                parse_mode="HTML",
                reply_markup=cancel_keyboard(),
            )
            return

    # IMPORTANTE: se compara contra floor_to_cents(balance), NO contra
    # round(balance, 2). El saldo real puede traer ruido de punto flotante
    # genuino (ej. 0.1 representado como 0.099999999999999996 — ese es el
    # caso que amerita un epsilon mínimo), pero round(balance, 2) además
    # puede redondear HACIA ARRIBA un saldo real como 0.0951 a "0.10" — no
    # es ruido, es medio centavo real que el usuario no tiene. Si se acepta
    # acá un monto que solo existe por ese redondeo hacia arriba, el usuario
    # pasa esta pantalla, cotiza, pone dirección... y recién en la
    # confirmación (db.debit_balance, que sí compara contra el saldo exacto)
    # se rechaza con un confuso "tu saldo cambió" — cuando en realidad nunca
    # cambió nada, simplemente nunca tuvo tanto. floor_to_cents nunca
    # sobreestima lo disponible, así que lo que se acepta acá siempre puede
    # confirmarse después.
    balance_available = floor_to_cents(balance)
    if amount <= 0 or amount > balance_available + 1e-9:
        await message.answer(
            f"⚠️ El monto debe ser mayor a $0 y no superar tu saldo disponible "
            f"({format_amount(balance_available, 'USD')}).",
            parse_mode="HTML",
            reply_markup=cancel_keyboard(),
        )
        return

    # Mínimo de retiro (ver config.WITHDRAWAL_MIN_USD): con precios de
    # números tan bajos, un retiro de centavos puede perder más en
    # comisión de red que lo que el usuario recibiría. Se compara contra
    # amount, no contra balance_available: si alguien pide "todo" y su
    # saldo total ya es menor al mínimo, este mismo chequeo lo cubre.
    if amount < WITHDRAWAL_MIN_USD:
        await message.answer(
            f"⚠️ El monto mínimo de retiro es {format_amount(WITHDRAWAL_MIN_USD, 'USD')}.\n"
            f"Tu saldo disponible es {format_amount(balance_available, 'USD')}. "
            "Puedes seguir acumulando saldo (se acredita automáticamente en "
            "reembolsos) y retirarlo cuando llegues al mínimo, o usarlo "
            "directamente en tu próxima compra.",
            parse_mode="HTML",
            reply_markup=cancel_keyboard(),
        )
        return

    await message.answer("🔍 Consultando cotización actual...")

    net_usd, fee_usd = apply_withdrawal_fee(amount, WITHDRAWAL_FEE_PCT)

    # Solo se ofrecen como DESTINO DE RETIRO las monedas que el negocio
    # mantiene fondeadas a propósito (ver config.WITHDRAWAL_ALLOWED_CURRENCIES
    # y su comentario: el balance del merchant en CCPayment NO es fungible
    # entre monedas distintas, solo entre redes de una misma moneda). Esto es
    # aparte de ccpay.get_supported_currencies(), que sigue devolviendo TODAS
    # las monedas habilitadas porque esa lista también se usa para RECIBIR
    # pagos (compras, depósitos), donde no aplica esta restricción.
    supported = await ccpay.get_supported_currencies()
    supported = [c for c in supported if c["currency"].upper() in WITHDRAWAL_ALLOWED_CURRENCIES]
    if not supported:
        logger.error(
            "WITHDRAWAL_ALLOWED_CURRENCIES (%s) no matchea ninguna moneda de "
            "get_supported_currencies(); revisar configuración.",
            WITHDRAWAL_ALLOWED_CURRENCIES,
        )
    token_ids = [cur["token_id"] for cur in supported]
    # Se cotiza sobre net_usd (lo que efectivamente se convierte y envía),
    # no sobre el monto bruto que se descuenta del saldo.
    estimates_by_token = await ccpay.get_estimated_amounts_batch(net_usd, token_ids)

    # Pista (no garantía, ver docstring de db.get_last_completed_deposit):
    # si la moneda/red con la que el usuario depositó sigue estando entre
    # las permitidas para retiro, se la destaca primero en la lista — es la
    # más probable de tener liquidez real, porque es la que él mismo trajo.
    last_deposit = await db.get_last_completed_deposit(message.from_user.id)
    last_deposit_token_id = last_deposit["token_id"] if last_deposit else None

    options = []
    for cur in supported:
        est = estimates_by_token.get(cur["token_id"])
        if est is None:
            continue
        options.append({
            "currency": cur["currency"],
            "network":  cur["network"],
            "label":    cur["label"],
            "amount":   est,
            "token_id": cur["token_id"],
            "low_fee":  cur.get("low_fee", False),
            "deposited_in": cur["token_id"] == last_deposit_token_id,
        })
    options.sort(key=lambda o: (not o["deposited_in"], not o["low_fee"]))

    if not options:
        await message.answer(
            "😔 No pudimos obtener cotizaciones en este momento. Intenta de "
            "nuevo en unos minutos con /saldo.",
        )
        await state.clear()
        return

    await state.update_data(
        withdraw_amount_usd = amount,
        withdraw_fee_usd    = fee_usd,
        withdraw_net_usd    = net_usd,
        withdraw_options    = options,
    )
    await state.set_state(WithdrawFlow.selecting_currency)
    await message.answer(
        MSG_WITHDRAW_SELECT_CURRENCY.format(
            amount_usd = format_amount(amount, "USD"),
            fee_pct    = f"{WITHDRAWAL_FEE_PCT:.0%}",
            fee_usd    = format_amount(fee_usd, "USD"),
            net_usd    = format_amount(net_usd, "USD"),
        ),
        parse_mode="HTML",
        reply_markup=withdraw_currencies_keyboard(options),
    )


@router.callback_query(WithdrawFlow.selecting_currency, F.data.startswith("wcur:"))
async def cb_withdraw_select_currency(call: CallbackQuery, state: FSMContext):
    await _safe_call_answer(call)
    idx = int(call.data.split(":", 1)[1])
    data = await state.get_data()
    options = data.get("withdraw_options", [])
    if idx < 0 or idx >= len(options):
        await call.message.answer(MSG_ERROR_GENERIC, parse_mode="HTML")
        await state.clear()
        return

    opt = options[idx]
    await state.update_data(
        withdraw_currency      = opt["currency"],
        withdraw_network       = opt["network"],
        withdraw_token_id      = opt["token_id"],
        withdraw_crypto_amount = opt["amount"],
    )
    await state.set_state(WithdrawFlow.awaiting_address)
    await _collapse_selection(call, f"✅ Retiro en: {opt['currency']} (red {opt['network']})")
    await call.message.answer(
        MSG_WITHDRAW_ASK_ADDRESS.format(currency=opt["currency"], network=opt["network"]),
        parse_mode="HTML",
        reply_markup=cancel_keyboard(),
    )


@router.message(WithdrawFlow.awaiting_address)
async def msg_withdraw_address(message: Message, state: FSMContext):
    address = (message.text or "").strip()
    # Validación mínima a propósito: cada red tiene su propio formato
    # (base58, hex con 0x, bech32...) y validar en serio requeriría una
    # librería por chain. Solo filtramos basura obvia; la advertencia de
    # MSG_WITHDRAW_ASK_ADDRESS ya deja claro el riesgo de una dirección mala.
    if not address or " " in address or len(address) < 8:
        await message.answer(
            "⚠️ Esa dirección no parece válida (muy corta o con espacios). "
            "Envía la dirección completa, sin espacios.",
            reply_markup=cancel_keyboard(),
        )
        return

    data = await state.get_data()
    await state.update_data(withdraw_address=address)
    await state.set_state(WithdrawFlow.confirming)

    await message.answer(
        MSG_WITHDRAW_CONFIRM.format(
            amount_usd    = format_amount(data["withdraw_amount_usd"], "USD"),
            fee_pct       = f"{WITHDRAWAL_FEE_PCT:.0%}",
            fee_usd       = format_amount(data["withdraw_fee_usd"], "USD"),
            net_usd       = format_amount(data["withdraw_net_usd"], "USD"),
            amount_crypto = format_amount(data["withdraw_crypto_amount"], data["withdraw_currency"]),
            network       = data["withdraw_network"],
            address       = address,
        ),
        parse_mode="HTML",
        reply_markup=withdraw_confirm_keyboard(),
    )


@router.callback_query(WithdrawFlow.confirming, F.data == "withdraw_confirm")
async def cb_withdraw_confirm(call: CallbackQuery, state: FSMContext):
    await _safe_call_answer(call)
    data = await state.get_data()
    user_id    = call.from_user.id
    amount_usd = data["withdraw_amount_usd"]
    currency   = data["withdraw_currency"]
    network    = data["withdraw_network"]
    address    = data["withdraw_address"]

    # Descontar saldo ANTES de llamar a CCPayment: si el proceso muere justo
    # después del POST, preferimos haber descontado ya (y re-acreditar si
    # falla) en vez de arriesgarnos a un doble retiro por reintento manual.
    ok = await db.debit_balance(
        user_id, amount_usd, reason=f"Retiro a {currency} ({network})", origin="crypto",
    )
    if not ok:
        await call.message.answer(
            "⚠️ Tu saldo cambió mientras confirmabas (quizás lo usaste en "
            "otra operación mientras tanto). Verifica tu saldo actual con "
            "/saldo e intenta de nuevo.",
        )
        await state.clear()
        return

    sent, err_code = await ccpay.refund_user(
        to_address = address,
        amount     = data["withdraw_crypto_amount"],
        token_id   = data["withdraw_token_id"],
        memo       = f"Retiro de saldo - user {user_id}",
    )

    if not sent:
        # Revertir siempre primero: pase lo que pase después, el usuario no
        # pierde saldo por un retiro que no se pudo procesar.
        await db.credit_balance(
            user_id, amount_usd, reason=f"Reversión retiro fallido a {currency}", origin="crypto",
        )

        # CCPAY_ERR_INSUFFICIENT_MERCHANT_BALANCE (14000): no es que falte
        # dinero real, es que ESA moneda/red puntual (de las varias
        # permitidas en WITHDRAWAL_ALLOWED_CURRENCIES) se quedó sin fondos
        # del lado del merchant (ver comentario en config.py sobre por qué
        # el saldo no es fungible entre monedas distintas). En vez de morir
        # con un error genérico, dejamos elegir OTRA de las monedas ya
        # cotizadas -> mejor experiencia y no hace falta repetir el monto.
        #
        # IMPORTANTE: nunca reintentamos automáticamente con la MISMA
        # dirección en otra red/moneda. Aunque algunas redes comparten
        # formato de dirección (ej. EVM: ETH/BSC/POLYGON), otras no
        # (TRC20, BTC, SOL...), y reenviar a ciegas a una dirección que
        # "parece" válida en otra red es exactamente el tipo de error que
        # pierde fondos sin posibilidad de recuperarlos. Se le vuelve a
        # pedir la dirección correspondiente a la nueva moneda/red elegida.
        if err_code == ccpay.CCPAY_ERR_INSUFFICIENT_MERCHANT_BALANCE:
            await _notify_admin(
                call.bot,
                f"💧 <b>Sin liquidez para retiro</b> (revertido a saldo, no se perdió nada)\n"
                f"👤 <code>{user_id}</code> · {format_amount(amount_usd, 'USD')} → "
                f"{currency} ({network})\n"
                "El merchant no tiene fondos de ESTA moneda/red puntual en CCPayment "
                "(las demás monedas permitidas pueden seguir teniendo saldo). "
                "Considera activar Auto-Swap a stablecoin en el dashboard de "
                "CCPayment para que esto no se repita (ver config.WITHDRAWAL_ALLOWED_CURRENCIES).",
            )

            remaining = [
                opt for opt in data.get("withdraw_options", [])
                if not (opt["currency"] == currency and opt["network"] == network)
            ]
            if remaining:
                await state.update_data(withdraw_options=remaining)
                await state.set_state(WithdrawFlow.selecting_currency)
                await call.message.answer(
                    f"😔 No pudimos procesar el retiro en {currency} ({network}) "
                    "por falta de liquidez momentánea de esa red puntual — tu "
                    f"saldo NO se tocó ({format_amount(amount_usd, 'USD')} siguen "
                    "disponibles). Elige otra moneda/red para el mismo monto:",
                    parse_mode="HTML",
                    reply_markup=withdraw_currencies_keyboard(remaining),
                )
                return

            await call.message.answer(
                "😔 No pudimos procesar el retiro en ninguna de las monedas "
                "disponibles por falta de liquidez momentánea. Tu saldo NO se "
                "tocó. Intenta de nuevo en unos minutos con /saldo, o contacta "
                "al soporte si se repite.",
            )
            await state.clear()
            return

        # Cualquier otro error (dirección rechazada, timeout, etc.): no es
        # seguro asumir que otra moneda lo resolvería, así que se corta acá.
        await call.message.answer(MSG_WITHDRAW_FAILED, parse_mode="HTML")
        await _notify_admin(
            call.bot,
            f"🚨 <b>Retiro fallido</b> (revertido a saldo)\n"
            f"👤 <code>{user_id}</code> · {format_amount(amount_usd, 'USD')} → "
            f"{currency} ({network})\nDirección: <code>{address}</code>",
        )
        await state.clear()
        return

    new_balance = await db.get_balance(user_id)
    await call.message.answer(
        MSG_WITHDRAW_SUCCESS.format(
            amount_usd  = format_amount(amount_usd, "USD"),
            new_balance = format_amount(new_balance, "USD"),
        ),
        parse_mode="HTML",
    )
    await _notify_admin(
        call.bot,
        f"💸 <b>Retiro procesado</b>\n👤 <code>{user_id}</code> · "
        f"{format_amount(amount_usd, 'USD')} → {currency} ({network})\n"
        f"Dirección: <code>{address}</code>",
    )
    await state.clear()


# ── Retiro de saldo CUP (a CUP real, vía Transfermóvil/EnZona) ────────────────
# Flujo: /saldo -> "Retirar en CUP" -> método (por dónde recibe) -> monto
# (USD, descontado de la bolsa de ORIGEN CUP) -> cuenta/tarjeta de destino ->
# confirmar -> se descuenta el saldo YA (de buena fe, antes de que el admin
# transfiera) y queda en cola de revisión -> admin transfiere a mano y
# aprueba, o rechaza y se devuelve el saldo. Es el inverso de
# ManualDepositFlow: allá el admin CONFIRMA que llegó un pago; acá el admin
# EJECUTA un pago. Sin verificación automática por el mismo motivo que los
# depósitos manuales (ningún método CUP da API a terceros, ver config.py).

@router.callback_query(F.data == "start_cup_withdraw")
async def cb_start_cup_withdraw(call: CallbackQuery, state: FSMContext):
    await _safe_call_answer(call)

    existing = await db.get_pending_manual_withdrawal(call.from_user.id)
    if existing:
        await call.message.answer(
            MSG_CUP_WITHDRAW_ALREADY_PENDING.format(reference_code=existing["reference_code"]),
            parse_mode="HTML",
        )
        return

    if not await db.get_payment_methods():
        await call.message.answer(MSG_ERROR_GENERIC, parse_mode="HTML")
        return

    # Solo el saldo de ORIGEN CUP es retirable en CUP (ver database.py
    # Database.balances) — el origen cripto tiene su propio flujo
    # (cb_start_withdraw).
    balance = (await db.get_balance_breakdown(call.from_user.id))["cup"]
    balance_available = floor_to_cents(balance)

    # Tasa de PAYOUT fijada acá y reutilizada durante TODO el flujo (select
    # method -> monto -> confirmar), en vez de recalcularla en cada paso:
    # así el CUP que se le muestra al usuario en la pantalla de monto y en
    # la confirmación final es siempre el mismo número, sin importar cuánto
    # tarde en completar el flujo.
    payout_rate = effective_cup_rate_payout(MANUAL_DEPOSIT_CUP_RATE, MANUAL_DEPOSIT_CUP_MARGIN_PCT)
    balance_cup = usd_to_cup(balance_available, payout_rate)

    if balance_available <= 0 or balance_available < CUP_WITHDRAWAL_MIN_USD:
        min_cup = usd_to_cup(CUP_WITHDRAWAL_MIN_USD, payout_rate)
        await call.message.answer(
            f"💰 El mínimo para retirar en CUP es {format_cup(min_cup)}. "
            f"Tu saldo de origen CUP es {format_cup(balance_cup)}.",
            parse_mode="HTML",
        )
        return

    await state.update_data(
        cup_withdraw_balance=balance_available,
        cup_withdraw_balance_cup=balance_cup,
        cup_withdraw_rate=payout_rate,
    )
    await state.set_state(CupWithdrawFlow.selecting_method)
    await call.message.answer(
        MSG_CUP_WITHDRAW_SELECT_METHOD.format(balance_cup=format_cup(balance_cup)),
        parse_mode="HTML",
        reply_markup=cup_withdraw_methods_keyboard(await db.get_payment_methods()),
    )


@router.callback_query(CupWithdrawFlow.selecting_method, F.data.startswith("wmethod:"))
async def cb_select_cup_withdraw_method(call: CallbackQuery, state: FSMContext):
    await _safe_call_answer(call)
    method_code = call.data.split(":", 1)[1]
    method = (await db.get_payment_methods()).get(method_code)
    if not method:
        await call.message.answer(MSG_ERROR_GENERIC, parse_mode="HTML")
        await state.clear()
        return

    data = await state.get_data()
    balance_cup = data["cup_withdraw_balance_cup"]

    await state.update_data(cup_withdraw_method=method_code, cup_withdraw_method_name=method["name"])
    await state.set_state(CupWithdrawFlow.awaiting_amount)
    await call.message.answer(
        MSG_CUP_WITHDRAW_ASK_AMOUNT.format(
            method_name=method["name"],
            balance_cup=format_cup(balance_cup),
        ),
        parse_mode="HTML",
        reply_markup=cancel_keyboard(),
    )


@router.message(CupWithdrawFlow.awaiting_amount)
async def msg_cup_withdraw_amount(message: Message, state: FSMContext):
    data = await state.get_data()
    balance_available = data["cup_withdraw_balance"]        # USD, unidad interna del saldo
    balance_cup        = data["cup_withdraw_balance_cup"]    # mismo saldo, ya convertido a CUP
    payout_rate         = data["cup_withdraw_rate"]           # fijada en cb_start_cup_withdraw
    text = (message.text or "").strip().lower()

    if text in ("todo", "all", "todos"):
        amount = balance_available
    else:
        try:
            # El usuario escribe el monto en CUP (ver MSG_CUP_WITHDRAW_ASK_AMOUNT);
            # se convierte a la unidad interna (USD) con la MISMA tasa que se
            # le mostró en pantalla, para que el CUP que confirme más abajo
            # coincida con el que escribió acá.
            amount_cup_requested = round(float(text.replace(",", ".")))
            amount = round(amount_cup_requested / payout_rate, 2)
        except (ValueError, ZeroDivisionError):
            await message.answer(
                "⚠️ Escribe un número válido en CUP (ej: 5000) o <b>todo</b> "
                "para retirar el saldo CUP completo.",
                parse_mode="HTML",
                reply_markup=cancel_keyboard(),
            )
            return

        # Chequeo directo en espacio CUP (además del chequeo en USD más abajo):
        # amount_available_usd se redondea a 2 decimales, lo que en una tasa
        # de ~900-1000 CUP/USD equivale a un margen de casi 10 CUP donde
        # montos distintos en CUP colapsan al mismo valor interno. Sin este
        # chequeo, pedir más CUP del saldo disponible podía "colarse" y
        # terminaba silenciosamente recortado al monto real en la pantalla
        # de confirmación, sin avisar al usuario.
        if amount_cup_requested > balance_cup:
            await message.answer(
                f"⚠️ El monto debe ser mayor a 0 CUP y no superar tu saldo CUP "
                f"disponible ({format_cup(balance_cup)}).",
                parse_mode="HTML",
                reply_markup=cancel_keyboard(),
            )
            return

    # Mismo criterio que msg_withdraw_amount (retiro cripto) para el redondeo
    # del saldo: comparar contra floor_to_cents, nunca contra el saldo
    # redondeado hacia arriba. El mínimo en sí es CUP_WITHDRAWAL_MIN_USD,
    # no WITHDRAWAL_MIN_USD (ver config.py: no aplica la razón de la
    # comisión de red, acá lo transfiere un admin).
    if amount <= 0 or amount > balance_available + 1e-9:
        await message.answer(
            f"⚠️ El monto debe ser mayor a 0 CUP y no superar tu saldo CUP "
            f"disponible ({format_cup(balance_cup)}).",
            parse_mode="HTML",
            reply_markup=cancel_keyboard(),
        )
        return

    if amount < CUP_WITHDRAWAL_MIN_USD:
        min_cup = usd_to_cup(CUP_WITHDRAWAL_MIN_USD, payout_rate)
        await message.answer(
            f"⚠️ El monto mínimo de retiro es {format_cup(min_cup)}.",
            parse_mode="HTML",
            reply_markup=cancel_keyboard(),
        )
        return

    net_usd, fee_usd = apply_withdrawal_fee(amount, WITHDRAWAL_FEE_PCT)
    amount_cup = usd_to_cup(net_usd, payout_rate)   # neto que el usuario recibe en CUP

    # Bruto (lo que se descuenta del saldo CUP) también en CUP, para mostrar
    # todo en la misma moneda. fee_cup se obtiene por RESTA (no reconvirtiendo
    # fee_usd por separado) para garantizar gross_cup == fee_cup + amount_cup
    # incluso con el redondeo a entero de CUP (mismo principio que
    # apply_withdrawal_fee con USD, ver utils.py).
    gross_cup = usd_to_cup(amount, payout_rate)
    fee_cup = gross_cup - amount_cup

    await state.update_data(
        cup_withdraw_amount_usd = amount,
        cup_withdraw_fee_usd    = fee_usd,
        cup_withdraw_net_usd    = net_usd,
        cup_withdraw_amount_cup = amount_cup,
        cup_withdraw_gross_cup  = gross_cup,
        cup_withdraw_fee_cup    = fee_cup,
        cup_withdraw_rate       = payout_rate,
    )
    await state.set_state(CupWithdrawFlow.awaiting_account)
    await message.answer(
        MSG_CUP_WITHDRAW_ASK_ACCOUNT.format(method_name=data["cup_withdraw_method_name"]),
        parse_mode="HTML",
        reply_markup=cancel_keyboard(),
    )


@router.message(CupWithdrawFlow.awaiting_account)
async def msg_cup_withdraw_account(message: Message, state: FSMContext):
    destination = (message.text or "").strip()
    if not destination:
        await message.answer(
            "⚠️ Escribe la cuenta/tarjeta donde quieres recibir el CUP.",
            parse_mode="HTML",
            reply_markup=cancel_keyboard(),
        )
        return

    data = await state.get_data()
    await state.update_data(cup_withdraw_destination=destination)
    await state.set_state(CupWithdrawFlow.confirming)
    await message.answer(
        MSG_CUP_WITHDRAW_CONFIRM.format(
            amount_cup    = format_cup(data["cup_withdraw_gross_cup"]),
            fee_pct       = f"{WITHDRAWAL_FEE_PCT:.0%}",
            fee_cup       = format_cup(data["cup_withdraw_fee_cup"]),
            net_cup       = format_cup(data["cup_withdraw_amount_cup"]),
            method_name   = data["cup_withdraw_method_name"],
            destination   = destination,
        ),
        parse_mode="HTML",
        reply_markup=cup_withdraw_confirm_keyboard(),
    )


@router.callback_query(CupWithdrawFlow.confirming, F.data == "cup_withdraw_confirm")
async def cb_cup_withdraw_confirm(call: CallbackQuery, state: FSMContext):
    await _safe_call_answer(call)
    data = await state.get_data()
    user_id     = call.from_user.id
    amount_usd  = data["cup_withdraw_amount_usd"]
    method_code = data["cup_withdraw_method"]
    method_name = data["cup_withdraw_method_name"]
    destination = data["cup_withdraw_destination"]

    # Descontar de la bolsa CUP ANTES de encolar la solicitud (mismo motivo
    # que el retiro cripto: preferimos haber descontado ya y revertir si el
    # admin rechaza, que arriesgar un doble retiro). origin='cup' estricto:
    # esto NUNCA debe tocar la bolsa cripto del usuario.
    ok = await db.debit_balance(
        user_id, amount_usd, reason=f"Retiro CUP a {method_name}", origin="cup",
    )
    if not ok:
        await call.message.answer(
            "⚠️ Tu saldo CUP cambió mientras confirmabas (quizás lo usaste en "
            "otra operación mientras tanto). Verifica tu saldo actual con "
            "/saldo e intenta de nuevo.",
        )
        await state.clear()
        return

    wd = await db.create_manual_withdrawal(
        user_id, method_code, destination,
        amount_usd=amount_usd, fee_usd=data["cup_withdraw_fee_usd"],
        net_usd=data["cup_withdraw_net_usd"], amount_cup=data["cup_withdraw_amount_cup"],
        cup_rate=data["cup_withdraw_rate"],
    )

    await call.message.answer(
        MSG_CUP_WITHDRAW_SUBMITTED.format(
            reference_code = wd["reference_code"],
            amount_usd     = format_cup(data["cup_withdraw_gross_cup"]),
            amount_cup     = f"{data['cup_withdraw_amount_cup']:,}".replace(",", " "),
        ),
        parse_mode="HTML",
    )

    cup_amount_str = f"{data['cup_withdraw_amount_cup']:,}".replace(",", " ")

    try:
        await call.bot.send_message(
            ADMIN_CHAT_ID,
            f"🇨🇺 <b>Nuevo retiro CUP a procesar</b>\n"
            f"Código: <code>{wd['reference_code']}</code>\n"
            f"Método: {method_name}\n"
            f"Cuenta destino: <code>{destination}</code>\n"
            f"Monto: {format_amount(amount_usd, 'USD')} → "
            f"{cup_amount_str} CUP\n"
            f"👤 <code>{user_id}</code>",
            parse_mode="HTML",
            reply_markup=manual_withdrawal_review_keyboard(wd["id"]),
        )
    except Exception as exc:
        logger.error("No se pudo notificar al admin el retiro CUP %s: %s", wd["id"], exc)

    await state.clear()


@router.callback_query(F.data.startswith("mwd_ok:"))
async def cb_admin_approve_cup_withdrawal(call: CallbackQuery):
    if not _is_admin(call.from_user.id):
        await _safe_call_answer(call, "No autorizado.", show_alert=True)
        return

    wd_id = int(call.data.split(":", 1)[1])
    wd = await db.get_manual_withdrawal_by_id(wd_id)
    if not wd:
        await _safe_call_answer(call, "No encontrado.", show_alert=True)
        return
    if wd["status"] != "pending_review":
        await _safe_call_answer(call, f"Ya estaba en estado '{wd['status']}'.", show_alert=True)
        return

    await db.set_manual_withdrawal_status(wd_id, "approved", reviewed_by=call.from_user.id)

    await _safe_call_answer(call, "Aprobado ✅")
    await _mark_admin_message_resolved(call.message, f"\n\n✅ Aprobado por {call.from_user.id}")

    await _safe_send(
        call.bot, wd["user_id"],
        MSG_CUP_WITHDRAW_APPROVED.format(
            reference_code = wd["reference_code"],
            amount_cup     = f"{wd['amount_cup']:,}".replace(",", " "),
        ),
    )


@router.callback_query(F.data.startswith("mwd_no:"))
async def cb_admin_reject_cup_withdrawal(call: CallbackQuery):
    if not _is_admin(call.from_user.id):
        await _safe_call_answer(call, "No autorizado.", show_alert=True)
        return

    wd_id = int(call.data.split(":", 1)[1])
    wd = await db.get_manual_withdrawal_by_id(wd_id)
    if not wd:
        await _safe_call_answer(call, "No encontrado.", show_alert=True)
        return
    if wd["status"] != "pending_review":
        await _safe_call_answer(call, f"Ya estaba en estado '{wd['status']}'.", show_alert=True)
        return

    # Se había descontado el saldo CUP al confirmar (buena fe) -> revertir
    # siempre primero, igual que el retiro cripto fallido: el usuario no
    # pierde saldo por un retiro que el admin no pudo/quiso ejecutar.
    await db.credit_balance(
        wd["user_id"], wd["amount_usd"], reason=f"Reversión retiro CUP rechazado wd={wd_id}",
        origin="cup",
    )
    await db.set_manual_withdrawal_status(wd_id, "rejected", reviewed_by=call.from_user.id)

    await _safe_call_answer(call, "Rechazado ❌")
    await _mark_admin_message_resolved(call.message, f"\n\n❌ Rechazado por {call.from_user.id}")

    await _safe_send(
        call.bot, wd["user_id"],
        MSG_CUP_WITHDRAW_REJECTED.format(
            reference_code = wd["reference_code"],
            amount_usd     = format_amount(wd["amount_usd"], "USD"),
        ),
    )


# Flujo: /saldo -> "Agregar saldo" -> monto (USD) -> moneda/red -> pagar.
# Es el inverso del retiro: acá CCPayment RECIBE el pago (crea una orden de
# cobro con ccpay.create_order, igual que una compra), así que no depende
# del balance disponible del merchant como sí puede pasarle a un retiro. Se
# acredita el 100% del monto pagado (ver config.DEPOSIT_MIN_USD), sin
# comisión de entrada.

@router.callback_query(F.data == "start_deposit")
async def cb_start_deposit(call: CallbackQuery, state: FSMContext):
    await _safe_call_answer(call)
    await state.set_state(DepositFlow.awaiting_amount)
    await call.message.answer(
        MSG_DEPOSIT_ASK_AMOUNT.format(min_usd=format_amount(DEPOSIT_MIN_USD, "USD")),
        parse_mode="HTML",
        reply_markup=cancel_keyboard(),
    )


@router.message(DepositFlow.awaiting_amount)
async def msg_deposit_amount(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    try:
        amount = round(float(text.replace(",", ".")), 2)
    except ValueError:
        await message.answer(
            "⚠️ Escribe un número válido (ej: 10 o 10.50).",
            parse_mode="HTML",
            reply_markup=cancel_keyboard(),
        )
        return

    if amount < DEPOSIT_MIN_USD:
        await message.answer(
            f"⚠️ El monto mínimo para depositar es "
            f"{format_amount(DEPOSIT_MIN_USD, 'USD')}.",
            parse_mode="HTML",
            reply_markup=cancel_keyboard(),
        )
        return

    deposit_id = await db.create_deposit(message.from_user.id, amount)

    await message.answer("🔍 Consultando monedas disponibles y cotización actual...")

    # Mismo patrón que cb_select_country: cotizar TODAS las monedas
    # soportadas en una sola llamada batch.
    supported = await ccpay.get_supported_currencies()
    token_ids = [cur["token_id"] for cur in supported]
    estimates_by_token = await ccpay.get_estimated_amounts_batch(amount, token_ids)

    options = []
    for cur in supported:
        est = estimates_by_token.get(cur["token_id"])
        if est is None:
            continue
        options.append({
            "currency": cur["currency"],
            "network":  cur["network"],
            "label":    cur["label"],
            "amount":   est,
            "token_id": cur["token_id"],
            "low_fee":  cur.get("low_fee", False),
        })
    options.sort(key=lambda o: not o["low_fee"])

    if not options:
        await db.set_deposit_status(deposit_id, "error")
        await message.answer(
            "😔 No pudimos obtener cotizaciones de pago en este momento. "
            "Intenta de nuevo en unos minutos con /saldo.",
        )
        await state.clear()
        return

    await state.update_data(
        deposit_id       = deposit_id,
        deposit_amount   = amount,
        deposit_options  = options,
    )
    await state.set_state(DepositFlow.selecting_currency)
    await message.answer(
        MSG_DEPOSIT_SELECT_CURRENCY.format(amount_usd=format_amount(amount, "USD")),
        parse_mode="HTML",
        reply_markup=deposit_currencies_keyboard(options),
    )


@router.callback_query(DepositFlow.selecting_currency, F.data.startswith("dcur:"))
async def cb_select_deposit_currency(call: CallbackQuery, state: FSMContext):
    await _safe_call_answer(call)
    idx = int(call.data.split(":", 1)[1])

    data = await state.get_data()
    options = data.get("deposit_options", [])
    if idx < 0 or idx >= len(options):
        await call.message.answer(MSG_ERROR_GENERIC, parse_mode="HTML")
        await state.clear()
        return

    chosen        = options[idx]
    currency      = chosen["currency"]
    network       = chosen["network"]
    currency_label = chosen["label"]
    token_id      = chosen["token_id"]

    deposit_id     = data["deposit_id"]
    amount_usd     = data["deposit_amount"]

    order = await ccpay.create_order(
        chosen["amount"], token_id, memo=f"Deposito-{deposit_id}",
    )
    if not order or not order.get("orderId") or not order.get("payAddress"):
        await db.set_deposit_status(deposit_id, "error")
        await call.message.answer(MSG_ERROR_GENERIC, parse_mode="HTML")
        await state.clear()
        return

    order_id    = order["orderId"]
    pay_address = order["payAddress"]
    pay_amount  = order["payAmount"] or chosen["amount"]

    await db.set_deposit_order_info(
        deposit_id, order_id, pay_address, currency, network, pay_amount, token_id,
    )

    await _collapse_selection(call, f"✅ Método de pago elegido: {currency_label} (red {network})")

    await state.update_data(
        order_id       = order_id,
        pay_address    = pay_address,
        pay_amount     = pay_amount,
        currency       = currency,
        network        = network,
        currency_label = currency_label,
        token_id       = token_id,
    )
    await state.set_state(DepositFlow.awaiting_payment)

    instructions_text = MSG_DEPOSIT_INSTRUCTIONS.format(
        currency_label = currency_label,
        amount         = format_amount(pay_amount, currency),
        amount_usd     = format_amount(amount_usd, "USD"),
        address        = pay_address,
        network        = network,
    )

    try:
        qr_bytes = generate_payment_qr(address=pay_address, amount=pay_amount, network=network)
        await call.message.answer_photo(
            photo=BufferedInputFile(qr_bytes, filename="deposit_qr.png"),
            caption=instructions_text,
            parse_mode="HTML",
            reply_markup=cancel_keyboard(),
        )
    except Exception as exc:
        logger.error("No se pudo generar/enviar el QR de depósito (id=%s): %s", deposit_id, exc)
        await call.message.answer(
            instructions_text, parse_mode="HTML", reply_markup=cancel_keyboard(),
        )

    asyncio.create_task(
        _poll_deposit(
            bot      = call.bot,
            chat_id  = call.message.chat.id,
            state    = state,
            deposit_id = deposit_id,
            order_id = order_id,
        )
    )


async def _poll_deposit(
    bot, chat_id: int, state: FSMContext, deposit_id: int, order_id: str,
    elapsed_start: int = 0,
):
    """
    Tarea asyncio: verifica el pago del depósito cada PAYMENT_POLL_INTERVAL
    segundos durante un máximo de PAYMENT_TIMEOUT_SECONDS. Análogo a
    _poll_payment pero termina en credit_balance en vez de pedir un número
    a HeroSMS.
    """
    elapsed = elapsed_start
    while elapsed < PAYMENT_TIMEOUT_SECONDS:
        await asyncio.sleep(PAYMENT_POLL_INTERVAL)
        elapsed += PAYMENT_POLL_INTERVAL

        dep = await db.get_deposit_by_id(deposit_id)
        if dep and dep["status"] == "cancelled":
            await _handle_cancelled_deposit(bot, chat_id, deposit_id, order_id)
            return

        status = await ccpay.get_order_status(order_id)

        if status == ccpay.ORDER_STATUS_COMPLETED:
            await _credit_deposit(bot, chat_id, deposit_id)
            await state.clear()
            return

        if status in (ccpay.ORDER_STATUS_EXPIRED, ccpay.ORDER_STATUS_CANCELLED):
            await db.set_deposit_status(deposit_id, "expired")
            await _safe_send(bot, chat_id, MSG_DEPOSIT_TIMEOUT)
            await state.clear()
            return

        logger.debug("Depósito pendiente (id=%s, elapsed=%ds)", deposit_id, elapsed)

    await db.set_deposit_status(deposit_id, "expired")
    await _safe_send(bot, chat_id, MSG_DEPOSIT_TIMEOUT)
    await state.clear()


async def _credit_deposit(bot, chat_id: int, deposit_id: int):
    """Acredita el saldo de un depósito confirmado y avisa al usuario/admin."""
    dep = await db.get_deposit_by_id(deposit_id)
    if not dep or dep["status"] == "completed":
        return  # ya procesado (evita doble crédito si se llama dos veces)

    new_balance = await db.credit_balance(
        dep["user_id"], dep["amount_usd"], reason=f"Depósito confirmado id={deposit_id}",
    )
    await db.set_deposit_status(deposit_id, "completed")

    await _safe_send(
        bot, chat_id,
        MSG_DEPOSIT_CONFIRMED.format(
            amount_usd  = format_amount(dep["amount_usd"], "USD"),
            new_balance = format_amount(new_balance, "USD"),
        ),
    )
    await _notify_admin(
        bot,
        f"➕ <b>Depósito confirmado</b>\n👤 <code>{dep['user_id']}</code> · "
        f"{format_amount(dep['amount_usd'], 'USD')} (pagado en {dep.get('currency') or '—'})",
    )


async def _handle_cancelled_deposit(bot, chat_id: int, deposit_id: int, order_id: str):
    """
    Igual que _handle_cancelled_order pero para depósitos: si el usuario
    canceló mientras esperábamos su pago, chequeamos una vez más si ya
    había llegado. Si llegó, se acredita igual (canceló la espera, no el
    dinero que ya envió); si no, simplemente dejamos de esperar.
    """
    status = await ccpay.get_order_status(order_id)
    if status != ccpay.ORDER_STATUS_COMPLETED:
        logger.info("Depósito %s (orden %s) cancelado, sin pago recibido.", deposit_id, order_id)
        return
    await _credit_deposit(bot, chat_id, deposit_id)


# ── Depósito manual (CUP vía Transfermóvil / EnZona) ──────────────────────────
# Flujo: /saldo -> "Agregar saldo (CUP)" -> método -> monto (USD) ->
# instrucciones + código de referencia -> usuario manda comprobante ->
# cola de revisión del admin -> aprobar/rechazar. Sin verificación
# automática: ningún método CUP disponible da webhook/API a terceros (ver
# config.py). Se arranca con un tope bajo por operación (MANUAL_DEPOSIT_MAX_USD)
# y máximo 1 solicitud pendiente por usuario a la vez.

@router.callback_query(F.data == "start_manual_deposit")
async def cb_start_manual_deposit(call: CallbackQuery, state: FSMContext):
    await _safe_call_answer(call)

    existing = await db.get_pending_manual_deposit(call.from_user.id)
    if existing:
        await call.message.answer(
            MSG_MANUAL_DEPOSIT_ALREADY_PENDING.format(
                reference_code=existing["reference_code"]
            ),
            parse_mode="HTML",
        )
        return

    if not await db.get_payment_methods():
        await call.message.answer(MSG_ERROR_GENERIC, parse_mode="HTML")
        return

    await state.set_state(ManualDepositFlow.selecting_method)
    await call.message.answer(
        MSG_MANUAL_DEPOSIT_SELECT_METHOD,
        parse_mode="HTML",
        reply_markup=manual_payment_methods_keyboard(await db.get_payment_methods()),
    )


@router.callback_query(ManualDepositFlow.selecting_method, F.data.startswith("mmethod:"))
async def cb_select_manual_method(call: CallbackQuery, state: FSMContext):
    await _safe_call_answer(call)
    method_code = call.data.split(":", 1)[1]
    method = (await db.get_payment_methods()).get(method_code)
    if not method:
        await call.message.answer(MSG_ERROR_GENERIC, parse_mode="HTML")
        await state.clear()
        return

    await state.update_data(manual_method=method_code, manual_method_name=method["name"])
    await state.set_state(ManualDepositFlow.awaiting_amount)
    await call.message.answer(
        MSG_MANUAL_DEPOSIT_ASK_AMOUNT.format(
            method_name=method["name"],
            min_usd=format_amount(MANUAL_DEPOSIT_MIN_USD, "USD"),
            max_usd=format_amount(MANUAL_DEPOSIT_MAX_USD, "USD"),
        ),
        parse_mode="HTML",
        reply_markup=cancel_keyboard(),
    )


@router.message(ManualDepositFlow.awaiting_amount)
async def msg_manual_deposit_amount(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    try:
        amount = round(float(text.replace(",", ".")), 2)
    except ValueError:
        await message.answer(
            "⚠️ Escribe un número válido (ej: 5 o 5.50).",
            parse_mode="HTML",
            reply_markup=cancel_keyboard(),
        )
        return

    if amount < MANUAL_DEPOSIT_MIN_USD or amount > MANUAL_DEPOSIT_MAX_USD:
        await message.answer(
            f"⚠️ El monto debe estar entre "
            f"{format_amount(MANUAL_DEPOSIT_MIN_USD, 'USD')} y "
            f"{format_amount(MANUAL_DEPOSIT_MAX_USD, 'USD')}.",
            parse_mode="HTML",
            reply_markup=cancel_keyboard(),
        )
        return

    data = await state.get_data()
    method_code = data["manual_method"]
    method_name = data["manual_method_name"]
    method = await db.get_payment_method(method_code)
    if not method or not method["active"]:
        # Puede pasar ahora que las cuentas son editables en caliente por el
        # admin (ver database.get_payment_method): el usuario eligió el
        # método hace un rato y mientras tanto el admin lo desactivó o
        # cambió de código. Mejor pedirle que arranque de nuevo que
        # mostrarle una cuenta vieja o crashear con un KeyError.
        await message.answer(
            "⚠️ Ese método de pago ya no está disponible. Usa /start para elegir otro.",
            parse_mode="HTML",
        )
        await state.clear()
        return

    effective_rate = effective_cup_rate(MANUAL_DEPOSIT_CUP_RATE, MANUAL_DEPOSIT_CUP_MARGIN_PCT)
    amount_cup = usd_to_cup(amount, effective_rate)
    dep = await db.create_manual_deposit(
        message.from_user.id, method_code, amount,
        amount_cup=amount_cup, cup_rate=effective_rate,
    )

    # Mismo aviso inmediato que en una compra pagada en CUP (ver
    # _start_manual_purchase_payment): el admin se entera de que hay un
    # depósito en camino antes de que llegue el comprobante.
    await _notify_admin(
        message.bot,
        f"🔔 <b>Pago CUP iniciado</b> (depósito de saldo)\n"
        f"{_user_label(message.from_user.id, message.from_user.username)} · "
        f"{format_amount(amount, 'USD')} ({f'{amount_cup:,}'.replace(',', ' ')} CUP)\n"
        f"Método: {method_name} · Código: <code>{dep['reference_code']}</code>\n"
        "Aún sin comprobante — avisamos apenas llegue.",
    )

    await state.update_data(
        manual_deposit_id     = dep["id"],
        manual_reference_code = dep["reference_code"],
        manual_amount         = amount,
    )
    await state.set_state(ManualDepositFlow.awaiting_proof)
    await message.answer(
        MSG_MANUAL_DEPOSIT_INSTRUCTIONS.format(
            method_name     = method_name,
            amount_usd      = format_amount(amount, "USD"),
            amount_cup      = f"{amount_cup:,}".replace(",", " "),
            account         = method["account"],
            reference_code  = dep["reference_code"],
        ),
        parse_mode="HTML",
        reply_markup=cancel_keyboard(),
    )


@router.message(ManualDepositFlow.awaiting_proof)
async def msg_manual_deposit_proof(message: Message, state: FSMContext):
    """
    Acepta foto (comprobante) o texto (ID/número de transacción) como
    prueba de pago. Cualquiera de los dos alcanza: no todos los usuarios
    pueden mandar captura de pantalla fácilmente.
    """
    data = await state.get_data()
    dep_id = data.get("manual_deposit_id")
    reference_code = data.get("manual_reference_code")
    if not dep_id:
        await state.clear()
        await message.answer(MSG_ERROR_GENERIC, parse_mode="HTML")
        return

    if not message.photo:
        await message.answer(
            "⚠️ Necesito una <b>captura de pantalla</b> del comprobante de la "
            "transferencia (no alcanza con escribir un ID/número).",
            parse_mode="HTML",
            reply_markup=cancel_keyboard(),
        )
        return

    proof_file_id = message.photo[-1].file_id
    proof_file_unique_id = message.photo[-1].file_unique_id
    proof_text = None

    await db.set_manual_deposit_proof(
        dep_id, proof_file_id=proof_file_id,
        proof_file_unique_id=proof_file_unique_id, proof_text=proof_text,
    )
    reused = await db.find_reused_proof(proof_file_unique_id, exclude_dep_id=dep_id)

    await message.answer(
        MSG_MANUAL_DEPOSIT_PROOF_RECEIVED.format(reference_code=reference_code),
        parse_mode="HTML",
    )

    dep = await db.get_manual_deposit_by_id(dep_id)
    cup_str = f"{dep['amount_cup']:,}".replace(",", " ") if dep.get("amount_cup") else "?"
    dep_method = await db.get_payment_method(dep["method"])
    caption = (
        _reused_proof_warning(reused) +
        f"🇨🇺 <b>Nuevo depósito CUP a revisar</b>\n"
        f"Código: <code>{reference_code}</code>\n"
        f"Método: {(dep_method or {}).get('name', dep['method'])}\n"
        f"Monto: {format_amount(dep['amount_usd'], 'USD')} ≈ {cup_str} CUP "
        f"(tasa {dep.get('cup_rate') or '?'})\n"
        f"👤 <code>{dep['user_id']}</code>"
    )
    if proof_text:
        caption += f"\n📝 Comprobante (texto): <code>{proof_text}</code>"

    try:
        if proof_file_id:
            await message.bot.send_photo(
                ADMIN_CHAT_ID, proof_file_id, caption=caption, parse_mode="HTML",
                reply_markup=manual_deposit_review_keyboard(dep_id),
            )
        else:
            await message.bot.send_message(
                ADMIN_CHAT_ID, caption, parse_mode="HTML",
                reply_markup=manual_deposit_review_keyboard(dep_id),
            )
    except Exception as exc:
        logger.error("No se pudo notificar al admin el depósito manual %s: %s", dep_id, exc)

    await state.clear()


@router.callback_query(F.data.startswith("mdep_ok:"))
async def cb_admin_approve_manual(call: CallbackQuery):
    if not _is_admin(call.from_user.id):
        await _safe_call_answer(call, "No autorizado.", show_alert=True)
        return

    dep_id = int(call.data.split(":", 1)[1])
    dep = await db.get_manual_deposit_by_id(dep_id)
    if not dep:
        await _safe_call_answer(call, "No encontrado.", show_alert=True)
        return
    if dep["status"] != "pending_review":
        await _safe_call_answer(call, f"Ya estaba en estado '{dep['status']}'.", show_alert=True)
        return

    new_balance = await db.credit_balance(
        dep["user_id"], dep["amount_usd"], reason=f"Depósito manual aprobado id={dep_id}",
        origin="cup",
    )
    await db.set_manual_deposit_status(dep_id, "approved", reviewed_by=call.from_user.id)

    await _safe_call_answer(call, "Aprobado ✅")
    await _mark_admin_message_resolved(call.message, f"\n\n✅ Aprobado por {call.from_user.id}")

    await _safe_send(
        call.bot, dep["user_id"],
        MSG_MANUAL_DEPOSIT_APPROVED.format(
            amount_usd  = format_amount(dep["amount_usd"], "USD"),
            new_balance = format_amount(new_balance, "USD"),
        ),
    )

    exposure = await db.get_cup_exposure()
    if exposure["total_usd"] >= MANUAL_DEPOSIT_CUP_EXPOSURE_ALERT_USD:
        cup_str = f"{exposure['total_cup']:,}".replace(",", " ")
        await _notify_admin(
            call.bot,
            f"🚨 <b>Exposición CUP sobre el umbral</b>\n"
            f"{exposure['count']} depósito(s) sin convertir · "
            f"{cup_str} CUP ≈ {format_amount(exposure['total_usd'], 'USD')}\n"
            f"Revisa con /exposicion_cup.",
        )


@router.callback_query(F.data.startswith("mdep_no:"))
async def cb_admin_reject_manual(call: CallbackQuery):
    if not _is_admin(call.from_user.id):
        await _safe_call_answer(call, "No autorizado.", show_alert=True)
        return

    dep_id = int(call.data.split(":", 1)[1])
    dep = await db.get_manual_deposit_by_id(dep_id)
    if not dep:
        await _safe_call_answer(call, "No encontrado.", show_alert=True)
        return
    if dep["status"] != "pending_review":
        await _safe_call_answer(call, f"Ya estaba en estado '{dep['status']}'.", show_alert=True)
        return

    await db.set_manual_deposit_status(dep_id, "rejected", reviewed_by=call.from_user.id)

    await _safe_call_answer(call, "Rechazado ❌")
    await _mark_admin_message_resolved(call.message, f"\n\n❌ Rechazado por {call.from_user.id}")

    await _safe_send(
        call.bot, dep["user_id"],
        MSG_MANUAL_DEPOSIT_REJECTED.format(reference_code=dep["reference_code"]),
    )


# ── Cancelación ───────────────────────────────────────────────────────────────

@router.callback_query(F.data == "cancel_op")
async def cb_cancel(call: CallbackQuery, state: FSMContext):
    """El usuario cancela la operación en curso."""
    await _safe_call_answer(call, "Operación cancelada.")
    current_state = await state.get_state()
    data = await state.get_data()
    tx_id = data.get("tx_id")

    if current_state in (
        WithdrawFlow.awaiting_amount, WithdrawFlow.selecting_currency,
        WithdrawFlow.awaiting_address, WithdrawFlow.confirming,
    ):
        # Ningún paso del flujo de retiro descuenta saldo hasta la
        # confirmación final (cb_withdraw_confirm), así que cancelar acá
        # nunca deja el saldo tocado.
        await state.clear()
        await call.message.answer("✅ Retiro cancelado. Tu saldo no fue modificado.")
        return

    if current_state in (DepositFlow.awaiting_amount, DepositFlow.selecting_currency):
        # Todavía no se generó ninguna orden de cobro, nada que revertir.
        await state.clear()
        await call.message.answer("✅ Depósito cancelado.")
        return

    if current_state in (
        ManualDepositFlow.selecting_method, ManualDepositFlow.awaiting_amount,
        ManualDepositFlow.awaiting_proof,
    ):
        # No se acreditó nada en ningún punto de este flujo hasta la
        # aprobación del admin, así que cancelar acá nunca deja saldo a
        # medio tocar. Si ya se había creado el registro (a partir de
        # awaiting_proof), lo marcamos 'cancelled' para que no bloquee al
        # usuario vía get_pending_manual_deposit.
        dep_id = data.get("manual_deposit_id")
        if dep_id:
            await db.set_manual_deposit_status(dep_id, "cancelled")
        await state.clear()
        await call.message.answer("✅ Depósito CUP cancelado.")
        return

    if current_state in (PurchaseFlow.selecting_manual_method, PurchaseFlow.awaiting_manual_review):
        # No se acreditó/cobró nada hasta que un admin aprueba el
        # comprobante, así que cancelar acá nunca deja nada a medio tocar.
        if tx_id:
            await db.set_status(tx_id, "error")
        await state.clear()
        await call.message.answer("✅ Compra cancelada. No se generó ningún cargo.")
        return

    if current_state == DepositFlow.awaiting_payment:
        # Ya se generó una orden. Igual que con una compra, CCPayment no
        # tiene forma de cerrar la orden de depósito -> se marca 'cancelled'
        # y _poll_deposit lo detecta en su próximo chequeo. Si el pago ya
        # había llegado, se acredita igual (ver _handle_cancelled_deposit).
        deposit_id = data.get("deposit_id")
        if deposit_id:
            await db.set_deposit_status(deposit_id, "cancelled")
        await state.clear()
        await call.message.answer(
            "✅ Depósito cancelado.\n"
            "Si ya habías enviado el pago antes de cancelar, lo detectaremos "
            "y se acreditará automáticamente a tu saldo."
        )
        return

    if current_state == PurchaseFlow.awaiting_sms:
        # Ya se le había asignado un número. Antes de cancelar, chequeamos
        # UNA vez si el código ya había llegado del lado de HeroSMS (puede
        # pasar justo antes de que el poller en background lo recoja) -
        # si es así, no tiene sentido "cancelar" algo que ya se completó:
        # se lo entregamos igual y no se reembolsa.
        act_id = data.get("activation_id")
        code_arrived = False
        if act_id:
            result = await hero.get_status(act_id)
            if result.get("status") == "ready" and result.get("code"):
                code = result["code"]
                await db.set_sms_code(tx_id, code)
                await db.set_status(tx_id, "completed")
                await hero.set_status_done(act_id)
                await call.message.answer(MSG_CODE_RECEIVED.format(code=code), parse_mode="HTML")
                if tx := await db.get_by_id(tx_id):
                    await _notify_admin(
                        bot=call.bot,
                        text=f"✅ <b>Venta completada</b> (código llegó justo al cancelar)\n{_tx_summary_line(tx)}",
                    )
                await _maybe_credit_referral_bonus(call.bot, tx_id)
                code_arrived = True

        if code_arrived:
            await state.clear()
            return

        # Cancelar en HeroSMS (libera el número y debería devolver el
        # costo a nuestro saldo). HeroSMS rechaza setStatus:8 si no pasaron
        # ~2 min desde que se asignó el número (ver panel web: el botón de
        # cancelar queda inactivo hasta entonces) -> si el usuario cancela
        # antes, esperamos el resto del tiempo en background antes de
        # llamarlo, en vez de comernos un rechazo seguro. El crédito al
        # usuario NO espera: se acredita de inmediato más abajo, de buena
        # fe, y solo la llamada real a HeroSMS se retrasa.
        tx_for_wait = await db.get_by_id(tx_id) if tx_id else None
        elapsed_since_assigned = _elapsed_seconds(tx_for_wait["updated_at"]) if tx_for_wait else HEROSMS_MIN_CANCEL_WAIT_SECONDS
        remaining_wait = max(0, HEROSMS_MIN_CANCEL_WAIT_SECONDS - elapsed_since_assigned)

        async def _delayed_cancel(activation_id: str, wait_s: int, tx_id_: int):
            if wait_s:
                await asyncio.sleep(wait_s)
            ok = await hero.cancel_number(activation_id)
            if not ok:
                if tx2 := await db.get_by_id(tx_id_):
                    await _notify_admin(
                        bot=call.bot,
                        text=f"🚨 <b>Cancelación con problemas</b>\n{_tx_summary_line(tx2)}\n"
                             "• HeroSMS rechazó la cancelación (revisar saldo/costo no recuperado)",
                    )

        if act_id:
            asyncio.create_task(_delayed_cancel(act_id, remaining_wait, tx_id))

        # Mismo cargo de servicio que en el timeout de SMS (ver config.py):
        # acá también ya se entregó un número real, así que cancelar manual
        # e inmediatamente es el mismo patrón de abuso, solo que más rápido.
        # Se acredita como saldo interno (sin comisión de red, instantáneo)
        # en vez de un reembolso cripto directo.
        if tx_id:
            price_usd = data.get("price_usd") or 0
            credit_amount = apply_refund_fee(price_usd, REFUND_FEE_PCT)
            new_balance = await _credit_refund_for_tx(
                call.from_user.id, tx_for_wait or {}, credit_amount, tx_id,
                reason=f"Cancelación manual tx={tx_id}",
            )
            await db.set_status(tx_id, "refunded")
            await call.message.answer(
                f"💰 Se acreditaron {format_amount(credit_amount, 'USD')} a tu saldo "
                f"interno (saldo total: {format_amount(new_balance, 'USD')}; se retiene "
                f"un {REFUND_FEE_PCT:.0%} de cargo de servicio). Úsalo en tu próxima "
                "compra con /start, o consulta /saldo."
            )

    elif current_state == PurchaseFlow.awaiting_payment and tx_id:
        # Todavía esperando el pago. CCPayment no tiene un endpoint para
        # cancelar/cerrar la orden de depósito (solo expira sola con su
        # `expiredAt`) -> marcamos la transacción como 'cancelled'. La
        # tarea _poll_payment que sigue corriendo en background la detecta
        # en su próximo chequeo y se detiene sola; si para entonces ya
        # habías pagado, te reembolsa automáticamente (ver
        # _handle_cancelled_order). Así evitamos que la tarea vieja siga
        # corriendo sobre datos de estado ya vaciados.
        await db.set_status(tx_id, "cancelled")

    elif tx_id:
        # Cancelado antes de generar una orden de pago (eligiendo
        # servicio/país/moneda): no hubo cargo, no hay nada que reembolsar.
        await db.set_status(tx_id, "error")

    await state.clear()
    await call.message.answer(
        "✅ Operación cancelada.\n"
        "Si ya habías enviado un pago antes de cancelar, lo detectaremos "
        "y se reembolsará automáticamente. Usa /start para comenzar de nuevo."
    )


# ── Catch-all: sesión sin estado válido ───────────────────────────────────────
# Se registran AL FINAL a propósito: aiogram prueba los handlers en orden de
# registro, así que estos solo se disparan cuando ningún handler de arriba
# matcheó. Caso típico: el usuario tenía un botón/flujo abierto de ANTES de
# un reinicio del bot. Usamos MemoryStorage, así que su FSM se perdió al
# reiniciar -> el callback_data (svc:, cnt:, cur:...) ya no tiene el State
# que su filtro exige, y sin esto aiogram lo descarta en silencio (se ve en
# el log como "Update ... is not handled") dejando al usuario sin respuesta.

@router.callback_query()
async def cb_fallback_expired_session(call: CallbackQuery, state: FSMContext):
    await _safe_call_answer(call, "Sesión expirada", show_alert=True)
    await state.clear()
    await _safe_answer(
        call.message,
        "⚠️ Tu sesión anterior ya no es válida (el bot se reinició o pasó "
        "demasiado tiempo). No se te cobró nada por esto — toca el botón "
        "de abajo para empezar de nuevo 👇",
        reply_markup=outbox.retry_keyboard(),
    )


@router.message()
async def msg_fallback_expired_session(message: Message, state: FSMContext):
    await state.clear()
    await _safe_answer(
        message,
        "🤔 No tengo una operación activa esperando ese mensaje. "
        "Toca el botón de abajo para empezar de nuevo 👇",
        reply_markup=outbox.retry_keyboard(),
    )