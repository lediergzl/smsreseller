"""
telegram_sender.py - Conecta welcome_card.py con datos REALES: arma el
UserStats a partir de la base de datos y descarga la foto de perfil de
Telegram del usuario (si la tiene y es pública), y manda la tarjeta ya
generada como foto en el chat.

Aislado de handlers.py a propósito: así welcome_card.py sigue siendo
testeable sin bot ni DB (ver su docstring), y handlers.py solo necesita
llamar a `send_welcome_card(bot, message)`.

Nunca lanza hacia arriba: si algo falla generando/enviando la tarjeta
(Pillow, fuente faltante, foto corrupta, etc.), se loguea y el llamador
puede seguir con un mensaje de texto de respaldo (ver handlers.cmd_start).
"""
import logging
import os
import tempfile
import uuid
from datetime import datetime

from aiogram import Bot
from aiogram.types import Message, FSInputFile

from database import db
from welcome_card import UserStats, generate_welcome_card

logger = logging.getLogger(__name__)

CARDS_DIR = "/tmp/otpvirtual_cards"
PHOTOS_DIR = "/tmp/otpvirtual_profile_photos"


def _format_joined_at(raw: str | None) -> str | None:
    """'2026-07-14 07:12:00' (formato SQLite) -> '14/07/2026'. None si no hay dato
    o el formato es inesperado (mejor omitir la fila que mostrar algo raro)."""
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt).strftime("%d/%m/%Y")
        except ValueError:
            continue
    return None


async def _download_profile_photo(bot: Bot, user_id: int) -> str | None:
    """
    Descarga la foto de perfil más reciente del usuario a un archivo local
    y devuelve la ruta, o None si no tiene foto pública o falla la descarga
    (privacidad activada, usuario sin foto, error de red, etc. — todos
    casos normales, no errores que deban interrumpir el /start).
    """
    try:
        photos = await bot.get_user_profile_photos(user_id, limit=1)
        if not photos.photos:
            return None
        # Cada entrada de photos.photos es una lista de tamaños del mismo
        # avatar; el último es el de mayor resolución.
        largest = photos.photos[0][-1]
        file = await bot.get_file(largest.file_id)

        os.makedirs(PHOTOS_DIR, exist_ok=True)
        dest_path = os.path.join(PHOTOS_DIR, f"{user_id}_{uuid.uuid4().hex[:8]}.jpg")
        await bot.download_file(file.file_path, destination=dest_path)
        return dest_path
    except Exception as exc:
        logger.warning("No se pudo descargar foto de perfil de %s: %s", user_id, exc)
        return None


def _build_user_stats(user_id: int, username: str | None, first_name: str | None,
                       photo_path: str | None) -> UserStats:
    """Arma el UserStats leyendo SOLO datos que existen de verdad en la DB.
    'Tipo' (account_type) y 'País' (country) vienen de la tabla `users` y
    quedan en None hasta que un admin los asigna con /set_tipo o /set_pais
    (ver handlers.py) — mientras tanto la tarjeta simplemente no muestra
    esas filas (ver welcome_card.generate_welcome_card)."""
    breakdown = db.get_balance_breakdown(user_id)
    orders_count = db.count_completed_orders(user_id)
    user_row = db.get_user(user_id)
    joined_at = _format_joined_at(user_row.get("first_seen")) if user_row else None

    return UserStats(
        user_id=user_id,
        username=username,
        first_name=first_name,
        balance_usd=breakdown["crypto"],
        balance_usd_cup=breakdown["cup"],
        orders_count=orders_count,
        joined_at=joined_at,
        account_type=user_row.get("account_type") if user_row else None,
        country=user_row.get("country") if user_row else None,
        profile_photo_path=photo_path,
    )


async def send_welcome_card(bot: Bot, message: Message, caption: str,
                             parse_mode: str = "HTML", reply_markup=None) -> bool:
    """
    Genera y envía la tarjeta de bienvenida personalizada para
    `message.from_user`, con `caption` como texto acompañante y, opcionalmente,
    el teclado (`reply_markup`) del menú principal ya adjunto a la foto.

    Devuelve True si se envió la tarjeta como foto, False si falló y el
    llamador debería mandar `caption` como mensaje de texto normal en su
    lugar (fallback, ver handlers.cmd_start).
    """
    user = message.from_user
    photo_path = None
    card_path = None
    try:
        photo_path = await _download_profile_photo(bot, user.id)
        stats = _build_user_stats(user.id, user.username, user.first_name, photo_path)
        card_path = generate_welcome_card(stats, out_dir=CARDS_DIR)

        await message.answer_photo(
            FSInputFile(card_path),
            caption=caption,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
        )
        return True
    except Exception as exc:
        logger.error(
            "send_welcome_card falló para user_id=%s: %s: %s",
            user.id, type(exc).__name__, exc,
        )
        return False
    finally:
        # La tarjeta y la foto descargada son archivos de un solo uso: se
        # regeneran en cada /start (los datos cambian), así que no vale la
        # pena cachearlos ni dejarlos acumulándose en /tmp.
        for path in (photo_path, card_path):
            if path and os.path.isfile(path):
                try:
                    os.remove(path)
                except OSError:
                    pass
