"""
Notificaciones de error por correo SMTP.
Activar con SCRAPING_NOTIFY_ON_ERROR=1 en .env
"""

import os
import smtplib
from email.mime.text import MIMEText
import app_paths

_errores_acumulados: list[tuple[str, str]] = []


def _cargar_env() -> None:
    app_paths.cargar_env()


def _env_flag(nombre: str, default: bool = False) -> bool:
    valor = (os.getenv(nombre) or "").strip().lower()
    if not valor:
        return default
    return valor in {"1", "true", "yes", "si", "sí"}


def notificaciones_activas() -> bool:
    _cargar_env()
    return _env_flag("SCRAPING_NOTIFY_ON_ERROR", default=False)


def limpiar_errores() -> None:
    _errores_acumulados.clear()


def agregar_error(seccion: str, mensaje: str) -> None:
    texto = (mensaje or "").strip()
    if not texto:
        texto = "(sin detalle)"
    _errores_acumulados.append((seccion, texto))


def tiene_errores() -> bool:
    return bool(_errores_acumulados)


def _enviar_correo(asunto: str, cuerpo: str) -> None:
    if not notificaciones_activas():
        print("Correo omitido: SCRAPING_NOTIFY_ON_ERROR no está activado en .env")
        return

    host = (os.getenv("SMTP_HOST") or "").strip()
    puerto_raw = (os.getenv("SMTP_PORT") or "587").strip()
    usuario = (os.getenv("SMTP_USER") or "").strip()
    password = (os.getenv("SMTP_PASSWORD") or "").replace(" ", "")
    destino = (os.getenv("NOTIFY_EMAIL_TO") or usuario).strip()

    if not all([host, usuario, password, destino]):
        print(
            "Correo omitido: faltan SMTP_HOST, SMTP_USER, SMTP_PASSWORD o NOTIFY_EMAIL_TO",
            flush=True,
        )
        return

    try:
        puerto = int(puerto_raw)
    except ValueError:
        print(f"SMTP_PORT inválido: {puerto_raw!r}", flush=True)
        return

    mensaje = MIMEText(cuerpo, "plain", "utf-8")
    mensaje["Subject"] = asunto
    mensaje["From"] = usuario
    mensaje["To"] = destino

    try:
        with smtplib.SMTP(host, puerto, timeout=30) as servidor:
            servidor.starttls()
            servidor.login(usuario, password)
            servidor.sendmail(usuario, [destino], mensaje.as_string())
        print(f"Correo de error enviado a {destino}", flush=True)
    except Exception as e:
        print(f"No se pudo enviar el correo: {e}", flush=True)


def enviar_errores_acumulados() -> None:
    if not _errores_acumulados:
        return

    bloques = [f"[{seccion}]\n{texto}" for seccion, texto in _errores_acumulados]
    cuerpo = "\n\n".join(bloques)
    _enviar_correo("LinkedIn Scraper — errores", cuerpo)
    limpiar_errores()
