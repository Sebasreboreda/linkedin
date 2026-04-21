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


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)

# Cargamos variables desde ambas ubicaciones habituales.
load_env_file(os.path.join(SCRIPT_DIR, ".env"))
load_env_file(os.path.join(ROOT_DIR, ".env"))

linkedin_email = os.getenv("LINKEDIN_EMAIL")
linkedin_password = os.getenv("LINKEDIN_PASSWORD")

if not linkedin_email or not linkedin_password:
    raise RuntimeError(
        "Faltan variables de entorno. Crea un archivo `.env` con:\n"
        "LINKEDIN_EMAIL=...\n"
        "LINKEDIN_PASSWORD=..."
    )

email_selectors = [
    'input[name="session_key"]',
    'input#username',
    'input[name="username"]',
]
password_selectors = [
    "input#password",
    'input[name="session_password"]',
    'input[name="password"]',
]
submit_selectors = [
    'button[type="submit"]',
    'button[data-id="sign-in-form__submit-btn"]',
]

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    context = browser.new_context()
    page = context.new_page()

    page.goto("https://www.linkedin.com/login")
    page.wait_for_load_state("domcontentloaded")

    # Intentamos rellenar el correo y la contraseña con varios selectores
    # porque LinkedIn cambia el formulario con frecuencia.
    email_input = None
    for sel in email_selectors:
        loc = page.locator(sel).first
        if loc.count() > 0:
            email_input = loc
            break

    password_input = None
    for sel in password_selectors:
        loc = page.locator(sel).first
        if loc.count() > 0:
            password_input = loc
            break

    if email_input and password_input:
        email_input.fill(linkedin_email)
        password_input.fill(linkedin_password)

        clicked = False
        for sel in submit_selectors:
            btn = page.locator(sel).first
            if btn.count() > 0:
                btn.click()
                clicked = True
                break

        if not clicked:
            password_input.press("Enter")

        # LinkedIn suele mantener requests en segundo plano y "networkidle"
        # puede agotar el timeout aunque la pagina este lista.
        try:
            page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            pass
        print("Credenciales insertadas. Continuando automaticamente...") #prueba
    else:
        print("No se encontraron campos de login o LinkedIn usó un flujo diferente.")
        print("Intentando continuar sin interaccion manual.")

    # Evita la pausa interactiva: enviamos ENTER y seguimos el flujo.
    page.keyboard.press("Enter")
    page.goto("https://www.linkedin.com/feed/")
    try:
        page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        pass


    state_path = os.path.join(SCRIPT_DIR, "state.json")
    context.storage_state(path=state_path)
    print(f"Sesion guardada en: {state_path}")
    browser.close()