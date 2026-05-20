"""
Simula historico de metricas_perfil con curvas distintas y no lineales por cuenta.

- Usa el ultimo seguidores real de cada perfil como ancla (dato actual).
- Borra el resto de metricas (solo deja la fila actual por perfil).
- Inserta ejecuciones simuladas hacia atras con patrones diferentes.

Uso:
  python seed_metricas_historico.py
  python seed_metricas_historico.py --insert
"""

import argparse
import math
import os
import random
import sys
import unicodedata
from datetime import datetime, timedelta

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

try:
    import psycopg
except ImportError:
    print("Instala: pip install psycopg[binary] python-dotenv")
    sys.exit(1)


TIPOS_CURVA = ("acelerado", "estable", "ondulado", "escalones", "volatil", "rebote")


def normalizar(nombre: str) -> str:
    t = (nombre or "").strip().lower()
    t = unicodedata.normalize("NFD", t)
    return "".join(c for c in t if unicodedata.category(c) != "Mn")


def debe_excluir_perfil(nombre: str) -> bool:
    return "midudev" in normalizar(nombre)


def cargar_env():
    base = os.path.dirname(os.path.abspath(__file__))
    if load_dotenv:
        load_dotenv(os.path.join(base, ".env"), override=True)


def conectar():
    password = os.getenv("PGPASSWORD")
    if password is None:
        password = "1234"

    cfg = {
        "host": os.getenv("PGHOST", "localhost"),
        "port": int(os.getenv("PGPORT", "5432")),
        "dbname": os.getenv("PGDATABASE", "linkedin_db"),
        "user": os.getenv("PGUSER", "postgres"),
        "connect_timeout": 10,
    }
    if password != "":
        cfg["password"] = password
    return psycopg.connect(**cfg)


def obtener_perfiles_con_metrica_actual(cur) -> list[dict]:
    cur.execute(
        """
        SELECT
            p.id,
            p.nombre_usuario,
            m.id AS metrica_id,
            m.seguidores,
            m.fecha_captura
        FROM public.perfiles p
        INNER JOIN LATERAL (
            SELECT id, seguidores, fecha_captura
            FROM public.metricas_perfil
            WHERE perfil_id = p.id
            ORDER BY fecha_captura DESC
            LIMIT 1
        ) m ON true
        ORDER BY p.id
        """
    )
    perfiles = []
    for perfil_id, nombre, metrica_id, seguidores, fecha_captura in cur.fetchall():
        if debe_excluir_perfil(nombre):
            continue
        if seguidores is None or int(seguidores) <= 0:
            continue
        perfiles.append(
            {
                "id": perfil_id,
                "nombre": nombre,
                "metrica_id": metrica_id,
                "ancla_seguidores": int(seguidores),
                "fecha_actual": fecha_captura,
            }
        )
    return perfiles


def configuracion_curva(perfil_id: int, nombre: str) -> dict:
    rng = random.Random(perfil_id * 9973 + sum(ord(c) for c in nombre))
    tipo = rng.choice(TIPOS_CURVA)
    return {
        "tipo": tipo,
        "variacion_total": rng.uniform(0.04, 0.14),
        "ruido": rng.uniform(0.003, 0.012),
        "fase": rng.uniform(0, math.pi * 2),
        "rng": rng,
    }


def _suavizar_monotono(valores: list[int], ancla: int) -> list[int]:
    """Evita caidas bruscas salvo en tipo volatil/rebote controlado."""
    out = []
    prev = valores[0]
    for v in valores:
        v = max(0, int(v))
        if out and v < prev * 0.985:
            v = int(prev * 0.985)
        out.append(v)
        prev = v
    out[-1] = max(out[-2] if len(out) > 1 else 0, min(out[-1], ancla - 1))
    return out


def generar_serie_no_lineal(ancla: int, num_puntos: int, config: dict) -> list[int]:
    if num_puntos <= 0:
        return []
    if num_puntos == 1:
        return [max(0, ancla - 1)]

    rng = config["rng"]
    tipo = config["tipo"]
    var_total = config["variacion_total"]
    inicio = max(int(ancla * (1 - var_total)), int(ancla * 0.82))
    rango = ancla - inicio

    progreso = [i / (num_puntos - 1) for i in range(num_puntos)]
    fracciones = []

    for t in progreso:
        if tipo == "acelerado":
            f = t**2.2
        elif tipo == "estable":
            f = t * 0.55 + rng.uniform(-0.02, 0.02)
        elif tipo == "ondulado":
            f = t + 0.08 * math.sin(t * math.pi * 3 + config["fase"])
        elif tipo == "escalones":
            escalon = math.floor(t * 5) / 5
            f = escalon * 0.95 + t * 0.05
        elif tipo == "volatil":
            f = t + rng.uniform(-0.06, 0.06)
        else:  # rebote
            f = t - 0.12 * math.sin(t * math.pi * 2.5 + config["fase"])

        fracciones.append(max(0.0, min(1.05, f)))

    min_f, max_f = min(fracciones), max(fracciones)
    if max_f - min_f < 1e-9:
        fracciones = progreso
    else:
        fracciones = [(f - min_f) / (max_f - min_f) for f in fracciones]

    valores = []
    for i, frac in enumerate(fracciones):
        base = inicio + rango * frac
        ruido = rng.uniform(-config["ruido"], config["ruido"]) * base
        if tipo == "volatil" and rng.random() < 0.12 and i > 0:
            ruido -= rng.uniform(0, 0.02) * base
        valores.append(int(base + ruido))

    if tipo in {"estable", "volatil", "rebote"}:
        valores = _suavizar_monotono(valores, ancla)
    else:
        for i in range(1, len(valores)):
            if valores[i] < valores[i - 1]:
                valores[i] = valores[i - 1]

    valores[-1] = max(valores[-2] if len(valores) > 1 else inicio, ancla - rng.randint(1, max(50, ancla // 1000)))
    return valores


def fechas_hacia_atras(fecha_actual: datetime, dias: int, hora: str) -> list[datetime]:
    hh, mm = map(int, hora.split(":"))
    base = fecha_actual.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return [base - timedelta(days=d) for d in range(dias, 0, -1)]


def borrar_metricas_antiguas(cur, perfiles: list[dict]) -> int:
    total = 0
    for p in perfiles:
        cur.execute(
            """
            DELETE FROM public.metricas_perfil
            WHERE perfil_id = %s AND id <> %s
            """,
            (p["id"], p["metrica_id"]),
        )
        total += cur.rowcount
    return total


def main():
    parser = argparse.ArgumentParser(
        description="Simular crecimiento de seguidores (no lineal, por cuenta)"
    )
    parser.add_argument("--insert", action="store_true", help="Aplicar cambios en BD")
    parser.add_argument("--dias", type=int, default=30, help="Dias simulados hacia atras")
    parser.add_argument("--hora", default="11:30", help="Hora de cada ejecucion simulada")
    args = parser.parse_args()

    cargar_env()
    conn = conectar()

    try:
        with conn.cursor() as cur:
            perfiles = obtener_perfiles_con_metrica_actual(cur)

            if not perfiles:
                print(
                    "No hay perfiles con metrica actual valida. "
                    "Ejecuta un scraping antes."
                )
                return

            print("\nAnclas (dato actual que se CONSERVA):")
            filas = []
            for p in perfiles:
                config = configuracion_curva(p["id"], p["nombre"])
                fechas = fechas_hacia_atras(p["fecha_actual"], args.dias, args.hora)
                serie = generar_serie_no_lineal(
                    p["ancla_seguidores"],
                    len(fechas),
                    config,
                )
                p["tipo_curva"] = config["tipo"]
                for fecha, seg in zip(fechas, serie):
                    filas.append(
                        {
                            "perfil_id": p["id"],
                            "nombre": p["nombre"],
                            "fecha_captura": fecha,
                            "seguidores": seg,
                        }
                    )
                print(
                    f"  {p['nombre']}: {p['ancla_seguidores']:,} @ "
                    f"{p['fecha_actual']} | curva: {config['tipo']}"
                )

            print("\n" + "=" * 88)
            print(f"PREVIEW — borrar historico + insertar {len(filas)} filas simuladas")
            print(f"Se mantienen {len(perfiles)} filas actuales (una por perfil)")
            print("=" * 88)

            for p in perfiles:
                datos = [f for f in filas if f["perfil_id"] == p["id"]]
                if not datos:
                    continue
                primero, ultimo = datos[0]["seguidores"], datos[-1]["seguidores"]
                print(f"\n{p['nombre']} [{p['tipo_curva']}]")
                print(f"  Simulado: {primero:,} -> {ultimo:,} ({len(datos)} puntos)")
                muestra = datos[:: max(1, len(datos) // 7)]
                for d in muestra:
                    print(
                        f"    {d['fecha_captura'].strftime('%Y-%m-%d %H:%M')}  "
                        f"{d['seguidores']:>10,}"
                    )

            if not args.insert:
                print("\nModo preview. Para aplicar: python seed_metricas_historico.py --insert")
                return

            borradas = borrar_metricas_antiguas(cur, perfiles)
            for f in filas:
                cur.execute(
                    """
                    INSERT INTO public.metricas_perfil (
                        perfil_id, fecha_captura, impresiones_totales, seguidores
                    )
                    VALUES (%s, %s, 0, %s)
                    """,
                    (f["perfil_id"], f["fecha_captura"], f["seguidores"]),
                )
            conn.commit()
            print(f"\nBorradas {borradas} metricas antiguas.")
            print(f"Insertadas {len(filas)} filas simuladas.")
            print(f"Conservadas {len(perfiles)} filas actuales.")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
