"""
database.py - Manejo de base de datos PostgreSQL (Neon) para transacciones y
estado del bot.

MIGRADO DE SQLITE: este módulo antes usaba sqlite3 con un archivo .db local
(ver git history si hace falta comparar). Se migró a Postgres porque:
  - Render (donde corre el bot) tiene filesystem EFÍMERO por defecto: un
    archivo .db local se borraría en cada redeploy/reinicio si no se paga
    un disco persistente. Postgres vive afuera del contenedor del bot.
  - Neon ya da backups/point-in-time recovery y branching gestionados, así
    que el backup casero a mano (ver la versión vieja de backup_task.py)
    deja de hacer falta.

La interfaz pública (nombres de métodos y qué devuelven) se mantuvo IGUAL
a la versión SQLite a propósito, para no tener que tocar handlers.py,
outbox.py, telegram_sender.py, etc. Solo cambiaron las tripas: placeholders
`?` -> `%s`, `cursor.lastrowid` -> `RETURNING id`, y se sacó toda la lógica
de migración incremental por PRAGMA table_info (no hace falta: una base
Postgres nueva arranca directo con el esquema completo).
"""
import logging
from datetime import datetime, timedelta
from typing import Optional

import psycopg2
import psycopg2.extras
from psycopg2.pool import SimpleConnectionPool

import config

logger = logging.getLogger(__name__)

# Statements DDL, uno por uno (evitamos ejecutar un script gigante con ";"
# porque psycopg2 no garantiza que un solo execute() corra varios statements
# de forma confiable en todos los casos, a diferencia de sqlite3.executescript).
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
]


class _PooledConnection:
    """
    Wrapper fino sobre una conexión sacada del pool, pensado para que el
    resto de este archivo pueda seguir escribiendo

        with self._conn() as conn:
            conn.execute(sql, params)

    igual que hacía con sqlite3.Connection. Al salir del `with`: hace commit
    si no hubo excepción (rollback si la hubo) y SIEMPRE devuelve la
    conexión al pool (nunca la cierra de verdad, para poder reusarla).
    """

    def __init__(self, pool: SimpleConnectionPool):
        self._pool = pool
        self._conn = pool.getconn()

    def execute(self, sql: str, params=()):
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params)
        return cur

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()
        finally:
            self._pool.putconn(self._conn)
        return False


class Database:
    def __init__(self, dsn: Optional[str] = None):
        self.dsn = dsn or config.DATABASE_URL
        if not self.dsn:
            raise EnvironmentError(
                "Falta DATABASE_URL. Copia la connection string de tu proyecto "
                "en https://console.neon.tech y ponla en el .env / variables de "
                "entorno de Render."
            )
        # Pool chico: alcanza de sobra para un bot de este tamaño y respeta
        # el límite de conexiones concurrentes del plan free de Neon.
        self._pool = SimpleConnectionPool(minconn=1, maxconn=5, dsn=self.dsn)
        self._create_tables()

    def _conn(self) -> _PooledConnection:
        return _PooledConnection(self._pool)

    def _create_tables(self):
        with self._conn() as conn:
            for stmt in _DDL_STATEMENTS:
                conn.execute(stmt)
        logger.info("Base de datos (Postgres/Neon) inicializada correctamente.")

    # ── Integridad / salud ────────────────────────────────────────────────────

    def integrity_check(self) -> tuple[bool, str]:
        """
        Con Neon ya no existe un archivo que se pueda corromper localmente
        (eso lo maneja Neon), así que esto pasó a ser un simple chequeo de
        conectividad: sirve para detectar temprano si el bot perdió la
        conexión a la base (ver backup_task.db_health_loop) en vez de
        enterarse recién cuando falla una compra real.
        """
        try:
            with self._conn() as conn:
                conn.execute("SELECT 1")
            return True, "ok"
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"

    # ── Insertar ──────────────────────────────────────────────────────────────

    def create_transaction(
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
        VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending')
        RETURNING id
        """
        with self._conn() as conn:
            cur = conn.execute(
                sql,
                (user_id, service, service_name, country, country_name,
                 cost_herosms, amount_usd),
            )
            return cur.fetchone()["id"]

    # ── Actualizar campos individuales ────────────────────────────────────────

    def set_order_info(
        self, tx_id: int, order_id: str, pay_address: str,
        currency: str, network: str, pay_amount: float,
        token_id: str = None,
    ):
        self._update(
            tx_id,
            order_id=order_id,
            pay_address=pay_address,
            currency=currency,
            network=network,
            pay_amount=pay_amount,
            token_id=token_id,
        )

    def set_activation(self, tx_id: int, activation_id: str, phone_number: str):
        self._update(tx_id, activation_id=activation_id, phone_number=phone_number)

    def set_refund_address(self, tx_id: int, refund_address: str):
        self._update(tx_id, refund_address=refund_address)

    def set_purchase_proof(
        self, tx_id: int, proof_file_id: str = None,
        proof_file_unique_id: str = None, proof_text: str = None,
    ):
        """
        Guarda el comprobante (foto/texto) de un pago CUP ligado directo a
        una compra. Ver find_reused_proof para la defensa contra reuso de
        la misma captura en varias órdenes.
        """
        self._update(
            tx_id,
            proof_file_id=proof_file_id,
            proof_file_unique_id=proof_file_unique_id,
            proof_text=proof_text,
        )

    def find_reused_proof(
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
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, status FROM transactions "
                "WHERE proof_file_unique_id = %s AND id != %s",
                (proof_file_unique_id, exclude_tx_id or -1),
            ).fetchall()
            matches.extend({"kind": "compra", "id": r["id"], "status": r["status"]} for r in rows)

            rows = conn.execute(
                "SELECT id, status FROM manual_deposits "
                "WHERE proof_file_unique_id = %s AND id != %s",
                (proof_file_unique_id, exclude_dep_id or -1),
            ).fetchall()
            matches.extend({"kind": "depósito", "id": r["id"], "status": r["status"]} for r in rows)

        return matches

    def set_sms_code(self, tx_id: int, sms_code: str):
        self._update(tx_id, sms_code=sms_code)

    def set_status(self, tx_id: int, status: str):
        self._update(tx_id, status=status)

    def _update(self, tx_id: int, **kwargs):
        kwargs["updated_at"] = datetime.utcnow()
        sets = ", ".join(f"{k} = %s" for k in kwargs)
        values = list(kwargs.values()) + [tx_id]
        sql = f"UPDATE transactions SET {sets} WHERE id = %s"
        with self._conn() as conn:
            conn.execute(sql, values)

    # ── Usuarios ──────────────────────────────────────────────────────────────

    def register_user(
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
        VALUES (%s, %s, %s, %s, %s, %s, NOW(), NOW())
        ON CONFLICT (user_id) DO UPDATE SET
            username      = EXCLUDED.username,
            first_name    = EXCLUDED.first_name,
            last_name     = EXCLUDED.last_name,
            language_code = EXCLUDED.language_code,
            is_premium    = EXCLUDED.is_premium,
            last_seen     = NOW()
        """
        try:
            with self._conn() as conn:
                conn.execute(sql, (
                    user_id, username, first_name, last_name, language_code,
                    int(is_premium) if is_premium is not None else None,
                ))
        except Exception as exc:
            logger.error("register_user(%s) error: %s: %s", user_id, type(exc).__name__, exc)

    def set_phone_number(self, user_id: int, phone_number: str) -> None:
        """
        Guarda el teléfono real del usuario cuando lo comparte de forma
        EXPLÍCITA (botón "compartir contacto" de Telegram). Nunca se pide
        como requisito, solo queda disponible para el admin si el usuario
        decide compartirlo.
        """
        with self._conn() as conn:
            conn.execute(
                "UPDATE users SET phone_number = %s, phone_verified_at = NOW() "
                "WHERE user_id = %s",
                (phone_number, user_id),
            )

    def get_user_count(self) -> int:
        """Total de usuarios distintos que alguna vez corrieron /start."""
        with self._conn() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()
            return int(row["c"] or 0)

    def get_user(self, user_id: int) -> Optional[dict]:
        """
        Fila cruda de la tabla `users`, o None si el usuario nunca corrió
        /start. Usado por telegram_sender.py para la tarjeta de bienvenida.
        """
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE user_id = %s", (user_id,)
            ).fetchone()
            return dict(row) if row else None

    def set_account_type(self, user_id: int, account_type: Optional[str]) -> bool:
        """
        Asigna el 'Nivel' de cuenta (cliente/reseller/vip). account_type=None
        borra el valor. Devuelve False si el usuario nunca corrió /start.
        """
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE users SET account_type = %s WHERE user_id = %s",
                (account_type, user_id),
            )
            return cur.rowcount > 0

    def set_country(self, user_id: int, country: Optional[str]) -> bool:
        """Asigna el país (texto libre). country=None borra el valor."""
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE users SET country = %s WHERE user_id = %s",
                (country, user_id),
            )
            return cur.rowcount > 0

    def count_completed_orders(self, user_id: int) -> int:
        """Total de compras COMPLETADAS (código OTP recibido y confirmado) del usuario."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM transactions "
                "WHERE user_id = %s AND status = 'completed'",
                (user_id,),
            ).fetchone()
            return int(row["c"] or 0)

    # ── Saldo interno (wallet virtual) ───────────────────────────────────────

    _ORIGIN_COLUMN = {"crypto": "balance_usd", "cup": "balance_usd_cup"}
    EPSILON = 1e-9

    def get_balance(self, user_id: int) -> float:
        """Saldo TOTAL (cripto + CUP) para mostrar en /saldo y para pagar compras."""
        b = self.get_balance_breakdown(user_id)
        return b["total"]

    def get_balance_breakdown(self, user_id: int) -> dict:
        """Devuelve {"crypto": x, "cup": y, "total": x+y}."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT balance_usd, balance_usd_cup FROM balances WHERE user_id = %s",
                (user_id,),
            ).fetchone()
            crypto = float(row["balance_usd"]) if row else 0.0
            cup = float(row["balance_usd_cup"]) if row else 0.0
            return {"crypto": crypto, "cup": cup, "total": crypto + cup}

    def credit_balance(
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
        with self._conn() as conn:
            conn.execute(
                f"INSERT INTO balances (user_id, {column}, updated_at) "
                f"VALUES (%s, %s, NOW()) "
                f"ON CONFLICT (user_id) DO UPDATE SET "
                f"{column} = balances.{column} + EXCLUDED.{column}, "
                f"updated_at = EXCLUDED.updated_at",
                (user_id, amount_usd),
            )
            conn.execute(
                "INSERT INTO balance_ledger (user_id, tx_id, delta_usd, origin, reason) "
                "VALUES (%s, %s, %s, %s, %s)",
                (user_id, tx_id, amount_usd, origin, reason),
            )
            row = conn.execute(
                "SELECT balance_usd, balance_usd_cup FROM balances WHERE user_id = %s",
                (user_id,),
            ).fetchone()
            return float(row["balance_usd"]) + float(row["balance_usd_cup"])

    def debit_balance(
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
        with self._conn() as conn:
            row = conn.execute(
                "SELECT balance_usd, balance_usd_cup FROM balances WHERE user_id = %s",
                (user_id,),
            ).fetchone()
            crypto_bal = float(row["balance_usd"]) if row else 0.0
            cup_bal = float(row["balance_usd_cup"]) if row else 0.0

            if origin in ("crypto", "cup"):
                column = self._ORIGIN_COLUMN[origin]
                current = crypto_bal if origin == "crypto" else cup_bal
                if amount_usd - current > self.EPSILON:
                    return False
                new_value = 0.0 if amount_usd >= current - self.EPSILON else current - amount_usd
                conn.execute(
                    f"UPDATE balances SET {column} = %s, updated_at = NOW() WHERE user_id = %s",
                    (new_value, user_id),
                )
                conn.execute(
                    "INSERT INTO balance_ledger (user_id, tx_id, delta_usd, origin, reason) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (user_id, tx_id, -amount_usd, origin, reason),
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

            conn.execute(
                "UPDATE balances SET balance_usd = %s, balance_usd_cup = %s, "
                "updated_at = NOW() WHERE user_id = %s",
                (new_crypto, new_cup, user_id),
            )
            if cup_used > self.EPSILON:
                conn.execute(
                    "INSERT INTO balance_ledger (user_id, tx_id, delta_usd, origin, reason) "
                    "VALUES (%s, %s, %s, 'cup', %s)",
                    (user_id, tx_id, -cup_used, reason),
                )
            if crypto_used > self.EPSILON:
                conn.execute(
                    "INSERT INTO balance_ledger (user_id, tx_id, delta_usd, origin, reason) "
                    "VALUES (%s, %s, %s, 'crypto', %s)",
                    (user_id, tx_id, -crypto_used, reason),
                )
            return True

    def get_purchase_origin_ratios(self, tx: dict) -> dict:
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
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT COALESCE(origin, 'crypto') AS origin, SUM(-delta_usd) AS debited "
                    "FROM balance_ledger WHERE tx_id = %s AND delta_usd < 0 "
                    "GROUP BY origin",
                    (tx_id,),
                ).fetchall()
            totals = {r["origin"]: float(r["debited"] or 0) for r in rows}
            grand_total = sum(totals.values())
            if grand_total <= self.EPSILON:
                return {"crypto": 1.0}
            return {origin: amt / grand_total for origin, amt in totals.items()}

        return {"crypto": 1.0}

    # ── Depósitos (agregar saldo) ────────────────────────────────────────────

    def create_deposit(self, user_id: int, amount_usd: float) -> int:
        """Crea un registro de depósito inicial (aún sin orden) y devuelve su id."""
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO deposits (user_id, amount_usd, status) "
                "VALUES (%s, %s, 'pending') RETURNING id",
                (user_id, amount_usd),
            )
            return cur.fetchone()["id"]

    def set_deposit_order_info(
        self, deposit_id: int, order_id: str, pay_address: str,
        currency: str, network: str, pay_amount: float, token_id: str,
    ):
        with self._conn() as conn:
            conn.execute(
                "UPDATE deposits SET order_id = %s, pay_address = %s, currency = %s, "
                "network = %s, pay_amount = %s, token_id = %s, updated_at = NOW() "
                "WHERE id = %s",
                (order_id, pay_address, currency, network, pay_amount, token_id, deposit_id),
            )

    def set_deposit_status(self, deposit_id: int, status: str):
        with self._conn() as conn:
            conn.execute(
                "UPDATE deposits SET status = %s, updated_at = NOW() WHERE id = %s",
                (status, deposit_id),
            )

    def get_deposit_by_id(self, deposit_id: int) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM deposits WHERE id = %s", (deposit_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_deposit_by_order_id(self, order_id: str) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM deposits WHERE order_id = %s", (order_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_pending_deposits(self) -> list[dict]:
        """Depósitos con una orden de pago generada, todavía sin confirmar (recovery al reiniciar)."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM deposits WHERE status = 'pending' AND order_id IS NOT NULL"
            ).fetchall()
            return [dict(r) for r in rows]

    def get_last_completed_deposit(self, user_id: int) -> Optional[dict]:
        """Depósito COMPLETADO más reciente del usuario (currency/network/token_id), o None."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT currency, network, token_id FROM deposits "
                "WHERE user_id = %s AND status = 'completed' AND token_id IS NOT NULL "
                "ORDER BY updated_at DESC LIMIT 1",
                (user_id,),
            ).fetchone()
            return dict(row) if row else None

    # ── Depósitos manuales (CUP vía Transfermóvil / EnZona) ──────────────────

    def create_manual_deposit(
        self, user_id: int, method: str, amount_usd: float,
        amount_cup: int = None, cup_rate: float = None,
    ) -> dict:
        """Crea el registro y genera su reference_code (REF-000123) a partir del id."""
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO manual_deposits (user_id, method, amount_usd, amount_cup, cup_rate, status) "
                "VALUES (%s, %s, %s, %s, %s, 'awaiting_proof') RETURNING id",
                (user_id, method, amount_usd, amount_cup, cup_rate),
            )
            dep_id = cur.fetchone()["id"]
            reference_code = f"REF-{dep_id:06d}"
            conn.execute(
                "UPDATE manual_deposits SET reference_code = %s WHERE id = %s",
                (reference_code, dep_id),
            )
            return {"id": dep_id, "reference_code": reference_code}

    def set_manual_deposit_proof(
        self, dep_id: int, proof_file_id: str = None,
        proof_file_unique_id: str = None, proof_text: str = None,
    ):
        self._update_manual_deposit(
            dep_id, status="pending_review",
            proof_file_id=proof_file_id,
            proof_file_unique_id=proof_file_unique_id,
            proof_text=proof_text,
        )

    def set_manual_deposit_status(self, dep_id: int, status: str, reviewed_by: int = None):
        kwargs = {"status": status}
        if reviewed_by is not None:
            kwargs["reviewed_by"] = reviewed_by
        self._update_manual_deposit(dep_id, **kwargs)

    def _update_manual_deposit(self, dep_id: int, **kwargs):
        kwargs["updated_at"] = datetime.utcnow()
        sets = ", ".join(f"{k} = %s" for k in kwargs)
        values = list(kwargs.values()) + [dep_id]
        sql = f"UPDATE manual_deposits SET {sets} WHERE id = %s"
        with self._conn() as conn:
            conn.execute(sql, values)

    def get_manual_deposit_by_id(self, dep_id: int) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM manual_deposits WHERE id = %s", (dep_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_pending_manual_deposit(self, user_id: int) -> Optional[dict]:
        """Solicitud NO resuelta más reciente del usuario (máximo 1 pendiente a la vez)."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM manual_deposits WHERE user_id = %s "
                "AND status IN ('awaiting_proof', 'pending_review') "
                "ORDER BY created_at DESC LIMIT 1",
                (user_id,),
            ).fetchone()
            return dict(row) if row else None

    def get_pending_manual_deposits_for_review(self) -> list[dict]:
        """Todas las solicitudes esperando aprobación de un admin (para /pendientes)."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM manual_deposits WHERE status = 'pending_review' "
                "ORDER BY created_at ASC"
            ).fetchall()
            return [dict(r) for r in rows]

    def get_cup_exposure(self) -> dict:
        """CUP ya aprobado (acreditado) que todavía no se marcó como convertido a USDT real."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c, COALESCE(SUM(amount_cup),0) AS cup, "
                "COALESCE(SUM(amount_usd),0) AS usd "
                "FROM manual_deposits "
                "WHERE status = 'approved' AND converted_to_usdt = 0"
            ).fetchone()
            return {
                "count": int(row["c"] or 0),
                "total_cup": int(row["cup"] or 0),
                "total_usd": float(row["usd"] or 0),
            }

    def get_unconverted_manual_deposits(self) -> list[dict]:
        """Detalle de los depósitos aprobados y aún sin convertir (para /exposicion_cup)."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM manual_deposits "
                "WHERE status = 'approved' AND converted_to_usdt = 0 "
                "ORDER BY created_at ASC"
            ).fetchall()
            return [dict(r) for r in rows]

    def mark_manual_deposits_converted(self, dep_ids: list[int]):
        """Marca uno o más depósitos como ya convertidos a USDT real."""
        if not dep_ids:
            return
        with self._conn() as conn:
            placeholders = ",".join("%s" for _ in dep_ids)
            conn.execute(
                f"UPDATE manual_deposits SET converted_to_usdt = 1, "
                f"updated_at = NOW() WHERE id IN ({placeholders})",
                dep_ids,
            )

    # ── Retiros manuales (CUP vía Transfermóvil / EnZona) ────────────────────

    def create_manual_withdrawal(
        self, user_id: int, method: str, destination: str,
        amount_usd: float, fee_usd: float, net_usd: float,
        amount_cup: int, cup_rate: float,
    ) -> dict:
        """Crea la solicitud y genera su reference_code (WD-000123) a partir del id."""
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO manual_withdrawals "
                "(user_id, method, destination, amount_usd, fee_usd, net_usd, "
                "amount_cup, cup_rate, status) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'pending_review') RETURNING id",
                (user_id, method, destination, amount_usd, fee_usd, net_usd,
                 amount_cup, cup_rate),
            )
            wd_id = cur.fetchone()["id"]
            reference_code = f"WD-{wd_id:06d}"
            conn.execute(
                "UPDATE manual_withdrawals SET reference_code = %s WHERE id = %s",
                (reference_code, wd_id),
            )
            return {"id": wd_id, "reference_code": reference_code}

    def set_manual_withdrawal_status(self, wd_id: int, status: str, reviewed_by: int = None):
        kwargs = {"status": status, "updated_at": datetime.utcnow()}
        if reviewed_by is not None:
            kwargs["reviewed_by"] = reviewed_by
        sets = ", ".join(f"{k} = %s" for k in kwargs)
        values = list(kwargs.values()) + [wd_id]
        with self._conn() as conn:
            conn.execute(f"UPDATE manual_withdrawals SET {sets} WHERE id = %s", values)

    def get_manual_withdrawal_by_id(self, wd_id: int) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM manual_withdrawals WHERE id = %s", (wd_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_pending_manual_withdrawal(self, user_id: int) -> Optional[dict]:
        """Igual que get_pending_manual_deposit pero para retiros: máximo 1 solicitud en curso."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM manual_withdrawals WHERE user_id = %s "
                "AND status = 'pending_review' ORDER BY created_at DESC LIMIT 1",
                (user_id,),
            ).fetchone()
            return dict(row) if row else None

    def get_by_id(self, tx_id: int) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM transactions WHERE id = %s", (tx_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_by_order_id(self, order_id: str) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM transactions WHERE order_id = %s", (order_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_user_transactions(self, user_id: int, limit: int = 10) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM transactions WHERE user_id = %s "
                "ORDER BY created_at DESC LIMIT %s",
                (user_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_pending_transactions(self) -> list[dict]:
        """Transacciones en estados activos (útil para recovery al reiniciar)."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM transactions WHERE status IN ('pending', 'paid', 'number_assigned')"
            ).fetchall()
            return [dict(r) for r in rows]

    def get_abuse_strikes(self, user_id: int, hours: int) -> int:
        """
        Cuenta cuántas veces este usuario recibió un número y la operación
        terminó SIN completarse en las últimas `hours` horas. Usado por
        handlers.cb_new_purchase para bloquear temporalmente abuso.
        """
        cutoff = datetime.utcnow() - timedelta(hours=hours)
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM transactions "
                "WHERE user_id = %s AND phone_number IS NOT NULL "
                "AND status IN ('sms_timeout', 'refunded', 'error') "
                "AND created_at >= %s",
                (user_id, cutoff),
            ).fetchone()
            return int(row["c"] or 0)

    def get_stats(self, days: Optional[int] = None) -> dict:
        """Métricas agregadas para el comando de admin /stats."""
        where = ""
        params: list = []
        if days is not None:
            where = "WHERE created_at >= %s"
            params.append(datetime.utcnow() - timedelta(days=int(days)))

        with self._conn() as conn:
            status_rows = conn.execute(
                f"SELECT status, COUNT(*) AS c FROM transactions {where} GROUP BY status",
                params,
            ).fetchall()
            by_status = {r["status"]: r["c"] for r in status_rows}

            completed_where = f"{where}{' AND' if where else 'WHERE'} status = 'completed'"
            revenue_row = conn.execute(
                f"SELECT COALESCE(SUM(amount_usd),0) AS revenue, "
                f"COALESCE(SUM(cost_herosms),0) AS cost, COUNT(*) AS n "
                f"FROM transactions {completed_where}",
                params,
            ).fetchone()

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

    def get_recent_sales(self, limit: int = 10) -> list[dict]:
        """Últimas ventas completadas de TODOS los usuarios (para /ventas)."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM transactions WHERE status = 'completed' "
                "ORDER BY updated_at DESC LIMIT %s",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_country_success_stats(self, service: str, min_samples: int = 5) -> dict:
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
        WHERE service = %s AND phone_number IS NOT NULL
        GROUP BY country
        HAVING COUNT(*) >= %s
        """
        with self._conn() as conn:
            rows = conn.execute(sql, (service, min_samples)).fetchall()

        return {
            r["country"]: {
                "attempts": r["attempts"],
                "completed": r["completed"],
                "rate": round(100 * r["completed"] / r["attempts"], 1),
            }
            for r in rows
        }

    # ── Outbox de notificaciones (ver outbox.py) ──────────────────────────────

    def enqueue_outbox(self, chat_id: int, text: str, reply_markup: Optional[str] = None) -> int:
        """Encola un mensaje ANTES de intentar enviarlo (sobrevive a un crash del proceso)."""
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO outbox (chat_id, text, reply_markup) VALUES (%s, %s, %s) RETURNING id",
                (chat_id, text, reply_markup),
            )
            return cur.fetchone()["id"]

    def mark_outbox_sent(self, outbox_id: int):
        with self._conn() as conn:
            conn.execute(
                "UPDATE outbox SET status = 'sent', updated_at = NOW() WHERE id = %s",
                (outbox_id,),
            )

    def mark_outbox_attempt_failed(
        self, outbox_id: int, error: str, next_attempt_at, give_up: bool = False,
    ):
        """
        Registra un intento fallido. `next_attempt_at` puede ser un string
        "YYYY-MM-DD HH:MM:SS" o un datetime (outbox.py manda un string,
        Postgres lo castea solo al insertar en una columna TIMESTAMPTZ).
        """
        with self._conn() as conn:
            conn.execute(
                "UPDATE outbox SET attempts = attempts + 1, last_error = %s, "
                "status = %s, next_attempt_at = %s, updated_at = NOW() WHERE id = %s",
                (error, "dead" if give_up else "pending", next_attempt_at, outbox_id),
            )

    def get_due_outbox(self, limit: int = 200) -> list[dict]:
        """Mensajes 'pending' cuyo próximo intento ya venció, los más viejos primero."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM outbox WHERE status = 'pending' "
                "AND next_attempt_at <= NOW() "
                "ORDER BY created_at ASC LIMIT %s",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_top_services(self, limit: int = 8, days: Optional[int] = None) -> list[dict]:
        """Servicios más comprados (solo compras EXITOSAS), de mayor a menor cantidad."""
        sql = """
        SELECT service AS code, service_name AS name, COUNT(*) AS count
        FROM transactions
        WHERE status = 'completed'
        """
        params: list = []
        if days is not None:
            sql += " AND created_at >= %s"
            params.append(datetime.utcnow() - timedelta(days=int(days)))
        sql += " GROUP BY service, service_name ORDER BY count DESC LIMIT %s"
        params.append(limit)

        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]


# Instancia global compartida
db = Database()
