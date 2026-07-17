"""
test_teth_payment.py - Prueba manual del flujo de pago con TETH (Sepolia
testnet) contra CCPayment, SIN pasar por el bot de Telegram ni por la
cotización en USD (TETH no tiene precio real, getCoinUSDTPrice devuelve 0).

Uso:
    python test_teth_payment.py listar          -> encuentra el token_id de TETH
    python test_teth_payment.py orden            -> crea una orden de 0.01 TETH
    python test_teth_payment.py estado <orderId> -> consulta el estado de una orden

Requiere el mismo .env que el bot (CCPAY_APP_ID, CCPAY_APP_SECRET, CCPAY_API_URL).
Corré este script desde la misma carpeta donde están config.py y ccpay_api.py.
"""
import asyncio
import sys

# Fix para Windows: aiodns (usado por aiohttp) requiere SelectorEventLoop,
# pero el loop por defecto en Windows es ProactorEventLoop. Debe configurarse
# ANTES de crear cualquier event loop (mismo fix que tiene main.py).
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import ccpay_api as ccpay


async def listar_teth():
    """Busca entre TODAS las monedas/redes habilitadas la que corresponda a TETH."""
    supported = await ccpay.get_supported_currencies(max_coins=50)
    if not supported:
        print("get_supported_currencies() devolvió vacío. Revisá CCPAY_APP_ID/SECRET en .env.")
        return

    matches = [c for c in supported if "TETH" in c["currency"].upper() or "SEPOLIA" in c["network"].upper()]

    if not matches:
        print("No encontré TETH en la lista de monedas soportadas.")
        print("Monedas disponibles (primeras 20):")
        for c in supported[:20]:
            print(f"  {c['currency']:10s} ({c['network']:12s}) -> token_id={c['token_id']}")
        return

    print("Coincidencias encontradas:")
    for c in matches:
        print(f"  currency={c['currency']}  network={c['network']}  token_id={c['token_id']}")
    print("\nCopiá el token_id que corresponda y usalo en 'orden' más abajo.")


async def crear_orden(token_id: str, monto: float = 0.01):
    """
    Crea una orden directo con create_order(), salteando get_estimated_amount
    (que descartaría TETH por no tener precio real en USD).
    """
    print(f"Creando orden: {monto} unidades, token_id={token_id} ...")
    order = await ccpay.create_order(monto, token_id, memo="test-teth")
    if not order:
        print("create_order devolvió None. Revisá los logs (logger.error) para el detalle del error.")
        return
    print("\n✅ Orden creada:")
    print(f"  orderId:    {order['orderId']}")
    print(f"  payAddress: {order['payAddress']}")
    print(f"  payAmount:  {order['payAmount']}")
    print(f"\nMandá {order['payAmount']} TETH a esa dirección desde el faucet de Sepolia,")
    print(f"y después corré: python test_teth_payment.py estado {order['orderId']}")


async def consultar_estado(order_id: str):
    status = await ccpay.get_order_status(order_id)
    labels = {
        ccpay.ORDER_STATUS_PENDING:   "PENDING (esperando pago)",
        ccpay.ORDER_STATUS_COMPLETED: "COMPLETED (¡pago recibido!)",
        ccpay.ORDER_STATUS_EXPIRED:   "EXPIRED",
        ccpay.ORDER_STATUS_CANCELLED: "CANCELLED",
        -1: "DESCONOCIDO (revisar logs / _STATUS_MAP)",
    }
    print(f"Estado de la orden {order_id}: {labels.get(status, status)}")


async def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    cmd = sys.argv[1]

    if cmd == "listar":
        await listar_teth()

    elif cmd == "orden":
        if len(sys.argv) < 3:
            print("Falta el token_id. Corré primero: python test_teth_payment.py listar")
            return
        token_id = sys.argv[2]
        monto = float(sys.argv[3]) if len(sys.argv) > 3 else 0.01
        await crear_orden(token_id, monto)

    elif cmd == "estado":
        if len(sys.argv) < 3:
            print("Falta el orderId. Uso: python test_teth_payment.py estado <orderId>")
            return
        await consultar_estado(sys.argv[2])

    else:
        print(__doc__)

    await ccpay.close_session()


if __name__ == "__main__":
    asyncio.run(main())