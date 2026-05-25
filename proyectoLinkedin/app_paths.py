"""
Rutas de la aplicación: carpeta del .exe o del proyecto en desarrollo.
"""

import glob
import os
import subprocess
import sys


def es_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def get_app_dir() -> str:
    if es_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def get_env_path() -> str:
    return os.path.join(get_app_dir(), ".env")


def _env_flag(nombre: str, default: bool = False) -> bool:
    valor = (os.getenv(nombre) or "").strip().lower()
    if not valor:
        return default
    return valor in {"1", "true", "yes", "si", "sí"}


def _ruta_browsers() -> str:
    return os.path.join(get_app_dir(), "ms-playwright")


def _chromium_instalado() -> bool:
    return bool(
        glob.glob(
            os.path.join(_ruta_browsers(), "chromium-*", "chrome-win", "chrome.exe")
        )
    )


def usar_navegador_sistema() -> bool:
    """Por defecto True: usa Chrome/Edge del PC y no hace falta ms-playwright en release."""
    return _env_flag("USE_SYSTEM_BROWSER", default=True)


def canal_navegador() -> str | None:
    raw = (os.getenv("PLAYWRIGHT_CHANNEL") or "").strip().lower()
    if raw in ("chrome", "msedge"):
        return raw
    if raw == "edge":
        return "msedge"
    if usar_navegador_sistema():
        return "chrome"
    return None


def configurar_playwright() -> None:
    browsers = _ruta_browsers()
    if os.path.isdir(browsers):
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", browsers)


def _instalar_chromium_portable() -> None:
    browsers = _ruta_browsers()
    os.makedirs(browsers, exist_ok=True)
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = browsers

    print(
        "Descargando Chromium portable (primera vez, requiere internet)...",
        flush=True,
    )

    if es_frozen():
        from playwright._impl._driver import compute_driver_executable, get_driver_env

        executable, cli = compute_driver_executable()
        subprocess.run(
            [executable, cli, "install", "chromium"],
            env=get_driver_env(),
            check=True,
        )
    else:
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            check=True,
            env=os.environ.copy(),
        )

    if not _chromium_instalado():
        raise SystemExit(
            f"No se encontró Chromium en {browsers}. "
            "Instala Google Chrome o pon USE_SYSTEM_BROWSER=1 en .env."
        )
    print(f"Chromium portable listo en: {browsers}", flush=True)


def asegurar_playwright_browsers() -> None:
    """Prepara el navegador: Chrome del sistema o Chromium portable."""
    canal = canal_navegador()
    if canal:
        print(f"Navegador: {canal} instalado en el PC (sin carpeta ms-playwright).", flush=True)
        return

    configurar_playwright()
    if _chromium_instalado():
        return

    _instalar_chromium_portable()


def launch_browser(playwright, *, headless: bool = False, slow_mo: int | None = None):
    """Abre el navegador (Chrome/Edge del sistema o Chromium portable)."""
    asegurar_playwright_browsers()
    opts: dict = {"headless": headless}
    if slow_mo is not None:
        opts["slow_mo"] = slow_mo

    canal = canal_navegador()
    if canal:
        opts["channel"] = canal
        try:
            return playwright.chromium.launch(**opts)
        except Exception as e:
            print(
                f"No se pudo abrir {canal} ({e}). Probando Chromium portable...",
                file=sys.stderr,
                flush=True,
            )
            opts.pop("channel", None)

    configurar_playwright()
    if not _chromium_instalado():
        _instalar_chromium_portable()
    return playwright.chromium.launch(**opts)


def verificar_env_usuario() -> None:
    env_path = get_env_path()
    if os.path.isfile(env_path):
        return
    ejemplo = os.path.join(get_app_dir(), ".env.example")
    print(
        f"\nFalta el archivo de configuración:\n  {env_path}\n\n"
        f"Copia .env.example a .env y rellena tus datos.",
        flush=True,
    )
    if os.path.isfile(ejemplo):
        print(f"Plantilla: {ejemplo}\n", flush=True)
    raise SystemExit(1)


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


def cargar_env() -> str:
    env_path = get_env_path()
    try:
        from dotenv import load_dotenv

        load_dotenv(dotenv_path=env_path, override=True)
    except ImportError:
        cargar_env_manual(env_path)
    return env_path
