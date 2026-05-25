import argparse
import hashlib
import logging
import os
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus

from playwright.sync_api import sync_playwright

try:
    import psycopg
except ImportError:
    psycopg = None

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

import app_paths


# ============================================================
# Configuración general
# ============================================================

def _leer_max_scrolls() -> int:
    return int(os.getenv("MAX_SCROLLS", "40"))


def _leer_headless_env() -> bool:
    return os.getenv("HEADLESS", "false").lower() in {"1", "true", "yes"}


def _leer_slow_mo() -> int:
    return int(os.getenv("PLAYWRIGHT_SLOW_MO", "50"))


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(message)s",
)


# ============================================================
# Utilidades básicas
# ============================================================

def delay(seconds: float = 2.0) -> None:
    time.sleep(seconds)


def normalizar_numero(valor: str) -> str:
    limpio = valor.replace("\u00a0", " ").strip()
    return re.sub(r"\s+", " ", limpio)


def normalizar_texto(valor: str) -> str:
    valor = (valor or "").strip().lower()
    valor = unicodedata.normalize("NFD", valor)
    valor = "".join(c for c in valor if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", valor)


def limpiar_valor_texto(valor: str) -> str:
    limpio = (valor or "").strip()
    if len(limpio) >= 2 and limpio[0] == limpio[-1] and limpio[0] in {"'", '"'}:
        return limpio[1:-1].strip()
    return limpio


def extraer_numero_por_etiqueta(texto: str, etiqueta: str) -> str:
    patron_antes = rf"(\d[\d.,\s]*)\s*{etiqueta}"
    patron_despues = rf"{etiqueta}\s*(\d[\d.,\s]*)"

    m = re.search(patron_antes, texto, re.IGNORECASE)
    if not m:
        m = re.search(patron_despues, texto, re.IGNORECASE)
    if not m:
        return "0"

    return normalizar_numero(m.group(1))


def convertir_a_entero(valor: str) -> int:
    if valor is None:
        return 0

    texto = normalizar_numero(str(valor)).lower()
    texto = texto.replace("seguidores", "").replace("followers", "").strip()
    texto = texto.replace(" ", "")

    m_sufijo = re.search(r"(\d+(?:[.,]\d+)?)\s*([km])$", texto)
    if m_sufijo:
        base = float(m_sufijo.group(1).replace(",", "."))
        mult = 1_000 if m_sufijo.group(2) == "k" else 1_000_000
        return int(base * mult)

    m_mil = re.search(r"(\d+(?:[.,]\d+)?)\s*mil\b", texto)
    if m_mil:
        base = float(m_mil.group(1).replace(",", "."))
        return int(base * 1_000)

    solo_digitos = re.sub(r"[^\d]", "", texto)
    return int(solo_digitos) if solo_digitos else 0


def _parsear_cantidad_unidad_relativa(texto: str) -> tuple[int, str] | None:
    m_hace = re.search(
        r"hace\s+(\d+)\s+(min(?:utos?)?|h(?:oras?)?|d(?:ias?)?|sem(?:anas?)?|mes(?:es)?|mo)",
        texto,
    )
    if m_hace:
        return int(m_hace.group(1)), m_hace.group(2)

    m_simple = re.search(
        r"(\d+)\s*(min(?:utos?)?|h(?:oras?)?|d(?:ias?)?|sem(?:anas?)?|mes(?:es)?|mo|w|weeks?)",
        texto,
    )
    if m_simple:
        return int(m_simple.group(1)), m_simple.group(2)

    return None


def convertir_fecha_publicacion(valor: str) -> datetime:
    if not valor:
        return datetime.now(timezone.utc)

    texto = normalizar_texto(valor)
    ahora = datetime.now(timezone.utc)

    m_fecha = re.search(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", texto)
    if m_fecha:
        dia = int(m_fecha.group(1))
        mes = int(m_fecha.group(2))
        anio = int(m_fecha.group(3))

        if anio < 100:
            anio += 2000

        try:
            return datetime(anio, mes, dia, tzinfo=timezone.utc)
        except ValueError:
            return ahora

    parsed = _parsear_cantidad_unidad_relativa(texto)
    if not parsed:
        return ahora

    cantidad, unidad = parsed

    if unidad.startswith("min"):
        return ahora - timedelta(minutes=cantidad)

    if unidad.startswith("h"):
        return ahora - timedelta(hours=cantidad)

    if unidad.startswith("d") or unidad.startswith("dia"):
        return ahora - timedelta(days=cantidad)

    if unidad.startswith("sem") or unidad.startswith("w"):
        return ahora - timedelta(weeks=cantidad)

    if unidad.startswith("mo") or unidad.startswith("mes"):
        return ahora - timedelta(days=30 * cantidad)

    return ahora


def post_fuera_de_ventana(fecha_str: str, limite: datetime, dias_ventana: int) -> bool:
    """
    True si el post es anterior al límite.
    Trata bien las etiquetas de LinkedIn (sem., mes, mo) para cortar al llegar al mes.
    """
    texto = normalizar_texto(fecha_str or "")
    if not texto or "fecha no encontrada" in texto:
        return False

    meses_ventana = max(1, (dias_ventana + 29) // 30)
    parsed = _parsear_cantidad_unidad_relativa(texto)

    if parsed:
        cantidad, unidad = parsed
        if unidad.startswith("mo") or unidad.startswith("mes"):
            # Parar al llegar a "1 mes" (o al mes configurado en SCRAPING_MONTHS_INITIAL)
            if cantidad >= meses_ventana:
                return True
        if unidad.startswith("sem") or unidad.startswith("w"):
            if cantidad * 7 > dias_ventana:
                return True
        if unidad.startswith("d") or unidad.startswith("dia"):
            if cantidad > dias_ventana:
                return True

    return convertir_fecha_publicacion(fecha_str) < limite


def fecha_indica_fin_de_ventana(fecha_str: str, dias_ventana: int) -> bool:
    """True al aparecer 1 mes (o el mes límite): detiene el scroll de inmediato."""
    texto = normalizar_texto(fecha_str or "")
    parsed = _parsear_cantidad_unidad_relativa(texto)
    meses_ventana = max(1, (dias_ventana + 29) // 30)

    if parsed:
        cantidad, unidad = parsed
        if unidad.startswith("mo") or unidad.startswith("mes"):
            return cantidad >= meses_ventana

    return bool(
        re.search(
            rf"\b{meses_ventana}\s*(?:mo|mes(?:es)?|month)\b",
            texto,
        )
    )


# ============================================================
# PostgreSQL
# ============================================================

def obtener_config_db() -> dict:
    password = os.getenv("PGPASSWORD")
    if password is None:
        password = "1234"

    config = {
        "host": os.getenv("PGHOST", "localhost"),
        "port": int(os.getenv("PGPORT", "5432")),
        "dbname": os.getenv("PGDATABASE", "linkedin_db"),
        "user": os.getenv("PGUSER", "user"),
        "connect_timeout": 10,
    }
    if password != "":
        config["password"] = password
    return config


def abrir_conexion_db():
    if psycopg is None:
        raise RuntimeError(
            "No se encontró psycopg. Instala la dependencia con: pip install psycopg[binary]"
        )

    config = obtener_config_db()
    return psycopg.connect(**config)


def verificar_y_migrar_esquema(conn) -> tuple[bool, str]:
    """Comprueba tablas requeridas y añade columnas nuevas si faltan."""
    tablas_requeridas = ("perfiles", "metricas_perfil", "publicaciones")
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name = ANY(%s)
                """,
                (list(tablas_requeridas),),
            )
            encontradas = {fila[0] for fila in cur.fetchall()}
            faltantes = [t for t in tablas_requeridas if t not in encontradas]
            if faltantes:
                return (
                    False,
                    "Faltan tablas en public: "
                    + ", ".join(faltantes)
                    + ". Ejecuta schema.sql en la base de datos.",
                )

            cur.execute(
                """
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'publicaciones'
                  AND column_name = 'contenido'
                """
            )
            if not cur.fetchone():
                cur.execute(
                    "ALTER TABLE public.publicaciones "
                    "ADD COLUMN contenido TEXT"
                )
                logging.info("Columna publicaciones.contenido añadida.")

        conn.commit()
        return True, "Esquema PostgreSQL verificado."
    except Exception as e:
        conn.rollback()
        return False, f"Error verificando esquema: {e}"


def asegurar_perfil_id(conn, nombre: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO public.perfiles (nombre_usuario)
            VALUES (%s)
            ON CONFLICT (nombre_usuario)
            DO UPDATE SET nombre_usuario = EXCLUDED.nombre_usuario
            RETURNING id
            """,
            (nombre,),
        )
        perfil_id = cur.fetchone()[0]
    return perfil_id


def cargar_variables_entorno(base_dir: str | None = None) -> None:
    if base_dir is None:
        app_paths.cargar_env()
        return
    env_path = os.path.join(base_dir, ".env")
    if load_dotenv and os.path.exists(env_path):
        load_dotenv(dotenv_path=env_path, override=True)


def leer_entero_env(
    nombre: str,
    default: int,
    minimo: int = 1,
    maximo: int = 365,
) -> int:
    raw = (os.getenv(nombre) or "").strip()
    if not raw:
        return default
    try:
        valor = int(raw)
    except ValueError:
        logging.warning("%s inválido (%r), usando %s", nombre, raw, default)
        return default
    return max(minimo, min(maximo, valor))


def obtener_scraping_days() -> int:
    """Días de intervalo y de consulta (variable unificada SCRAPING_DAYS)."""
    return leer_entero_env("SCRAPING_DAYS", 1)


def obtener_meses_inicial() -> int:
    """Meses hacia atrás para perfiles sin datos en BD (SCRAPING_MONTHS_INITIAL)."""
    return leer_entero_env("SCRAPING_MONTHS_INITIAL", 1, minimo=1, maximo=24)


def obtener_dias_perfil_inicial() -> int:
    """Convierte meses del .env a días (~30 por mes)."""
    return obtener_meses_inicial() * 30


def obtener_config_ventana_temporal() -> tuple[int, int]:
    """(días perfil en BD, días perfil nuevo sin registrar)."""
    return obtener_scraping_days(), obtener_dias_perfil_inicial()


def describir_ventana_temporal(dias: int) -> str:
    if dias <= 1:
        return "último día"
    meses = dias // 30
    if dias % 30 == 0 and meses >= 1:
        return f"último mes" if meses == 1 else f"últimos {meses} meses"
    return f"últimos {dias} días"


def parsear_lista_cuentas(raw: str) -> list[str]:
    if not raw:
        return []

    nombres = []
    for parte in re.split(r"[;\n|]+", raw):
        nombre = limpiar_valor_texto(parte)
        if nombre and nombre not in nombres:
            nombres.append(nombre)
    return nombres


def obtener_cuentas_desde_env() -> list[dict]:
    cuentas_raw = parsear_lista_cuentas(os.getenv("SCRAPING_ACCOUNTS", ""))

    if not cuentas_raw:
        cuenta_unica = limpiar_valor_texto(os.getenv("SCRAPING_ACCOUNT", ""))
        if cuenta_unica:
            cuentas_raw = [cuenta_unica]

    return [
        {"id": None, "nombre_usuario": nombre}
        for nombre in cuentas_raw
    ]


def cuenta_ya_en_lista(cuentas: list[dict], nombre: str) -> bool:
    objetivo = normalizar_texto(nombre)
    return any(
        normalizar_texto(c["nombre_usuario"]) == objetivo
        for c in cuentas
    )


def perfil_registrado_en_bd(conn, cuenta: dict) -> bool:
    """True si el perfil ya existe en public.perfiles (aunque no tenga publicaciones)."""
    if cuenta.get("id") is not None:
        return True
    if conn is None:
        return False

    nombre = cuenta.get("nombre_usuario", "")
    if not nombre:
        return False

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1
            FROM public.perfiles
            WHERE lower(btrim(nombre_usuario)) = lower(btrim(%s))
            LIMIT 1
            """,
            (nombre,),
        )
        return cur.fetchone() is not None


def dias_atras_para_cuenta(conn, cuenta: dict) -> int:
    """
    Perfil en BD → SCRAPING_DAYS (último día, solo seguidores + posts recientes).
    Perfil nuevo (solo .env) → SCRAPING_MONTHS_INITIAL (carga del último mes).
    """
    dias_diario, dias_inicial = obtener_config_ventana_temporal()
    if conn is None:
        return dias_inicial
    if perfil_registrado_en_bd(conn, cuenta):
        return dias_diario
    return dias_inicial


def resolver_cuentas_sin_db(nombre: str | None = None) -> tuple[list[dict], str]:
    if nombre:
        nombre_limpio = limpiar_valor_texto(nombre)
        if not nombre_limpio:
            return [], "ninguna"
        return [{"id": None, "nombre_usuario": nombre_limpio}], "manual"

    cuentas_env = obtener_cuentas_desde_env()
    if cuentas_env:
        return cuentas_env, "env"
    return [], "ninguna"


def resolver_cuentas_a_scrapear(
    conn,
    nombre: str | None = None,
) -> tuple[list[dict], str]:
    """
    Devuelve (cuentas, origen).
    - BD con perfiles: cuentas de la BD + nuevas del .env no duplicadas.
    - BD vacía: solo cuentas del .env.
    La ventana temporal se resuelve en dias_atras_para_cuenta (BD=diario, nuevo=mes inicial).
    """
    if nombre:
        nombre_limpio = limpiar_valor_texto(nombre)
        if not nombre_limpio:
            return [], "ninguna"
        return [{"id": None, "nombre_usuario": nombre_limpio}], "manual"

    cuentas_db = obtener_cuentas_linkedin(conn)
    if cuentas_db:
        cuentas = list(cuentas_db)
        origen = "base_datos"
        for cuenta_env in obtener_cuentas_desde_env():
            nombre_env = cuenta_env["nombre_usuario"]
            if not cuenta_ya_en_lista(cuentas, nombre_env):
                cuentas.append(cuenta_env)
                if origen == "base_datos":
                    origen = "base_datos+env"
        return cuentas, origen

    cuentas_env = obtener_cuentas_desde_env()
    if cuentas_env:
        return cuentas_env, "env"

    return [], "ninguna"


def obtener_cuentas_linkedin(conn) -> list[dict]:
    """
    Obtiene todas las cuentas guardadas en public.perfiles.

    Devuelve una lista con esta forma:
    [
        {"id": 1, "nombre_usuario": "Nombre Cuenta"},
        {"id": 2, "nombre_usuario": "Otra Cuenta"}
    ]
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, nombre_usuario
            FROM public.perfiles
            WHERE nombre_usuario IS NOT NULL
              AND btrim(nombre_usuario) <> ''
            ORDER BY id
            """
        )

        cuentas = []
        for perfil_id, nombre_usuario in cur.fetchall():
            nombre_limpio = limpiar_valor_texto(nombre_usuario)
            if nombre_limpio:
                cuentas.append(
                    {
                        "id": perfil_id,
                        "nombre_usuario": nombre_limpio,
                    }
                )

        return cuentas


def guardar_resultados_db(
    conn,
    nombre: str,
    seguidores: str,
    posts: list,
    perfil_id: int | None = None,
) -> tuple[bool, str]:
    """
    Guarda seguidores y publicaciones en PostgreSQL.

    Evita duplicados de publicaciones usando:
        UNIQUE (perfil_id, id_publicacion)

    Inserta una fila nueva en metricas_perfil en cada ejecución (histórico de seguidores).
    """
    try:
        perfil_id = asegurar_perfil_id(conn, nombre)

        seguidores_int = convertir_a_entero(seguidores)
        if seguidores_int == 0 and seguidores.strip().lower() not in {"0", ""}:
            logging.warning(
                "Seguidores no numéricos para '%s' (%r); se guardará como 0.",
                nombre,
                seguidores,
            )

        guardados = 0
        omitidos = 0

        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.metricas_perfil (
                    perfil_id,
                    impresiones_totales,
                    seguidores
                )
                VALUES (%s, %s, %s)
                """,
                (perfil_id, 0, seguidores_int),
            )

            for post in posts:
                try:
                    cur.execute("SAVEPOINT guardar_post")
                    cur.execute(
                        """
                        INSERT INTO public.publicaciones (
                            perfil_id,
                            id_publicacion,
                            fecha_publicacion,
                            contenido,
                            reacciones,
                            comentarios,
                            compartidos,
                            envios
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (perfil_id, id_publicacion)
                        DO UPDATE SET
                            fecha_publicacion = EXCLUDED.fecha_publicacion,
                            contenido = EXCLUDED.contenido,
                            reacciones = EXCLUDED.reacciones,
                            comentarios = EXCLUDED.comentarios,
                            compartidos = EXCLUDED.compartidos,
                            envios = EXCLUDED.envios
                        """,
                        (
                            perfil_id,
                            post["id_publicacion"],
                            convertir_fecha_publicacion(
                                post.get("fecha", "")
                            ).date(),
                            (post.get("contenido") or "")[:4000],
                            convertir_a_entero(post.get("recomendaciones", "0")),
                            convertir_a_entero(post.get("comentarios", "0")),
                            convertir_a_entero(post.get("compartidos", "0")),
                            convertir_a_entero(post.get("envios", "0")),
                        ),
                    )
                    cur.execute("RELEASE SAVEPOINT guardar_post")
                    guardados += 1
                except Exception as e:
                    cur.execute("ROLLBACK TO SAVEPOINT guardar_post")
                    omitidos += 1
                    logging.warning(
                        "Post omitido para '%s' (id=%s): %s",
                        nombre,
                        post.get("id_publicacion"),
                        e,
                    )

        conn.commit()

        return (
            True,
            f"BD OK '{nombre}' (perfil_id={perfil_id}): "
            f"{guardados} publicaciones guardadas, {omitidos} omitidas, "
            f"nueva métrica de seguidores={seguidores_int}.",
        )

    except Exception as e:
        conn.rollback()
        logging.exception("Error guardando en PostgreSQL para '%s'", nombre)
        return False, f"Error guardando en PostgreSQL para '{nombre}': {e}"


# ============================================================
# Extracción desde LinkedIn
# ============================================================

def extraer_recomendaciones(tarjeta, texto: str) -> str:
    selectores_reacciones = [
        ".social-details-social-counts__reactions-count",
        ".social-details-social-counts .reactions-count",
        "button[aria-label*='reacci'] span",
        "button[aria-label*='reaction'] span",
    ]

    for selector in selectores_reacciones:
        try:
            loc = tarjeta.locator(selector).first
            if loc.count() > 0:
                txt = (loc.inner_text(timeout=1500) or "").strip()
                m = re.search(r"(\d[\d.,]*)", txt)
                if m:
                    return normalizar_numero(m.group(1))
        except Exception:
            continue

    valor = extraer_numero_por_etiqueta(
        texto,
        r"(?:reacciones?|reactions?|likes?|me gusta|recomendaciones?)",
    )

    if valor != "0":
        return valor

    lineas = [linea.strip() for linea in texto.split("\n") if linea.strip()]

    for linea in lineas:
        linea_norm = normalizar_texto(linea)

        if any(
            keyword in linea_norm
            for keyword in ["comentario", "compartido", "recomendar", "comentar"]
        ):
            continue

        m = re.search(r"(\d[\d.,]*)", linea)
        if m:
            return normalizar_numero(m.group(1))

    return "0"


def extraer_comentarios(texto: str) -> str:
    m = re.search(r"\b(\d[\d.,]*)\s*comentarios?\b", texto, re.IGNORECASE)
    if m:
        return normalizar_numero(m.group(1))

    return "0"


def extraer_compartidos(texto: str) -> str:
    m = re.search(
        r"\b(\d[\d.,]*)\s*(?:vez|veces)\s*compartid[oa]s?\b",
        texto,
        re.IGNORECASE,
    )

    if m:
        return normalizar_numero(m.group(1))

    return "0"


def extraer_fecha(texto: str) -> str:
    fecha_match = re.search(
        r"(\d+\s*(?:h|min|d|sem(?:anas?)?|mes(?:es)?|mo|w)|"
        r"hace\s+\d+\s+(?:min(?:utos?)?|h(?:oras?)?|d(?:ias?)?|sem(?:anas?)?|mes(?:es)?|mo)|"
        r"\d{1,2}/\d{1,2}/\d{2,4})",
        texto,
        re.IGNORECASE,
    )

    if fecha_match:
        return fecha_match.group(1)

    return "Fecha no encontrada"


def extraer_contenido_desde_tarjeta(tarjeta) -> str:
    selectores_contenido = [
        ".update-components-text .break-words",
        ".feed-shared-inline-show-more-text .break-words",
        ".feed-shared-text",
    ]

    for selector in selectores_contenido:
        try:
            loc = tarjeta.locator(selector).first
            if loc.count() > 0:
                texto = (loc.inner_text(timeout=2000) or "").strip()
                if texto:
                    return texto[:1200]
        except Exception:
            continue

    try:
        texto_tarjeta = (tarjeta.inner_text(timeout=3000) or "").strip()
    except Exception:
        texto_tarjeta = ""

    if not texto_tarjeta:
        return "Sin contenido de texto"

    lineas = [linea.strip() for linea in texto_tarjeta.split("\n") if linea.strip()]

    if len(lineas) >= 3:
        return " ".join(lineas[2:12])[:1200]

    return lineas[0][:1200] if lineas else "Sin contenido de texto"


def _extraer_numero_seguidores_de_texto(texto: str) -> str | None:
    if not texto:
        return None

    texto = normalizar_texto(texto.replace("\u00a0", " "))

    patrones = [
        r"([\d][\d.,\s]*(?:\s*(?:mil|[km]|m(?:illones)?))?)\s*(?:seguidores|followers)\b",
        r"\b(?:seguidores|followers)\s*[·•|]?\s*([\d][\d.,\s]*(?:\s*(?:mil|[km]))?)",
        r"([\d][\d.,\s]*)\s*(?:seguidores|followers)\b",
    ]

    for patron in patrones:
        m = re.search(patron, texto, re.IGNORECASE)
        if m:
            candidato = normalizar_numero(m.group(1))
            if candidato and re.search(r"\d", candidato):
                return candidato

    return None


def extraer_seguidores_perfil(page) -> str:
    """
    Lee seguidores en la página de perfil. LinkedIn los pinta tarde (JS)
    y a veces usa '12 mil', '1,2 K', etc.
    """
    try:
        page.wait_for_load_state("domcontentloaded")
        delay(2)
    except Exception:
        pass

    selectores = [
        'main a[href*="follower"]',
        'section.artdeco-card a[href*="follower"]',
        'li.text-body-small a[href*="follower"]',
        ".pv-top-card--list a[href*=\"follower\"]",
    ]

    for selector in selectores:
        try:
            enlaces = page.locator(selector)
            for i in range(min(enlaces.count(), 3)):
                bloque = (enlaces.nth(i).inner_text(timeout=2000) or "").strip()
                padre = ""
                try:
                    padre = (
                        enlaces.nth(i).locator("xpath=ancestor::li[1]").inner_text(
                            timeout=1500
                        )
                        or ""
                    ).strip()
                except Exception:
                    pass
                for fragmento in (bloque, padre, f"{bloque} seguidores"):
                    valor = _extraer_numero_seguidores_de_texto(fragmento)
                    if valor:
                        return valor
        except Exception:
            continue

    try:
        texto_main = page.locator("main").first.inner_text(timeout=5000)
    except Exception:
        texto_main = ""

    valor = _extraer_numero_seguidores_de_texto(texto_main)
    if valor:
        return valor

    texto_body = page.inner_text("body", timeout=8000)
    valor = _extraer_numero_seguidores_de_texto(texto_body)
    if valor:
        return valor

    logging.warning(
        "No se detectaron seguidores en el perfil (DOM aún no listo o perfil sin dato público)."
    )
    return "No encontrado"


def abrir_perfil_desde_busqueda(page, nombre_objetivo: str) -> str | None:
    query = quote_plus(nombre_objetivo)

    url_busqueda = (
        "https://www.linkedin.com/search/results/people/"
        f"?keywords={query}&origin=SWITCH_SEARCH_VERTICAL"
    )

    page.goto(url_busqueda, wait_until="domcontentloaded")
    delay(2)

    nombre_objetivo_norm = normalizar_texto(nombre_objetivo)

    links_perfil = page.locator(
        'main a[href*="/in/"], .search-results-container a[href*="/in/"]'
    )

    total_links = links_perfil.count()
    candidatos = []

    for i in range(total_links):
        link = links_perfil.nth(i)

        try:
            href = (link.get_attribute("href") or "").split("?")[0]
            if "/in/" not in href:
                continue

            texto_link = (link.inner_text(timeout=1500) or "").strip()
            texto_norm = normalizar_texto(texto_link)

            if not texto_norm:
                continue

            puntaje = 0

            if texto_norm == nombre_objetivo_norm:
                puntaje = 100
            elif nombre_objetivo_norm in texto_norm:
                puntaje = 70
            elif texto_norm in nombre_objetivo_norm:
                puntaje = 50
            else:
                continue

            candidatos.append((puntaje, i, href))

        except Exception:
            continue

    candidatos.sort(key=lambda item: item[0], reverse=True)

    for _, i, _ in candidatos:
        link = links_perfil.nth(i)

        try:
            link.click(timeout=5000)
            page.wait_for_load_state("domcontentloaded")
            delay(1)

            if "/in/" in page.url:
                return page.url.split("?")[0]

        except Exception:
            continue

    url_perfil = page.evaluate(
        """(objetivo) => {
            const normalizar = (t) => (
                (t || "")
                    .toLowerCase()
                    .normalize("NFD")
                    .replace(/[\\u0300-\\u036f]/g, "")
                    .replace(/\\s+/g, " ")
                    .trim()
            );

            const objetivoNorm = normalizar(objetivo);

            const links = Array.from(
                document.querySelectorAll(
                    'main a[href*="/in/"], .search-results-container a[href*="/in/"]'
                )
            );

            for (const link of links) {
                const href = (link.href || "").split("?")[0];
                const nombre = normalizar(link.innerText || "");

                if (!href.includes("/in/")) continue;

                if (
                    nombre === objetivoNorm ||
                    nombre.includes(objetivoNorm) ||
                    objetivoNorm.includes(nombre)
                ) {
                    return href;
                }
            }

            return null;
        }""",
        nombre_objetivo,
    )

    if url_perfil:
        page.goto(url_perfil, wait_until="domcontentloaded")
        delay(1)
        return page.url.split("?")[0]

    return None


def _scroll_y_actual(page) -> float:
    try:
        return float(
            page.evaluate(
                "() => window.scrollY || document.documentElement.scrollTop || 0"
            )
        )
    except Exception:
        return 0.0


def _scroll_cargar_mas_publicaciones(page, tarjetas, total_tarjetas: int) -> None:
    """Avanza el scroll sin volver arriba: ancla la última tarjeta y scrollBy incremental."""
    scroll_antes = _scroll_y_actual(page)

    if total_tarjetas > 0:
        try:
            tarjetas.nth(total_tarjetas - 1).evaluate(
                """(el) => el.scrollIntoView({
                    block: 'end',
                    inline: 'nearest',
                    behavior: 'instant'
                })"""
            )
        except Exception:
            pass

    try:
        page.evaluate(
            """() => {
                const paso = Math.max(window.innerHeight * 0.85, 500);
                window.scrollBy({ top: paso, left: 0, behavior: 'instant' });
            }"""
        )
    except Exception:
        pass

    delay(1.5)

    if _scroll_y_actual(page) <= scroll_antes + 20:
        try:
            page.evaluate(
                "() => window.scrollBy({ top: 1200, left: 0, behavior: 'instant' })"
            )
        except Exception:
            pass
        delay(1)


def extraer_posts_por_antiguedad(
    page,
    dias_atras: int = 1,
    max_intentos: int | None = None,
) -> list[dict]:
    if max_intentos is None:
        max_intentos = _leer_max_scrolls()
    posts = []
    firmas_vistas = set()

    page.wait_for_load_state("domcontentloaded")
    delay(2)

    limite = datetime.now(timezone.utc) - timedelta(days=dias_atras)
    logging.info("Filtro temporal: %s (%s días)", describir_ventana_temporal(dias_atras), dias_atras)

    intentos_scroll = 0
    posts_fuera_de_rango_consecutivos = 0
    scrolls_sin_nuevas_tarjetas = 0
    ultimo_indice_procesado = 0

    while (
        intentos_scroll < max_intentos
        and posts_fuera_de_rango_consecutivos < 8
        and scrolls_sin_nuevas_tarjetas < 5
    ):
        tarjetas = page.locator("div.feed-shared-update-v2, article")
        total_tarjetas = tarjetas.count()

        if total_tarjetas < ultimo_indice_procesado:
            logging.debug(
                "El feed se recargó (%s -> %s tarjetas); reiniciando índice.",
                ultimo_indice_procesado,
                total_tarjetas,
            )
            ultimo_indice_procesado = 0

        if total_tarjetas <= ultimo_indice_procesado:
            scrolls_sin_nuevas_tarjetas += 1
        else:
            scrolls_sin_nuevas_tarjetas = 0

        for i in range(ultimo_indice_procesado, total_tarjetas):
            tarjeta = tarjetas.nth(i)

            try:
                contenido = extraer_contenido_desde_tarjeta(tarjeta)
                texto_tarjeta = (tarjeta.inner_text(timeout=3000) or "").strip()

                if not texto_tarjeta:
                    continue

                fecha = extraer_fecha(texto_tarjeta)

                if post_fuera_de_ventana(fecha, limite, dias_atras):
                    posts_fuera_de_rango_consecutivos += 1
                    if fecha_indica_fin_de_ventana(fecha, dias_atras):
                        logging.info(
                            "Fecha '%s' (límite del mes alcanzado); deteniendo scroll.",
                            fecha,
                        )
                        posts_fuera_de_rango_consecutivos = 8
                    continue

                posts_fuera_de_rango_consecutivos = 0

                recomendaciones = extraer_recomendaciones(tarjeta, texto_tarjeta)
                comentarios = extraer_comentarios(texto_tarjeta)
                compartidos = extraer_compartidos(texto_tarjeta)
                envios = extraer_numero_por_etiqueta(
                    texto_tarjeta,
                    r"(?:envios?|sends?)",
                )

                firma = f"{fecha}-{contenido[:80]}"

                if firma in firmas_vistas:
                    continue

                firmas_vistas.add(firma)

                id_publicacion = hashlib.sha1(
                    f"{firma}-{contenido}".encode("utf-8")
                ).hexdigest()

                posts.append(
                    {
                        "firma": firma,
                        "id_publicacion": id_publicacion,
                        "contenido": contenido,
                        "fecha": fecha,
                        "recomendaciones": recomendaciones,
                        "comentarios": comentarios,
                        "compartidos": compartidos,
                        "envios": envios,
                    }
                )

                logging.debug("Post capturado. Total acumulado: %s", len(posts))

            except Exception as e:
                logging.debug("Error procesando una tarjeta: %s", e)
                continue

        ultimo_indice_procesado = total_tarjetas
        _scroll_cargar_mas_publicaciones(page, tarjetas, total_tarjetas)

        intentos_scroll += 1

    return posts


# ============================================================
# Flujo de scraping por cuenta
# ============================================================

def scrapear_cuenta(page, nombre: str, dias_atras: int = 1) -> dict:
    logging.info("Buscando perfil de: %s", nombre)

    perfil_url = abrir_perfil_desde_busqueda(page, nombre)

    if not perfil_url:
        raise RuntimeError(f"No se pudo encontrar el perfil en LinkedIn: {nombre}")

    logging.info("Perfil encontrado: %s", perfil_url)

    seguidores = extraer_seguidores_perfil(page)
    logging.info("Seguidores detectados para '%s': %s", nombre, seguidores)

    actividad_url = perfil_url.rstrip("/") + "/recent-activity/all/"

    logging.info("Abriendo actividad reciente de: %s", nombre)

    page.goto(actividad_url, wait_until="domcontentloaded")
    delay(2)

    posts = extraer_posts_por_antiguedad(page, dias_atras=dias_atras)

    return {
        "nombre": nombre,
        "perfil_url": perfil_url,
        "seguidores": seguidores,
        "posts": posts,
        "dias_atras": dias_atras,
    }


def imprimir_resumen_cuenta(resultado: dict, db_ok: bool | None = None) -> None:
    nombre = resultado["nombre"]
    posts = resultado["posts"]
    dias = resultado.get("dias_atras", 1)
    ventana = describir_ventana_temporal(dias)

    if db_ok is True:
        bd = "guardado en BD"
    elif db_ok is False:
        bd = "sin BD"
    else:
        bd = "BD no disponible"

    print(
        f"  {nombre} | Seguidores: {resultado['seguidores']} | "
        f"Posts: {len(posts)} ({ventana}) | {bd}"
    )


def procesar_cuenta(
    context,
    conn,
    cuenta: dict,
    dias_atras: int = 1,
) -> dict:
    nombre = limpiar_valor_texto(cuenta["nombre_usuario"])
    perfil_id = cuenta.get("id")

    if not nombre:
        return {
            "ok": False,
            "nombre": nombre,
            "posts": 0,
            "error": "Nombre de cuenta vacío",
        }

    page = context.new_page()

    try:
        resultado = scrapear_cuenta(page, nombre, dias_atras=dias_atras)

        ok_db = False
        msg_db = "BD no disponible."

        if conn is not None:
            ok_db, msg_db = guardar_resultados_db(
                conn=conn,
                nombre=nombre,
                seguidores=resultado["seguidores"],
                posts=resultado["posts"],
                perfil_id=perfil_id,
            )
            if ok_db:
                logging.info(msg_db)
            else:
                logging.error(msg_db)

        imprimir_resumen_cuenta(resultado, db_ok=ok_db if conn is not None else None)

        return {
            "ok": True,
            "db_ok": ok_db,
            "nombre": nombre,
            "seguidores": resultado["seguidores"],
            "posts": len(resultado["posts"]),
            "mensaje_db": msg_db,
        }

    except Exception as e:
        logging.exception("Falló el scraping de la cuenta '%s'", nombre)

        try:
            conn.rollback()
        except Exception:
            pass

        return {
            "ok": False,
            "nombre": nombre,
            "posts": 0,
            "error": str(e),
        }

    finally:
        try:
            page.close()
        except Exception:
            pass


# ============================================================
# Main
# ============================================================

def resolver_headless(cli_headless: bool) -> bool:
    if cli_headless:
        return True
    return _leer_headless_env()


def main(
    nombre: str | None = None,
    headless: bool = False,
) -> tuple[int, str]:
    base_dir = app_paths.get_app_dir()
    cargar_variables_entorno()
    headless = resolver_headless(headless)
    state_path = os.path.join(base_dir, "state.json")

    if not os.path.exists(state_path):
        msg = (
            f"No existe el archivo de sesión: {state_path}\n"
            "Ejecuta primero login.py para generar state.json."
        )
        print(msg)
        return 2, msg

    conn = None
    esquema_ok = False
    try:
        conn = abrir_conexion_db()
        esquema_ok, msg_esquema = verificar_y_migrar_esquema(conn)
        if esquema_ok:
            print(msg_esquema)
        else:
            print(f"Error de esquema PostgreSQL: {msg_esquema}")
            conn.close()
            conn = None
    except Exception as e:
        print(f"ERROR: no se pudo conectar a PostgreSQL: {e}")
        print(
            "Revisa PGHOST, PGPORT, PGDATABASE, PGUSER y PGPASSWORD en .env "
            "y que el contenedor Docker esté en ejecución."
        )
        print("Se continuará con cuentas del .env (sin guardar en BD).")

    try:
        if conn is not None:
            cuentas, origen = resolver_cuentas_a_scrapear(conn, nombre=nombre)
        else:
            cuentas, origen = resolver_cuentas_sin_db(nombre=nombre)

        if not cuentas:
            msg = (
                "No hay cuentas para scrapear. "
                "Añade perfiles en la BD o define SCRAPING_ACCOUNTS / SCRAPING_ACCOUNT en .env"
            )
            print(msg)
            return 2, msg

        print("\n" + "=" * 60)
        print(f"Cuentas a scrapear: {len(cuentas)}")
        print(f"Origen de cuentas: {origen}")
        scraping_days = obtener_scraping_days()
        meses_inicial = obtener_meses_inicial()
        dias_perfil_vacio = obtener_dias_perfil_inicial()
        print(
            f"SCRAPING_DAYS={scraping_days}: "
            f"{describir_ventana_temporal(scraping_days)} para perfiles ya en la BD"
        )
        print(
            f"SCRAPING_MONTHS_INITIAL={meses_inicial}: "
            f"{describir_ventana_temporal(dias_perfil_vacio)} solo para perfiles nuevos del .env"
        )
        print("=" * 60)

        resultados = []

        with sync_playwright() as p:
            browser = app_paths.launch_browser(
                p,
                headless=headless,
                slow_mo=_leer_slow_mo(),
            )

            context = browser.new_context(
                storage_state=state_path,
                viewport={
                    "width": 1280,
                    "height": 900,
                },
            )

            context.set_default_timeout(90000)
            context.set_default_navigation_timeout(90000)

            try:
                print("\nProcesando cuentas...")
                for indice, cuenta in enumerate(cuentas, 1):
                    dias_atras = dias_atras_para_cuenta(conn, cuenta)
                    print(f"[{indice}/{len(cuentas)}] {cuenta['nombre_usuario']}...", flush=True)

                    resultado = procesar_cuenta(
                        context=context,
                        conn=conn,
                        cuenta=cuenta,
                        dias_atras=dias_atras,
                    )

                    resultados.append(resultado)

            finally:
                try:
                    context.close()
                except Exception:
                    pass

                browser.close()

        print("\n" + "=" * 60)
        print("RESUMEN FINAL")
        print("=" * 60)

        exitosas = [r for r in resultados if r.get("ok")]
        fallidas = [r for r in resultados if not r.get("ok")]
        sin_bd = [r for r in resultados if r.get("ok") and not r.get("db_ok", True)]

        print(f"Cuentas procesadas: {len(resultados)}")
        print(f"Scraping correcto: {len(exitosas)}")
        print(f"Scraping con error: {len(fallidas)}")
        if conn is not None and sin_bd:
            print(f"Sin guardar en BD: {len(sin_bd)}")

        for resultado in resultados:
            scrape_ok = resultado.get("ok")
            db_ok = resultado.get("db_ok", True)
            if scrape_ok and db_ok:
                estado = "OK"
            elif scrape_ok:
                estado = "OK (sin BD)"
            else:
                estado = "ERROR"
            nombre_resultado = resultado.get("nombre", "Sin nombre")
            posts = resultado.get("posts", 0)
            seguidores = resultado.get("seguidores", "—")

            print(
                f"- [{estado}] {nombre_resultado} | "
                f"Seguidores: {seguidores} | Posts: {posts}"
            )

            if resultado.get("error"):
                print(f"    Error: {resultado['error']}")
            elif resultado.get("mensaje_db") and not db_ok:
                print(f"    BD: {resultado['mensaje_db']}")

        if fallidas:
            lineas = []
            for r in fallidas:
                nombre_err = r.get("nombre", "Sin nombre")
                detalle = r.get("error") or "Error desconocido"
                lineas.append(f"- {nombre_err}: {detalle}")
            msg = (
                f"Scraping con errores ({len(fallidas)}/{len(resultados)} cuentas):\n"
                + "\n".join(lineas)
            )
            return 1, msg

        return 0, ""

    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Scraper de métricas de LinkedIn para una o varias cuentas desde PostgreSQL."
    )

    parser.add_argument(
        "--nombre",
        type=str,
        default=None,
        help="Scrapea solo una cuenta concreta. Si no se indica, scrapea todas las cuentas de public.perfiles.",
    )

    parser.add_argument(
        "--headless",
        action="store_true",
        help="Ejecuta Chromium en modo headless.",
    )

    args = parser.parse_args()

    codigo, _ = main(
        nombre=args.nombre,
        headless=args.headless,
    )
    raise SystemExit(codigo)
