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


def _limpiar_valor_env(valor: str) -> str:
    limpio = (valor or "").strip()
    if len(limpio) >= 2 and limpio[0] == limpio[-1] and limpio[0] in {"'", '"'}:
        return limpio[1:-1].strip()
    return limpio


def refrescar_login() -> int:
    base_dir = os.path.dirname(os.path.abspath(__file__))
    login_path = os.path.join(base_dir, "login.py")
    if not os.path.exists(login_path):
        print("No se encontro login.py, se omite refresco de sesion.")
        return 0

    print(f"[{datetime.now().isoformat(timespec='seconds')}] Refrescando sesion LinkedIn...")
    try:
        subprocess.run([sys.executable, login_path], check=True)
        print(f"[{datetime.now().isoformat(timespec='seconds')}] Login refrescado.")
        return 0
    except subprocess.CalledProcessError as e:
        print(f"Error ejecutando login.py (codigo {e.returncode}).")
        return 1


def ejecutar_job() -> int:
    login_result = refrescar_login()
    if login_result != 0:
        return login_result

    cuenta = _limpiar_valor_env(os.getenv("SCRAPING_ACCOUNT") or "")
    if not cuenta:
        print(
            "Falta SCRAPING_ACCOUNT en variables de entorno. "
            "No se puede ejecutar el scraping automatico."
        )
        return 1

    print(f"[{datetime.now().isoformat(timespec='seconds')}] Iniciando scraping...")
    try:
        scrapping_general.main(cuenta)
        print(f"[{datetime.now().isoformat(timespec='seconds')}] Scraping completado.")
        return 0
    except Exception:
        print(f"[{datetime.now().isoformat(timespec='seconds')}] Error en scraping.")
        traceback.print_exc()
        return 1


def main() -> None:
    base_dir = os.path.dirname(os.path.abspath(__file__))
    env_path = os.path.join(base_dir, ".env")
    if load_dotenv:
        load_dotenv(dotenv_path=env_path, override=True)
    else:
        _cargar_env_manual(env_path)
    raise SystemExit(ejecutar_job())


if __name__ == "__main__":
    main()
