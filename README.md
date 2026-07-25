# 🤖 OTPVirtual — Bot Reseller de Números Virtuales (HeroSMS + CCPayment)

Bot de Telegram en Python (aiogram 3.x) que revende números virtuales para
recepción de SMS/OTP. Usa **HeroSMS** como proveedor de números y
**CCPayment (API v2)** para cobrar en cripto. Además del flujo de compra
tiene **saldo interno** (recarga y retiro, tanto en cripto como en CUP vía
Transfermóvil/EnZona con aprobación manual), **sistema de referidos**,
**reembolsos post-entrega** y controles **antiabuso**.

> ⚠️ Este README fue regenerado a partir del código actual
> (`handlers.py`, `utils.py`, `database.py`, `config.py`). Los archivos
> `main.py`, `herosms_api.py`, `ccpay_api.py`, `outbox.py`,
> `telegram_sender.py`, `backup_task.py` y `welcome_card.py` no se
> revisaron línea por línea para esta actualización — sus secciones abajo
> están basadas en cómo se usan/importan desde `handlers.py` y en los
> comentarios de `config.py`, así que si algo no calza, esos son los
> archivos a mirar primero.

---

## 📁 Estructura del proyecto

```
sms_reseller_bot/
├── main.py              # Punto de entrada: logging, arranca el bot (polling o webhook), registra routers
├── config.py             # Variables de entorno y constantes (ver sección 3)
├── herosms_api.py         # Cliente async para la API de HeroSMS
├── ccpay_api.py            # Cliente async para CCPayment API v2 (firma HMAC-SHA256)
├── database.py              # Clase async para Postgres/Neon (asyncpg), todas las tablas del negocio
├── handlers.py                # Controladores FSM: compra, saldo, retiros/depósitos, referidos, reembolsos, admin
├── utils.py                    # Teclados inline, mensajes (MSG_*), formateo, helpers de CUP/cripto
├── outbox.py                    # Cola de reintento para mensajes al usuario que fallaron al enviarse
├── telegram_sender.py             # Helpers de envío a Telegram usados por handlers/outbox
├── backup_task.py                  # Health-check periódico de la conexión a Postgres + keep-alive
├── welcome_card.py                  # Genera la tarjeta de bienvenida (imagen) que se manda en /start
├── assets/                            # Logo y fuentes usadas por welcome_card.py
├── requirements.txt
└── .env.example
```

No hay archivo `.db` local: la persistencia es **Postgres (Neon)**, no
SQLite (ver sección 5).

---

## ⚙️ 1. Requisitos previos

- Python 3.10+ (recomendado 3.11)
- Un VPS Linux (Ubuntu 22.04/24.04) **o** una cuenta en [Render](https://render.com) (ver sección 4)
- Un token de bot de Telegram (vía [@BotFather](https://t.me/BotFather))
- Cuenta y credenciales de API en **HeroSMS**
- Cuenta de comercio en **CCPayment** (API v2) con saldo para poder ejecutar reembolsos/retiros
- Un proyecto Postgres en [Neon](https://console.neon.tech) (o cualquier Postgres accesible por URL)

---

## 🔑 2. Obtener las claves de API

### HeroSMS

1. Regístrate en HeroSMS y recarga saldo (necesario para comprar números).
2. Copia tu `api_key` desde el panel.
3. `herosms_api.py` asume un endpoint único estilo SMS-Activate
   (`<HEROSMS_API_URL>/stubs/handler_api.php`) — **no** incluyas `/api` ni
   `/stubs` en `HEROSMS_API_URL`, ya se agregan solos.

### CCPayment (API v2)

La integración usa **API v2**, no v1:

1. Regístrate como comercio y consigue tu `Appid`/`AppSecret` desde
   [admin.ccpayment.com/developer/config](https://admin.ccpayment.com/developer/config).
2. La firma es **HMAC-SHA256** sobre `Appid + Timestamp + body_json` (con
   `AppSecret` como key), **no** el SHA-256 plano de v1.
3. El `Timestamp` va en **milisegundos** (v1 usaba segundos).
4. Los tokens ya no se identifican con un `token_id` único: ahora es
   `coinId` (entero) + `chain` (string, ej. `"TRX"`, `"POLYGON"`).
5. Asegúrate de tener saldo suficiente en la moneda de liquidación (ver
   `WITHDRAWAL_ALLOWED_CURRENCIES` en la sección 5) para poder pagar
   retiros y reembolsos automáticos.

> ⚠️ Verifica siempre los nombres exactos de campos/endpoints contra la
> documentación oficial vigente de HeroSMS y CCPayment antes de producción.

---

## 🛠️ 3. Instalación

```bash
cd /opt
mkdir sms_reseller_bot && cd sms_reseller_bot
# (copia aquí todos los archivos .py, assets/, requirements.txt, .env.example)

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
nano .env
```

### Variables de entorno principales (`.env`)

| Variable | Descripción | Por defecto |
|---|---|---|
| `BOT_TOKEN` | Token del bot de Telegram | — (obligatorio) |
| `HEROSMS_API_KEY` | API key de HeroSMS | — (obligatorio) |
| `HEROSMS_API_URL` | Base de la API de HeroSMS | `https://hero-sms.com` |
| `CCPAY_APP_ID` | Appid de CCPayment (API v2) | — (obligatorio) |
| `CCPAY_APP_SECRET` | AppSecret de CCPayment (API v2) | — (obligatorio) |
| `CCPAY_API_URL` | Base de la API de CCPayment | `https://ccpayment.com/ccpayment/v2` |
| `DATABASE_URL` | Connection string de Postgres/Neon (`?sslmode=require`) | — (obligatorio) |
| `ADMIN_IDS` | IDs de Telegram con acceso a comandos de admin, separados por coma | — |
| `ADMIN_CHAT_ID` | Chat/grupo donde llegan los avisos de admin (0 = desactivado) | `0` |
| `MARKUP` | Multiplicador de precio sobre el costo de HeroSMS | `2.0` |
| `PAYMENT_TIMEOUT_SECONDS` | Tiempo límite para pagar | `900` |
| `SMS_TIMEOUT_SECONDS` | Tiempo límite para recibir SMS | `600` |
| `PAYMENT_POLL_INTERVAL` / `SMS_POLL_INTERVAL` | Frecuencia de polling | `8` / `5` seg |
| `REFUND_FEE_PCT` | % no reembolsable si se asignó número y no se completó | `0.10` |
| `ABUSE_MAX_STRIKES` / `ABUSE_WINDOW_HOURS` / `ABUSE_BLOCK_HOURS` | Antiabuso (ver sección 6) | `3` / `24` / `24` |
| `WITHDRAWAL_FEE_PCT` | Comisión sobre retiro de saldo interno a cripto | `0.05` |
| `WITHDRAWAL_MIN_USD` | Mínimo para retirar a cripto | `1.0` |
| `CUP_WITHDRAWAL_MIN_USD` | Mínimo para retirar en CUP | `0.0` |
| `WITHDRAWAL_ALLOWED_CURRENCIES` | Monedas habilitadas como destino de retiro (ver nota abajo) | `USDT` |
| `DEPOSIT_MIN_USD` | Mínimo para depositar (cripto) | `1.0` |
| `MANUAL_PAYMENT_METHODS` | Métodos CUP disponibles, formato `code:nombre:cuenta\|code2:...` | Transfermóvil + EnZona |
| `MANUAL_DEPOSIT_CUP_RATE` | Tasa informal USDT/CUP de referencia | `960` |
| `MANUAL_DEPOSIT_CUP_MARGIN_PCT` | Margen sobre la tasa de referencia que ve el cliente | `0.04` |
| `MANUAL_DEPOSIT_CUP_EXPOSURE_ALERT_USD` | Umbral de CUP sin convertir antes de alertar al admin | `50.0` |
| `MANUAL_DEPOSIT_MIN_USD` / `MANUAL_DEPOSIT_MAX_USD` | Rango por depósito manual | `1.0` / `10.0` |
| `MANUAL_PURCHASE_MIN_USD` | Piso de cobro para una compra pagada directo en CUP | `0.30` |
| `REFERRAL_BONUS_PCT` | % de bono al referidor sobre la primera compra del referido | `0.10` |
| `REFERRAL_MIN_PURCHASE_USD` | Piso de compra para que aplique el bono | `0.20` |
| `REFERRAL_HOLD_HOURS` | Horas de gracia antes de liberar el bono | `24` |
| `COMMUNITY_CHANNEL_URL` / `COMMUNITY_CHANNEL_CHAT_ID` | Canal de comunidad (opcional) | — |
| `COMMUNITY_GROUP_URL` | Grupo de soporte entre usuarios (opcional) | — |
| `OUTBOX_RETRY_INTERVAL_SECONDS` / `OUTBOX_MAX_ATTEMPTS` | Reintento de mensajes fallidos al usuario | `30` / `8` |
| `DB_PING_INTERVAL_MINUTES` | Keep-alive de Postgres (evita cold start en plan free de Neon) | `10` |
| `PORT` / `WEBHOOK_HOST` / `WEBHOOK_PATH` / `WEBHOOK_SECRET` | Solo si se despliega con webhook (ver sección 4) | — |

> **Nota sobre `WITHDRAWAL_ALLOWED_CURRENCIES`:** CCPayment consolida el
> saldo del merchant por símbolo, no de forma global — si la mayoría de lo
> que entra es en TRX, no necesariamente hay ETH/SOL reales para pagar un
> retiro en esas monedas aunque el saldo interno (en USD) del usuario
> alcance. Por eso el retiro se restringe a un set chico de monedas de
> liquidación que el negocio mantiene fondeadas a propósito (recomendado:
> activar auto-convert a stablecoin en el dashboard de CCPayment).

Revisa `config.py` para el resto de variables (branding de la tarjeta de
bienvenida, tipos de cuenta, etc.) — todas tienen comentarios explicando el
porqué.

---

## ▶️ 4. Ejecución

### Modo polling (pruebas / VPS propio)

```bash
source venv/bin/activate
python3 main.py
```

Si `WEBHOOK_HOST` no está seteado, el bot corre en **long polling**.

### Modo webhook (Render, plan free)

Render no ofrece "background worker" gratis, solo web services — que
necesitan escuchar HTTP. Si se define `WEBHOOK_HOST` (o si el bot corre en
Render, que expone `RENDER_EXTERNAL_URL` automáticamente), `main.py` sirve
los updates de Telegram por webhook en vez de polling. No suele hacer
falta setear `WEBHOOK_HOST` a mano en Render.

### Modo producción con `systemd` (VPS propio)

```ini
[Unit]
Description=Bot Telegram OTPVirtual
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/sms_reseller_bot
ExecStart=/opt/sms_reseller_bot/venv/bin/python3 main.py
Restart=always
RestartSec=5
StandardOutput=append:/opt/sms_reseller_bot/systemd.log
StandardError=append:/opt/sms_reseller_bot/systemd.log

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable sms-bot
sudo systemctl start sms-bot
journalctl -u sms-bot -f
```

---

## 🗃️ 5. Base de datos

**Postgres (Neon), vía `asyncpg`** — ya no es SQLite. El esquema se crea y
migra solo al arrancar (`CREATE TABLE IF NOT EXISTS` / `ALTER TABLE ADD
COLUMN IF NOT EXISTS`), sin herramienta de migraciones externa. Tablas
principales (`database.py`):

| Tabla | Para qué |
|---|---|
| `transactions` | Cada compra de número (costo HeroSMS, precio cobrado, estado, comprobante si se pagó en CUP) |
| `users` | Perfil de Telegram, tipo de cuenta, referido por, fechas de contacto |
| `balances` / `balance_ledger` | Saldo interno por usuario, separado en bolsa "cripto" y bolsa "CUP", con historial de cada movimiento |
| `deposits` | Depósitos de saldo pagados vía CCPayment (cripto) |
| `manual_deposits` | Depósitos CUP (Transfermóvil/EnZona) pendientes de aprobación manual |
| `manual_withdrawals` | Retiros de saldo CUP a CUP real, aprobación manual |
| `payment_methods` | Cuentas CUP activas/editables desde Telegram (ver `/metodos`) |
| `referrals` | Bonos de referidos, con período de gracia antes de liberarse |
| `refund_requests` | Reclamos de reembolso post-entrega |
| `outbox` | Cola de reintento para mensajes al usuario que fallaron al enviarse |

Neon gestiona sus propios backups/point-in-time recovery — no hay backup
local a archivo. Lo que sí corre en background es un ping periódico
(`DB_PING_INTERVAL_MINUTES`) para detectar cortes de conexión y evitar que
el compute de Neon (plan free) se duerma por inactividad.

---

## 🧩 6. Flujo implementado

### Compra de número (cripto)

1. `/start` → menú principal.
2. Selección de servicio y país (`getCountries` en HeroSMS).
3. El bot consulta a CCPayment qué monedas/redes soporta y su cotización
   en tiempo real; el usuario elige con cuál pagar.
4. Se crea la orden CCPayment, se pide la dirección de origen del usuario
   (para poder reembolsar si algo falla) y se hace polling del pago.
5. Confirmado el pago → `getNumber` en HeroSMS. Sin stock → reembolso
   automático en la misma moneda/red que pagó.
6. Polling de SMS hasta recibir el código o agotar el timeout. Timeout →
   `cancel` en HeroSMS + reembolso (con `REFUND_FEE_PCT` retenido si ya se
   había asignado número, ver antiabuso más abajo).

### Compra pagada directo en CUP

Mismo flujo, pero el pago se resuelve por aprobación manual del admin
(comprobante + revisión) en vez de CCPayment, con un piso de cobro
(`MANUAL_PURCHASE_MIN_USD`) para que la venta valga la pena revisar.

### Saldo interno (`/saldo`)

- **Recargar en cripto**: vía CCPayment, se acredita 100% de lo pagado.
- **Recargar en CUP**: transferencia manual (Transfermóvil/EnZona) con
  código de referencia, comprobante y aprobación de admin; si el monto
  declarado no coincide con lo pedido, la aprobación exige un segundo
  clic explícito del admin en vez de acreditar de largada.
- **Retirar a cripto**: descuenta la bolsa "cripto", con `WITHDRAWAL_FEE_PCT`
  y `WITHDRAWAL_MIN_USD`, restringido a `WITHDRAWAL_ALLOWED_CURRENCIES`.
- **Retirar en CUP**: descuenta la bolsa "CUP", aprobación manual del
  admin (transferencia real fuera del bot); el usuario puede cancelar su
  propia solicitud mientras esté pendiente.

### Referidos (`/referidos`)

Bono en saldo (bolsa "cripto") al referidor cuando el referido completa su
primera compra sobre `REFERRAL_MIN_PURCHASE_USD`, con `REFERRAL_HOLD_HOURS`
de gracia antes de liberarse (para no pagar un bono sobre una compra que
termina reembolsada).

### Reembolsos (`/reembolso`)

Reclamo post-entrega (número ya asignado) revisado manualmente por un
admin, con `REFUND_FEE_PCT` de comisión de red no reembolsable.

### Antiabuso

Un usuario que acumula `ABUSE_MAX_STRIKES` operaciones "número asignado y
no completado" dentro de `ABUSE_WINDOW_HOURS` queda bloqueado para nuevas
compras por `ABUSE_BLOCK_HOURS`.

Todo el flujo usa **FSM** de aiogram para no perder el contexto del
usuario, con tareas `asyncio.create_task` que no bloquean el bot mientras
esperan pago o SMS.

---

## 🛡️ 7. Comandos de administración

Requieren estar en `ADMIN_IDS` y (salvo excepciones marcadas) ejecutarse
en chat **privado** con el bot, no en el grupo:

| Comando | Qué hace |
|---|---|
| `/stats [días]` | Estadísticas de ventas por estado |
| `/ventas [n]` | Últimas n ventas |
| `/pendientes` | Depósitos/retiros/reembolsos esperando revisión |
| `/detalle` | Detalle de una operación puntual |
| `/exposicion_cup` | Cuánto CUP aprobado sigue sin convertir a USDT real |
| `/convertido` | Marca depósitos CUP como ya convertidos |
| `/metodos` / `/set_metodo` / `/quitar_metodo` | Gestión de cuentas CUP activas |
| `/set_tipo` / `/set_pais` | Ajustes de cuenta de usuario |
| `/anunciar` | Difusión a la base de usuarios |
| `/chatid` | Devuelve el chat_id (útil para configurar `ADMIN_CHAT_ID`/`COMMUNITY_CHANNEL_CHAT_ID`) |

---

## ⚠️ 8. Puntos importantes antes de producción

1. **Verifica los contratos reales de las APIs** de HeroSMS y CCPayment v2
   contra su documentación oficial vigente antes de operar con dinero real.
2. **Saldo de liquidación:** mantené saldo suficiente en
   `WITHDRAWAL_ALLOWED_CURRENCIES` para poder pagar retiros y reembolsos
   automáticos sin que fallen por falta de fondos reales en esa moneda.
3. **Concurrencia / FSM:** si el bot corre con `MemoryStorage` de aiogram
   (estado en RAM), un reinicio en medio de una operación pierde el
   contexto de esos usuarios. Si esto sigue siendo así, considera migrar a
   un storage persistente (Redis/Postgres) para operación seria.
4. **Rate limits:** ajusta `PAYMENT_POLL_INTERVAL`/`SMS_POLL_INTERVAL` si
   HeroSMS o CCPayment aplican límites de peticiones.
5. **Depósitos/retiros manuales en CUP:** dependen de revisión humana —
   dimensiona el tiempo de respuesta del admin en función del volumen
   esperado, y revisá `MANUAL_DEPOSIT_CUP_EXPOSURE_ALERT_USD` según cuánto
   CUP sin convertir estás dispuesto a tolerar.
6. **Cumplimiento legal:** verifica que la reventa de SMS/OTP y el
   procesamiento de pagos cripto/CUP sean conformes con las regulaciones
   de tu jurisdicción (KYC/AML según corresponda).

---

## 🧪 9. Cómo probar sin gastar saldo real

1. Mockea `herosms_api.get_countries/get_number/get_status` para devolver
   datos de prueba fijos.
2. Mockea `ccpay_api.create_order/get_order_status` para simular un pago
   confirmado tras unos segundos.
3. Para el flujo CUP, podés probar el circuito completo sin plata real:
   pedís el depósito/retiro, mandás cualquier comprobante, y aprobás/
   rechazás vos mismo como admin desde el chat.

---

## 📞 Soporte

Si algún endpoint de HeroSMS o CCPayment no coincide exactamente con lo
implementado (nombres de parámetros, formato de respuesta, headers de
autenticación), ajustá `herosms_api.py` / `ccpay_api.py` — el resto del
bot (FSM, base de datos, lógica de negocio) no debería necesitar cambios
por eso.
