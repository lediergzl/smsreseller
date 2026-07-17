# 🤖 Bot Reseller de Números Virtuales (HeroSMS + CCPay/USDT)

Bot de Telegram en Python (aiogram 3.x) que revende números virtuales para
recepción de SMS/OTP. Usa **HeroSMS** como proveedor de números y **CCPay
(CCTip)** para cobrar en USDT (red TRC-20).

---

## 📁 Estructura del proyecto

```
sms_reseller_bot/
├── main.py            # Punto de entrada: logging, inicia el bot, registra routers
├── config.py           # Carga de variables de entorno y constantes
├── herosms_api.py       # Cliente async para la API de HeroSMS
├── ccpay_api.py         # Cliente async para la API de CCPay (CCTip)
├── database.py           # Clase SQLite para persistir transacciones
├── handlers.py            # Controladores FSM (comandos y callbacks)
├── utils.py                # Teclados, formateo, mensajes y helpers de asyncio
├── requirements.txt
├── .env.example
└── sms_reseller.db        # (se crea automáticamente al ejecutar)
```

---

## ⚙️ 1. Requisitos previos

- Python 3.10+ (recomendado 3.11)
- Un VPS Linux (Ubuntu 22.04/24.04 recomendado) con acceso a internet saliente
- Un token de bot de Telegram (vía [@BotFather](https://t.me/BotFather))
- Cuenta y credenciales de API en **HeroSMS**
- Cuenta y credenciales de **CCPay / CCTip** con saldo para poder emitir reembolsos

---

## 🔑 2. Obtener las claves de API

### HeroSMS

1. Regístrate en [https://herosms.com](https://herosms.com).
2. Recarga saldo en tu cuenta (necesario para comprar números).
3. Ve a tu **Panel de usuario → API / Configuración** y copia tu `api_key`.
4. Verifica en la documentación oficial de HeroSMS (sección "API") los
   nombres exactos de los parámetros de cada endpoint (`getServices`,
   `getCountries`, `getNumber`, `getStatus`, `setStatus`, `cancel`), ya que
   pueden variar ligeramente entre versiones de la API. Ajusta los nombres
   de campos en `herosms_api.py` si difieren de los asumidos en este código
   (por ejemplo, si el campo de país se llama `country_id` en vez de
   `country`).

### CCPay / CCTip

1. Regístrate como comercio (merchant) en la plataforma de CCPay/CCTip.
2. Solicita/genera tu `businessKey` y `businessSecret` desde el panel de
   comerciante.
3. Asegúrate de tener saldo en USDT en tu cuenta CCPay para poder ejecutar
   reembolsos (`/v1/merchant/transfer`), ya que ese saldo es distinto del
   saldo que reciben tus clientes.
4. Revisa la documentación oficial de CCPay para confirmar el formato
   exacto de firma/autenticación de las peticiones. Este código asume
   autenticación por `businessKey` + `businessSecret` en el cuerpo JSON;
   si CCPay requiere una firma HMAC adicional, debes agregarla en la
   función `_post()` de `ccpay_api.py`.

> ⚠️ **Importante:** Los nombres de campos y endpoints exactos (`orderId`
> vs `order_id`, `payAddress` vs `pay_address`, etc.) pueden variar según
> la versión de la API que te entreguen. El código incluye manejo
> defensivo (`data.get("orderId") or data.get("order_id")`) pero debes
> validar con la documentación real antes de pasar a producción.

---

## 🛠️ 3. Instalación

```bash
# 1. Clona o copia los archivos del proyecto a tu VPS
cd /opt
mkdir sms_reseller_bot && cd sms_reseller_bot
# (copia aquí todos los archivos .py, requirements.txt, .env.example)

# 2. Crea un entorno virtual
python3 -m venv venv
source venv/bin/activate

# 3. Instala dependencias
pip install -r requirements.txt

# 4. Configura las variables de entorno
cp .env.example .env
nano .env   # Rellena BOT_TOKEN, HEROSMS_API_KEY, CCPAY_BUSINESS_KEY, CCPAY_BUSINESS_SECRET
```

### Variables de entorno (`.env`)

| Variable | Descripción | Por defecto |
|---|---|---|
| `BOT_TOKEN` | Token del bot de Telegram | — (obligatorio) |
| `HEROSMS_API_KEY` | API key de HeroSMS | — (obligatorio) |
| `HEROSMS_API_URL` | Base URL de la API de HeroSMS | `https://herosms.com/api` |
| `CCPAY_BUSINESS_KEY` | Business key de CCPay | — (obligatorio) |
| `CCPAY_BUSINESS_SECRET` | Business secret de CCPay | — (obligatorio) |
| `CCPAY_API_URL` | Base URL de CCPay | `https://api.ccpay.ai` |
| `MARKUP` | Multiplicador de precio (margen) | `2.0` |
| `PAYMENT_TIMEOUT_SECONDS` | Tiempo límite para pagar | `900` (15 min) |
| `SMS_TIMEOUT_SECONDS` | Tiempo límite para recibir SMS | `600` (10 min) |
| `PAYMENT_POLL_INTERVAL` | Frecuencia de verificación de pago | `8` seg |
| `SMS_POLL_INTERVAL` | Frecuencia de verificación de SMS | `5` seg |

---

## ▶️ 4. Ejecución

### Modo manual (pruebas)

```bash
source venv/bin/activate
python3 main.py
```

### Modo producción con `systemd` (recomendado para VPS)

Crea el archivo `/etc/systemd/system/sms-bot.service`:

```ini
[Unit]
Description=Bot Telegram Reseller de Numeros Virtuales
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

Luego:

```bash
sudo systemctl daemon-reload
sudo systemctl enable sms-bot
sudo systemctl start sms-bot
sudo systemctl status sms-bot

# Ver logs en vivo
journalctl -u sms-bot -f
```

---

## 🗃️ 5. Base de datos

El bot crea automáticamente `sms_reseller.db` (SQLite) en el directorio de
trabajo, con la tabla `transactions`. Esto permite:

- Llevar registro contable de cada operación (costo HeroSMS vs. cobro al usuario).
- Auditar reembolsos.
- Recuperar contexto si el bot se reinicia (ver `recover_pending_transactions`
  en `main.py` — notifica a usuarios con operaciones interrumpidas).

Puedes inspeccionar la base con:

```bash
sqlite3 sms_reseller.db "SELECT id, user_id, service, country, amount_usdt, status FROM transactions ORDER BY id DESC LIMIT 20;"
```

---

## 🧩 6. Flujo implementado

1. `/start` → menú principal con botón **Comprar número virtual**.
2. Selección de servicio → consulta `getCountries` en HeroSMS.
3. Selección de país → se crea la transacción en SQLite (precio base en USD,
   con markup ya aplicado).
4. **Selección de moneda de pago** → el bot consulta dinámicamente a CCPay
   qué monedas/redes soporta (`get_supported_currencies`) y pide la
   cotización actual de cada una para el precio en USD
   (`get_estimated_amount`). El usuario ve el equivalente real en USDT, BTC,
   ETH, etc. y elige con cuál pagar.
5. Se crea la orden CCPay (`createOrder`) ya en la moneda/red elegida,
   mostrando dirección + monto exacto a pagar.
6. Se solicita la dirección del usuario **en esa misma moneda/red** (para
   poder reembolsar correctamente si algo falla).
7. Polling de pago (`getOrderStatus`) cada `PAYMENT_POLL_INTERVAL` segundos,
   máximo `PAYMENT_TIMEOUT_SECONDS`.
8. Al confirmarse el pago → `getNumber` en HeroSMS.
   - Si falla (sin stock) → reembolso automático en la moneda/red original
     vía `ccpay_transfer` y aviso al usuario.
9. Si se obtiene número → se muestra al usuario y arranca el polling de SMS
   (`getStatus`) cada `SMS_POLL_INTERVAL` segundos, máximo `SMS_TIMEOUT_SECONDS`.
10. Al recibir el código → se envía al usuario y se llama `setStatus` con
    estado `6` (completado).
11. Si se agota el tiempo sin código → `cancel` en HeroSMS + reembolso
    automático al usuario, en la misma moneda/red que pagó.

Todo el flujo usa **FSM** (`PurchaseFlow`) para no perder el contexto del
usuario, y temporizadores con `asyncio.create_task` que no bloquean el bot
mientras esperan pago o SMS (otros usuarios pueden seguir usando el bot
simultáneamente).

### 💱 Multi-moneda: detalles importantes

- El precio del servicio siempre se calcula y guarda **en USD** (`amount_usd`
  en la tabla `transactions`), con el markup ya aplicado. Esto te da un
  registro contable estable independiente de la volatilidad cripto.
- La cotización a la cripto elegida se pide a CCPay en tiempo real
  (`get_estimated_amount`), no se calcula con un precio fijo local, para
  evitar pérdidas por fluctuación de precio entre la consulta y el pago.
- El reembolso (`refund_user`) siempre se hace **en la misma `currency` y
  `network`** con las que el usuario pagó — nunca se reembolsa en una
  moneda distinta, para evitar pérdidas por conversión y para que la
  dirección de destino sea válida en esa red.
- Si `get_supported_currencies()` o `get_estimated_amount()` fallan, hay un
  fallback estático (`FALLBACK_CURRENCIES` en `ccpay_api.py`) solo como
  último recurso — ajusta esa lista a las monedas que realmente quieras
  ofrecer si tu integración con CCPay llegara a caerse.

---

## ⚠️ 7. Puntos importantes antes de producción

1. **Verifica los contratos reales de las APIs.** Los nombres de campos de
   HeroSMS y CCPay en este código son una interpretación razonable de tu
   especificación, pero debes confirmarlos contra la documentación oficial
   actual y ajustar `herosms_api.py` / `ccpay_api.py` si difieren. Esto
   aplica especialmente a los endpoints nuevos de moneda:
   `/v1/merchant/currency/list` (monedas soportadas) y
   `/v1/merchant/rate/estimate` (cotización USD → cripto) — confirma los
   nombres reales de estos endpoints con CCPay, ya que el código asume
   nombres razonables pero no documentados en tu mensaje original.
2. **Autenticación CCPay:** si la API exige firma HMAC-SHA256 sobre el
   payload (común en gateways de pago cripto), añade esa lógica en
   `ccpay_api._post()` antes de salir a producción — actualmente solo se
   manda `businessKey`/`businessSecret` en el body.
3. **Saldo de reembolso:** asegúrate de mantener saldo USDT suficiente en tu
   cuenta CCPay para poder ejecutar `refund_user()` automáticamente; si
   falla, el código deja log de "reembolso manual pendiente" para que lo
   proceses tú mismo.
4. **Concurrencia:** con `MemoryStorage` de aiogram, el estado FSM vive en
   RAM. Si reinicias el bot con operaciones en curso, esos usuarios deberán
   contactar soporte (el bot los notifica automáticamente al arrancar, ver
   `recover_pending_transactions`). Para alta concurrencia/producción seria,
   considera migrar a `RedisStorage`.
5. **Rate limits:** ajusta `PAYMENT_POLL_INTERVAL` y `SMS_POLL_INTERVAL` si
   HeroSMS o CCPay aplican límites de peticiones por segundo/minuto.
6. **Cumplimiento legal:** verifica que la actividad de reventa de SMS/OTP
   y el procesamiento de pagos cripto sean conformes con las regulaciones
   de tu jurisdicción (KYC/AML según corresponda).

---

## 🧪 8. Cómo probar sin gastar saldo real

Antes de operar con dinero real, puedes:

1. Mockear `herosms_api.get_countries/get_number/get_status` para devolver
   datos de prueba fijos.
2. Mockear `ccpay_api.create_order/get_order_status` para simular un pago
   confirmado tras unos segundos.

Esto te permite validar el flujo de FSM y la base de datos sin tocar las
APIs reales.

---

## 📞 Soporte

Si algún endpoint de HeroSMS o CCPay no coincide exactamente con lo aquí
implementado (nombres de parámetros, formato de respuesta, headers de
autenticación), ajusta únicamente `herosms_api.py` y `ccpay_api.py` — el
resto del bot (FSM, base de datos, lógica de negocio) no necesita cambios.
