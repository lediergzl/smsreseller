"""
webhooks_herosms.py - Receptor del webhook "SMS entrantes" de HeroSMS.

Complementa a _poll_sms (ver handlers.py), no lo reemplaza: ese polling
sigue corriendo igual que antes, cada SMS_POLL_INTERVAL segundos, como red
de seguridad. Este webhook es un camino MÁS RÁPIDO en paralelo -HeroSMS nos
avisa apenas le llega el SMS, en vez de que nosotros nos enteremos recién en
el próximo tick del polling (hasta SMS_POLL_INTERVAL segundos de por
medio)- pero si por lo que sea el webhook no llega (Render con cold start,
un despliegue justo en el medio, HeroSMS con problemas, etc.) el polling
sigue ahí y la compra se completa igual, solo que un poco más lento. Ver
handlers._complete_sms_success: la lógica de "completar la venta" es
compartida entre ambos caminos y es idempotente a propósito, así que da
igual cuál de los dos llegue primero.

AUTENTICACIÓN: la documentación de HeroSMS no especifica ningún mecanismo
(sin header de firma HMAC, sin secreto en el body -a diferencia del webhook
de Telegram, que sí manda `secret_token`, ver config.WEBHOOK_SECRET). Lo
único que evita que cualquiera en internet mande eventos falsos de "SMS
recibido" es que la URL no sea adivinable: el secreto va como parte del
PATH (config.HEROSMS_WEBHOOK_SECRET), no como query param (esos quedan más
fácil expuestos en logs de proxies/analytics de terceros), y se compara con
hmac.compare_digest en vez de `==` para no filtrar el secreto de a poco vía
timing attack. Si en algún momento HeroSMS agrega una firma real (revisar
su doc de tanto en tanto), conviene migrar a validarla en vez de depender
solo de la URL secreta.

CONTRATO DE RESPUESTA: HeroSMS reintenta hasta 7 veces si no recibe 200, así
que:
  - Se devuelve 200 en CUALQUIER caso ya "entendido" (tx no encontrada, tx
    ya resuelta por otra vía, código nulo sin texto, etc.) para no generar
    reintentos inútiles sobre algo que no va a cambiar.
  - Solo se devuelve algo distinto de 200 (400) si el path secreto no
    coincide o el JSON viene malformado -casos donde SÍ tendría sentido que
    HeroSMS reintente con un payload/URL corregido del lado de ellos, o
    donde ni siquiera se pudo interpretar el request como para decidir algo.
"""
import hmac
import logging

from aiohttp import web

import config
from database import db
import handlers

logger = logging.getLogger(__name__)

WEBHOOK_PATH_TEMPLATE = "/webhooks/herosms/sms-incoming/{secret}"


def _secret_matches(candidate: str) -> bool:
    return hmac.compare_digest(candidate.encode(), config.HEROSMS_WEBHOOK_SECRET.encode())


async def _sms_incoming(request: web.Request) -> web.Response:
    bot = request.app["bot"]
    storage = request.app["fsm_storage"]

    candidate_secret = request.match_info.get("secret", "")
    if not _secret_matches(candidate_secret):
        logger.warning(
            "webhooks_herosms: intento con path secreto inválido (ip=%s).",
            request.remote,
        )
        # 404 en vez de 400/403: no confirmamos ni que la ruta existe, para
        # no ayudar a alguien que esté probando a fuerza bruta.
        return web.Response(status=404, text="not found")

    try:
        payload = await request.json()
    except Exception as exc:
        logger.warning("webhooks_herosms: body no es JSON válido: %s", exc)
        return web.Response(status=400, text="invalid json")

    activation_id = payload.get("activationId")
    code = payload.get("code")
    text = payload.get("text")

    if not activation_id:
        logger.warning("webhooks_herosms: payload sin activationId: %s", payload)
        return web.Response(status=400, text="missing activationId")

    if not code:
        # El campo `code` puede venir null aunque `text` no lo esté (ver
        # SKILL/doc del webhook). Sin un código ya extraído no hay nada
        # útil que completar acá -el polling (_poll_sms, que sigue vivo en
        # paralelo) usa hero.get_status, que si HeroSMS ya parseó el
        # código en su propio backend lo va a traer igual en la próxima
        # vuelta. No tiene sentido que este endpoint intente su propio
        # regex sobre `text`: duplicaría lógica de parseo que HeroSMS ya
        # hace del otro lado, con más chance de errar el formato.
        logger.info(
            "webhooks_herosms: SMS sin code para activation_id=%s (dejo que "
            "el polling lo resuelva). text=%r",
            activation_id, text,
        )
        return web.Response(status=200, text="ok, no code yet")

    tx = await db.get_by_activation_id(str(activation_id))
    if not tx:
        # Puede pasar por una activación vieja/de prueba, o un reintento de
        # HeroSMS que llega mucho después de que ya limpiamos algo -no es
        # un caso donde reintentar vaya a ayudar.
        logger.warning(
            "webhooks_herosms: no se encontró ninguna tx con activation_id=%s",
            activation_id,
        )
        return web.Response(status=200, text="ok, tx not found")

    result = await handlers.complete_sms_from_webhook(bot, storage, tx, str(code))
    logger.info(
        "webhooks_herosms: activation_id=%s tx=%s -> %s",
        activation_id, tx["id"], result,
    )
    return web.Response(status=200, text="ok")


def setup_herosms_webhook(app: web.Application, bot, storage) -> None:
    """Registra la ruta en la misma app aiohttp que ya sirve el webhook de
    Telegram y el panel de admin (ver main.py:_run_webhook). Si no hay
    secreto configurado, NO se registra la ruta -mejor no exponer un
    endpoint sin ninguna protección que exponerlo con un secreto vacío que
    hmac.compare_digest("", "") aceptaría como válido para cualquiera."""
    if not config.HEROSMS_WEBHOOK_SECRET:
        logger.warning(
            "HEROSMS_WEBHOOK_SECRET no está configurado: el webhook de SMS "
            "entrantes de HeroSMS queda DESACTIVADO (el bot sigue funcionando "
            "normal solo con el polling de siempre, más lento). Configurala "
            "para activarlo y avisale la URL a HeroSMS desde su panel."
        )
        return

    app["bot"] = bot
    app["fsm_storage"] = storage
    # WEBHOOK_PATH_TEMPLATE ya es un patrón de ruta dinámica de aiohttp
    # ("{secret}" como segmento variable): la comparación real contra
    # config.HEROSMS_WEBHOOK_SECRET pasa DENTRO de _sms_incoming (con
    # hmac.compare_digest), no acá. Esto es a propósito: así cualquier
    # valor en ese segmento hace match a nivel de ruteo (evita filtrar por
    # 404 "genérico" de aiohttp si no matchea vs. 404 nuestro si el
    # secreto está mal -mismo status code en ambos casos, ver _sms_incoming).
    app.router.add_post(WEBHOOK_PATH_TEMPLATE, _sms_incoming)
    real_url = config.WEBHOOK_HOST.rstrip("/") + WEBHOOK_PATH_TEMPLATE.format(
        secret=config.HEROSMS_WEBHOOK_SECRET
    )
    logger.info(
        "Webhook de SMS entrantes de HeroSMS activado. Configurá esta URL "
        "en el panel de HeroSMS (Webhooks -> SMS entrantes): %s",
        real_url,
    )
