import os
import subprocess
import sys
import traceback
from datetime import datetime

import app_paths
import notificaciones
import scrapping_general


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

    base_dir = app_paths.get_app_dir()
    print(
        f"[{datetime.now().isoformat(timespec='seconds')}] "
        "Refrescando sesión LinkedIn..."
    )

    os.environ["LOGIN_DESDE_SCHEDULER"] = "1"

    detalle_login = None
    if app_paths.es_frozen():
        import login

        codigo = login.main()
        detalle_login = login.ultimo_error_login
    else:
        login_path = os.path.join(base_dir, "login.py")
        if not os.path.exists(login_path):
            print("No se encontró login.py, se omite refresco de sesión.")
            return 0

        entorno = os.environ.copy()
        entorno["LOGIN_DESDE_SCHEDULER"] = "1"
        resultado = subprocess.run(
            [sys.executable, "-u", login_path],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=entorno,
            cwd=base_dir,
        )

        if resultado.stdout:
            print(resultado.stdout, end="")
        if resultado.stderr:
            print(resultado.stderr, end="", file=sys.stderr)
        codigo = resultado.returncode
        partes = [resultado.stderr.strip(), resultado.stdout.strip()]
        detalle_login = "\n".join(p for p in partes if p) or None

    if codigo == 0:
        print(
            f"[{datetime.now().isoformat(timespec='seconds')}] "
            "Login refrescado."
        )
        return 0

    detalle = (detalle_login or "").strip() or f"El login terminó con código {codigo}"
    print(f"Error de login: {detalle}", file=sys.stderr)
    notificaciones.agregar_error("Login", detalle)
    return 1


def ejecutar_job() -> int:
    notificaciones.limpiar_errores()

    try:
        login_result = refrescar_login()
        if login_result != 0:
            return 1

        print(f"[{datetime.now().isoformat(timespec='seconds')}] Iniciando scraping...")
        codigo, mensaje = scrapping_general.main()
        if codigo == 0:
            print(
                f"[{datetime.now().isoformat(timespec='seconds')}] "
                "Scraping completado."
            )
            return 0

        print(
            f"[{datetime.now().isoformat(timespec='seconds')}] "
            f"Scraping terminado con código {codigo}.",
            file=sys.stderr,
        )
        if mensaje:
            print(mensaje, file=sys.stderr)
        notificaciones.agregar_error("Scraping", mensaje or f"código {codigo}")
        return codigo

    except Exception as exc:
        print(
            f"[{datetime.now().isoformat(timespec='seconds')}] "
            "Error en scraping.",
            file=sys.stderr,
        )
        traceback.print_exc()
        detalle = "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        )
        notificaciones.agregar_error("Excepción", detalle)
        return 1

    finally:
        if notificaciones.tiene_errores():
            notificaciones.enviar_errores_acumulados()


def main() -> None:
    app_paths.verificar_env_usuario()
    app_paths.cargar_env()
    raise SystemExit(ejecutar_job())


if __name__ == "__main__":
    main()
