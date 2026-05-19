import argparse
import hashlib
import json
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


# ============================================================
# Configuración general
# ============================================================

DEFAULT_MAX_SCROLLS = int(os.getenv("MAX_SCROLLS", "40"))
DEFAULT_HEADLESS = os.getenv("HEADLESS", "false").lower() in {"1", "true", "yes"}
DEFAULT_SLOW_MO = int(os.getenv("PLAYWRIGHT_SLOW_MO", "50"))


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

    m_mil = re.search(r"(\d+(?:[.,]\d+)?)\s*mil$", texto)
    if m_mil:
        base = float(m_mil.group(1).replace(",", "."))
        return int(base * 1_000)

    solo_digitos = re.sub(r"[^\d]", "", texto)
    return int(solo_digitos) if solo_digitos else 0


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

    m_hace = re.search(r"hace\s+(\d+)\s+([a-z]+)", texto)
    if m_hace:
        cantidad = int(m_hace.group(1))
        unidad = m_hace.group(2)
    else:
        m_simple = re.search(r"(\d+)\s*(h|min|d|sem|mo)", texto)
        if not m_simple:
            return ahora

        cantidad = int(m_simple.group(1))
        unidad = m_simple.group(2)

    if unidad.startswith("min"):
        return ahora - timedelta(minutes=cantidad)

    if unidad.startswith("h"):
        return ahora - timedelta(hours=cantidad)

    if unidad.startswith("d") or unidad.startswith("dia"):
        return ahora - timedelta(days=cantidad)

    if unidad.startswith("sem"):
        return ahora - timedelta(weeks=cantidad)

    if unidad.startswith("mo") or unidad.startswith("mes"):
        return ahora - timedelta(days=30 * cantidad)

    return ahora


# ============================================================
# PostgreSQL
# ============================================================

def obtener_config_db() -> dict:
    return {
        "host": os.getenv("PGHOST", "localhost"),
        "port": int(os.getenv("PGPORT", "5432")),
        "dbname": os.getenv("PGDATABASE", "Linkedin_Scrapper"),
        "user": os.getenv("PGUSER", "postgres"),
        "password": os.getenv("PGPASSWORD", ""),
        "connect_timeout": 10,
    }


def abrir_conexion_db():
    if psycopg is None:
        raise RuntimeError(
            "No se encontró psycopg. Instala la dependencia con: pip install psycopg[binary]"
        )

    config = obtener_config_db()
    return psycopg.connect(**config)


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

    Evita duplicar métricas de perfil en el mismo día:
        - Si existe una métrica del día actual, la actualiza.
        - Si no existe, inserta una nueva.
    """
    try:
        with conn.cursor() as cur:
            if perfil_id is None:
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

            seguidores_int = convertir_a_entero(seguidores)

            cur.execute(
                """
                SELECT id
                FROM public.metricas_perfil
                WHERE perfil_id = %s
                  AND fecha_captura >= date_trunc('day', NOW())
                ORDER BY fecha_captura DESC
                LIMIT 1
                """,
                (perfil_id,),
            )

            metrica_existente = cur.fetchone()

            if metrica_existente:
                metrica_id = metrica_existente[0]

                cur.execute(
                    """
                    UPDATE public.metricas_perfil
                    SET fecha_captura = NOW(),
                        impresiones_totales = %s,
                        seguidores = %s
                    WHERE id = %s
                    """,
                    (
                        0,
                        seguidores_int,
                        metrica_id,
                    ),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO public.metricas_perfil (
                        perfil_id,
                        impresiones_totales,
                        seguidores
                    )
                    VALUES (%s, %s, %s)
                    """,
                    (
                        perfil_id,
                        0,
                        seguidores_int,
                    ),
                )

            for post in posts:
                cur.execute(
                    """
                    INSERT INTO public.publicaciones (
                        perfil_id,
                        id_publicacion,
                        fecha_publicacion,
                        reacciones,
                        comentarios,
                        compartidos,
                        envios
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (perfil_id, id_publicacion)
                    DO UPDATE SET
                        fecha_publicacion = EXCLUDED.fecha_publicacion,
                        reacciones = EXCLUDED.reacciones,
                        comentarios = EXCLUDED.comentarios,
                        compartidos = EXCLUDED.compartidos,
                        envios = EXCLUDED.envios
                    """,
                    (
                        perfil_id,
                        post["id_publicacion"],
                        convertir_fecha_publicacion(post.get("fecha", "")).date(),
                        convertir_a_entero(post.get("recomendaciones", "0")),
                        convertir_a_entero(post.get("comentarios", "0")),
                        convertir_a_entero(post.get("compartidos", "0")),
                        convertir_a_entero(post.get("envios", "0")),
                    ),
                )

        conn.commit()

        return (
            True,
            f"Datos guardados en PostgreSQL para '{nombre}'. Posts procesados: {len(posts)}",
        )

    except Exception as e:
        conn.rollback()
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
        r"(\d+\s*(?:h|min|sem|d)|\d+\s*mo|hace\s+\d+\s+\w+|\d{1,2}/\d{1,2}/\d{2,4})",
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


def extraer_seguidores_perfil(page) -> str:
    texto = page.inner_text("body")

    m = re.search(
        r"(\d[\d.,\s]*)\s*(seguidores|followers)",
        texto,
        re.IGNORECASE,
    )

    if m:
        return normalizar_numero(m.group(1))

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


def extraer_posts_ultimo_dia(page, max_intentos: int = DEFAULT_MAX_SCROLLS) -> list[dict]:
    posts = []
    firmas_vistas = set()

    page.wait_for_load_state("domcontentloaded")
    delay(2)

    limite = datetime.now(timezone.utc) - timedelta(days=1)

    intentos_scroll = 0
    posts_fuera_de_rango_consecutivos = 0
    total_tarjetas_anterior = 0
    scrolls_sin_nuevas_tarjetas = 0

    while (
        intentos_scroll < max_intentos
        and posts_fuera_de_rango_consecutivos < 8
        and scrolls_sin_nuevas_tarjetas < 5
    ):
        tarjetas = page.locator("div.feed-shared-update-v2, article")
        total_tarjetas = tarjetas.count()

        if total_tarjetas <= total_tarjetas_anterior:
            scrolls_sin_nuevas_tarjetas += 1
        else:
            scrolls_sin_nuevas_tarjetas = 0
            total_tarjetas_anterior = total_tarjetas

        for i in range(total_tarjetas):
            tarjeta = tarjetas.nth(i)

            try:
                tarjeta.scroll_into_view_if_needed(timeout=2500)
            except Exception:
                pass

            delay(0.2)

            try:
                contenido = extraer_contenido_desde_tarjeta(tarjeta)
                texto_tarjeta = (tarjeta.inner_text(timeout=3000) or "").strip()

                if not texto_tarjeta:
                    continue

                fecha = extraer_fecha(texto_tarjeta)
                fecha_dt = convertir_fecha_publicacion(fecha)

                if fecha_dt < limite:
                    posts_fuera_de_rango_consecutivos += 1
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

                logging.info("Post capturado. Total acumulado: %s", len(posts))

            except Exception as e:
                logging.debug("Error procesando una tarjeta: %s", e)
                continue

        page.mouse.wheel(0, 3500)
        delay(2)

        intentos_scroll += 1

    return posts


# ============================================================
# Guardado JSON
# ============================================================

def guardar_resultados_json(
    base_dir: str,
    nombre: str,
    perfil_url: str,
    seguidores: str,
    posts: list,
) -> str:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    nombre_limpio = re.sub(r"[^a-zA-Z0-9_-]+", "_", nombre).strip("_") or "cuenta"

    salida_path = os.path.join(
        base_dir,
        f"posts_{nombre_limpio}_{timestamp}.json",
    )

    data = {
        "cuenta_buscada": nombre,
        "perfil_url": perfil_url,
        "seguidores": seguidores,
        "filtro_temporal": "ultimo_dia",
        "cantidad_obtenida": len(posts),
        "generado_en": datetime.now().isoformat(timespec="seconds"),
        "posts": [
            {
                "contenido": post["contenido"],
                "id_publicacion": post["id_publicacion"],
                "fecha": convertir_fecha_publicacion(post["fecha"]).date().isoformat(),
                "recomendaciones": post["recomendaciones"],
                "comentarios": post["comentarios"],
                "compartidos": post["compartidos"],
                "envios": post.get("envios", "0"),
            }
            for post in posts
        ],
    }

    with open(salida_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    return salida_path


# ============================================================
# Flujo de scraping por cuenta
# ============================================================

def scrapear_cuenta(page, nombre: str) -> dict:
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

    posts = extraer_posts_ultimo_dia(page)

    return {
        "nombre": nombre,
        "perfil_url": perfil_url,
        "seguidores": seguidores,
        "posts": posts,
    }


def imprimir_resultados_cuenta(resultado: dict) -> None:
    nombre = resultado["nombre"]
    seguidores = resultado["seguidores"]
    posts = resultado["posts"]

    print("\n" + "=" * 60)
    print(f"RESULTADOS DE {nombre.upper()}")
    print("=" * 60)
    print(f"Seguidores: {seguidores}")
    print(f"Publicaciones encontradas en el último día: {len(posts)}")

    if not posts:
        print("No se encontraron publicaciones recientes.")
        return

    for i, post in enumerate(posts, 1):
        print(f"\nPost #{i}")
        print(f"Fecha: {post['fecha']}")
        print(f"Contenido: {post['contenido']}")
        print(f"Recomendaciones: {post['recomendaciones']}")
        print(f"Comentarios: {post['comentarios']}")
        print(f"Compartidos: {post['compartidos']}")
        print(f"Envios: {post.get('envios', '0')}")


def procesar_cuenta(
    context,
    conn,
    cuenta: dict,
    base_dir: str,
    guardar_json: bool = True,
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
        resultado = scrapear_cuenta(page, nombre)
        imprimir_resultados_cuenta(resultado)

        salida_json = None

        if guardar_json:
            salida_json = guardar_resultados_json(
                base_dir=base_dir,
                nombre=nombre,
                perfil_url=resultado["perfil_url"],
                seguidores=resultado["seguidores"],
                posts=resultado["posts"],
            )

            logging.info("JSON guardado en: %s", salida_json)

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

        return {
            "ok": ok_db,
            "nombre": nombre,
            "posts": len(resultado["posts"]),
            "json": salida_json,
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

def main(
    nombre: str | None = None,
    guardar_json: bool = True,
    headless: bool = DEFAULT_HEADLESS,
) -> None:
    base_dir = os.path.dirname(os.path.abspath(__file__))
    state_path = os.path.join(base_dir, "state.json")

    if not os.path.exists(state_path):
        print(f"No existe el archivo de sesión: {state_path}")
        print("Ejecuta primero login.py para generar state.json.")
        return

    try:
        conn = abrir_conexion_db()
    except Exception as e:
        print(f"No se pudo conectar a PostgreSQL: {e}")
        return

    try:
        if nombre:
            nombre_limpio = limpiar_valor_texto(nombre)

            if not nombre_limpio:
                print("Debes introducir un nombre válido.")
                return

            cuentas = [
                {
                    "id": None,
                    "nombre_usuario": nombre_limpio,
                }
            ]
        else:
            cuentas = obtener_cuentas_linkedin(conn)

        if not cuentas:
            print("No hay cuentas de LinkedIn guardadas en public.perfiles.")
            return

        print("\n" + "=" * 60)
        print(f"Cuentas a scrapear: {len(cuentas)}")
        print("=" * 60)

        resultados = []

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=headless,
                slow_mo=DEFAULT_SLOW_MO,
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
                for indice, cuenta in enumerate(cuentas, 1):
                    print("\n" + "-" * 60)
                    print(
                        f"[{indice}/{len(cuentas)}] Procesando cuenta: "
                        f"{cuenta['nombre_usuario']}"
                    )
                    print("-" * 60)

                    resultado = procesar_cuenta(
                        context=context,
                        conn=conn,
                        cuenta=cuenta,
                        base_dir=base_dir,
                        guardar_json=guardar_json,
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

        print(f"Cuentas procesadas: {len(resultados)}")
        print(f"Cuentas correctas: {len(exitosas)}")
        print(f"Cuentas con error: {len(fallidas)}")

        for resultado in resultados:
            estado = "OK" if resultado.get("ok") else "ERROR"
            nombre_resultado = resultado.get("nombre", "Sin nombre")
            posts = resultado.get("posts", 0)

            print(f"- [{estado}] {nombre_resultado} | Posts: {posts}")

            if resultado.get("error"):
                print(f"  Error: {resultado['error']}")

            if resultado.get("mensaje_db") and not resultado.get("ok"):
                print(f"  BD: {resultado['mensaje_db']}")

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
        "--no-json",
        action="store_true",
        help="No genera archivos JSON locales.",
    )

    parser.add_argument(
        "--headless",
        action="store_true",
        help="Ejecuta Chromium en modo headless.",
    )

    args = parser.parse_args()

    main(
        nombre=args.nombre,
        guardar_json=not args.no_json,
        headless=args.headless,
    )
