import os
import traceback
from datetime import datetime

from apscheduler.schedulers.blocking import BlockingScheduler
try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

import scrapping_general


def _limpiar_valor_env(valor: str) -> str:
    limpio = (valor or "").strip()
    if len(limpio) >= 2 and limpio[0] == limpio[-1] and limpio[0] in {"'", '"'}:
        return limpio[1:-1].strip()
    return limpio


def _leer_hora_programada() -> tuple[int, int]:
    hora_programada = _limpiar_valor_env(os.getenv("SCRAPING_SCHEDULE_TIME", "08:00"))
    try:
        hora, minuto = hora_programada.split(":", 1)
        return int(hora), int(minuto)
    except (ValueError, TypeError):
        raise ValueError(
            "SCRAPING_SCHEDULE_TIME debe tener formato HH:MM, por ejemplo 08:00"
        )


def ejecutar_job() -> None:
    cuenta = _limpiar_valor_env(os.getenv("SCRAPING_ACCOUNT") or "")
    if not cuenta:
        print(
            "Falta SCRAPING_ACCOUNT en variables de entorno. "
            "No se puede ejecutar el scraping automatico."
        )
        return

    print(f"[{datetime.now().isoformat(timespec='seconds')}] Iniciando scraping...")
    try:
        scrapping_general.main(cuenta)
        print(f"[{datetime.now().isoformat(timespec='seconds')}] Scraping completado.")
    except Exception:
        print(f"[{datetime.now().isoformat(timespec='seconds')}] Error en scraping.")
        traceback.print_exc()


def main() -> None:
    if load_dotenv:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        env_path = os.path.join(base_dir, ".env")
        load_dotenv(dotenv_path=env_path, override=True)

    timezone = _limpiar_valor_env(os.getenv("SCRAPING_TIMEZONE") or "Europe/Madrid")
    hora, minuto = _leer_hora_programada()

    scheduler = BlockingScheduler(timezone=timezone)
    scheduler.add_job(
        ejecutar_job,
        "cron",
        hour=hora,
        minute=minuto,
        id="daily_scraping",
        replace_existing=True,
    )
    print(
        f"Scheduler activo ({timezone}). "
        f"Ejecutara scraping diario a las {hora:02d}:{minuto:02d}."
    )
    scheduler.start()


if __name__ == "__main__":
    main()
