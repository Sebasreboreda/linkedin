CREATE TABLE metricas_perfil (
    id BIGSERIAL PRIMARY KEY,
    fecha_captura TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    impresiones_totales INTEGER NOT NULL,
    apariciones_busqueda INTEGER NOT NULL,
    seguidores INTEGER NOT NULL
);

CREATE TABLE metricas_publicacion (
    id BIGSERIAL PRIMARY KEY,
    id_publicacion TEXT NOT NULL,
    fecha_captura TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    impresiones INTEGER NOT NULL,
    miembros_alcanzados INTEGER NOT NULL,
    seguidores_ganados INTEGER NOT NULL,
    reacciones INTEGER NOT NULL,
    comentarios INTEGER NOT NULL,
    compartidos INTEGER NOT NULL,
    guardados INTEGER NOT NULL,
    envios INTEGER NOT NULL
);
