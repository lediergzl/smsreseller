"""
backup_task.py - Con Neon como base de datos, esto YA NO hace backups de
archivo ni los manda por Telegram (esa era la versión SQLite, ver git
history si hace falta el código viejo). Neon ya da:
  - Backups automáticos + point-in-time recovery (recuperar la base a
    cualquier momento dentro de la ventana de tu plan).
  - Branching: se puede crear una copia completa de la base en un click
    desde https://console.neon.tech para probar algo sin tocar producción.

Lo que sigue haciendo falta desde el lado del bot:
  1. Un chequeo de conectividad periódico: si la base no responde, avisar
     al admin YA en vez de enterarse recién cuando falla una compra real.
  2. Un "ping" liviano y regular: el compute de Neon (plan free) se
     suspende solo tras un rato de inactividad; si nadie usa el bot por un
     rato, el PRIMER mensaje real después de esa pausa paga el costo de
     reactivarlo (unos segundos de más). Este ping evita que eso le toque
     justo al primer cliente del día.
"""
import asyncio
import logging

from aiogram import Bot

import config
from database import db

logger = logging.getLogger(__name__)


async def _notify_admin(bot: Bot, text: str):
    if not config.ADMIN_CHAT_ID:
        return
    try:
        await bot.send_message(config.ADMIN_CHAT_ID, text, parse_mode="HTML")
    except Exception as exc:
        logger.error("No se pudo notificar al canal de admin: %s", exc)


async def db_health_loop(bot: Bot):
    """
    Tarea en background (ver main.py): cada config.DB_PING_INTERVAL_MINUTES
    hace un chequeo liviano contra la base. Nunca lanza hacia arriba: un
    fallo puntual se loguea/alerta y el loop sigue vivo.
    """
    interval_seconds = config.DB_PING_INTERVAL_MINUTES * 60
    while True:
        try:
            ok, detail = await db.integrity_check()
            if not ok:
                logger.error("Chequeo de conectividad a la base falló: %s", detail)
                await _notify_admin(
                    bot,
                    "🚨 <b>ALERTA: el bot no pudo conectarse a la base de datos (Neon)</b>\n"
                    f"Detalle: <code>{detail}</code>\n"
                    "Revisa el estado de tu proyecto en https://console.neon.tech cuanto antes.",
                )
        except Exception as exc:
            logger.error("db_health_loop: error inesperado: %s: %s", type(exc).__name__, exc)
        await asyncio.sleep(interval_seconds)
