CREATE TABLE perfiles (
    id BIGSERIAL PRIMARY KEY,
    nombre_usuario TEXT NOT NULL UNIQUE
);

CREATE TABLE metricas_perfil (
    id BIGSERIAL PRIMARY KEY,
    perfil_id BIGINT NOT NULL REFERENCES perfiles(id) ON DELETE CASCADE,
    fecha_captura TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    impresiones_totales INTEGER NOT NULL,
    seguidores INTEGER NOT NULL
);

CREATE TABLE publicaciones (
    id BIGSERIAL PRIMARY KEY,
    perfil_id BIGINT NOT NULL REFERENCES perfiles(id) ON DELETE CASCADE,
    id_publicacion TEXT NOT NULL,
    fecha_publicacion DATE NOT NULL,
    reacciones INTEGER NOT NULL,
    comentarios INTEGER NOT NULL,
    compartidos INTEGER NOT NULL,
    envios INTEGER NOT NULL,
    UNIQUE (perfil_id, id_publicacion)
);
