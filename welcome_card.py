"""
welcome_card.py - Genera la credencial digital de bienvenida de OTPVirtual.

Segunda iteración del diseño: en vez de la tarjeta con lista larga de
beneficios (aprobada al inicio, ver git history / tarjeta_ejemplo_*.png),
esto es una credencial compacta estilo Binance/Stripe: logo, foto real del
usuario, nombre, datos de cuenta y un badge de "cuenta activa". Más cuadrada
y con texto grande para que se lea bien en móvil (el pitch de venta de
beneficios ahora vive en el texto del mensaje, ver utils.MSG_WELCOME, no en
la imagen — evita una tarjeta gigante y saturada).

No hace queries a la base ni llama a Telegram: recibe todo ya resuelto en
un `UserStats` y una foto de perfil ya descargada (opcional). Eso mantiene
este módulo testeable sin bot ni DB de por medio. Ver telegram_sender.py
para cómo se arma ese UserStats y se descarga la foto.
"""
import os
import logging
from dataclasses import dataclass
from typing import Optional

from PIL import Image, ImageDraw, ImageFont, ImageFilter

import config as cfg

logger = logging.getLogger(__name__)

CARD_W = cfg.CARD_W


@dataclass
class UserStats:
    """Todo lo que la tarjeta necesita saber sobre el usuario. Los campos
    opcionales que valen None simplemente no se muestran en la tarjeta, en
    vez de mostrar un dato inventado (hoy `account_type` y `country` NO
    existen como columnas reales en la base — ver database.py, tabla
    `users` — así que llegan en None hasta que se agreguen de verdad)."""
    user_id: int
    username: Optional[str] = None
    first_name: Optional[str] = None
    balance_usd: float = 0.0
    balance_usd_cup: float = 0.0
    orders_count: Optional[int] = None
    joined_at: Optional[str] = None       # ya formateado como string legible
    account_type: Optional[str] = None    # "Nivel" — None = no mostrar la fila
    country: Optional[str] = None         # "País" — None = no mostrar la fila
    profile_photo_path: Optional[str] = None  # PNG/JPG local ya descargado


def _font(path: str, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        # No debe volver a pasar lo que pasó en producción (fuente ausente
        # en el servidor real) tumbe el /start entero. Se ve peor (fuente
        # bitmap fija, sin tildes) pero la tarjeta sigue generándose.
        logger.error("No se pudo abrir la fuente %s, usando fuente por defecto.", path)
        try:
            return ImageFont.load_default(size=size)  # Pillow >= 10.1
        except TypeError:
            return ImageFont.load_default()  # Pillow más viejo, tamaño fijo


def _circular_avatar(path: Optional[str], diameter: int) -> Image.Image:
    """Foto de perfil REAL recortada en círculo con borde cian + halo, o una
    silueta genérica si Telegram no entregó ninguna foto (usuario sin foto
    pública, privacidad activada, o falló la descarga — ver
    telegram_sender._download_profile_photo, que ya intenta la foto real
    antes de llegar acá)."""
    canvas = Image.new("RGBA", (diameter, diameter), (0, 0, 0, 0))
    mask = Image.new("L", (diameter, diameter), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, diameter, diameter), fill=255)

    if path and os.path.isfile(path):
        img = Image.open(path).convert("RGBA")
        w, h = img.size
        side = min(w, h)
        img = img.crop(((w - side) // 2, (h - side) // 2, (w + side) // 2, (h + side) // 2))
        img = img.resize((diameter, diameter), Image.LANCZOS)
        canvas.paste(img, (0, 0), mask)
    else:
        d = ImageDraw.Draw(canvas)
        d.ellipse((0, 0, diameter, diameter), fill=(*cfg.BLUE, 255))
        r = diameter
        d.ellipse((r*0.32, r*0.20, r*0.68, r*0.56), fill=(*cfg.LIGHT_GRAY, 255))
        d.pieslice((r*0.15, r*0.55, r*0.85, r*1.15), start=180, end=360, fill=(*cfg.LIGHT_GRAY, 255))

    ring = Image.new("RGBA", (diameter, diameter), (0, 0, 0, 0))
    ImageDraw.Draw(ring).ellipse((3, 3, diameter - 3, diameter - 3), outline=(*cfg.CYAN, 255), width=7)
    return Image.alpha_composite(canvas, ring)


def _draw_check_badge(draw: ImageDraw.ImageDraw, card: Image.Image, cx: int, cy: int,
                       text: str, f_text: ImageFont.FreeTypeFont):
    """Badge tipo 'pill' verde con un check vectorial (no emoji: evita
    depender de glifos que la fuente del servidor podría no tener)."""
    tw = draw.textlength(text, font=f_text)
    pad_x, icon_gap, icon_d = 34, 14, 26
    pill_w = int(icon_d + icon_gap + tw + pad_x * 2)
    pill_h = 60
    x0, y0 = cx - pill_w // 2, cy - pill_h // 2
    x1, y1 = cx + pill_w // 2, cy + pill_h // 2

    draw.rounded_rectangle((x0, y0, x1, y1), radius=pill_h // 2, fill=(34, 197, 94))

    icx, icy = x0 + pad_x + icon_d // 2, cy
    r = icon_d / 2
    draw.line((icx - r*0.5, icy, icx - r*0.1, icy + r*0.45), fill=cfg.WHITE, width=5)
    draw.line((icx - r*0.1, icy + r*0.45, icx + r*0.6, icy - r*0.45), fill=cfg.WHITE, width=5)

    draw.text((x0 + pad_x + icon_d + icon_gap, y0 + (pill_h - f_text.size) / 2 - 4),
               text, font=f_text, fill=cfg.WHITE)


def generate_welcome_card(stats: UserStats, out_dir: str = "/tmp/otpvirtual_cards") -> str:
    """Genera la credencial y devuelve la ruta del PNG resultante."""
    os.makedirs(out_dir, exist_ok=True)

    f_wordmark = _font(cfg.FONT_BOLD, 38)
    f_greeting = _font(cfg.FONT_REG, 28)
    f_name = _font(cfg.FONT_BOLD, 52)
    f_label = _font(cfg.FONT_REG, 28)
    f_value = _font(cfg.FONT_BOLD, 32)
    f_badge = _font(cfg.FONT_BOLD, 26)
    f_footer_brand = _font(cfg.FONT_BOLD, 28)
    f_footer_tag = _font(cfg.FONT_REG, 22)

    # ── Filas de datos: solo las que tienen un valor real ────────────────
    rows = [("ID", str(stats.user_id))]
    if stats.username:
        rows.append(("Usuario", f"@{stats.username}"))
    if stats.account_type:
        label = cfg.ACCOUNT_TYPE_LABELS.get(stats.account_type.lower(), stats.account_type)
        rows.append(("Nivel", label))
    if stats.country:
        rows.append(("País", stats.country))
    total_balance = stats.balance_usd + stats.balance_usd_cup
    rows.append(("Balance", f"${total_balance:.2f} USD"))
    if stats.orders_count is not None:
        rows.append(("Pedidos", str(stats.orders_count)))
    if stats.joined_at:
        rows.append(("Miembro desde", stats.joined_at))

    # ── Layout (todo medido antes de crear el canvas final) ──────────────
    header_h = 130
    avatar_d = 260
    avatar_top_pad = 50
    avatar_bottom_pad = 30

    greeting_h = 28 + 6 + 60 + 30  # "Bienvenido" + gap + Nombre + padding

    box_margin = 60
    box_pad = 34
    row_h = 74
    box_h = box_pad * 2 + row_h * len(rows)

    badge_h = 60
    badge_section_h = 40 + badge_h + 40

    footer_h = 110

    card_h = int(
        header_h + avatar_top_pad + avatar_d + avatar_bottom_pad
        + greeting_h + box_h + badge_section_h + footer_h
    )

    card = Image.new("RGB", (CARD_W, card_h), cfg.NAVY)
    draw = ImageDraw.Draw(card)

    # ── Header: logo + wordmark, compacto ────────────────────────────────
    logo_d = 80
    if os.path.isfile(cfg.LOGO_PATH):
        logo = Image.open(cfg.LOGO_PATH).convert("RGBA").resize((logo_d, logo_d), Image.LANCZOS)
        wm_w = draw.textlength("OTPVIRTUAL", font=f_wordmark)
        group_w = logo_d + 22 + wm_w
        lx = int((CARD_W - group_w) / 2)
        ly = int((header_h - logo_d) / 2)
        card.paste(logo, (lx, ly), logo)
        draw.text((lx + logo_d + 22, header_h / 2 - 22), "OTPVIRTUAL", font=f_wordmark, fill=cfg.WHITE)
    else:
        wm_w = draw.textlength("OTPVIRTUAL", font=f_wordmark)
        draw.text(((CARD_W - wm_w) / 2, header_h / 2 - 22), "OTPVIRTUAL", font=f_wordmark, fill=cfg.WHITE)

    draw.line((0, header_h, CARD_W, header_h), fill=(30, 48, 71), width=2)

    # ── Avatar con halo ───────────────────────────────────────────────────
    halo_d = avatar_d + 40
    halo = Image.new("RGBA", (halo_d, halo_d), (0, 0, 0, 0))
    ImageDraw.Draw(halo).ellipse((0, 0, halo_d, halo_d), fill=(*cfg.CYAN, 50))
    halo = halo.filter(ImageFilter.GaussianBlur(10))
    ay = header_h + avatar_top_pad
    ax = (CARD_W - avatar_d) // 2
    card.paste(halo, (ax - 20, ay - 20), halo)

    avatar = _circular_avatar(stats.profile_photo_path, avatar_d)
    card.paste(avatar, (ax, ay), avatar)

    # ── Saludo + nombre ───────────────────────────────────────────────────
    gy = ay + avatar_d + avatar_bottom_pad
    gw = draw.textlength("Bienvenido", font=f_greeting)
    draw.text(((CARD_W - gw) / 2, gy), "Bienvenido", font=f_greeting, fill=cfg.LIGHT_CYAN)

    display_name = stats.first_name or (f"@{stats.username}" if stats.username else "Usuario")
    nw = draw.textlength(display_name, font=f_name)
    draw.text(((CARD_W - nw) / 2, gy + 34), display_name, font=f_name, fill=cfg.WHITE)

    # ── Panel de datos (glass panel, un tono más claro que el fondo) ─────
    box_top = gy + greeting_h
    draw.rounded_rectangle(
        (box_margin, box_top, CARD_W - box_margin, box_top + box_h),
        radius=26, fill=(23, 40, 61),
    )
    ry = box_top + box_pad
    for label, value in rows:
        draw.text((box_margin + 34, ry + 6), label, font=f_label, fill=(168, 178, 194))
        vw = draw.textlength(value, font=f_value)
        draw.text((CARD_W - box_margin - 34 - vw, ry), value, font=f_value, fill=cfg.CYAN)
        if (label, value) != rows[-1]:
            sep_y = ry + row_h - 20
            draw.line((box_margin + 30, sep_y, CARD_W - box_margin - 30, sep_y),
                      fill=(40, 58, 82), width=1)
        ry += row_h

    # ── Badge de estado ───────────────────────────────────────────────────
    badge_cy = box_top + box_h + 40 + badge_h // 2
    _draw_check_badge(draw, card, CARD_W // 2, badge_cy, "Cuenta activa", f_badge)

    # ── Footer ────────────────────────────────────────────────────────────
    footer_top = badge_cy + badge_h // 2 + 30
    fw = draw.textlength("OTPVIRTUAL", font=f_footer_brand)
    draw.text(((CARD_W - fw) / 2, footer_top), "OTPVIRTUAL", font=f_footer_brand, fill=cfg.WHITE)
    tw = draw.textlength(cfg.TAGLINE, font=f_footer_tag)
    draw.text(((CARD_W - tw) / 2, footer_top + 38), cfg.TAGLINE, font=f_footer_tag, fill=cfg.LIGHT_CYAN)

    out_path = os.path.join(out_dir, f"welcome_{stats.user_id}.png")
    card.save(out_path, "PNG")
    return out_path