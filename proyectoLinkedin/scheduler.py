import os
import subprocess
import sys
import traceback
from datetime import datetime

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

import scrapping_general


def _cargar_env_manual(env_path: str) -> None:
    if not os.path.exists(env_path):
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ[key] = value


def cargar_env(base_dir: str) -> None:
    env_path = os.path.join(base_dir, ".env")
    if load_dotenv:
        load_dotenv(dotenv_path=env_path, override=True)
    else:
        _cargar_env_manual(env_path)


def _env_flag(nombre: str, default: bool = False) -> bool:
    valor = (os.getenv(nombre) or "").strip().lower()
    if not valor:
        return default
    return valor in {"1", "true", "yes", "si", "sí"}


def refrescar_login() -> int:
    if not _env_flag("SCRAPING_RUN_LOGIN", default=False):
        print(
            f"[{datetime.now().isoformat(timespec='seconds')}] "
            "Login omitido (SCRAPING_RUN_LOGIN=0)."
        )
        return 0

    base_dir = os.path.dirname(os.path.abspath(__file__))
    login_path = os.path.join(base_dir, "login.py")
    if not os.path.exists(login_path):
        print("No se encontró login.py, se omite refresco de sesión.")
        return 0

    print(
        f"[{datetime.now().isoformat(timespec='seconds')}] "
        "Refrescando sesión LinkedIn..."
    )
    try:
        subprocess.run([sys.executable, login_path], check=True)
        print(
            f"[{datetime.now().isoformat(timespec='seconds')}] "
            "Login refrescado."
        )
        return 0
    except subprocess.CalledProcessError as e:
        print(f"Error ejecutando login.py (código {e.returncode}).")
        return 1


def ejecutar_job() -> int:
    login_result = refrescar_login()
    if login_result != 0:
        return login_result

    print(f"[{datetime.now().isoformat(timespec='seconds')}] Iniciando scraping...")
    try:
        scrapping_general.main()
        print(f"[{datetime.now().isoformat(timespec='seconds')}] Scraping completado.")
        return 0
    except Exception:
        print(f"[{datetime.now().isoformat(timespec='seconds')}] Error en scraping.")
        traceback.print_exc()
        return 1


def main() -> None:
    base_dir = os.path.dirname(os.path.abspath(__file__))
    cargar_env(base_dir)
    raise SystemExit(ejecutar_job())


if __name__ == "__main__":
    main()
