import os
import re
import time
import json
import hashlib
import unicodedata
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus

from playwright.sync_api import sync_playwright
try:
    import psycopg
except ImportError:
    psycopg = None


def delay(seconds: float = 2.0) -> None:
    time.sleep(seconds)


def normalizar_numero(valor: str) -> str:
    limpio = valor.replace("\u00a0", " ").strip()
    return re.sub(r"\s+", " ", limpio)


def normalizar_texto(valor: str) -> str:
    valor = valor.strip().lower()
    valor = unicodedata.normalize("NFD", valor)
    valor = "".join(c for c in valor if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", valor)


def extraer_numero_por_etiqueta(texto: str, etiqueta: str) -> str:
    patron_antes = rf"(\d[\d.,\s]*)\s*{etiqueta}"
    patron_despues = rf"{etiqueta}\s*(\d[\d.,\s]*)"

    m = re.search(patron_antes, texto, re.IGNORECASE)
    if not m:
        m = re.search(patron_despues, texto, re.IGNORECASE)
    if not m:
        return "0"

    return normalizar_numero(m.group(1))


def extraer_recomendaciones(tarjeta, texto: str) -> str:
    # 1) Prioridad: contador DOM de reacciones (LinkedIn UI).
    selectores_reacciones = [
        ".social-details-social-counts__reactions-count",
        ".social-details-social-counts .reactions-count",
        "button[aria-label*='reacci'] span",
        "button[aria-label*='reaction'] span",
    ]
    for selector in selectores_reacciones:
        loc = tarjeta.locator(selector).first
        if loc.count() > 0:
            txt = (loc.inner_text(timeout=1500) or "").strip()
            m = re.search(r"(\d[\d.,]*)", txt)
            if m:
                return normalizar_numero(m.group(1))

    # 2) Intento por etiqueta explícita en texto.
    valor = extraer_numero_por_etiqueta(
        texto, r"(?:reacciones?|reactions?|likes?|me gusta|recomendaciones?)"
    )
    if valor != "0":
        return valor

    # 3) Fallback: linea con iconos + numero (izquierda del pie del post).
    lineas = [l.strip() for l in texto.split("\n") if l.strip()]
    for linea in lineas:
        lnorm = normalizar_texto(linea)
        if any(k in lnorm for k in ["comentario", "compartido", "recomendar", "comentar"]):
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


def guardar_resultados_db(nombre: str, seguidores: str, posts: list) -> tuple[bool, str]:
    if psycopg is None:
        return False, "No se encontro psycopg. Instala: pip install psycopg[binary]"

    host = os.getenv("PGHOST", "localhost")
    port = int(os.getenv("PGPORT", "5433"))
    dbname = os.getenv("PGDATABASE", "linkedin_db")
    user = os.getenv("PGUSER", "user")
    password = os.getenv("PGPASSWORD", "1234")

    try:
        with psycopg.connect(
            host=host,
            port=port,
            dbname=dbname,
            user=user,
            password=password,
        ) as conn:
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

                cur.execute(
                    """
                    INSERT INTO public.metricas_perfil (
                        perfil_id, impresiones_totales, seguidores
                    )
                    VALUES (%s, %s, %s)
                    """,
                    (perfil_id, 0, convertir_a_entero(seguidores)),
                )

                for post in posts:
                    cur.execute(
                        """
                        INSERT INTO public.publicaciones (
                            perfil_id, id_publicacion, fecha_publicacion,
                            reacciones, comentarios, compartidos, envios
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

        return True, f"Datos guardados en PostgreSQL ({host}:{port}/{dbname})"
    except Exception as e:
        return False, f"Error guardando en BD: {e}"


def extraer_contenido_desde_tarjeta(tarjeta) -> str:
    selectores_contenido = [
        ".update-components-text .break-words",
        ".feed-shared-inline-show-more-text .break-words",
        ".feed-shared-text",
    ]
    for selector in selectores_contenido:
        loc = tarjeta.locator(selector).first
        if loc.count() > 0:
            texto = (loc.inner_text(timeout=2000) or "").strip()
            if texto:
                return texto[:1200]

    texto_tarjeta = (tarjeta.inner_text(timeout=3000) or "").strip()
    if not texto_tarjeta:
        return "Sin contenido de texto"

    lineas = [l.strip() for l in texto_tarjeta.split("\n") if l.strip()]
    if len(lineas) >= 3:
        return " ".join(lineas[2:12])[:1200]
    return lineas[0][:1200] if lineas else "Sin contenido de texto"


def extraer_seguidores_perfil(page) -> str:
    texto = page.inner_text("body")
    # Ejemplos: "1.234 seguidores", "123 followers"
    m = re.search(r"(\d[\d.,\s]*)\s*(seguidores|followers)", texto, re.IGNORECASE)
    if m:
        return normalizar_numero(m.group(1))
    return "No encontrado"


def abrir_perfil_desde_busqueda(page, nombre_objetivo: str) -> str | None:
    query = quote_plus(nombre_objetivo)
    url_busqueda = (
        "https://www.linkedin.com/search/results/people/"
        f"?keywords={query}&origin=SWITCH_SEARCH_VERTICAL"
    )
    page.goto(url_busqueda)
    page.wait_for_load_state("domcontentloaded")
    delay(2)

    nombre_objetivo_norm = normalizar_texto(nombre_objetivo)

    # 1) Intento principal: click real en el resultado cuyo nombre coincida.
    links_perfil = page.locator(
        'main a[href*="/in/"], .search-results-container a[href*="/in/"]'
    )
    total_links = links_perfil.count()
    candidatos = []
    for i in range(total_links):
        link = links_perfil.nth(i)
        href = (link.get_attribute("href") or "").split("?")[0]
        if "/in/" not in href:
            continue
        texto_link = (link.inner_text(timeout=1500) or "").strip()
        texto_norm = normalizar_texto(texto_link)
        if not texto_norm:
            continue

        # Priorizamos coincidencia exacta y luego coincidencia parcial fuerte.
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

    candidatos.sort(key=lambda x: x[0], reverse=True)
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

    # 2) Fallback: coger URL con coincidencia por texto y navegar directo.
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
            const links = Array.from(document.querySelectorAll('main a[href*="/in/"], .search-results-container a[href*="/in/"]'));
            for (const link of links) {
                const href = (link.href || '').split('?')[0];
                const nombre = normalizar(link.innerText || "");
                if (!href.includes('/in/')) continue;
                if (nombre === objetivoNorm || nombre.includes(objetivoNorm) || objetivoNorm.includes(nombre)) {
                    return href;
                }
            }
            return null;
        }"""
        ,
        nombre_objetivo
    )
    if url_perfil:
        page.goto(url_perfil)
        page.wait_for_load_state("domcontentloaded")
        delay(1)
        return page.url.split("?")[0]

    return None


def extraer_posts(page, cantidad: int):
    posts = []
    page.wait_for_load_state("domcontentloaded")
    delay(2)

    # Scroll progresivo hasta alcanzar el numero solicitado.
    intentos_scroll = 0
    max_intentos = max(20, cantidad * 4)
    while intentos_scroll < max_intentos and len(posts) < cantidad:
        tarjetas = page.locator("div.feed-shared-update-v2, article")
        total_tarjetas = tarjetas.count()

        # Recorremos todas las tarjetas cargadas hasta el momento.
        for i in range(total_tarjetas):
            tarjeta = tarjetas.nth(i)
            if len(posts) >= cantidad:
                break

            # Aseguramos que la tarjeta se renderice en pantalla.
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
                recomendaciones = extraer_recomendaciones(tarjeta, texto_tarjeta)
                comentarios = extraer_comentarios(texto_tarjeta)
                compartidos = extraer_compartidos(texto_tarjeta)
                envios = extraer_numero_por_etiqueta(texto_tarjeta, r"(?:envios?|sends?)")

                firma = f"{fecha}-{contenido[:80]}"
                if any(p["firma"] == firma for p in posts):
                    continue
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
                print(f"  Post {len(posts)}/{cantidad} capturado")
            except Exception:
                continue

        if len(posts) >= cantidad:
            break

        # Scroll de carga de mas publicaciones.
        page.mouse.wheel(0, 3500)
        delay(2)
        intentos_scroll += 1

    return posts[:cantidad]


def guardar_resultados_json(
    base_dir: str,
    nombre: str,
    cantidad: int,
    perfil_url: str,
    seguidores: str,
    posts: list,
) -> str:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    nombre_limpio = re.sub(r"[^a-zA-Z0-9_-]+", "_", nombre).strip("_") or "cuenta"
    salida_path = os.path.join(base_dir, f"posts_{nombre_limpio}_{timestamp}.json")

    data = {
        "cuenta_buscada": nombre,
        "perfil_url": perfil_url,
        "seguidores": seguidores,
        "cantidad_solicitada": cantidad,
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


def main():
    nombre = input("Nombre de la cuenta a buscar: ").strip()
    if not nombre:
        print("Debes introducir un nombre.")
        return

    try:
        cantidad = int(input("Numero de posts a sacar: ").strip())
        if cantidad <= 0:
            print("El numero debe ser mayor que 0.")
            return
    except ValueError:
        print("Debes introducir un numero valido.")
        return

    base_dir = os.path.dirname(os.path.abspath(__file__))
    state_path = os.path.join(base_dir, "state.json")
    if not os.path.exists(state_path):
        print(f"No existe el archivo de sesion: {state_path}")
        print("Ejecuta primero login.py para generar state.json.")
        return

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=50)
        context = browser.new_context(
            storage_state=state_path,
            viewport={"width": 1280, "height": 900},
        )
        context.set_default_timeout(90000)
        context.set_default_navigation_timeout(90000)
        page = context.new_page()

        print(f"\nBuscando perfil de: {nombre}")
        perfil_url = abrir_perfil_desde_busqueda(page, nombre)
        if not perfil_url:
            print("No se pudo encontrar el perfil en la busqueda.")
            browser.close()
            return

        actividad_url = perfil_url.rstrip("/") + "/recent-activity/all/"
        print(f"Perfil encontrado: {perfil_url}")
        seguidores = extraer_seguidores_perfil(page)
        print(f"Seguidores detectados: {seguidores}")
        print("Abriendo actividad para extraer publicaciones...")
        page.goto(actividad_url)
        page.wait_for_load_state("domcontentloaded")
        delay(2)

        posts = extraer_posts(page, cantidad)
        browser.close()

    salida_json = guardar_resultados_json(
        base_dir, nombre, cantidad, perfil_url, seguidores, posts
    )
    ok_db, msg_db = guardar_resultados_db(nombre, seguidores, posts)

    print("\n" + "=" * 60)
    print(f"RESULTADOS DE {nombre.upper()}")
    print("=" * 60)
    print(f"Seguidores: {seguidores}")

    if not posts:
        print("No se pudieron extraer publicaciones.")
        return

    for i, post in enumerate(posts, 1):
        print(f"\nPost #{i}")
        print(f"Fecha: {post['fecha']}")
        print(f"Contenido: {post['contenido']}")
        print(f"Recomendaciones: {post['recomendaciones']}")
        print(f"Comentarios: {post['comentarios']}")
        print(f"Compartidos: {post['compartidos']}")

    print("\n" + "=" * 60)
    print(f"JSON guardado automaticamente en: {salida_json}")
    print(msg_db)
    if not ok_db:
        print(
            "Si quieres guardar en BD, revisa credenciales PGHOST/PGPORT/PGDATABASE/PGUSER/PGPASSWORD."
        )


if __name__ == "__main__":
    main()
