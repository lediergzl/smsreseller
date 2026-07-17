"""
restore_backup.py - Restaura la base de datos desde uno de los backups
automáticos generados por backup_task.py.

IMPORTANTE: corre esto con el BOT DETENIDO. Restaurar mientras el bot
tiene sms_reseller.db abierta puede dejar todo en un estado inconsistente.

Uso:
    python restore_backup.py                          # lista backups disponibles
    python restore_backup.py backups/backup_20260712_030000.db
"""
import os
import shutil
import sys
from datetime import datetime

import config
from database import DB_PATH


def list_backups() -> list[str]:
    if not os.path.isdir(config.BACKUP_DIR):
        print(f"No existe el directorio de backups: {config.BACKUP_DIR}")
        return []
    return sorted(
        f for f in os.listdir(config.BACKUP_DIR)
        if f.startswith("backup_") and f.endswith(".db")
    )


def main():
    if len(sys.argv) == 1:
        files = list_backups()
        if not files:
            print("No hay backups locales disponibles.")
            print(
                "Si BACKUP_SEND_TELEGRAM está activo, también puedes buscar "
                "el backup más reciente en el canal de admin de Telegram y "
                "descargarlo manualmente antes de restaurar."
            )
            return
        print("Backups disponibles (más viejo primero, el último es el más reciente):")
        for f in files:
            print(f"  {os.path.join(config.BACKUP_DIR, f)}")
        print("\nUso: python restore_backup.py <ruta_al_backup>")
        return

    backup_path = sys.argv[1]
    if not os.path.isfile(backup_path):
        print(f"No se encontró el archivo: {backup_path}")
        sys.exit(1)

    print(f"⚠️  Esto va a REEMPLAZAR {DB_PATH} con el contenido de {backup_path}.")
    print("Asegúrate de que el bot esté DETENIDO antes de continuar.")
    confirm = input("Escribe 'si' para confirmar: ").strip().lower()
    if confirm not in ("si", "sí", "yes", "y"):
        print("Cancelado, no se hizo ningún cambio.")
        return

    if os.path.isfile(DB_PATH):
        safety_copy = f"{DB_PATH}.before_restore_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
        shutil.copy2(DB_PATH, safety_copy)
        print(f"Copia de la base actual (por si acaso) guardada en: {safety_copy}")

    shutil.copy2(backup_path, DB_PATH)

    # El modo WAL deja archivos -wal/-shm junto a la base; si son de la base
    # VIEJA (ya reemplazada) hay que borrarlos para que SQLite no intente
    # "recuperar" cambios que ya no corresponden al archivo restaurado.
    for ext in ("-wal", "-shm"):
        stale = DB_PATH + ext
        if os.path.isfile(stale):
            os.remove(stale)
            print(f"Eliminado archivo residual: {stale}")

    print(f"\n✅ Base de datos restaurada desde {backup_path}.")
    print("Ya puedes volver a iniciar el bot.")


if __name__ == "__main__":
    main()
