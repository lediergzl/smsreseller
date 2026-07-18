"""
config.py - Carga de variables de entorno y constantes globales
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ── Telegram ──────────────────────────────────────────────────────────────────
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")

# ── HeroSMS ───────────────────────────────────────────────────────────────────
HEROSMS_API_KEY: str  = os.getenv("HEROSMS_API_KEY", "")
# Base del endpoint único estilo SMS-Activate: <URL>/stubs/handler_api.php
# NO incluir /api ni /stubs aquí, herosms_api.py ya lo agrega.
HEROSMS_API_URL: str  = os.getenv("HEROSMS_API_URL", "https://hero-sms.com")

# ── CCPay (CCPayment) ─────────────────────────────────────────────────────────
# IMPORTANTE (API v2, obligatoria desde que la cuenta quedó en "solo v2"):
# - Credenciales Appid/AppSecret, igual que antes.
# - Firma: HMAC-SHA256(key=AppSecret, msg=Appid + Timestamp + body_json), NO el
#   SHA-256 plano que usaba v1.
# - Timestamp en MILISEGUNDOS (v1 usaba segundos).
# - Los tokens ya no se identifican con un "token_id" UUID único: ahora son
#   coinId (entero) + chain (string, ej. "TRX", "POLYGON"). Ver ccpay_api.py.
# Consíguelas en: https://admin.ccpayment.com/developer/config
CCPAY_APP_ID:     str = os.getenv("CCPAY_APP_ID", "")
CCPAY_APP_SECRET: str = os.getenv("CCPAY_APP_SECRET", "")
CCPAY_API_URL:    str = os.getenv("CCPAY_API_URL", "https://ccpayment.com/ccpayment/v2")

# ── Negocio ───────────────────────────────────────────────────────────────────
# Multiplicador de precio: precio_cliente = costo_herosms * MARKUP
MARKUP: float = float(os.getenv("MARKUP", "2.0"))

# Tiempo máximo (segundos) que el usuario tiene para pagar
PAYMENT_TIMEOUT_SECONDS: int = int(os.getenv("PAYMENT_TIMEOUT_SECONDS", "900"))   # 15 min

# Tiempo máximo (segundos) para esperar el SMS después de obtener el número
SMS_TIMEOUT_SECONDS: int = int(os.getenv("SMS_TIMEOUT_SECONDS", "600"))           # 10 min

# Intervalo de polling para verificar pago (segundos)
PAYMENT_POLL_INTERVAL: int = int(os.getenv("PAYMENT_POLL_INTERVAL", "8"))

# Intervalo de polling para verificar SMS (segundos)
SMS_POLL_INTERVAL: int = int(os.getenv("SMS_POLL_INTERVAL", "5"))

# ── Antiabuso / reembolsos ─────────────────────────────────────────────────────
# Problema que cubre este bloque: un usuario puede pedir un número, pagarlo,
# NO usarlo nunca (o cancelarlo manualmente ni bien lo recibe) y esperar el
# reembolso automático. Aunque HeroSMS devuelva el costo del número a nuestro
# saldo, la comisión de red del reembolso cripto SIEMPRE la pagábamos nosotros
# (ver ccpay_api.refund_user -> merchantPayNetworkFee). Repetido muchas veces,
# eso drena el saldo disponible para retiros aunque cada caso individual sea
# barato. Los dos controles de abajo atacan justo ese patrón.

# Porcentaje NO reembolsable cuando el usuario ya recibió un número y la
# operación termina sin completarse (timeout de SMS, o cancelación manual
# estando en awaiting_sms). Cubre la comisión de red y desincentiva el
# "pedir y no usar". NO aplica a timeout de pago ni a "sin números
# disponibles" (ahí el usuario nunca llegó a tener un número asignado, no es
# un caso de abuso posible y se reembolsa 100%).
REFUND_FEE_PCT: float = float(os.getenv("REFUND_FEE_PCT", "0.10"))  # 10%

# Si un usuario acumula ABUSE_MAX_STRIKES operaciones "número asignado y no
# completado" (timeout de SMS o cancelación manual con número ya asignado)
# dentro de ABUSE_WINDOW_HOURS, se le bloquean nuevas compras por
# ABUSE_BLOCK_HOURS (ver database.get_abuse_strikes y handlers.cb_new_purchase).
ABUSE_MAX_STRIKES:  int = int(os.getenv("ABUSE_MAX_STRIKES", "3"))
ABUSE_WINDOW_HOURS: int = int(os.getenv("ABUSE_WINDOW_HOURS", "24"))
ABUSE_BLOCK_HOURS:  int = int(os.getenv("ABUSE_BLOCK_HOURS", "24"))

# Comisión de servicio sobre RETIROS de saldo interno a cripto (distinto de
# REFUND_FEE_PCT: esto no es antiabuso, es el margen por convertir/enviar
# saldo legítimo del usuario). Sin monto mínimo de retiro a propósito: el
# usuario ve cuánto recibiría neto (ver handlers.msg_withdraw_amount) y
# decide si le conviene retirar montos chicos o no — no es al bot a quien
# le corresponde decidirlo por él.
WITHDRAWAL_FEE_PCT: float = float(os.getenv("WITHDRAWAL_FEE_PCT", "0.05"))  # 5%

# Monto mínimo en USD para poder retirar saldo interno a cripto.
# Por qué SÍ hace falta un mínimo acá (a diferencia de lo que dice el
# comentario de arriba, de cuando no lo había): con precios de números tan
# bajos como $0.11-0.15 (ver discusión de MARKUP), un retiro de un par de
# centavos puede perder MÁS en comisión de red (ver ccpay_api.refund_user,
# merchantPayNetworkFee=False → la comisión se descuenta del monto que
# recibe el usuario) que el monto retirado en sí. El usuario terminaría
# recibiendo $0 o una fracción ridícula, mala experiencia sin beneficio
# para nadie. Este mínimo NO aplica a DEPOSIT_MIN_USD (ese ya existe y
# cubre el caso análogo del lado de depósitos).
WITHDRAWAL_MIN_USD: float = float(os.getenv("WITHDRAWAL_MIN_USD", "1.0"))

# Monto mínimo en USD (unidad interna) para poder retirar saldo de ORIGEN
# CUP. Deliberadamente separado de WITHDRAWAL_MIN_USD: la razón de ser de
# ese mínimo es la comisión de red al enviar cripto (ver comentario arriba),
# y un retiro en CUP no tiene comisión de red — lo transfiere un admin
# manualmente (ver handlers.cb_admin_approve_cup_withdrawal). Por defecto en
# 0: no hay motivo para bloquear un retiro CUP chico. Si en el futuro se
# necesita un piso (p.ej. para evitar spam de retiros de centavos), se puede
# subir con esta variable sin tocar el mínimo de cripto.
CUP_WITHDRAWAL_MIN_USD: float = float(os.getenv("CUP_WITHDRAWAL_MIN_USD", "0.0"))

# ── Depósitos (agregar saldo) ──────────────────────────────────────────────────
# A diferencia de un retiro, un depósito es CCPayment cobrando (no pagando),
# así que no hay riesgo de "no enough balance for withdrawal" del lado de
# CCPayment. Se acredita el 100% del monto pagado, sin comisión de entrada:
# cobrar acá desalentaría justo lo que más nos conviene (que el usuario
# tenga saldo interno en vez de depender de reembolsos on-chain). El margen
# ya se cobra al momento del retiro (WITHDRAWAL_FEE_PCT), no al depositar.
#
# Monto mínimo en USD para poder depositar. Existe para evitar depósitos tan
# chicos que la comisión de red que paga el usuario al enviar valga más que
# lo depositado (mala experiencia, no aporta nada a ninguna de las partes).
DEPOSIT_MIN_USD: float = float(os.getenv("DEPOSIT_MIN_USD", "1.0"))

# ── Monedas habilitadas para RETIRO ────────────────────────────────────────────
# Problema que cubre: CCPayment consolida el balance del merchant SOLO entre
# redes de un mismo símbolo (ej. USDT-TRC20 y USDT-ERC20 son la misma bolsa
# de fondos), pero NO entre símbolos distintos (TRX, ETH, SOL, LTC son bolsas
# separadas). ccpay.get_supported_currencies() devuelve ~12 monedas porque
# son las que el merchant puede RECIBIR (compras/depósitos, donde no hay
# ningún riesgo: ahí es CCPayment quien recibe). Pero para un RETIRO, es el
# merchant quien tiene que TENER esa moneda específica para enviarla — y si
# el saldo entrante fue mayormente en TRX (por ejemplo), no hay ETH ni SOL
# reales para pagar un retiro en esas monedas aunque el saldo INTERNO
# (fungible, en USD) del usuario alcance de sobra.
#
# En vez de ofrecer las ~12 monedas como destino de retiro y descubrir la
# falta de fondos recién al intentar pagar (ver ccpay_api.refund_user y el
# revert automático en cb_withdraw_confirm), se restringe el retiro a un set
# chico de monedas "de liquidación" que el negocio mantiene fondeadas a
# propósito. Recomendado: activar en el dashboard de CCPayment la conversión
# automática a stablecoin de todo lo que entra (Settings > Auto-convert),
# así el saldo real termina concentrado en USDT sin importar en qué pagó
# cada cliente, y esa es la única moneda que hace falta tener siempre lista.
#
# Formato en .env: WITHDRAWAL_ALLOWED_CURRENCIES=USDT o USDT,USDC
WITHDRAWAL_ALLOWED_CURRENCIES: set[str] = {
    c.strip().upper()
    for c in os.getenv("WITHDRAWAL_ALLOWED_CURRENCIES", "USDT").split(",")
    if c.strip()
}

# ── Base de datos (Postgres / Neon) ────────────────────────────────────────────
# Connection string completa de tu proyecto en https://console.neon.tech
# (Dashboard -> Connect -> "Connection string"). Incluye usuario, password,
# host, db y "?sslmode=require". Ejemplo:
#   postgresql://usuario:password@ep-xxxx.neon.tech/neondb?sslmode=require
DATABASE_URL: str = os.getenv("DATABASE_URL", "")

# ── Salud de la base de datos ──────────────────────────────────────────────────
# ANTES (SQLite) acá vivía la config de backups automáticos a archivo local
# + envío a Telegram (ver git history de backup_task.py). Con Neon eso ya no
# hace falta: Neon gestiona sus propios backups/point-in-time recovery y
# branching. Lo único que sigue corriendo en background (ver
# backup_task.db_health_loop) es:
#   1. Un chequeo de conectividad periódico que alerta al admin si el bot
#      pierde la conexión a la base.
#   2. Un "ping" liviano que evita que el compute de Neon (plan free) se
#      quede dormido por inactividad y el primer usuario real pague ese
#      cold start.
DB_PING_INTERVAL_MINUTES: int = int(os.getenv("DB_PING_INTERVAL_MINUTES", "10"))

# ── Despliegue en Render (web service, plan free) ─────────────────────────────
# Render no ofrece "background worker" gratis: solo web services gratis, y
# esos necesitan escuchar HTTP. Por eso el bot corre en modo WEBHOOK (Telegram
# nos manda los updates por HTTP) en vez de long polling. Si WEBHOOK_HOST
# queda vacío, main.py cae de vuelta a polling (cómodo para correr local).
#
# Render define automáticamente RENDER_EXTERNAL_URL con la URL pública del
# servicio, así que normalmente NO hace falta setear WEBHOOK_HOST a mano.
PORT: int = int(os.getenv("PORT", "10000"))
WEBHOOK_HOST: str = os.getenv("WEBHOOK_HOST") or os.getenv("RENDER_EXTERNAL_URL", "")
WEBHOOK_PATH: str = os.getenv("WEBHOOK_PATH", "/webhook")
# Secreto que Telegram devuelve en cada request al webhook (header
# X-Telegram-Bot-Api-Secret-Token) para poder verificar que el POST viene
# de verdad de Telegram y no de cualquiera que adivine la URL del webhook.
WEBHOOK_SECRET: str = os.getenv("WEBHOOK_SECRET", "")

# ── Depósitos manuales (CUP vía Transfermóvil / EnZona) ────────────────────────
# Problema que cubre: a diferencia de CCPay, Transfermóvil/EnZona no dan
# webhook ni API de verificación a un tercero individual -> no se puede
# confirmar el pago automáticamente como con cripto. Se resuelve con
# aprobación MANUAL de un admin: el usuario transfiere con un código de
# referencia único en el concepto, manda el comprobante, y un admin
# aprueba/rechaza desde Telegram (ver handlers.py, tabla manual_deposits en
# database.py). Se arranca solo con estos dos métodos (los más usados);
# saldo móvil/MLC se agregan después si hay demanda, no antes.
#
# Formato en .env: MANUAL_PAYMENT_METHODS=transfermovil:Transfermóvil (CUP):<tarjeta>|enzona:EnZona (CUP):<cuenta>
# code:nombre_visible:datos_de_cuenta separados por "|" entre métodos.
def _parse_manual_methods(raw: str) -> dict:
    methods = {}
    for chunk in raw.split("|"):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split(":", 2)
        if len(parts) != 3:
            continue
        code, name, account = parts
        methods[code.strip()] = {"name": name.strip(), "account": account.strip()}
    return methods

MANUAL_PAYMENT_METHODS: dict[str, dict] = _parse_manual_methods(
    os.getenv(
        "MANUAL_PAYMENT_METHODS",
        "transfermovil:Transfermóvil (CUP):Tarjeta 9234 XXXX XXXX 1234|"
        "enzona:EnZona (CUP):Cuenta 9224-XXXX-XXXX",
    )
)

# Tasa informal USDT/CUP del mercado cubano (varía día a día, no hay API
# oficial confiable para esto). Es la tasa de REFERENCIA, la que se ve
# circular en los grupos/mercado — NO es la que se le aplica al cliente
# directamente (ver MANUAL_DEPOSIT_CUP_MARGIN_PCT abajo).
MANUAL_DEPOSIT_CUP_RATE: float = float(os.getenv("MANUAL_DEPOSIT_CUP_RATE", "960"))

# Por qué hace falta un margen y no usar la tasa de referencia tal cual:
# el operador es comprador Y vendedor de USDT en la misma cadena. El CUP
# que un cliente transfiere HOY no se convierte a USDT en el mismo
# instante (el admin aprueba, se acredita saldo, y recién más tarde se
# compra USDT real para recargar HeroSMS) — y la tasa informal cubana casi
# siempre se mueve en contra del CUP mientras tanto. Sin margen, cada
# depósito CUP queda expuesto a perder margen (o directamente dar
# pérdida) solo por el desfase entre "acreditar" y "convertir".
#
# La tasa EFECTIVA que se le muestra al cliente es más cara que la de
# referencia por este porcentaje: efective_rate = REFERENCE * (1 + margin).
# Con margin=0.04 y referencia=960 -> al cliente se le pide CUP como si la
# tasa fuera 998, no 960 (ver utils.usd_to_cup_effective).
MANUAL_DEPOSIT_CUP_MARGIN_PCT: float = float(os.getenv("MANUAL_DEPOSIT_CUP_MARGIN_PCT", "0.04"))

# Tope de exposición: cuánto CUP acumulado (ya aprobado, todavía sin
# convertir a USDT real) se tolera antes de que el bot alerte al admin. No
# bloquea nuevos depósitos por sí solo (ver handlers.py) — es una alerta,
# no un freno automático, porque el admin puede estar en proceso de
# convertir y el bot no tiene forma de saberlo. Ver db.get_cup_exposure()
# y el comando /exposicion_cup.
MANUAL_DEPOSIT_CUP_EXPOSURE_ALERT_USD: float = float(
    os.getenv("MANUAL_DEPOSIT_CUP_EXPOSURE_ALERT_USD", "50.0")
)

# Monto máximo por depósito manual mientras el proceso está nuevo y sin
# probar a fondo (un error de aprobación pesa menos si el tope es bajo).
# Se sube más adelante cuando el flujo esté validado en producción.
MANUAL_DEPOSIT_MAX_USD: float = float(os.getenv("MANUAL_DEPOSIT_MAX_USD", "10.0"))

# Monto mínimo, mismo motivo que DEPOSIT_MIN_USD.
MANUAL_DEPOSIT_MIN_USD: float = float(os.getenv("MANUAL_DEPOSIT_MIN_USD", "1.0"))

# ── Pago en CUP de UNA COMPRA (número), directo, sin pasar por /saldo ─────────
# Problema que cubre: a diferencia de un depósito de saldo (donde el monto lo
# elige el usuario, así que ya se controla con MANUAL_DEPOSIT_MIN_USD), acá
# el monto lo fija el PRECIO DEL NÚMERO del catálogo, que puede ser de pocos
# centavos (ej. $0.06). Revisar un comprobante a mano y más tarde convertir
# ese CUP a USDT real cuesta tiempo del operador que una venta de centavos no
# alcanza a cubrir, sin importar la tasa aplicada.
#
# La solución NO es sacar la opción CUP para números baratos (eso le quita al
# cliente cubano justo la ventaja que tiene: pagar sin cripto) sino ponerle
# un PISO al monto cobrado: si el precio del número convertido a CUP da menos
# que el equivalente de este mínimo, se le cobra este mínimo igual (sigue
# siendo poca plata para el cliente, y para el operador ya es una venta que
# vale la pena revisar/convertir). Se expresa en USD (no en CUP fijo) para
# que se reajuste solo si cambia MANUAL_DEPOSIT_CUP_RATE.
MANUAL_PURCHASE_MIN_USD: float = float(os.getenv("MANUAL_PURCHASE_MIN_USD", "0.30"))

# ── Depósitos manuales (CUP vía Transfermóvil / EnZona) — continuación ────────
# Tasa informal USDT/CUP del mercado cubano definida arriba se reutiliza acá.

# ── Sistema de referidos ────────────────────────────────────────────────────────
# Un usuario invita a otro con su enlace (/referidos); cuando el invitado
# completa su PRIMERA compra, quien invitó recibe un bono en crédito de
# saldo interno (origen 'crypto', retirable, igual que una recarga — ver
# database.register_referral_bonus). El porcentaje es configurable por
# variable de entorno a propósito: es un parámetro de campaña de marketing
# que puede necesitar ajustarse sin tocar código ni redeploy manual de la
# lógica de crédito.
REFERRAL_BONUS_PCT: float = float(os.getenv("REFERRAL_BONUS_PCT", "0.10"))  # 10%

# Piso en USD sobre el monto de esa primera compra para que se pague el
# bono. Mismo motivo que MANUAL_PURCHASE_MIN_USD: una compra de pocos
# centavos generaría un bono de fracciones de centavo, que no vale ni el
# mensaje de notificación al referidor.
#
# IMPORTANTE: por defecto queda POR DEBAJO de MANUAL_PURCHASE_MIN_USD
# (0.30) a propósito. CUP es el mercado donde el bono de referidos sí es
# tangible (ver discusión de REFERRAL_BONUS_PCT): con MANUAL_PURCHASE_MIN_USD
# como piso de cobro, toda compra pagada en CUP factura como mínimo $0.30,
# así que un REFERRAL_MIN_PURCHASE_USD >= 0.30 dejaría a ESE mercado sin
# bono -justo el que más lo necesita para motivar a compartir. Si se sube
# este valor por env var, hay que subirlo con cuidado de no pisar el piso
# de CUP sin querer.
REFERRAL_MIN_PURCHASE_USD: float = float(os.getenv("REFERRAL_MIN_PURCHASE_USD", "0.20"))

# Horas de "período de gracia" antes de que un bono de referido se
# acredite de verdad al saldo del referidor. Cierra el hueco donde el
# bono se pagaba al instante al completarse la compra, pero esa compra
# podía terminar reembolsada (timeout de SMS, cancelación, reembolso
# manual futuro) sin que el bono ya cobrado se revirtiera. Con esto, el
# bono queda en `referrals.status = 'pending'` hasta que pasan estas
# horas Y la tx sigue 'completed' -ver database.release_pending_referrals
# y el loop handlers._release_referrals_loop-. 24h es suficiente para
# cubrir timeouts de SMS y reclamos típicos sin que el referidor note
# la demora (ver MSG_REFERRAL_NEW_SIGNUP, que ya avisa "bono automático"
# sin prometer que sea instantáneo).
REFERRAL_HOLD_HOURS: float = float(os.getenv("REFERRAL_HOLD_HOURS", "24"))

# Cada cuántos segundos handlers._release_referrals_loop revisa si hay
# bonos 'pending' que ya cumplieron REFERRAL_HOLD_HOURS. No hace falta
# que sea muy frecuente -a diferencia de PAYMENT_POLL_INTERVAL/
# SMS_POLL_INTERVAL, que sí necesitan reaccionar rápido a algo que el
# usuario está esperando en pantalla, acá nadie está mirando el reloj.
# Default 1 hora: suficiente resolución para un umbral de 24h sin generar
# carga extra en la base de datos.
REFERRAL_RELEASE_INTERVAL: int = int(os.getenv("REFERRAL_RELEASE_INTERVAL", str(3600)))

# ── Administración / Reportes ─────────────────────────────────────────────────
# Chat/canal donde el bot manda alertas en tiempo real (pago confirmado, venta
# completada, reembolsos, timeouts). Usa el ID de un canal o grupo privado
# donde hayas agregado al bot como admin. Dejar en 0 para desactivar alertas.
ADMIN_CHAT_ID: int = int(os.getenv("ADMIN_CHAT_ID", "0") or 0)

# ── Entrega de mensajes al usuario (outbox) ────────────────────────────────────
# Ver outbox.py. Problema que cubre: si bot.send_message falla (red caída,
# Telegram con problemas, rate limit), antes solo quedaba un log.error() y el
# mensaje se perdía para siempre — incluyendo avisos importantes como "ya se
# reintegró tu saldo" o "puedes reintentar con /start" tras un reinicio.
# Ahora ese mensaje se persiste en SQLite y se reintenta con backoff hasta
# entregarse o agotar OUTBOX_MAX_ATTEMPTS.

# Cada cuántos segundos revisa outbox.retry_loop si hay mensajes cuyo
# próximo intento ya venció.
OUTBOX_RETRY_INTERVAL_SECONDS: int = int(os.getenv("OUTBOX_RETRY_INTERVAL_SECONDS", "30"))

# Backoff entre reintentos, en segundos (crece progresivamente; el fallo más
# común -un corte de red de unos segundos- se resuelve en el 2do intento sin
# esperar minutos, pero si sigue fallando no insiste cada 30s para siempre).
# El último valor se repite si se necesitan más intentos que pasos hay acá.
OUTBOX_BACKOFF_SCHEDULE: list[int] = [30, 60, 120, 300, 600]  # 30s,1m,2m,5m,10m...

# Intentos antes de darse por vencido, marcar el mensaje 'dead' y avisar al
# admin (ver outbox._alert_admin_dead_message) en vez de reintentar para
# siempre sobre un chat quizás inalcanzable (usuario bloqueó al bot, etc.).
OUTBOX_MAX_ATTEMPTS: int = int(os.getenv("OUTBOX_MAX_ATTEMPTS", "8"))

# IDs de Telegram autorizados a usar los comandos de reportes (/stats, /ventas,
# /pendientes). Formato en .env: ADMIN_IDS=123456789,987654321
ADMIN_IDS: set[int] = {
    int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()
}

# Servicios que se mostrarán en el menú principal (código HeroSMS → nombre)
# Puedes ampliar esta lista o reemplazarla con una llamada a getServices
SERVICES: dict[str, str] = {
    "tg":      "Telegram",
    "wa":      "WhatsApp",
    "go":      "Gmail / Google",
    "ds":      "Discord",
    "fb":      "Facebook",
    "lf":      "TikTok",
    "in":      "Instagram",
    "tw":      "Twitter / X",
}

# ── Tarjeta de bienvenida (welcome_card.py) ────────────────────────────────────
# Problema que cubre: /start mandaba solo texto plano. La tarjeta personalizada
# (foto de perfil real + datos de la cuenta) es la primera impresión que recibe
# un reseller nuevo, así que vale la pena que se vea a la altura del bot.

ASSETS_DIR: str = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
LOGO_PATH: str = os.path.join(ASSETS_DIR, "otpvirtual_logo_circular.png")

FONT_DIR: str = os.path.join(ASSETS_DIR, "fonts")
FONT_BOLD: str = os.path.join(FONT_DIR, "DejaVuSans-Bold.ttf")
FONT_REG: str = os.path.join(FONT_DIR, "DejaVuSans.ttf")

# Fallback: si por lo que sea las fuentes empaquetadas en assets/fonts/ no
# están (ej. se armó el deploy sin copiar esa carpeta), se prueba con las
# rutas típicas donde Debian/Ubuntu instalan DejaVu vía el paquete
# fonts-dejavu-core, en vez de fallar directo con "cannot open resource".
# Esto es justo lo que rompió en el servidor real: asumir una ruta de
# sistema que no estaba garantizada.
if not os.path.isfile(FONT_BOLD):
    for _candidate_dir in (
        "/usr/share/fonts/truetype/dejavu",
        "/usr/share/fonts/dejavu",
    ):
        _b = os.path.join(_candidate_dir, "DejaVuSans-Bold.ttf")
        _r = os.path.join(_candidate_dir, "DejaVuSans.ttf")
        if os.path.isfile(_b) and os.path.isfile(_r):
            FONT_BOLD, FONT_REG = _b, _r
            break

CARD_W: int = 900

# Paleta de marca oficial de OTPVirtual (la misma del logo y de las tarjetas
# de ejemplo ya aprobadas). Un solo tema por ahora; si más adelante hace
# falta un modo claro, se puede parametrizar sin tocar la estructura del
# generador.
NAVY        = (13, 27, 42)      # #0D1B2A - header / footer
BLUE        = (21, 101, 192)    # #1565C0 - iconos / acentos / avatar genérico
CYAN        = (0, 194, 255)     # #00C2FF - highlights, título, borde de avatar
LIGHT_CYAN  = (110, 239, 255)   # #6EF7FF - detalles secundarios
LIGHT_GRAY  = (242, 244, 247)   # #F2F4F7 - fondo del cuerpo de la tarjeta
WHITE       = (255, 255, 255)
TEXT_DARK   = (30, 41, 59)
TEXT_MUTED  = (90, 100, 115)

# account_type todavía no existe como columna en la base de datos (ver
# database.py, tabla users) — este mapeo queda listo para cuando se agregue
# un sistema de tipos de cuenta/reseller; hoy simplemente no se usa y la
# tarjeta no muestra la fila "Tipo" (ver welcome_card.UserStats.account_type).
ACCOUNT_TYPE_LABELS: dict[str, str] = {
    "cliente":  "Cliente",
    "reseller": "Reseller",
    "vip":      "VIP",
}

TAGLINE: str = "Tu negocio, sin límites."

# Validaciones mínimas al arrancar
def validate():
    missing = []
    for var in ("BOT_TOKEN", "HEROSMS_API_KEY", "CCPAY_APP_ID", "CCPAY_APP_SECRET", "DATABASE_URL"):
        if not os.getenv(var):
            missing.append(var)
    if missing:
        raise EnvironmentError(
            f"Faltan variables de entorno obligatorias: {', '.join(missing)}\n"
            "Crea un archivo .env con esas variables."
        )