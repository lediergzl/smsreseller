"""
fix_missing_refund.py - Acredita manualmente como saldo interno un reembolso
que falló ANTES de que existiera el sistema de saldo interno (wallet virtual).

Uso (ejecutar en el mismo directorio que database.py y config.py, con
DATABASE_URL apuntando a tu base Neon real):

    python fix_missing_refund.py <activation_id_o_id_de_tx>

Ejemplo con el caso real:
    python fix_missing_refund.py 597590851

Busca la transacción, muestra sus datos, te pregunta si quieres acreditar
el monto completo o con el mismo descuento (REFUND_FEE_PCT) que se aplica
normalmente en timeouts de SMS, y recién ahí hace el cambio.

NOTA: este script quedó desactualizado tras la migración de la base a
Postgres/Neon + asyncpg (ver database.py) — usaba psycopg2 sincrónico con
placeholders "%s". Esta versión usa `await db.connect()` y placeholders
"$1, $2, ..." de asyncpg, igual que el resto del bot.
"""
import asyncio
import sys

from database import db
from config import REFUND_FEE_PCT


async def find_tx(identifier: str):
    async with db._conn() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM transactions WHERE activation_id = $1", identifier
        )
        if row:
            return dict(row)
        if identifier.isdigit():
            row = await conn.fetchrow(
                "SELECT * FROM transactions WHERE id = $1", int(identifier)
            )
            if row:
                return dict(row)
    return None


async def main():
    if len(sys.argv) != 2:
        print("Uso: python fix_missing_refund.py <activation_id_o_id_de_tx>")
        sys.exit(1)

    identifier = sys.argv[1]

    await db.connect()
    tx = await find_tx(identifier)
    if not tx:
        print(f"No se encontró ninguna transacción con activation_id/id = {identifier}")
        sys.exit(1)

    print("Transacción encontrada:")
    for k in ("id", "user_id", "service_name", "country_name", "amount_usd",
              "pay_amount", "currency", "network", "status", "phone_number",
              "activation_id", "refund_address", "created_at"):
        print(f"  {k}: {tx.get(k)}")

    if tx["status"] == "refunded":
        print("\n⚠️  Esta transacción YA está marcada como 'refunded'. "
              "Si continúas, se acreditaría el saldo DE NUEVO (duplicado).")
    elif tx["status"] not in ("sms_timeout", "error"):
        print(f"\n⚠️  El estado actual es '{tx['status']}', no 'sms_timeout'/'error'. "
              "Verifica que sea la transacción correcta antes de continuar.")

    full_amount = float(tx["amount_usd"] or 0)
    discounted = round(full_amount * (1 - REFUND_FEE_PCT), 4)

    print(f"\nMonto completo:                {full_amount:.2f} USD")
    print(f"Con cargo de servicio ({REFUND_FEE_PCT:.0%}):  {discounted:.2f} USD  "
          "(igual al que aplicarían timeouts de SMS normales)")

    choice = input(
        "\n¿Qué monto acreditar? [c]ompleto / [d]escontado / [n]ada (cancelar): "
    ).strip().lower()

    if choice == "c":
        amount = full_amount
    elif choice == "d":
        amount = discounted
    else:
        print("Cancelado, no se hizo ningún cambio.")
        return

    new_balance = await db.credit_balance(
        tx["user_id"], amount, tx["id"],
        reason=f"Recuperación manual - reembolso fallido tx={tx['id']}",
    )
    await db.set_status(tx["id"], "refunded")

    print(f"\n✅ Acreditados {amount:.2f} USD.")
    print(f"   Nuevo saldo del usuario {tx['user_id']}: {new_balance:.2f} USD")
    print("   El usuario ya puede verlo con /saldo en el bot.")

    await db.close()


if __name__ == "__main__":
    asyncio.run(main())
