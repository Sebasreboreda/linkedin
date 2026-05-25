import json
import os
import re
import sys
import time

from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

import app_paths

ultimo_error_login: str | None = None

TIMEOUT_FORMULARIO_MS = 15_000
TIMEOUT_TRAS_ENVIO_MS = 20_000
TIMEOUT_NAVEGACION_MS = 25_000

EMAIL_SELECTORS = [
    'input[type="email"]',
    'input#username',
    'input[name="session_key"]',
    'input[name="username"]',
    'input[autocomplete*="username"]',
]
PASSWORD_SELECTORS = [
    'input[type="password"]',
    'input#password',
    'input[name="session_password"]',
    'input[name="password"]',
]

ERROR_SELECTORS = [
    "#error-for-password",
    "#error-for-username",
    '[data-test-id="sign-in-error"]',
    "div.alert-content",
    'div[role="alert"]',
    ".form__label--error",
    ".form__input--error",
]

PATRONES_ERROR_CREDENCIALES = [
    re.compile(r"contraseña.*(incorrect|no es correct)", re.I),
    re.compile(r"password.*(isn't right|is not correct|incorrect|wrong)", re.I),
    re.compile(r"wrong (email|password)", re.I),
    re.compile(r"correo electrónico o la contraseña", re.I),
    re.compile(r"email or password", re.I),
    re.compile(r"no reconoce", re.I),
    re.compile(r"couldn't find a match", re.I),
    re.compile(r"inténtalo de nuevo", re.I),
    re.compile(r"try again", re.I),
]


class ErrorLogin(Exception):
    """El inicio de sesión en LinkedIn no se completó."""


def load_env_file(path: str) -> None:
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def cargar_credenciales() -> tuple[str, str]:
    app_paths.cargar_env()

    return (
        (os.getenv("LINKEDIN_EMAIL") or "").strip(),
        (os.getenv("LINKEDIN_PASSWORD") or "").strip(),
    )


def _notificar_fallo(mensaje: str) -> None:
    if os.getenv("LOGIN_DESDE_SCHEDULER") == "1":
        return
    try:
        import notificaciones

        notificaciones.limpiar_errores()
        notificaciones.agregar_error("Login", mensaje)
        notificaciones.enviar_errores_acumulados()
    except Exception as e:
        print(f"No se pudo enviar el correo: {e}", file=sys.stderr)


def _contextos(page):
    vistos = {id(page)}
    yield page
    for frame in page.frames:
        if id(frame) not in vistos:
            vistos.add(id(frame))
            yield frame


def _aceptar_cookies(page) -> None:
    for sel in ('button:has-text("Aceptar")', 'button:has-text("Accept")'):
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=1500):
                btn.click()
                page.wait_for_timeout(400)
                return
        except Exception:
            continue


def _tiene_tamano_util(locator) -> bool:
    try:
        box = locator.bounding_box()
        return bool(box and box.get("width", 0) > 8 and box.get("height", 0) > 8)
    except Exception:
        return False


def _campo_interactivo(ctx, selectores: list[str], etiquetas: tuple[str, ...] = ()):
    for texto in etiquetas:
        try:
            loc = ctx.get_by_label(texto, exact=False)
            for i in range(loc.count()):
                item = loc.nth(i)
                if _tiene_tamano_util(item):
                    return item
        except Exception:
            continue

    for sel in selectores:
        loc = ctx.locator(sel)
        for i in range(loc.count()):
            item = loc.nth(i)
            try:
                if (item.get_attribute("type") or "").lower() == "hidden":
                    continue
            except Exception:
                pass
            if _tiene_tamano_util(item):
                return item
    return None


def _encontrar_campos(page):
    for ctx in _contextos(page):
        email = _campo_interactivo(
            ctx, EMAIL_SELECTORS, ("Email o teléfono", "Email or phone")
        )
        password = _campo_interactivo(ctx, PASSWORD_SELECTORS, ("Contraseña", "Password"))
        if email and password:
            return email, password, ctx

    return None, None, page


def _esperar_y_encontrar_campos(page):
    intentos = TIMEOUT_FORMULARIO_MS // 500
    for _ in range(intentos):
        _aceptar_cookies(page)
        email, password, ctx = _encontrar_campos(page)
        if email and password:
            return email, password, ctx
        page.wait_for_timeout(500)

    return None, None, page


def _click_iniciar_sesion(ctx) -> None:
    candidatos = [
        ctx.get_by_role("button", name="Iniciar sesión", exact=True).first,
        ctx.get_by_role("button", name="Sign in", exact=True).first,
        ctx.locator("button").filter(
            has_text=re.compile(r"^Iniciar sesión$|^Sign in$", re.I)
        ).last,
        ctx.locator('button[type="submit"]').first,
        ctx.locator('button[data-id="sign-in-form__submit-btn"]').first,
    ]

    for btn in candidatos:
        try:
            if btn.count() > 0:
                btn.click(force=True, timeout=5000)
                return
        except Exception:
            continue

    raise ErrorLogin("No se encontró el botón «Iniciar sesión».")


def _sesion_ok(url: str) -> bool:
    u = url.lower()
    if "/login" in u and "/feed" not in u:
        return False
    return "/feed" in u or "/mynetwork" in u or "/checkpoint" in u


def _sigue_en_pantalla_login(url: str) -> bool:
    u = url.lower()
    return "/login" in u or "/uas/login" in u or "session_redirect" in u


def _texto_pagina(page) -> str:
    try:
        return page.locator("body").inner_text(timeout=3000)
    except Exception:
        try:
            return page.content()
        except Exception:
            return ""


def _mensaje_desde_selectores(page) -> str | None:
    for ctx in _contextos(page):
        for sel in ERROR_SELECTORS:
            try:
                loc = ctx.locator(sel)
                for i in range(min(loc.count(), 3)):
                    item = loc.nth(i)
                    if not item.is_visible(timeout=500):
                        continue
                    texto = (item.inner_text(timeout=1000) or "").strip()
                    if texto:
                        return texto
            except Exception:
                continue
    return None


def _mensaje_desde_patrones(texto: str) -> str | None:
    for patron in PATRONES_ERROR_CREDENCIALES:
        m = patron.search(texto)
        if m:
            return m.group(0).strip()
    return None


def _error_credenciales_en_pagina(page) -> str | None:
    desde_dom = _mensaje_desde_selectores(page)
    if desde_dom:
        return desde_dom

    cuerpo = _texto_pagina(page)
    desde_texto = _mensaje_desde_patrones(cuerpo)
    if desde_texto:
        return desde_texto

    if _sigue_en_pantalla_login(page.url) and _encontrar_campos(page)[0]:
        if re.search(r"incorrect|wrong|no es correct|isn't right", cuerpo, re.I):
            return "Credenciales incorrectas (LinkedIn muestra error en pantalla)."
    return None


def _esperar_resultado_login(page) -> None:
    """Tras pulsar Iniciar sesión: éxito en URL o error visible (p. ej. contraseña mal)."""
    limite = time.monotonic() + TIMEOUT_TRAS_ENVIO_MS / 1000

    while time.monotonic() < limite:
        err = _error_credenciales_en_pagina(page)
        if err:
            raise ErrorLogin(f"Credenciales incorrectas: {err}")

        if _sesion_ok(page.url):
            return

        page.wait_for_timeout(400)

    err = _error_credenciales_en_pagina(page)
    if err:
        raise ErrorLogin(f"Credenciales incorrectas: {err}")

    if _sigue_en_pantalla_login(page.url):
        print("Abriendo feed para confirmar sesión...")
        page.goto(
            "https://www.linkedin.com/feed/",
            wait_until="domcontentloaded",
            timeout=TIMEOUT_NAVEGACION_MS,
        )
        page.wait_for_timeout(2000)
        if _sesion_ok(page.url) or not _sigue_en_pantalla_login(page.url):
            return
        raise ErrorLogin(
            "No se pudo iniciar sesión: sigues en la pantalla de login. "
            "Comprueba LINKEDIN_EMAIL y LINKEDIN_PASSWORD en .env."
        )


def _asegurar_pagina_sesion(page) -> None:
    if "/checkpoint" in page.url.lower():
        raise ErrorLogin(
            "LinkedIn pide verificación (captcha/2FA). URL: " + page.url
        )

    if _sesion_ok(page.url) and not _sigue_en_pantalla_login(page.url):
        return

    print("Comprobando sesión en el feed...")
    page.goto(
        "https://www.linkedin.com/feed/",
        wait_until="domcontentloaded",
        timeout=TIMEOUT_NAVEGACION_MS,
    )
    try:
        page.wait_for_load_state("networkidle", timeout=12_000)
    except PlaywrightTimeout:
        page.wait_for_load_state("domcontentloaded", timeout=10_000)
    page.wait_for_timeout(2000)

    err = _error_credenciales_en_pagina(page)
    if err:
        raise ErrorLogin(f"Credenciales incorrectas: {err}")

    if _sigue_en_pantalla_login(page.url):
        raise ErrorLogin(
            "Sesión no válida: LinkedIn redirige al login. "
            "Revisa email y contraseña en .env."
        )

    if "/checkpoint" in page.url.lower():
        raise ErrorLogin(
            "LinkedIn pide verificación (captcha/2FA). URL: " + page.url
        )


def _tiene_cookie_sesion(state_path: str) -> bool:
    try:
        with open(state_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return False
    cookies = data.get("cookies") or []
    return any(c.get("name") == "li_at" and c.get("value") for c in cookies)


def _guardar_sesion(context, page, state_path: str) -> None:
    _asegurar_pagina_sesion(page)

    selectores_nav = (
        "nav.global-nav, header.global-nav, "
        '[data-global-nav="true"], div[class*="global-nav"]'
    )
    try:
        page.locator(selectores_nav).first.wait_for(state="visible", timeout=8000)
    except PlaywrightTimeout:
        print(
            "Aviso: no se detectó la barra de navegación; se guardará la sesión igualmente.",
            file=sys.stderr,
        )

    carpeta = os.path.dirname(state_path)
    if carpeta:
        os.makedirs(carpeta, exist_ok=True)

    context.storage_state(path=state_path)

    if not os.path.isfile(state_path):
        raise ErrorLogin(f"No se creó el archivo de sesión: {state_path}")

    if os.path.getsize(state_path) < 80:
        raise ErrorLogin(f"El archivo de sesión está vacío o incompleto: {state_path}")

    if not _tiene_cookie_sesion(state_path):
        raise ErrorLogin(
            "No se guardó la cookie de sesión (li_at). "
            "El login no quedó registrado; revisa credenciales o verificación de LinkedIn."
        )


_JS_REACT_INPUT = """
(el, value) => {
  el.focus();
  const setter = Object.getOwnPropertyDescriptor(
    window.HTMLInputElement.prototype, "value"
  )?.set;
  const prev = el.value;
  if (setter) {
    setter.call(el, value);
  } else {
    el.value = value;
  }
  if (el._valueTracker) {
    el._valueTracker.setValue(prev);
  }
  el.dispatchEvent(new Event("input", { bubbles: true }));
  el.dispatchEvent(new Event("change", { bubbles: true }));
}
"""


def _valor_en_campo(campo, valor: str) -> bool:
    try:
        return campo.input_value(timeout=2000) == valor
    except Exception:
        return False


def _rellenar(campo, valor: str, page, nombre: str) -> None:
    """Escribe en inputs React de LinkedIn (el visible, no copias ocultas)."""
    errores: list[str] = []

    def _intentar_press_sequentially() -> None:
        campo.evaluate("el => { el.focus(); el.select(); }")
        page.wait_for_timeout(200)
        campo.press_sequentially(valor, delay=45)

    def _intentar_react() -> None:
        campo.evaluate(_JS_REACT_INPUT, valor)

    def _intentar_fill() -> None:
        campo.fill(valor, force=True, timeout=5000)

    for metodo, fn in (
        ("teclado en el campo", _intentar_press_sequentially),
        ("React value tracker", _intentar_react),
        ("fill forzado", _intentar_fill),
    ):
        try:
            fn()
            if _valor_en_campo(campo, valor):
                print(f"  {nombre}: escrito ({metodo})")
                return
        except Exception as e:
            errores.append(f"{metodo}: {e}")

    raise ErrorLogin(
        f"No se pudo escribir en {nombre}. "
        f"Intentos: {'; '.join(errores[:2])}"
    )


def _login_automatico(page, email: str, password: str) -> None:
    print(f"Rellenando credenciales desde .env ({email})...")

    page.wait_for_timeout(1000)
    email_input, password_input, ctx = _esperar_y_encontrar_campos(page)
    if not email_input or not password_input:
        raise ErrorLogin(
            f"No se encontraron email y contraseña en el DOM. URL: {page.url}"
        )

    _rellenar(email_input, email, page, "Email")
    page.wait_for_timeout(400)
    _rellenar(password_input, password, page, "Contraseña")

    _click_iniciar_sesion(ctx)
    page.wait_for_timeout(800)
    _esperar_resultado_login(page)


def main() -> int:
    global ultimo_error_login
    ultimo_error_login = None
    app_paths.cargar_env()
    email, password = cargar_credenciales()
    if not email or not password:
        env_path = app_paths.get_env_path()
        print(
            f"ERROR: faltan LINKEDIN_EMAIL y LINKEDIN_PASSWORD en {env_path}",
            file=sys.stderr,
        )
        return 1

    state_path = os.path.join(app_paths.get_app_dir(), "state.json")
    if os.path.isfile(state_path):
        try:
            os.remove(state_path)
            print(f"Sesión anterior eliminada: {state_path}")
        except OSError as e:
            print(
                f"No se pudo borrar {state_path}: {e}",
                file=sys.stderr,
            )
            return 1

    browser = None
    try:
        with sync_playwright() as p:
            browser = app_paths.launch_browser(p, headless=False)
            context = browser.new_context(
                viewport={"width": 1280, "height": 900},
                locale="es-ES",
            )
            page = context.new_page()

            print("Abriendo login de LinkedIn...")
            page.goto(
                "https://www.linkedin.com/login",
                wait_until="load",
                timeout=TIMEOUT_NAVEGACION_MS,
            )

            _login_automatico(page, email, password)
            _guardar_sesion(context, page, state_path)
            print(f"Sesión guardada en: {os.path.abspath(state_path)}")
            return 0

    except ErrorLogin as e:
        ultimo_error_login = str(e)
        msg = f"ERROR DE LOGIN: {e}"
        print(f"\n{msg}", file=sys.stderr)
        _notificar_fallo(msg)
        return 1
    except Exception as e:
        ultimo_error_login = str(e)
        msg = f"ERROR DE LOGIN inesperado: {e}"
        print(f"\n{msg}", file=sys.stderr)
        _notificar_fallo(msg)
        return 1
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
