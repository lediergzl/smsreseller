"""
database.py - Manejo de base de datos PostgreSQL (Neon) para transacciones y
estado del bot.

MIGRADO A ASYNCPG: este módulo antes usaba `psycopg2` (síncrono/bloqueante)
por dentro de un pool sincrónico (`SimpleConnectionPool`). El problema real
que eso causaba: aiogram corre TODO en un único event loop (un solo hilo).
Cada llamada a `db.algo(...)` desde un handler `async def` se quedaba
esperando la respuesta de red de Neon de forma bloqueante, y mientras esa
espera duraba, el event loop entero quedaba congelado — no solo para el
usuario que disparó esa acción, sino para TODOS los usuarios del bot al
mismo tiempo (nadie recibía nada hasta que esa query terminara). Esto se
notaba peor todavía cuando el compute de Neon (plan free) estaba
"dormido" tras un rato de inactividad: el primer query después de la
pausa podía tardar varios segundos en despertarlo, y ese tiempo entero
bloqueaba el bot completo para cualquier otro usuario.

Con `asyncpg` cada método de esta clase es `async def` y usa un pool
async (`asyncpg.create_pool`): mientras una query espera la red, el event
loop queda libre para seguir atendiendo a otros usuarios en paralelo. Cada
handler que llama a `db.algo(...)` ahora hace `await db.algo(...)`.

DIFERENCIAS DE INTERFAZ respecto a la versión psycopg2 (importante para
quien toque este archivo más adelante):
  - TODOS los métodos públicos son ahora `async def` → hay que hacer
    `await db.metodo(...)` en cada lugar donde se llaman.
  - Antes de usar `db` por primera vez hay que llamar una vez
    `await db.connect()` (crea el pool y las tablas). Se llama desde
    main.py al arrancar. Los scripts sueltos (fix_missing_refund.py, etc.)
    también deben llamar `await db.connect()` antes de usar `db`.
  - asyncpg usa placeholders posicionales `$1, $2, ...` en vez de `%s`.
  - `conn.fetchrow(...)` reemplaza a `cursor.fetchone()`, `conn.fetch(...)`
    reemplaza a `cursor.fetchall()`. Ambos devuelven `asyncpg.Record`
    (se convierte a dict normal con `dict(record)`).
  - No existe `cursor.rowcount`: para saber si un UPDATE afectó alguna
    fila se parsea el "command tag" que devuelve `conn.execute(...)`
    (ej. "UPDATE 1") con `_affected_rows(...)`.
"""
import logging
from datetime import datetime, timedelta
from typing import Optional

import asyncpg

import config

logger = logging.getLogger(__name__)

# Statements DDL, uno por uno (mismo motivo que en la versión psycopg2: más
# simple y confiable que confiar en que un solo execute() corra un script
# gigante con varios ";" de forma correcta).
_DDL_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS transactions (
        id                   BIGSERIAL PRIMARY KEY,
        user_id              BIGINT NOT NULL,
        order_id             TEXT UNIQUE,
        activation_id        TEXT,
        service              TEXT NOT NULL,
        service_name         TEXT,
        country              TEXT NOT NULL,
        country_name         TEXT,
        cost_herosms         DOUBLE PRECISION,
        amount_usd           DOUBLE PRECISION,
        currency             TEXT,
        network              TEXT,
        pay_amount           DOUBLE PRECISION,
        pay_address          TEXT,
        token_id             TEXT,
        refund_address       TEXT,
        phone_number         TEXT,
        sms_code             TEXT,
        status               TEXT DEFAULT 'pending',
        proof_file_id        TEXT,
        proof_file_unique_id TEXT,
        proof_text           TEXT,
        created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_user_id    ON transactions(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_order_id   ON transactions(order_id)",
    "CREATE INDEX IF NOT EXISTS idx_activation ON transactions(activation_id)",
    """
    CREATE TABLE IF NOT EXISTS users (
        user_id           BIGINT PRIMARY KEY,
        username          TEXT,
        first_name        TEXT,
        last_name         TEXT,
        language_code     TEXT,
        is_premium        SMALLINT,
        phone_number      TEXT,
        phone_verified_at TIMESTAMPTZ,
        account_type      TEXT,
        country           TEXT,
        first_seen        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        last_seen         TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS balances (
        user_id         BIGINT PRIMARY KEY,
        balance_usd     DOUBLE PRECISION NOT NULL DEFAULT 0,
        balance_usd_cup DOUBLE PRECISION NOT NULL DEFAULT 0,
        updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS balance_ledger (
        id         BIGSERIAL PRIMARY KEY,
        user_id    BIGINT NOT NULL,
        tx_id      BIGINT,
        delta_usd  DOUBLE PRECISION NOT NULL,
        origin     TEXT DEFAULT 'crypto',
        reason     TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ledger_user ON balance_ledger(user_id)",
    """
    CREATE TABLE IF NOT EXISTS manual_withdrawals (
        id             BIGSERIAL PRIMARY KEY,
        user_id        BIGINT NOT NULL,
        method         TEXT NOT NULL,
        destination    TEXT NOT NULL,
        amount_usd     DOUBLE PRECISION NOT NULL,
        fee_usd        DOUBLE PRECISION NOT NULL,
        net_usd        DOUBLE PRECISION NOT NULL,
        amount_cup     BIGINT NOT NULL,
        cup_rate       DOUBLE PRECISION,
        reference_code TEXT UNIQUE,
        status         TEXT DEFAULT 'pending_review',
        reviewed_by    BIGINT,
        created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_manual_wd_user   ON manual_withdrawals(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_manual_wd_status ON manual_withdrawals(status)",
    """
    CREATE TABLE IF NOT EXISTS deposits (
        id          BIGSERIAL PRIMARY KEY,
        user_id     BIGINT NOT NULL,
        order_id    TEXT UNIQUE,
        amount_usd  DOUBLE PRECISION,
        currency    TEXT,
        network     TEXT,
        pay_amount  DOUBLE PRECISION,
        pay_address TEXT,
        token_id    TEXT,
        status      TEXT DEFAULT 'pending',
        created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_deposit_user  ON deposits(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_deposit_order ON deposits(order_id)",
    """
    CREATE TABLE IF NOT EXISTS manual_deposits (
        id                   BIGSERIAL PRIMARY KEY,
        user_id              BIGINT NOT NULL,
        method               TEXT NOT NULL,
        amount_usd           DOUBLE PRECISION NOT NULL,
        amount_cup           BIGINT,
        cup_rate             DOUBLE PRECISION,
        reference_code       TEXT UNIQUE,
        proof_file_id        TEXT,
        proof_text           TEXT,
        proof_file_unique_id TEXT,
        status               TEXT DEFAULT 'awaiting_proof',
        reviewed_by          BIGINT,
        converted_to_usdt    SMALLINT DEFAULT 0,
        created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_manual_dep_user   ON manual_deposits(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_manual_dep_status ON manual_deposits(status)",
    """
    CREATE TABLE IF NOT EXISTS outbox (
        id              BIGSERIAL PRIMARY KEY,
        chat_id         BIGINT NOT NULL,
        text            TEXT NOT NULL,
        reply_markup    TEXT,
        status          TEXT DEFAULT 'pending',
        attempts        INTEGER DEFAULT 0,
        last_error      TEXT,
        next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_outbox_status       ON outbox(status)",
    "CREATE INDEX IF NOT EXISTS idx_outbox_next_attempt  ON outbox(next_attempt_at)",
    """
    CREATE TABLE IF NOT EXISTS payment_methods (
        code        TEXT PRIMARY KEY,
        name        TEXT NOT NULL,
        account     TEXT NOT NULL,
        active      BOOLEAN NOT NULL DEFAULT TRUE,
        sort_order  INTEGER NOT NULL DEFAULT 0,
        updated_by  BIGINT,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    # ── Referidos ─────────────────────────────────────────────────────────
    # referrer_id: quién invitó a este usuario (NULL si nadie, o si vino
    # orgánico). referral_code: código propio del usuario para invitar a
    # otros (se genera perezosamente, ver Database.ensure_referral_code).
    # No se agrega un contador denormalizado (referral_count): las
    # estadísticas de /referidos se calculan con COUNT sobre `referrals`
    # para no arrastrar un número que se pueda desincronizar del real.
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS referrer_id BIGINT",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS referral_code TEXT UNIQUE",
    "CREATE INDEX IF NOT EXISTS idx_users_referral_code ON users(referral_code)",
    "CREATE INDEX IF NOT EXISTS idx_users_referrer_id    ON users(referrer_id)",
    """
    CREATE TABLE IF NOT EXISTS referrals (
        id          BIGSERIAL PRIMARY KEY,
        referrer_id BIGINT NOT NULL,
        referred_id BIGINT NOT NULL,
        tx_id       BIGINT,
        bonus_usd   DOUBLE PRECISION NOT NULL,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals(referrer_id)",
    "CREATE INDEX IF NOT EXISTS idx_referrals_referred ON referrals(referred_id)",
]


def _affected_rows(command_tag: str) -> int:
    """
    asyncpg no expone `cursor.rowcount`: `conn.execute(...)` devuelve un
    "command tag" de Postgres como "UPDATE 3" o "INSERT 0 1". El número de
    filas afectadas es siempre el ÚLTIMO token. Se usa donde antes se
    miraba `cur.rowcount > 0` (ej. set_account_type, set_country).
    """
    try:
        return int(command_tag.split()[-1])
    except (ValueError, IndexError):
        return 0


def _set_clause(kwargs: dict, start_idx: int = 1) -> tuple[str, list, int]:
    """
    Arma dinámicamente "col1 = $1, col2 = $2, ..." a partir de un dict,
    devolviendo también los valores en el mismo orden y el próximo índice
    de placeholder libre (para que el llamador pueda seguir numerando,
    típicamente para el WHERE id = $N). Reemplaza el patrón que antes
    generaba "col = %s" (los placeholders posicionales `%s` de psycopg2
    no llevan número, así que no hacía falta llevar este índice).
    """
    parts, values, idx = [], [], start_idx
    for k, v in kwargs.items():
        parts.append(f"{k} = ${idx}")
        values.append(v)
        idx += 1
    return ", ".join(parts), values, idx


class _PooledConnection:
    """
    Wrapper async sobre una conexión sacada del pool de asyncpg. Se usa
    como:

        async with self._conn() as conn:
            row = await conn.fetchrow(sql, *params)

    Al salir del `async with`: hace commit de la transacción si no hubo
    excepción (rollback si la hubo) y SIEMPRE devuelve la conexión al
    pool (nunca la cierra de verdad, para poder reusarla). Mismo
    contrato que la versión psycopg2, solo que ahora todo es async para
    no bloquear el event loop mientras se espera la red hacia Neon.
    """

    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool
        self._conn: Optional[asyncpg.Connection] = None
        self._tx = None

    async def __aenter__(self) -> "_PooledConnection":
        self._conn = await self._pool.acquire()
        self._tx = self._conn.transaction()
        await self._tx.start()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                await self._tx.commit()
            else:
                await self._tx.rollback()
        finally:
            await self._pool.release(self._conn)
        return False

    async def fetchrow(self, sql: str, *params):
        return await self._conn.fetchrow(sql, *params)

    async def fetch(self, sql: str, *params):
        return await self._conn.fetch(sql, *params)

    async def fetchval(self, sql: str, *params):
        return await self._conn.fetchval(sql, *params)

    async def execute(self, sql: str, *params) -> str:
        return await self._conn.execute(sql, *params)


class Database:
    def __init__(self, dsn: Optional[str] = None):
        self.dsn = dsn or config.DATABASE_URL
        if not self.dsn:
            raise EnvironmentError(
                "Falta DATABASE_URL. Copia la connection string de tu proyecto "
                "en https://console.neon.tech y ponla en el .env / variables de "
                "entorno de Render."
            )
        # El pool NO se crea acá: asyncpg.create_pool() es una corrutina y
        # __init__ no puede ser async. Se crea de verdad en connect(), que
        # hay que llamar una vez (await db.connect()) antes de usar `db`,
        # típicamente al arrancar el bot (ver main.py: _startup_sequence).
        self._pool: Optional[asyncpg.Pool] = None

    async def connect(self):
        """Crea el pool de conexiones y asegura que las tablas existan.
        Idempotente: si ya se conectó antes, no hace nada."""
        if self._pool is not None:
            return
        # Pool chico: alcanza de sobra para un bot de este tamaño y respeta
        # el límite de conexiones concurrentes del plan free de Neon.
        self._pool = await asyncpg.create_pool(dsn=self.dsn, min_size=1, max_size=5)
        await self._create_tables()
        logger.info("Base de datos (Postgres/Neon, asyncpg) inicializada correctamente.")

    async def close(self):
        """Cierra el pool. Llamar al apagar el bot (ver main.py on_shutdown)."""
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    def _conn(self) -> _PooledConnection:
        if self._pool is None:
            raise RuntimeError(
                "Database.connect() no fue llamado todavía. Hay que hacer "
                "'await db.connect()' antes de usar cualquier método de `db`."
            )
        return _PooledConnection(self._pool)

    async def _create_tables(self):
        async with self._conn() as conn:
            for stmt in _DDL_STATEMENTS:
                await conn.execute(stmt)

    # ── Integridad / salud ────────────────────────────────────────────────────

    async def integrity_check(self) -> tuple[bool, str]:
        """
        Con Neon ya no existe un archivo que se pueda corromper localmente
        (eso lo maneja Neon), así que esto pasó a ser un simple chequeo de
        conectividad: sirve para detectar temprano si el bot perdió la
        conexión a la base (ver backup_task.db_health_loop) en vez de
        enterarse recién cuando falla una compra real.
        """
        try:
            async with self._conn() as conn:
                await conn.fetchval("SELECT 1")
            return True, "ok"
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"

    # ── Insertar ──────────────────────────────────────────────────────────────

    async def create_transaction(
        self,
        user_id: int,
        service: str,
        service_name: str,
        country: str,
        country_name: str,
        cost_herosms: float,
        amount_usd: float,
    ) -> int:
        """Crea un registro de transacción inicial y devuelve su id."""
        sql = """
        INSERT INTO transactions
            (user_id, service, service_name, country, country_name,
             cost_herosms, amount_usd, status)
        VALUES ($1, $2, $3, $4, $5, $6, $7, 'pending')
        RETURNING id
        """
        async with self._conn() as conn:
            return await conn.fetchval(
                sql, user_id, service, service_name, country, country_name,
                cost_herosms, amount_usd,
            )

    # ── Actualizar campos individuales ────────────────────────────────────────

    async def set_order_info(
        self, tx_id: int, order_id: str, pay_address: str,
        currency: str, network: str, pay_amount: float,
        token_id: str = None, amount_usd: float = None,
    ):
        """
        `amount_usd` es opcional y normalmente NO hace falta (el precio ya
        quedó fijado en create_transaction). Existe para el caso de pago
        manual en CUP: ahí el monto realmente cobrado puede ser MAYOR al
        precio base por el piso MANUAL_PURCHASE_MIN_USD (ver
        handlers._start_manual_purchase_payment). Sin este ajuste,
        transactions.amount_usd se quedaba con el precio pre-piso y
        cualquier cosa que dependiera de él después -reembolsos por
        timeout/cancelación, y el bono de referido- calculaba sobre un
        monto menor al que el cliente pagó de verdad.
        """
        kwargs = dict(
            order_id=order_id,
            pay_address=pay_address,
            currency=currency,
            network=network,
            pay_amount=pay_amount,
            token_id=token_id,
        )
        if amount_usd is not None:
            kwargs["amount_usd"] = amount_usd
        await self._update(tx_id, **kwargs)

    async def set_activation(self, tx_id: int, activation_id: str, phone_number: str):
        await self._update(tx_id, activation_id=activation_id, phone_number=phone_number)

    async def set_refund_address(self, tx_id: int, refund_address: str):
        await self._update(tx_id, refund_address=refund_address)

    async def set_purchase_proof(
        self, tx_id: int, proof_file_id: str = None,
        proof_file_unique_id: str = None, proof_text: str = None,
    ):
        """
        Guarda el comprobante (foto/texto) de un pago CUP ligado directo a
        una compra. Ver find_reused_proof para la defensa contra reuso de
        la misma captura en varias órdenes.
        """
        await self._update(
            tx_id,
            proof_file_id=proof_file_id,
            proof_file_unique_id=proof_file_unique_id,
            proof_text=proof_text,
        )

    async def find_reused_proof(
        self, proof_file_unique_id: str,
        exclude_tx_id: int = None, exclude_dep_id: int = None,
    ) -> list[dict]:
        """
        Busca OTRAS compras o depósitos (excluyendo la orden actual) que ya
        usaron esta misma captura (mismo file_unique_id de Telegram). Es la
        señal de alarma que se muestra al admin al revisar un comprobante;
        la decisión de bloquear o no sigue siendo suya.
        """
        if not proof_file_unique_id:
            return []

        matches = []
        async with self._conn() as conn:
            rows = await conn.fetch(
                "SELECT id, status FROM transactions "
                "WHERE proof_file_unique_id = $1 AND id != $2",
                proof_file_unique_id, exclude_tx_id or -1,
            )
            matches.extend({"kind": "compra", "id": r["id"], "status": r["status"]} for r in rows)

            rows = await conn.fetch(
                "SELECT id, status FROM manual_deposits "
                "WHERE proof_file_unique_id = $1 AND id != $2",
                proof_file_unique_id, exclude_dep_id or -1,
            )
            matches.extend({"kind": "depósito", "id": r["id"], "status": r["status"]} for r in rows)

        return matches

    async def set_sms_code(self, tx_id: int, sms_code: str):
        await self._update(tx_id, sms_code=sms_code)

    async def set_status(self, tx_id: int, status: str):
        await self._update(tx_id, status=status)

    async def _update(self, tx_id: int, **kwargs):
        kwargs["updated_at"] = datetime.utcnow()
        sets, values, next_idx = _set_clause(kwargs)
        sql = f"UPDATE transactions SET {sets} WHERE id = ${next_idx}"
        async with self._conn() as conn:
            await conn.execute(sql, *values, tx_id)

    # ── Usuarios ──────────────────────────────────────────────────────────────

    async def register_user(
        self, user_id: int, username: str = None, first_name: str = None,
        last_name: str = None, language_code: str = None, is_premium: bool = None,
    ) -> None:
        """
        Registra al usuario si es la primera vez que se lo ve, o actualiza
        username/first_name/last_seen si ya existía. Se llama en cada
        /start (ver handlers.cmd_start): tiene que ser barato y nunca
        lanzar, un fallo acá no debe romper el flujo de bienvenida.
        """
        sql = """
        INSERT INTO users (
            user_id, username, first_name, last_name, language_code,
            is_premium, first_seen, last_seen
        )
        VALUES ($1, $2, $3, $4, $5, $6, NOW(), NOW())
        ON CONFLICT (user_id) DO UPDATE SET
            username      = EXCLUDED.username,
            first_name    = EXCLUDED.first_name,
            last_name     = EXCLUDED.last_name,
            language_code = EXCLUDED.language_code,
            is_premium    = EXCLUDED.is_premium,
            last_seen     = NOW()
        """
        try:
            async with self._conn() as conn:
                await conn.execute(
                    sql, user_id, username, first_name, last_name, language_code,
                    int(is_premium) if is_premium is not None else None,
                )
        except Exception as exc:
            logger.error("register_user(%s) error: %s: %s", user_id, type(exc).__name__, exc)

    async def set_phone_number(self, user_id: int, phone_number: str) -> None:
        """
        Guarda el teléfono real del usuario cuando lo comparte de forma
        EXPLÍCITA (botón "compartir contacto" de Telegram). Nunca se pide
        como requisito, solo queda disponible para el admin si el usuario
        decide compartirlo.
        """
        async with self._conn() as conn:
            await conn.execute(
                "UPDATE users SET phone_number = $1, phone_verified_at = NOW() "
                "WHERE user_id = $2",
                phone_number, user_id,
            )

    async def get_user_count(self) -> int:
        """Total de usuarios distintos que alguna vez corrieron /start."""
        async with self._conn() as conn:
            count = await conn.fetchval("SELECT COUNT(*) FROM users")
            return int(count or 0)

    async def get_user(self, user_id: int) -> Optional[dict]:
        """
        Fila cruda de la tabla `users`, o None si el usuario nunca corrió
        /start. Usado por telegram_sender.py para la tarjeta de bienvenida.
        """
        async with self._conn() as conn:
            row = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)
            return dict(row) if row else None

    async def set_account_type(self, user_id: int, account_type: Optional[str]) -> bool:
        """
        Asigna el 'Nivel' de cuenta (cliente/reseller/vip). account_type=None
        borra el valor. Devuelve False si el usuario nunca corrió /start.
        """
        async with self._conn() as conn:
            tag = await conn.execute(
                "UPDATE users SET account_type = $1 WHERE user_id = $2",
                account_type, user_id,
            )
            return _affected_rows(tag) > 0

    async def set_country(self, user_id: int, country: Optional[str]) -> bool:
        """Asigna el país (texto libre). country=None borra el valor."""
        async with self._conn() as conn:
            tag = await conn.execute(
                "UPDATE users SET country = $1 WHERE user_id = $2",
                country, user_id,
            )
            return _affected_rows(tag) > 0

    async def count_completed_orders(self, user_id: int) -> int:
        """Total de compras COMPLETADAS (código OTP recibido y confirmado) del usuario."""
        async with self._conn() as conn:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM transactions "
                "WHERE user_id = $1 AND status = 'completed'",
                user_id,
            )
            return int(count or 0)

    # ── Referidos ────────────────────────────────────────────────────────────
    # Ver database._DDL_STATEMENTS (columnas en `users` + tabla `referrals`)
    # y config.REFERRAL_BONUS_PCT / REFERRAL_MIN_PURCHASE_USD para el
    # porcentaje y piso, configurables por variable de entorno. El disparo
    # (detectar "primera compra completada" y llamar a
    # register_referral_bonus) vive en handlers._maybe_credit_referral_bonus,
    # no acá: este módulo solo expone las operaciones atómicas de datos.

    async def ensure_referral_code(self, user_id: int) -> str:
        """
        Devuelve el código de referido del usuario, generándolo y
        persistiéndolo la primera vez que hace falta (ej. al correr
        /referidos). Determinístico a partir del user_id -no un random
        guardado aparte- para no depender de una tabla de secuencias extra
        ni de manejo de colisiones.
        """
        user = await self.get_user(user_id)
        if user and user.get("referral_code"):
            return user["referral_code"]

        code = f"REF{user_id}"
        async with self._conn() as conn:
            await conn.execute(
                "UPDATE users SET referral_code = $1 WHERE user_id = $2",
                code, user_id,
            )
        return code

    async def get_user_by_referral_code(self, code: str) -> Optional[dict]:
        """Fila completa del dueño de ese código, o None si no existe."""
        async with self._conn() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM users WHERE referral_code = $1", code,
            )
            return dict(row) if row else None

    async def set_referrer(self, user_id: int, referrer_id: int) -> bool:
        """
        Vincula `user_id` a su referidor, SOLO si todavía no tenía uno
        asignado (evita que reusar un link viejo/ajeno más tarde cambie
        de referidor a alguien ya vinculado). Devuelve True si se asignó
        de verdad en esta llamada.
        """
        async with self._conn() as conn:
            tag = await conn.execute(
                "UPDATE users SET referrer_id = $1 "
                "WHERE user_id = $2 AND referrer_id IS NULL",
                referrer_id, user_id,
            )
            return _affected_rows(tag) > 0

    async def get_referral_stats(self, user_id: int) -> dict:
        """
        Estadísticas para /referidos: cuántos usuarios entraron con su
        enlace (`invited`), a cuántos ya se les pagó bono (`paid`, hecho
        vía `referrals`) y el total ganado en USD.
        """
        async with self._conn() as conn:
            invited = await conn.fetchval(
                "SELECT COUNT(*) FROM users WHERE referrer_id = $1", user_id,
            )
            row = await conn.fetchrow(
                "SELECT COUNT(*) AS paid, COALESCE(SUM(bonus_usd), 0) AS total_bonus "
                "FROM referrals WHERE referrer_id = $1",
                user_id,
            )
            return {
                "invited": int(invited or 0),
                "paid": int(row["paid"]),
                "total_bonus": float(row["total_bonus"]),
            }

    async def register_referral_bonus(
        self, referrer_id: int, referred_id: int, tx_id: int, bonus_usd: float,
    ) -> float:
        """
        Registra el bono en `referrals` (auditoría/estadísticas de
        /referidos) y lo acredita al saldo del referidor con origen
        'crypto' (retirable, igual que una recarga cripto). Devuelve el
        nuevo saldo TOTAL del referidor. Pensado para llamarse UNA sola
        vez por usuario referido -ver handlers._maybe_credit_referral_bonus,
        que solo dispara en la primera compra completada del referido.
        """
        async with self._conn() as conn:
            await conn.execute(
                "INSERT INTO referrals (referrer_id, referred_id, tx_id, bonus_usd) "
                "VALUES ($1, $2, $3, $4)",
                referrer_id, referred_id, tx_id, bonus_usd,
            )
        return await self.credit_balance(
            referrer_id, bonus_usd, tx_id,
            reason=f"Bono de referido (usuario {referred_id})", origin="crypto",
        )

    # ── Saldo interno (wallet virtual) ───────────────────────────────────────

    _ORIGIN_COLUMN = {"crypto": "balance_usd", "cup": "balance_usd_cup"}
    EPSILON = 1e-9

    async def get_balance(self, user_id: int) -> float:
        """Saldo TOTAL (cripto + CUP) para mostrar en /saldo y para pagar compras."""
        b = await self.get_balance_breakdown(user_id)
        return b["total"]

    async def get_balance_breakdown(self, user_id: int) -> dict:
        """Devuelve {"crypto": x, "cup": y, "total": x+y}."""
        async with self._conn() as conn:
            row = await conn.fetchrow(
                "SELECT balance_usd, balance_usd_cup FROM balances WHERE user_id = $1",
                user_id,
            )
            crypto = float(row["balance_usd"]) if row else 0.0
            cup = float(row["balance_usd_cup"]) if row else 0.0
            return {"crypto": crypto, "cup": cup, "total": crypto + cup}

    async def credit_balance(
        self, user_id: int, amount_usd: float, tx_id: int = None,
        reason: str = "", origin: str = "crypto",
    ) -> float:
        """
        Acredita saldo interno. `origin` determina a cuál de las dos
        "bolsas" entra el crédito y por lo tanto cómo se podrá retirar
        después: 'crypto' (retirable a cripto) o 'cup' (retirable solo como
        CUP real). Devuelve el nuevo balance TOTAL después del crédito.
        """
        column = self._ORIGIN_COLUMN.get(origin, "balance_usd")
        amount_usd = round(float(amount_usd), 4)
        async with self._conn() as conn:
            await conn.execute(
                f"INSERT INTO balances (user_id, {column}, updated_at) "
                f"VALUES ($1, $2, NOW()) "
                f"ON CONFLICT (user_id) DO UPDATE SET "
                f"{column} = balances.{column} + EXCLUDED.{column}, "
                f"updated_at = EXCLUDED.updated_at",
                user_id, amount_usd,
            )
            await conn.execute(
                "INSERT INTO balance_ledger (user_id, tx_id, delta_usd, origin, reason) "
                "VALUES ($1, $2, $3, $4, $5)",
                user_id, tx_id, amount_usd, origin, reason,
            )
            row = await conn.fetchrow(
                "SELECT balance_usd, balance_usd_cup FROM balances WHERE user_id = $1",
                user_id,
            )
            return float(row["balance_usd"]) + float(row["balance_usd_cup"])

    async def debit_balance(
        self, user_id: int, amount_usd: float, tx_id: int = None,
        reason: str = "", origin: str = None,
    ) -> bool:
        """
        Descuenta saldo interno de forma atómica. Devuelve False sin tocar
        nada si no alcanza el saldo.

        `origin`:
          - 'crypto'/'cup': descuenta ESTRICTAMENTE de esa bolsa (retiro real).
          - None: pago de compra con "saldo" - se combinan ambas bolsas,
            drenando primero CUP (la menos flexible de las dos).
        """
        amount_usd = round(float(amount_usd), 4)
        async with self._conn() as conn:
            row = await conn.fetchrow(
                "SELECT balance_usd, balance_usd_cup FROM balances WHERE user_id = $1",
                user_id,
            )
            crypto_bal = float(row["balance_usd"]) if row else 0.0
            cup_bal = float(row["balance_usd_cup"]) if row else 0.0

            if origin in ("crypto", "cup"):
                column = self._ORIGIN_COLUMN[origin]
                current = crypto_bal if origin == "crypto" else cup_bal
                if amount_usd - current > self.EPSILON:
                    return False
                new_value = 0.0 if amount_usd >= current - self.EPSILON else current - amount_usd
                await conn.execute(
                    f"UPDATE balances SET {column} = $1, updated_at = NOW() WHERE user_id = $2",
                    new_value, user_id,
                )
                await conn.execute(
                    "INSERT INTO balance_ledger (user_id, tx_id, delta_usd, origin, reason) "
                    "VALUES ($1, $2, $3, $4, $5)",
                    user_id, tx_id, -amount_usd, origin, reason,
                )
                return True

            # origin=None: combinado, CUP primero.
            total = crypto_bal + cup_bal
            if amount_usd - total > self.EPSILON:
                return False

            cup_used = min(cup_bal, amount_usd)
            crypto_used = amount_usd - cup_used
            new_cup = 0.0 if cup_used >= cup_bal - self.EPSILON else cup_bal - cup_used
            new_crypto = 0.0 if crypto_used >= crypto_bal - self.EPSILON else crypto_bal - crypto_used

            await conn.execute(
                "UPDATE balances SET balance_usd = $1, balance_usd_cup = $2, "
                "updated_at = NOW() WHERE user_id = $3",
                new_crypto, new_cup, user_id,
            )
            if cup_used > self.EPSILON:
                await conn.execute(
                    "INSERT INTO balance_ledger (user_id, tx_id, delta_usd, origin, reason) "
                    "VALUES ($1, $2, $3, 'cup', $4)",
                    user_id, tx_id, -cup_used, reason,
                )
            if crypto_used > self.EPSILON:
                await conn.execute(
                    "INSERT INTO balance_ledger (user_id, tx_id, delta_usd, origin, reason) "
                    "VALUES ($1, $2, $3, 'crypto', $4)",
                    user_id, tx_id, -crypto_used, reason,
                )
            return True

    async def get_purchase_origin_ratios(self, tx: dict) -> dict:
        """
        Determina en qué proporción hay que devolver un reembolso ligado a
        `tx` entre las bolsas 'crypto' y 'cup', según cómo se pagó
        ORIGINALMENTE esa compra. Devuelve algo como {"crypto": 0.7, "cup": 0.3}.
        """
        currency = (tx.get("currency") or "").upper()
        if currency == "CUP":
            return {"cup": 1.0}

        order_id = tx.get("order_id") or ""
        if order_id.startswith("balance-"):
            tx_id = tx.get("id")
            async with self._conn() as conn:
                rows = await conn.fetch(
                    "SELECT COALESCE(origin, 'crypto') AS origin, SUM(-delta_usd) AS debited "
                    "FROM balance_ledger WHERE tx_id = $1 AND delta_usd < 0 "
                    "GROUP BY origin",
                    tx_id,
                )
            totals = {r["origin"]: float(r["debited"] or 0) for r in rows}
            grand_total = sum(totals.values())
            if grand_total <= self.EPSILON:
                return {"crypto": 1.0}
            return {origin: amt / grand_total for origin, amt in totals.items()}

        return {"crypto": 1.0}

    # ── Depósitos (agregar saldo) ────────────────────────────────────────────

    async def create_deposit(self, user_id: int, amount_usd: float) -> int:
        """Crea un registro de depósito inicial (aún sin orden) y devuelve su id."""
        async with self._conn() as conn:
            return await conn.fetchval(
                "INSERT INTO deposits (user_id, amount_usd, status) "
                "VALUES ($1, $2, 'pending') RETURNING id",
                user_id, amount_usd,
            )

    async def set_deposit_order_info(
        self, deposit_id: int, order_id: str, pay_address: str,
        currency: str, network: str, pay_amount: float, token_id: str,
    ):
        async with self._conn() as conn:
            await conn.execute(
                "UPDATE deposits SET order_id = $1, pay_address = $2, currency = $3, "
                "network = $4, pay_amount = $5, token_id = $6, updated_at = NOW() "
                "WHERE id = $7",
                order_id, pay_address, currency, network, pay_amount, token_id, deposit_id,
            )

    async def set_deposit_status(self, deposit_id: int, status: str):
        async with self._conn() as conn:
            await conn.execute(
                "UPDATE deposits SET status = $1, updated_at = NOW() WHERE id = $2",
                status, deposit_id,
            )

    async def get_deposit_by_id(self, deposit_id: int) -> Optional[dict]:
        async with self._conn() as conn:
            row = await conn.fetchrow("SELECT * FROM deposits WHERE id = $1", deposit_id)
            return dict(row) if row else None

    async def get_deposit_by_order_id(self, order_id: str) -> Optional[dict]:
        async with self._conn() as conn:
            row = await conn.fetchrow("SELECT * FROM deposits WHERE order_id = $1", order_id)
            return dict(row) if row else None

    async def get_pending_deposits(self) -> list[dict]:
        """Depósitos con una orden de pago generada, todavía sin confirmar (recovery al reiniciar)."""
        async with self._conn() as conn:
            rows = await conn.fetch(
                "SELECT * FROM deposits WHERE status = 'pending' AND order_id IS NOT NULL"
            )
            return [dict(r) for r in rows]

    async def get_last_completed_deposit(self, user_id: int) -> Optional[dict]:
        """Depósito COMPLETADO más reciente del usuario (currency/network/token_id), o None."""
        async with self._conn() as conn:
            row = await conn.fetchrow(
                "SELECT currency, network, token_id FROM deposits "
                "WHERE user_id = $1 AND status = 'completed' AND token_id IS NOT NULL "
                "ORDER BY updated_at DESC LIMIT 1",
                user_id,
            )
            return dict(row) if row else None

    # ── Depósitos manuales (CUP vía Transfermóvil / EnZona) ──────────────────

    async def create_manual_deposit(
        self, user_id: int, method: str, amount_usd: float,
        amount_cup: int = None, cup_rate: float = None,
    ) -> dict:
        """Crea el registro y genera su reference_code (REF-000123) a partir del id."""
        async with self._conn() as conn:
            dep_id = await conn.fetchval(
                "INSERT INTO manual_deposits (user_id, method, amount_usd, amount_cup, cup_rate, status) "
                "VALUES ($1, $2, $3, $4, $5, 'awaiting_proof') RETURNING id",
                user_id, method, amount_usd, amount_cup, cup_rate,
            )
            reference_code = f"REF-{dep_id:06d}"
            await conn.execute(
                "UPDATE manual_deposits SET reference_code = $1 WHERE id = $2",
                reference_code, dep_id,
            )
            return {"id": dep_id, "reference_code": reference_code}

    async def set_manual_deposit_proof(
        self, dep_id: int, proof_file_id: str = None,
        proof_file_unique_id: str = None, proof_text: str = None,
    ):
        await self._update_manual_deposit(
            dep_id, status="pending_review",
            proof_file_id=proof_file_id,
            proof_file_unique_id=proof_file_unique_id,
            proof_text=proof_text,
        )

    async def set_manual_deposit_status(self, dep_id: int, status: str, reviewed_by: int = None):
        kwargs = {"status": status}
        if reviewed_by is not None:
            kwargs["reviewed_by"] = reviewed_by
        await self._update_manual_deposit(dep_id, **kwargs)

    async def _update_manual_deposit(self, dep_id: int, **kwargs):
        kwargs["updated_at"] = datetime.utcnow()
        sets, values, next_idx = _set_clause(kwargs)
        sql = f"UPDATE manual_deposits SET {sets} WHERE id = ${next_idx}"
        async with self._conn() as conn:
            await conn.execute(sql, *values, dep_id)

    async def get_manual_deposit_by_id(self, dep_id: int) -> Optional[dict]:
        async with self._conn() as conn:
            row = await conn.fetchrow("SELECT * FROM manual_deposits WHERE id = $1", dep_id)
            return dict(row) if row else None

    async def get_pending_manual_deposit(self, user_id: int) -> Optional[dict]:
        """Solicitud NO resuelta más reciente del usuario (máximo 1 pendiente a la vez)."""
        async with self._conn() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM manual_deposits WHERE user_id = $1 "
                "AND status IN ('awaiting_proof', 'pending_review') "
                "ORDER BY created_at DESC LIMIT 1",
                user_id,
            )
            return dict(row) if row else None

    async def get_pending_manual_deposits_for_review(self) -> list[dict]:
        """Todas las solicitudes esperando aprobación de un admin (para /pendientes)."""
        async with self._conn() as conn:
            rows = await conn.fetch(
                "SELECT * FROM manual_deposits WHERE status = 'pending_review' "
                "ORDER BY created_at ASC"
            )
            return [dict(r) for r in rows]

    async def get_cup_exposure(self) -> dict:
        """CUP ya aprobado (acreditado) que todavía no se marcó como convertido a USDT real."""
        async with self._conn() as conn:
            row = await conn.fetchrow(
                "SELECT COUNT(*) AS c, COALESCE(SUM(amount_cup),0) AS cup, "
                "COALESCE(SUM(amount_usd),0) AS usd "
                "FROM manual_deposits "
                "WHERE status = 'approved' AND converted_to_usdt = 0"
            )
            return {
                "count": int(row["c"] or 0),
                "total_cup": int(row["cup"] or 0),
                "total_usd": float(row["usd"] or 0),
            }

    async def get_unconverted_manual_deposits(self) -> list[dict]:
        """Detalle de los depósitos aprobados y aún sin convertir (para /exposicion_cup)."""
        async with self._conn() as conn:
            rows = await conn.fetch(
                "SELECT * FROM manual_deposits "
                "WHERE status = 'approved' AND converted_to_usdt = 0 "
                "ORDER BY created_at ASC"
            )
            return [dict(r) for r in rows]

    async def mark_manual_deposits_converted(self, dep_ids: list[int]):
        """Marca uno o más depósitos como ya convertidos a USDT real."""
        if not dep_ids:
            return
        async with self._conn() as conn:
            placeholders = ",".join(f"${i + 1}" for i in range(len(dep_ids)))
            await conn.execute(
                f"UPDATE manual_deposits SET converted_to_usdt = 1, "
                f"updated_at = NOW() WHERE id IN ({placeholders})",
                *dep_ids,
            )

    # ── Retiros manuales (CUP vía Transfermóvil / EnZona) ────────────────────

    async def create_manual_withdrawal(
        self, user_id: int, method: str, destination: str,
        amount_usd: float, fee_usd: float, net_usd: float,
        amount_cup: int, cup_rate: float,
    ) -> dict:
        """Crea la solicitud y genera su reference_code (WD-000123) a partir del id."""
        async with self._conn() as conn:
            wd_id = await conn.fetchval(
                "INSERT INTO manual_withdrawals "
                "(user_id, method, destination, amount_usd, fee_usd, net_usd, "
                "amount_cup, cup_rate, status) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'pending_review') RETURNING id",
                user_id, method, destination, amount_usd, fee_usd, net_usd,
                amount_cup, cup_rate,
            )
            reference_code = f"WD-{wd_id:06d}"
            await conn.execute(
                "UPDATE manual_withdrawals SET reference_code = $1 WHERE id = $2",
                reference_code, wd_id,
            )
            return {"id": wd_id, "reference_code": reference_code}

    async def set_manual_withdrawal_status(self, wd_id: int, status: str, reviewed_by: int = None):
        kwargs = {"status": status, "updated_at": datetime.utcnow()}
        if reviewed_by is not None:
            kwargs["reviewed_by"] = reviewed_by
        sets, values, next_idx = _set_clause(kwargs)
        async with self._conn() as conn:
            await conn.execute(
                f"UPDATE manual_withdrawals SET {sets} WHERE id = ${next_idx}",
                *values, wd_id,
            )

    async def get_manual_withdrawal_by_id(self, wd_id: int) -> Optional[dict]:
        async with self._conn() as conn:
            row = await conn.fetchrow("SELECT * FROM manual_withdrawals WHERE id = $1", wd_id)
            return dict(row) if row else None

    async def get_pending_manual_withdrawal(self, user_id: int) -> Optional[dict]:
        """Igual que get_pending_manual_deposit pero para retiros: máximo 1 solicitud en curso."""
        async with self._conn() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM manual_withdrawals WHERE user_id = $1 "
                "AND status = 'pending_review' ORDER BY created_at DESC LIMIT 1",
                user_id,
            )
            return dict(row) if row else None

    async def get_by_id(self, tx_id: int) -> Optional[dict]:
        async with self._conn() as conn:
            row = await conn.fetchrow("SELECT * FROM transactions WHERE id = $1", tx_id)
            return dict(row) if row else None

    async def get_by_order_id(self, order_id: str) -> Optional[dict]:
        async with self._conn() as conn:
            row = await conn.fetchrow("SELECT * FROM transactions WHERE order_id = $1", order_id)
            return dict(row) if row else None

    async def get_user_transactions(self, user_id: int, limit: int = 10) -> list[dict]:
        async with self._conn() as conn:
            rows = await conn.fetch(
                "SELECT * FROM transactions WHERE user_id = $1 "
                "ORDER BY created_at DESC LIMIT $2",
                user_id, limit,
            )
            return [dict(r) for r in rows]

    async def get_pending_transactions(self) -> list[dict]:
        """Transacciones en estados activos (útil para recovery al reiniciar)."""
        async with self._conn() as conn:
            rows = await conn.fetch(
                "SELECT * FROM transactions WHERE status IN ('pending', 'paid', 'number_assigned')"
            )
            return [dict(r) for r in rows]

    async def get_abuse_strikes(self, user_id: int, hours: int) -> int:
        """
        Cuenta cuántas veces este usuario recibió un número y la operación
        terminó SIN completarse en las últimas `hours` horas. Usado por
        handlers.cb_new_purchase para bloquear temporalmente abuso.
        """
        cutoff = datetime.utcnow() - timedelta(hours=hours)
        async with self._conn() as conn:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM transactions "
                "WHERE user_id = $1 AND phone_number IS NOT NULL "
                "AND status IN ('sms_timeout', 'refunded', 'error') "
                "AND created_at >= $2",
                user_id, cutoff,
            )
            return int(count or 0)

    async def get_stats(self, days: Optional[int] = None) -> dict:
        """Métricas agregadas para el comando de admin /stats."""
        where = ""
        cutoff = None
        if days is not None:
            where = "WHERE created_at >= $1"
            cutoff = datetime.utcnow() - timedelta(days=int(days))

        async with self._conn() as conn:
            status_params = (cutoff,) if cutoff is not None else ()
            status_rows = await conn.fetch(
                f"SELECT status, COUNT(*) AS c FROM transactions {where} GROUP BY status",
                *status_params,
            )
            by_status = {r["status"]: r["c"] for r in status_rows}

            completed_where = f"{where}{' AND' if where else 'WHERE'} status = 'completed'"
            revenue_row = await conn.fetchrow(
                f"SELECT COALESCE(SUM(amount_usd),0) AS revenue, "
                f"COALESCE(SUM(cost_herosms),0) AS cost, COUNT(*) AS n "
                f"FROM transactions {completed_where}",
                *status_params,
            )

        revenue   = float(revenue_row["revenue"] or 0)
        cost      = float(revenue_row["cost"] or 0)
        completed = int(revenue_row["n"] or 0)

        return {
            "by_status":        by_status,
            "orders_completed": completed,
            "revenue_usd":      revenue,
            "cost_usd":         cost,
            "avg_ticket_usd":   round(revenue / completed, 2) if completed else 0.0,
        }

    async def get_recent_sales(self, limit: int = 10) -> list[dict]:
        """Últimas ventas completadas de TODOS los usuarios (para /ventas)."""
        async with self._conn() as conn:
            rows = await conn.fetch(
                "SELECT * FROM transactions WHERE status = 'completed' "
                "ORDER BY updated_at DESC LIMIT $1",
                limit,
            )
            return [dict(r) for r in rows]

    async def get_country_success_stats(self, service: str, min_samples: int = 5) -> dict:
        """
        Tasa de éxito por país para un servicio dado (ver docstring de la
        versión anterior en git history para el detalle completo del
        razonamiento de negocio).
        """
        sql = """
        SELECT country,
               COUNT(*) AS attempts,
               SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed
        FROM transactions
        WHERE service = $1 AND phone_number IS NOT NULL
        GROUP BY country
        HAVING COUNT(*) >= $2
        """
        async with self._conn() as conn:
            rows = await conn.fetch(sql, service, min_samples)

        return {
            r["country"]: {
                "attempts": r["attempts"],
                "completed": r["completed"],
                "rate": round(100 * r["completed"] / r["attempts"], 1),
            }
            for r in rows
        }

    # ── Outbox de notificaciones (ver outbox.py) ──────────────────────────────

    async def enqueue_outbox(self, chat_id: int, text: str, reply_markup: Optional[str] = None) -> int:
        """Encola un mensaje ANTES de intentar enviarlo (sobrevive a un crash del proceso)."""
        async with self._conn() as conn:
            return await conn.fetchval(
                "INSERT INTO outbox (chat_id, text, reply_markup) VALUES ($1, $2, $3) RETURNING id",
                chat_id, text, reply_markup,
            )

    async def mark_outbox_sent(self, outbox_id: int):
        async with self._conn() as conn:
            await conn.execute(
                "UPDATE outbox SET status = 'sent', updated_at = NOW() WHERE id = $1",
                outbox_id,
            )

    async def mark_outbox_attempt_failed(
        self, outbox_id: int, error: str, next_attempt_at, give_up: bool = False,
    ):
        """
        Registra un intento fallido. `next_attempt_at` debe ser un datetime:
        asyncpg no castea strings para una columna TIMESTAMPTZ (a diferencia
        de psycopg2), así que si llega un string "YYYY-MM-DD HH:MM:SS" (por
        compatibilidad con algún caller viejo) se convierte acá antes de
        mandarlo a la query.
        """
        if isinstance(next_attempt_at, str):
            next_attempt_at = datetime.strptime(next_attempt_at, "%Y-%m-%d %H:%M:%S")
        async with self._conn() as conn:
            await conn.execute(
                "UPDATE outbox SET attempts = attempts + 1, last_error = $1, "
                "status = $2, next_attempt_at = $3, updated_at = NOW() WHERE id = $4",
                error, "dead" if give_up else "pending", next_attempt_at, outbox_id,
            )

    async def get_due_outbox(self, limit: int = 200) -> list[dict]:
        """Mensajes 'pending' cuyo próximo intento ya venció, los más viejos primero."""
        async with self._conn() as conn:
            rows = await conn.fetch(
                "SELECT * FROM outbox WHERE status = 'pending' "
                "AND next_attempt_at <= NOW() "
                "ORDER BY created_at ASC LIMIT $1",
                limit,
            )
            return [dict(r) for r in rows]

    # ── Métodos de pago manual (CUP) ────────────────────────────────────────
    # Reemplaza el antiguo config.MANUAL_PAYMENT_METHODS (dict hardcodeado):
    # ahora el admin agrega/actualiza/desactiva tarjetas desde el propio bot
    # (ver handlers.cmd_metodos / cmd_set_metodo / cmd_quitar_metodo), sin
    # tocar código ni redeployar cada vez que cambia una cuenta.

    async def get_payment_methods(self, active_only: bool = True) -> dict:
        """
        {code: {"name":..., "account":..., "active":..., "sort_order":...}},
        mismo shape que antes tenía el dict estático
        config.MANUAL_PAYMENT_METHODS — los call sites existentes solo leen
        "name" y "account", así que los campos de más no rompen nada.
        Ordenado por sort_order y luego code, para que el orden de los
        botones sea predecible y el admin lo pueda controlar con
        /set_metodo.
        """
        sql = "SELECT * FROM payment_methods"
        if active_only:
            sql += " WHERE active = TRUE"
        sql += " ORDER BY sort_order ASC, code ASC"
        async with self._conn() as conn:
            rows = await conn.fetch(sql)
            return {r["code"]: dict(r) for r in rows}

    async def get_payment_method(self, code: str) -> Optional[dict]:
        async with self._conn() as conn:
            row = await conn.fetchrow("SELECT * FROM payment_methods WHERE code = $1", code)
            return dict(row) if row else None

    async def upsert_payment_method(
        self, code: str, name: str, account: str,
        updated_by: Optional[int] = None, sort_order: Optional[int] = None,
    ) -> None:
        """
        Crea el método si no existe, o actualiza nombre/cuenta si ya existe.
        Siempre lo deja activo: corregir una tarjeta con /set_metodo también
        sirve para reactivar una que se había desactivado con
        /quitar_metodo.
        """
        async with self._conn() as conn:
            await conn.execute(
                """
                INSERT INTO payment_methods (code, name, account, sort_order, updated_by)
                VALUES ($1, $2, $3, COALESCE($4, 0), $5)
                ON CONFLICT (code) DO UPDATE SET
                    name       = EXCLUDED.name,
                    account    = EXCLUDED.account,
                    active     = TRUE,
                    sort_order = COALESCE($4, payment_methods.sort_order),
                    updated_by = EXCLUDED.updated_by,
                    updated_at = NOW()
                """,
                code, name, account, sort_order, updated_by,
            )

    async def set_payment_method_active(self, code: str, active: bool) -> bool:
        """
        Desactiva (o reactiva) un método sin borrarlo — así una tarjeta
        vieja deja de ofrecerse a usuarios nuevos pero sigue existiendo
        para resolver el nombre en transacciones/retiros históricos que ya
        la usaron (ver handlers._find_manual_method_name). Devuelve False
        si el code no existe.
        """
        async with self._conn() as conn:
            tag = await conn.execute(
                "UPDATE payment_methods SET active = $1, updated_at = NOW() WHERE code = $2",
                active, code,
            )
            return _affected_rows(tag) > 0

    async def get_top_services(self, limit: int = 8, days: Optional[int] = None) -> list[dict]:
        """Servicios más comprados (solo compras EXITOSAS), de mayor a menor cantidad."""
        sql = """
        SELECT service AS code, service_name AS name, COUNT(*) AS count
        FROM transactions
        WHERE status = 'completed'
        """
        params: list = []
        if days is not None:
            params.append(datetime.utcnow() - timedelta(days=int(days)))
            sql += f" AND created_at >= ${len(params)}"
        sql += " GROUP BY service, service_name ORDER BY count DESC"
        params.append(limit)
        sql += f" LIMIT ${len(params)}"

        async with self._conn() as conn:
            rows = await conn.fetch(sql, *params)
            return [dict(r) for r in rows]


# Instancia global compartida. OJO: hay que llamar `await db.connect()` una
# vez (ver main.py) antes de usar cualquier método — acá solo se arma el
# objeto, todavía sin pool de conexiones.
db = Database()
