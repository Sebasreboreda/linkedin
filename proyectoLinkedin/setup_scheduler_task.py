import os
import subprocess
import sys

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None


TASK_NAME = "LinkedinScraperDaily"


def cargar_env_manual(env_path: str) -> None:
    if not os.path.exists(env_path):
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ[key.strip()] = value.strip().strip('"').strip("'")


def leer_hora() -> str:
    hora = (os.getenv("SCRAPING_SCHEDULE_TIME") or "08:00").strip().strip('"').strip("'")
    partes = hora.split(":")
    if len(partes) != 2:
        raise ValueError("SCRAPING_SCHEDULE_TIME debe tener formato HH:MM")
    hh, mm = int(partes[0]), int(partes[1])
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError("SCRAPING_SCHEDULE_TIME fuera de rango")
    return f"{hh:02d}:{mm:02d}"


def leer_scraping_days() -> int:
    raw = (os.getenv("SCRAPING_DAYS") or "1").strip().strip('"').strip("'")
    try:
        dias = int(raw)
    except ValueError as e:
        raise ValueError("SCRAPING_DAYS debe ser un número entero") from e
    if dias < 1:
        raise ValueError("SCRAPING_DAYS debe ser >= 1")
    return dias


def crear_tarea_scheduler() -> None:
    base_dir = os.path.dirname(os.path.abspath(__file__))
    env_path = os.path.join(base_dir, ".env")
    if load_dotenv:
        load_dotenv(dotenv_path=env_path, override=True)
    else:
        cargar_env_manual(env_path)

    hora = leer_hora()
    scraping_days = leer_scraping_days()

    scheduler_exe = os.path.join(base_dir, "scheduler.exe")
    scheduler_py = os.path.join(base_dir, "scheduler.py")
    python_venv = os.path.join(base_dir, "venv", "Scripts", "python.exe")

    if os.path.exists(scheduler_exe):
        ejecutable = scheduler_exe
        tr = f'cmd /c "cd /d \\"{base_dir}\\" && \\"{ejecutable}\\""'
    elif os.path.exists(scheduler_py):
        if os.path.exists(python_venv):
            python_exe = python_venv
        else:
            python_exe = sys.executable
        tr = (
            f'cmd /c "cd /d \\"{base_dir}\\" && '
            f'\\"{python_exe}\\" \\"{scheduler_py}\\""'
        )
    else:
        raise FileNotFoundError(
            f"No se encontró scheduler.exe ni scheduler.py en: {base_dir}"
        )
    cmd = [
        r"C:\Windows\System32\schtasks.exe",
        "/Create",
        "/TN",
        TASK_NAME,
        "/TR",
        tr,
        "/SC",
        "DAILY",
        "/MO",
        str(scraping_days),
        "/ST",
        hora,
        "/F",
    ]
    subprocess.run(cmd, check=True)
    if scraping_days == 1:
        frecuencia = "cada día"
    else:
        frecuencia = f"cada {scraping_days} días"
    print(
        f"Tarea '{TASK_NAME}' creada/actualizada para ejecutar scheduler.py "
        f"{frecuencia} a las {hora} (SCRAPING_DAYS={scraping_days})."
    )


if __name__ == "__main__":
    try:
        crear_tarea_scheduler()
    except Exception as e:
        print(f"Error configurando la tarea del scheduler: {e}")
        sys.exit(1)
