"""
Simula ejecuciones pasadas en metricas_perfil usando SOLO datos ya guardados en BD.

Uso:
  python seed_metricas_historico.py
  python seed_metricas_historico.py --insert
"""

import argparse
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


def obtener_perfiles_con_metricas(cur) -> list[dict]:
    cur.execute(
        """
        SELECT
            p.id,
            p.nombre_usuario,
            m.seguidores,
            m.fecha_captura
        FROM public.perfiles p
        INNER JOIN LATERAL (
            SELECT seguidores, fecha_captura
            FROM public.metricas_perfil
            WHERE perfil_id = p.id
            ORDER BY fecha_captura DESC
            LIMIT 1
        ) m ON true
        ORDER BY p.id
        """
    )
    perfiles = []
    for perfil_id, nombre, seguidores, fecha_captura in cur.fetchall():
        if debe_excluir_perfil(nombre):
            continue
        if seguidores is None or int(seguidores) <= 0:
            continue
        perfiles.append(
            {
                "id": perfil_id,
                "nombre": nombre,
                "ancla_seguidores": int(seguidores),
                "ultima_fecha_real": fecha_captura,
            }
        )
    return perfiles


def generar_serie(ancla: int, num_puntos: int, crecimiento_diario_pct: float, semilla: int) -> list[int]:
    rng = random.Random(semilla)
    if num_puntos <= 1:
        return [ancla]

    factor = (1 + crecimiento_diario_pct / 100) ** (num_puntos - 1)
    inicio = max(int(ancla / factor), int(ancla * 0.88))

    valores = []
    for i in range(num_puntos):
        t = i / max(num_puntos - 1, 1)
        base = inicio + (ancla - inicio) * t
        ruido = rng.uniform(-0.0012, 0.0012) * base
        if rng.random() < 0.07 and i > 0:
            ruido -= rng.uniform(0, 0.0025) * base
        valores.append(max(0, int(base + ruido)))

    valores[-1] = ancla
    return valores


def fechas_ejecucion(dias: int, hora: str, excluir_desde: datetime | None) -> list[datetime]:
    hh, mm = map(int, hora.split(":"))
    hoy = datetime.now().replace(hour=hh, minute=mm, second=0, microsecond=0)
    fechas = [hoy - timedelta(days=d) for d in range(dias, 0, -1)]
    if excluir_desde:
        fechas = [f for f in fechas if f.date() < excluir_desde.date()]
    return fechas


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--insert", action="store_true")
    parser.add_argument("--dias", type=int, default=30)
    parser.add_argument("--hora", default="11:30")
    parser.add_argument(
        "--crecimiento",
        type=float,
        default=0.12,
        help="Porcentaje medio compuesto por dia hacia atras",
    )
    args = parser.parse_args()

    cargar_env()
    conn = conectar()

    try:
        with conn.cursor() as cur:
            perfiles = obtener_perfiles_con_metricas(cur)

            if not perfiles:
                print(
                    "No hay perfiles con metricas validas en BD "
                    "(o solo midudev / seguidores=0). Ejecuta antes un scraping."
                )
                return

            print("\nPerfiles usados (datos reales de BD como ancla):")
            for p in perfiles:
                print(
                    f"  - {p['nombre']}: {p['ancla_seguidores']:,} seguidores "
                    f"(ultima captura {p['ultima_fecha_real']})"
                )
            print("\nExcluidos: nombres con 'midudev' o sin metricas en BD.\n")

            filas = []
            for p in perfiles:
                fechas = fechas_ejecucion(
                    args.dias,
                    args.hora,
                    excluir_desde=p["ultima_fecha_real"],
                )
                if not fechas:
                    print(
                        f"  [omitido] {p['nombre']}: no hay fechas anteriores "
                        "a la ultima metrica."
                    )
                    continue

                serie = generar_serie(
                    ancla=p["ancla_seguidores"],
                    num_puntos=len(fechas),
                    crecimiento_diario_pct=args.crecimiento,
                    semilla=p["id"] * 1000,
                )
                for fecha, seg in zip(fechas, serie):
                    filas.append(
                        {
                            "perfil_id": p["id"],
                            "nombre": p["nombre"],
                            "fecha_captura": fecha,
                            "seguidores": seg,
                        }
                    )

            print("=" * 88)
            print(f"VISTA PREVIA — {len(filas)} filas")
            print("=" * 88)
            for p in perfiles:
                datos = [f for f in filas if f["perfil_id"] == p["id"]]
                if not datos:
                    continue
                primero, ultimo = datos[0]["seguidores"], datos[-1]["seguidores"]
                print(f"\n{p['nombre']} | ancla BD: {p['ancla_seguidores']:,}")
                print(f"  Simulado: {primero:,} -> {ultimo:,} ({len(datos)} puntos)")
                for d in datos[:: max(1, len(datos) // 6)]:
                    print(
                        f"    {d['fecha_captura'].strftime('%Y-%m-%d %H:%M')}  "
                        f"{d['seguidores']:>10,}"
                    )
                d = datos[-1]
                print(
                    f"    {d['fecha_captura'].strftime('%Y-%m-%d %H:%M')}  "
                    f"{d['seguidores']:>10,}"
                )

            if not args.insert:
                print("\nPREVIEW solamente. Para insertar: --insert")
                return

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
            print(f"\nInsertadas {len(filas)} filas.")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
