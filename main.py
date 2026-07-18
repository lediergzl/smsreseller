"""
main.py - Punto de entrada del bot. Configura logging, inicia el bot y registra routers.

MODO DE EJECUCIÓN: Render (plan free) no tiene "background worker" gratis,
solo web services gratis, y esos necesitan atender HTTP. Por eso, si
config.WEBHOOK_HOST está seteado (Render lo autocompleta con
RENDER_EXTERNAL_URL, ver config.py), el bot corre como servidor aiohttp
escuchando en $PORT y Telegram le manda los updates por POST a /webhook. Si
WEBHOOK_HOST está vacío (por ejemplo corriendo en tu máquina), cae de vuelta
a long polling de toda la vida - no hace falta nada especial para desarrollo
local.
"""
import asyncio
import logging
import sys
from logging.handlers import RotatingFileHandler

# Fix para Windows: aiodns (usado por aiohttp) requiere SelectorEventLoop,
# pero el loop por defecto en Windows es ProactorEventLoop. Debe configurarse
# ANTES de crear cualquier event loop, por eso va aquí a nivel de módulo.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

import config
from handlers import router
import handlers
from database import db
import herosms_api as hero
import ccpay_api as ccpay
import backup_task
import outbox


def setup_logging():
    """Configura logging a consola y a archivo rotativo."""
    log_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    formatter = logging.Formatter(log_format)

    # Archivo rotativo: máx 5 MB por archivo, conserva 5 backups.
    # NOTA: en Render el filesystem es efímero (se borra en cada
    # redeploy/reinicio), así que este archivo es solo para debug puntual
    # mientras el servicio está corriendo, no un log persistente. Los logs
    # "de verdad" para revisar después conviene mirarlos en el dashboard de
    # Render (Logs), que sí los retiene.
    file_handler = RotatingFileHandler(
        "bot.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.INFO)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(logging.INFO)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(console_handler)

    # Silenciar logs muy verbosos de librerías HTTP
    logging.getLogger("aiohttp").setLevel(logging.WARNING)


async def recover_pending_transactions(bot: Bot, storage):
    """
    Al reiniciar el bot, revisa transacciones que quedaron en estados
    intermedios (pending, paid, number_assigned) y las REANUDA automáticamente.
    """
    pending = await db.get_pending_transactions()
    if not pending:
        return

    logger = logging.getLogger(__name__)
    logger.warning("Recuperando %d transacciones pendientes tras reinicio.", len(pending))

    for tx in pending:
        try:
            action = await handlers.resume_transaction(bot, storage, tx)
            logger.info("TX %s recuperada automáticamente: %s", tx["id"], action)
        except Exception as exc:
            logger.error("No se pudo reanudar tx %s: %s", tx["id"], exc)
            await outbox.notify(
                bot, tx["user_id"],
                "⚠️ El bot se reinició mientras tenías una operación en curso.\n"
                "Por seguridad, esa operación fue marcada como interrumpida.\n"
                "Si NO llegaste a pagar, no se te cobró nada: puedes tocar el botón "
                "de abajo (o usar /start) para intentarlo de nuevo sin problema.\n"
                "Si ya habías pagado y no recibiste tu número o código, contacta al "
                f"soporte indicando este ID: <code>{tx['id']}</code>",
                reply_markup=outbox.retry_keyboard(),
            )
            await db.set_status(tx["id"], "error")


async def recover_pending_deposits(bot: Bot, storage):
    """Análoga a recover_pending_transactions pero para depósitos pendientes."""
    pending = await db.get_pending_deposits()
    if not pending:
        return

    logger = logging.getLogger(__name__)
    logger.warning("Recuperando %d depósitos pendientes tras reinicio.", len(pending))

    for dep in pending:
        try:
            action = await handlers.resume_deposit(bot, storage, dep)
            logger.info("Depósito %s recuperado automáticamente: %s", dep["id"], action)
        except Exception as exc:
            logger.error("No se pudo reanudar depósito %s: %s", dep["id"], exc)
            await outbox.notify(
                bot, dep["user_id"],
                "⚠️ El bot se reinició mientras tenías un depósito en curso.\n"
                "Por seguridad, esa operación fue marcada como interrumpida.\n"
                "Si NO llegaste a pagar, no pasa nada: usa /saldo para intentar "
                "depositar de nuevo cuando quieras.\n"
                "Si ya habías pagado y no se acreditó, contacta al soporte "
                f"indicando este ID: <code>{dep['id']}</code>",
            )
            await db.set_deposit_status(dep["id"], "error")


def _build_bot_and_dispatcher():
    bot = Bot(
        token=config.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)
    # Se expone la instancia de storage como dato de workflow para poder
    # reconstruir el FSMContext de un usuario distinto al que originó el
    # update actual (ver handlers.cb_admin_approve_purchase_cup).
    dp["fsm_storage"] = storage
    dp.include_router(router)
    return bot, dp, storage


async def _startup_sequence(bot: Bot, storage):
    """Todo lo que antes corría al principio de main(): chequeo de
    integridad, recovery de transacciones/depósitos, outbox, warm cache y
    las tareas en background. Se llama tanto desde el arranque en modo
    webhook (on_startup) como desde el modo polling local."""
    logger = logging.getLogger(__name__)

    # Crea el pool de conexiones (asyncpg) y asegura que las tablas existan.
    # Tiene que correr antes que cualquier otro uso de `db` en este arranque.
    await db.connect()

    ok, detail = await db.integrity_check()
    if not ok:
        logger.error("¡La base de datos no respondió al chequeo de conectividad!: %s", detail)
        if config.ADMIN_CHAT_ID:
            try:
                await bot.send_message(
                    config.ADMIN_CHAT_ID,
                    "🚨 <b>El bot no pudo conectarse a la base de datos al arrancar</b>\n"
                    f"Detalle: <code>{detail}</code>\n"
                    "Revisa el estado de tu proyecto en https://console.neon.tech",
                    parse_mode="HTML",
                )
            except Exception as exc:
                logger.error("No se pudo alertar al admin sobre la base de datos: %s", exc)

    await recover_pending_transactions(bot, storage)
    await recover_pending_deposits(bot, storage)

    # Reintentar de una vez cualquier aviso que haya quedado pendiente en el
    # outbox de una corrida anterior.
    await outbox.flush_pending(bot)
    asyncio.create_task(outbox.retry_loop(bot))

    # Precalentar caché de servicios/países en background.
    asyncio.create_task(hero.warm_cache())

    # Chequeo de salud de la DB + keep-alive de Neon (ver backup_task.py).
    asyncio.create_task(backup_task.db_health_loop(bot))

    logger.info("Bot iniciado correctamente.")


def _run_webhook(bot: Bot, dp: Dispatcher, storage):
    """
    Modo Render: servidor HTTP escuchando en $PORT, Telegram entrega los
    updates por POST a WEBHOOK_PATH.

    IMPORTANTE: esta función NO es async. web.run_app() ya crea y maneja su
    propio event loop por dentro (llama a asyncio.run internamente) - si la
    llamáramos desde dentro de una corrutina ya envuelta en asyncio.run(),
    chocarían dos loops. Todo el trabajo async real (on_startup/on_shutdown)
    queda delegado a los hooks de aiogram/aiohttp, que sí corren dentro del
    loop que arma web.run_app().
    """
    logger = logging.getLogger(__name__)
    webhook_url = config.WEBHOOK_HOST.rstrip("/") + config.WEBHOOK_PATH

    async def on_startup(bot: Bot):
        await bot.set_webhook(
            url=webhook_url,
            secret_token=config.WEBHOOK_SECRET or None,
            drop_pending_updates=False,
            # Sin esto, Telegram NO manda updates de tipo chat_member por
            # default (a diferencia de message, callback_query, etc.), así
            # que on_channel_member_update nunca dispararía y
            # channel_joined_at quedaría siempre vacío. resolve_used_update_types()
            # arma la lista automáticamente en base a los handlers ya
            # registrados en el router (incluye chat_member porque ya lo
            # registraste ahí).
            allowed_updates=dp.resolve_used_update_types(),
        )
        logger.info("Webhook configurado en %s", webhook_url)
        await _startup_sequence(bot, storage)

    async def on_shutdown(bot: Bot):
        await bot.delete_webhook()
        await bot.session.close()
        await hero.close_session()
        await ccpay.close_session()
        await db.close()

    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    app = web.Application()

    async def health(request):
        # Usado por: (a) el health check de Render, (b) un pinger externo
        # (UptimeRobot / cron-job.org) para evitar que el free tier se
        # duerma tras 15 min sin tráfico. No expone nada sensible.
        return web.Response(text="ok")

    app.router.add_get("/", health)
    app.router.add_get("/health", health)

    SimpleRequestHandler(
        dispatcher=dp, bot=bot, secret_token=config.WEBHOOK_SECRET or None,
    ).register(app, path=config.WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)

    logger.info("Arrancando servidor web en el puerto %d...", config.PORT)
    web.run_app(app, host="0.0.0.0", port=config.PORT, print=None)


async def _run_polling(bot: Bot, dp: Dispatcher, storage):
    """Modo local: long polling de siempre, sin necesidad de URL pública."""
    logger = logging.getLogger(__name__)
    await _startup_sequence(bot, storage)
    logger.info("Bot iniciado. Escuchando actualizaciones (polling)...")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        await hero.close_session()
        await ccpay.close_session()
        await db.close()


def main():
    setup_logging()
    logger = logging.getLogger(__name__)
    config.validate()

    bot, dp, storage = _build_bot_and_dispatcher()

    if config.WEBHOOK_HOST:
        logger.info("WEBHOOK_HOST configurado (%s): corriendo como web service.", config.WEBHOOK_HOST)
        _run_webhook(bot, dp, storage)
    else:
        logger.info("WEBHOOK_HOST vacío: corriendo en modo polling (desarrollo local).")
        asyncio.run(_run_polling(bot, dp, storage))


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, SystemExit):
        logging.getLogger(__name__).info("Bot detenido manualmente.")
