import os

from playwright.sync_api import sync_playwright


def load_env_file(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


load_env_file()

linkedin_email = os.getenv("LINKEDIN_EMAIL")
linkedin_password = os.getenv("LINKEDIN_PASSWORD")

if not linkedin_email or not linkedin_password:
    raise RuntimeError(
        "Faltan variables de entorno. Crea un archivo `.env` con:\n"
        "LINKEDIN_EMAIL=...\n"
        "LINKEDIN_PASSWORD=..."
    )

email_selectors = [
    'input#username',
    'input[name="session_key"]',
    'input[name="email"]',
    'input[type="email"]',
]

password_selectors = [
    'input#password',
    'input[name="session_password"]',
    'input[name="password"]',
    'input[type="password"]',
]

submit_selectors = [
    'button[type="submit"]',
    'button:has-text("Sign in")',
    'button:has-text("Iniciar sesión")',
    'button:has-text("Iniciar sesion")',
]

continue_selectors = [
    'button:has-text("Continue")',
    'button:has-text("Continuar")',
    'button:has-text("Next")',
    'button:has-text("Siguiente")',
]


with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    context = browser.new_context()
    page = context.new_page()

    page.goto("https://www.linkedin.com/login")
    page.wait_for_load_state("domcontentloaded")

    # Intentamos rellenar el correo y la contraseña si el formulario aparece.
    filled_email = False
    for sel in email_selectors:
        loc = page.locator(sel).first
        try:
            if loc.count() > 0 and loc.is_visible():
                loc.fill(linkedin_email)
                filled_email = True
                break
        except Exception:
            continue

    filled_password = False
    for sel in password_selectors:
        loc = page.locator(sel).first
        try:
            if loc.count() > 0 and loc.is_visible():
                loc.fill(linkedin_password)
                filled_password = True
                break
        except Exception:
            continue

    # Si LinkedIn muestra un paso previo (solo email) intentamos avanzar y luego rellenar password.
    if filled_email and not filled_password:
        for sel in continue_selectors:
            loc = page.locator(sel).first
            try:
                if loc.count() > 0 and loc.is_visible():
                    loc.click()
                    page.wait_for_timeout(1500)
                    break
            except Exception:
                continue

        for sel in password_selectors:
            loc = page.locator(sel).first
            try:
                if loc.count() > 0 and loc.is_visible():
                    loc.fill(linkedin_password)
                    filled_password = True
                    break
            except Exception:
                continue

    # En cuanto el formulario esté rellenado, intentamos enviar el login.
    if filled_email and filled_password:
        for sel in submit_selectors:
            loc = page.locator(sel).first
            try:
                if loc.count() > 0 and loc.is_visible():
                    loc.click()
                    break
            except Exception:
                continue
        print("Credenciales insertadas. Completa MFA u otros pasos y pulsa ENTER.")
    else:
        print("No se encontraron campos de login o LinkedIn usó un flujo diferente.")
        print("Inicia sesión manualmente en la ventana del navegador y pulsa ENTER.")

    input("Pulsa ENTER para guardar `state.json`...")
    context.storage_state(path="state.json")
    browser.close()