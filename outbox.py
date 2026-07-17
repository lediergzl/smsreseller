"""
outbox.py - Cola de entrega de mensajes con reintentos y backoff.

Problema que cubre: _safe_send/_safe_answer (ver handlers.py) intentaban un
envío único y, si Telegram lo rechazaba (red caída, timeout, rate limit), el
mensaje se perdía para siempre — solo quedaba un log.error() sin que nadie
se enterara. Esto duele más justo en los casos que más importan: avisar un
reembolso, o avisarle al usuario que puede reintentar tras un reinicio del
bot (el caso real que motivó esto: un usuario no se enteró de que le habían
reembolsado hasta que reclamó por soporte).

`notify()` persiste el mensaje en la tabla `outbox` (ver database.py) ANTES
de intentar enviarlo, así un fallo -o un crash del proceso justo después- no
lo hace desaparecer: `retry_loop()` sigue insistiendo con backoff exponencial
hasta entregarlo o agotar los intentos (ver config.OUTBOX_MAX_ATTEMPTS),
momento en el que se marca 'dead' y se alerta al admin en vez de reintentar
para siempre sobre un chat que quizás ya no es alcanzable (usuario bloqueó
al bot, cuenta eliminada, etc.).
"""
import asyncio
import logging
from datetime import datetime, timedelta

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import (
    OUTBOX_RETRY_INTERVAL_SECONDS, OUTBOX_BACKOFF_SCHEDULE,
    OUTBOX_MAX_ATTEMPTS, ADMIN_CHAT_ID,
)
from database import db

logger = logging.getLogger(__name__)


def retry_keyboard() -> InlineKeyboardMarkup:
    """
    Botón de 'reintentar' que apunta al mismo callback_data que ya usa el
    menú principal para arrancar una compra (ver handlers.cb_new_purchase,
    utils.main_menu_keyboard). Pensado para avisos donde el usuario tiene
    que tomar acción ("usa /start") — así no depende de que se acuerde de
    escribir el comando a mano, alcanza con tocar el botón.
    """
    builder = InlineKeyboardBuilder()
    builder.button(text="🔄 Reintentar ahora", callback_data="new_purchase")
    return builder.as_markup()


def _next_attempt_delay(attempts_so_far: int) -> int:
    """attempts_so_far = intentos fallidos YA registrados antes de este. El
    backoff crece según OUTBOX_BACKOFF_SCHEDULE; el último valor de la lista
    se repite si nos quedamos sin pasos antes de llegar a OUTBOX_MAX_ATTEMPTS."""
    idx = min(attempts_so_far, len(OUTBOX_BACKOFF_SCHEDULE) - 1)
    return OUTBOX_BACKOFF_SCHEDULE[idx]


async def notify(bot, chat_id: int, text: str, reply_markup: InlineKeyboardMarkup = None) -> bool:
    """
    Encola el mensaje (persistido en SQLite, sobrevive a un crash del bot) e
    intenta entregarlo de inmediato. Devuelve True si se entregó en el acto;
    si falla, la fila queda 'pending' y retry_loop() sigue intentando solo —
    el llamador NO necesita manejar el error, la garantía de entrega ya
    quedó delegada acá.
    """
    markup_json = reply_markup.model_dump_json() if reply_markup else None
    outbox_id = await db.enqueue_outbox(chat_id, text, markup_json)
    return await _attempt_send(bot, outbox_id, chat_id, text, reply_markup)


async def _attempt_send(
    bot, outbox_id: int, chat_id: int, text: str,
    reply_markup: InlineKeyboardMarkup, attempts_so_far: int = 0,
) -> bool:
    try:
        await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=reply_markup)
        await db.mark_outbox_sent(outbox_id)
        return True
    except Exception as exc:
        give_up = attempts_so_far + 1 >= OUTBOX_MAX_ATTEMPTS
        delay = _next_attempt_delay(attempts_so_far)
        next_attempt_at = (datetime.utcnow() + timedelta(seconds=delay)).strftime("%Y-%m-%d %H:%M:%S")
        logger.warning(
            "outbox: envío falló (id=%s chat=%s intento=%d/%d, próximo en %ds): %s",
            outbox_id, chat_id, attempts_so_far + 1, OUTBOX_MAX_ATTEMPTS, delay, exc,
        )
        await db.mark_outbox_attempt_failed(outbox_id, str(exc), next_attempt_at, give_up)
        if give_up:
            await _alert_admin_dead_message(bot, outbox_id, chat_id, text)
        return False


async def _alert_admin_dead_message(bot, outbox_id: int, chat_id: int, text: str):
    """Si se agotan los reintentos, el admin se entera enseguida en vez de
    descubrirlo semanas después por un reclamo de soporte o revisando logs."""
    if not ADMIN_CHAT_ID:
        return
    preview = text[:200] + ("…" if len(text) > 200 else "")
    try:
        await bot.send_message(
            ADMIN_CHAT_ID,
            f"🚨 <b>No se pudo entregar un mensaje tras {OUTBOX_MAX_ATTEMPTS} intentos</b>\n"
            f"outbox #{outbox_id} · chat_id <code>{chat_id}</code>\n"
            f"Texto: {preview}",
            parse_mode="HTML",
        )
    except Exception as exc:
        logger.error("No se pudo alertar al admin sobre outbox muerto #%s: %s", outbox_id, exc)


async def flush_pending(bot):
    """
    Reintenta de una vez todo lo que quedó 'pending' de una corrida
    anterior (ej. el bot se cayó justo después de encolar un aviso, antes de
    que retry_loop tuviera oportunidad de correr). Se llama una vez al
    arrancar, ANTES de entrar al polling, para que esos avisos salgan cuanto
    antes en vez de esperar el primer tick de retry_loop.
    """
    due = await db.get_due_outbox(limit=200)
    if not due:
        return
    logger.info("outbox: reintentando %d mensaje(s) pendiente(s) de antes del reinicio.", len(due))
    for row in due:
        reply_markup = (
            InlineKeyboardMarkup.model_validate_json(row["reply_markup"])
            if row.get("reply_markup") else None
        )
        await _attempt_send(
            bot, row["id"], row["chat_id"], row["text"], reply_markup,
            attempts_so_far=row["attempts"],
        )


async def retry_loop(bot):
    """
    Tarea en background (ver main.py: asyncio.create_task(outbox.retry_loop(bot))).
    Cada OUTBOX_RETRY_INTERVAL_SECONDS revisa mensajes pendientes cuyo
    próximo intento ya venció y los reintenta. Corre para siempre; un fallo
    inesperado en una iteración se loguea y el loop sigue (no debe morirse
    silenciosamente y dejar de reintentar todo lo demás).
    """
    while True:
        await asyncio.sleep(OUTBOX_RETRY_INTERVAL_SECONDS)
        try:
            due = await db.get_due_outbox()
            for row in due:
                reply_markup = (
                    InlineKeyboardMarkup.model_validate_json(row["reply_markup"])
                    if row.get("reply_markup") else None
                )
                await _attempt_send(
                    bot, row["id"], row["chat_id"], row["text"], reply_markup,
                    attempts_so_far=row["attempts"],
                )
        except Exception as exc:
            logger.error("outbox.retry_loop: fallo inesperado en la iteración: %s", exc)
