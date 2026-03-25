from playwright.sync_api import sync_playwright
import re, time

def delay(s=4): time.sleep(s)

def sacar_textos(page):
    return page.evaluate("""() => {
        const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
        const out = []; let n;
        while (n = walk.nextNode()) {
            const t = n.textContent.trim();
            if (t.length > 0 && t.length < 200) out.push(t);
        }
        return out;
    }""")

def buscar(textos, patrones):
    resultados = []
    vistas = set()
    for texto in textos:
        for patron, etiqueta in patrones:
            if etiqueta in vistas: continue
            m = re.search(patron, texto, re.IGNORECASE)
            if m:
                vistas.add(etiqueta)
                resultados.append((etiqueta, m.group(1)))
    return resultados

def extraer_metricas_post(textos):
    # Patrón real del DOM: etiqueta en índice i, número en índice i+1
    etiquetas = {
        "reacciones":                 "❤️  Reacciones",
        "comentarios":                "💬 Comentarios",
        "veces compartido":           "🔁 Compartidos",
        "veces guardado":             "🔖 Guardados",
        "envíos en linkedin":         "📨 Envíos",
    }
    # Estos van número ANTES, etiqueta DESPUÉS (patrón distinto)
    etiquetas_invertidas = {
        "impresiones":         "📈 Impresiones",
        "miembros alcanzados": "👀 Miembros alcanzados",
    }

    inicio = 0
    for i, t in enumerate(textos):
        if re.search(r'análisis de la publicación|descubrimiento', t, re.IGNORECASE):
            inicio = i
            break

    # Buscar seguidores ganados (patrón: número solo antes de "Interacción social")
    seguidores_ganados = None
    for i in range(inicio, len(textos)):
        if re.search(r'interacción social', textos[i], re.IGNORECASE):
            # El número está en i-1
            if i > 0 and re.fullmatch(r'\d[\d.,]*', textos[i-1]):
                seguidores_ganados = textos[i-1]
            break

    resultado = {}
    if seguidores_ganados:
        resultado["👥 Seguidores ganados"] = seguidores_ganados

    textos_zona = textos[inicio:]

    # Patrón 1: número ANTES → etiqueta DESPUÉS (Impresiones, Miembros alcanzados)
    for i in range(len(textos_zona) - 1):
        if re.fullmatch(r'\d[\d.,]*', textos_zona[i]):
            siguiente = textos_zona[i+1].lower()
            for clave, etiqueta in etiquetas_invertidas.items():
                if clave in siguiente and etiqueta not in resultado:
                    resultado[etiqueta] = textos_zona[i]

    # Patrón 2: etiqueta ANTES → número DESPUÉS (Reacciones, Comentarios, etc.)
    for i in range(len(textos_zona) - 1):
        t_lower = textos_zona[i].lower()
        for clave, etiqueta in etiquetas.items():
            if clave in t_lower and etiqueta not in resultado:
                siguiente = textos_zona[i+1]
                if re.fullmatch(r'\d[\d.,]*', siguiente):
                    resultado[etiqueta] = siguiente

    return resultado


with sync_playwright() as p:
    browser = p.chromium.launch(headless=False, slow_mo=50)
    ctx = browser.new_context(storage_state="state.json", viewport={"width": 1280, "height": 900})
    page = ctx.new_page()

    print("➡️  Perfil...")
    page.goto("https://www.linkedin.com/in/me/")
    delay(5)
    page.mouse.wheel(0, 2000); delay(2)

    metricas = buscar(sacar_textos(page), [
        (r'(\d[\d.,]*)\s*impresiones?',                   "📈 Impresiones totales"),
        (r'(\d[\d.,]*)\s*apariciones?\s*en\s*búsquedas?', "🔍 Apariciones en búsqueda"),
        (r'(\d[\d.,]*)\s*(?:seguidores?|followers?)',      "👥 Seguidores"),
        (r'(\d[\d.,]*)\s*visitas?\s*al\s*perfil',          "👁️  Visitas al perfil"),
        (r'(\d[\d.,]*)\s*conexiones?',                     "🤝 Conexiones"),
    ])

    print("➡️  Buscando publicaciones...")
    page.goto("https://www.linkedin.com/in/me/recent-activity/all/")
    delay(6)
    for _ in range(6): page.mouse.wheel(0, 2000); delay(2)

    links_posts = page.evaluate("""() => {
        const urls = new Set();
        document.querySelectorAll('a[href*="post-summary"]').forEach(a => {
            const href = a.href.split('?')[0];
            if (!href.includes('help')) urls.add(href);
        });
        return [...urls].slice(0, 10);
    }""")

    print(f"   🔎 Posts encontrados: {len(links_posts)}")

    posts_info = []
    for url in links_posts:
        page.goto(url); delay(5)
        page.mouse.wheel(0, 1000); delay(2)
        textos = sacar_textos(page)
        stats = extraer_metricas_post(textos)

        contenido = "Sin texto"
        for i, t in enumerate(textos):
            if re.search(r'ha publicado esto', t, re.IGNORECASE):
                for j in range(i+1, min(i+10, len(textos))):
                    c = textos[j]
                    if len(c) > 40 and not c.startswith("{") and "urn:li" not in c:
                        contenido = c[:80]; break
                break

        posts_info.append({"contenido": contenido, "stats": stats})
        print(f"   ✓ {contenido[:50]}...")

    browser.close()

print("\n" + "="*50)
print("       📊 TUS MÉTRICAS DE LINKEDIN")
print("="*50)

for etiqueta, valor in metricas:
    print(f"{etiqueta}: {valor}")

if posts_info:
    print("\n── Estadísticas por publicación ──")
    for i, post in enumerate(posts_info, 1):
        print(f"\n[{i}] {post['contenido']}...")
        orden = ["📈 Impresiones", "👀 Miembros alcanzados", "👥 Seguidores ganados",
                 "❤️  Reacciones", "💬 Comentarios", "🔁 Compartidos", "🔖 Guardados", "📨 Envíos"]
        for etiqueta in orden:
            if etiqueta in post["stats"]:
                print(f"    {etiqueta}: {post['stats'][etiqueta]}")
else:
    print("\n⚠️  No se encontraron publicaciones.")

print("="*50)
input("\nPulsa ENTER para salir...")