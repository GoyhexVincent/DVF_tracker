-- ═══════════════════════════════════════════════════════════════════════════
--  DVF Tracker — schéma de base de données
--  Base   : marine_database (partagée avec NDBC_tracker)
--  Schéma : dvf
--  Rôle propriétaire : pi
--
--  À exécuter une fois, en tant que propriétaire du schéma :
--      psql -U pi -d marine_database -f dvf_schema.sql
--
--  Le script est IDEMPOTENT : il peut être rejoué sans perte de données.
--  DVF_data.py crée de toute façon ces objets automatiquement à chaque import
--  (init_db) ; ce fichier sert à préparer la base à la main, à la documenter
--  et à rejouer les GRANT.
-- ═══════════════════════════════════════════════════════════════════════════

CREATE SCHEMA IF NOT EXISTS dvf;

-- Le schéma est dédié au projet ; on évite de le laisser accessible à tous.
-- Bloc tolérant aux erreurs : si le script est lancé par un rôle qui n'est pas
-- propriétaire du schéma (ou si le rôle « pi » n'existe pas encore), on se
-- contente d'un avertissement plutôt que d'interrompre la création des tables.
DO $$
BEGIN
    EXECUTE 'REVOKE ALL ON SCHEMA dvf FROM PUBLIC';
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pi') THEN
        EXECUTE 'GRANT USAGE, CREATE ON SCHEMA dvf TO pi';
    END IF;
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'Droits sur le schéma dvf non modifiés : %', SQLERRM;
END $$;

-- ───────────────────────────────────────────────────────────────────────────
--  1. dvf.communes — référentiel des communes suivies
-- ───────────────────────────────────────────────────────────────────────────
--  Sert de table de référence pour les jointures et pour les panels Grafana
--  (« quelle ville suit-on ? »). Une seule commune est suivie aujourd'hui :
--  Saint-Maur-des-Fossés (INSEE 94068, Val-de-Marne). Ajouter une ligne ici
--  + la constante COMMUNES de DVF_data.py suffit à étendre le périmètre.
-- ───────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS dvf.communes (
    code_commune        VARCHAR(5)  PRIMARY KEY,
    nom_commune         TEXT        NOT NULL,
    code_departement    VARCHAR(3)  NOT NULL,
    codes_postaux       TEXT[],
    latitude            DOUBLE PRECISION,
    longitude           DOUBLE PRECISION,
    population          INTEGER,
    source_url          TEXT,
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

COMMENT ON TABLE  dvf.communes IS
    'Référentiel des communes suivies par le pipeline DVF.';
COMMENT ON COLUMN dvf.communes.code_commune IS
    'Code INSEE de la commune (5 caractères), clé de jointure avec les fichiers DVF.';
COMMENT ON COLUMN dvf.communes.codes_postaux IS
    'Tous les codes postaux rattachés à la commune (une commune peut en avoir plusieurs).';

-- ───────────────────────────────────────────────────────────────────────────
--  2. dvf.lignes_mutation — données DVF brutes (fidélité maximale)
-- ───────────────────────────────────────────────────────────────────────────
--  Une ligne = une ligne du fichier « DVF géolocalisé » (geo-dvf, Etalab) :
--  un local (appartement, maison, dépendance, local commercial…) ou une
--  parcelle de terrain, rattaché à une mutation (id_mutation).
--
--  ⚠️  Une même mutation (un acte notarié) occupe PLUSIEURS lignes : la
--      « valeur foncière » est répétée à l'identique sur chacune d'elles.
--      Ne jamais sommer cette colonne sur cette table — c'est le piège
--      classique du jeu de données DVF. Pour les analyses, utiliser
--      dvf.transactions (une ligne = un acte).
--
--  Partitionnement TimescaleDB sur date_mutation (chunk = 1 an) : le
--  remplacement d'un millésime se fait par un DELETE borné dans le temps,
--  qui ne touche qu'un seul chunk.
-- ───────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS dvf.lignes_mutation (
    ligne_id                    BIGSERIAL,
    id_mutation                 TEXT        NOT NULL,
    date_mutation               DATE        NOT NULL,
    numero_disposition          TEXT,
    nature_mutation             TEXT,
    valeur_fonciere             NUMERIC(14,2),

    -- Adresse déclarative (celle du local, pas de la parcelle)
    adresse_numero              TEXT,
    adresse_suffixe             TEXT,
    adresse_nom_voie            TEXT,
    adresse_code_voie           TEXT,
    code_postal                 VARCHAR(5),

    -- Localisation administrative
    code_commune                VARCHAR(5)  NOT NULL,
    nom_commune                 TEXT,
    code_departement            VARCHAR(3),
    ancien_code_commune         TEXT,
    ancien_nom_commune          TEXT,

    -- Parcelle cadastrale
    id_parcelle                 TEXT,
    ancien_id_parcelle          TEXT,
    numero_volume               TEXT,

    -- Lots de copropriété (données Carrez, souvent partielles)
    lot1_numero                 TEXT,
    lot1_surface_carrez         NUMERIC(10,2),
    lot2_numero                 TEXT,
    lot2_surface_carrez         NUMERIC(10,2),
    lot3_numero                 TEXT,
    lot3_surface_carrez         NUMERIC(10,2),
    lot4_numero                 TEXT,
    lot4_surface_carrez         NUMERIC(10,2),
    lot5_numero                 TEXT,
    lot5_surface_carrez         NUMERIC(10,2),
    nombre_lots                 SMALLINT,

    -- Description du local
    code_type_local             VARCHAR(2),
    type_local                  TEXT,
    surface_reelle_bati         NUMERIC(10,2),
    nombre_pieces_principales   SMALLINT,

    -- Terrains (lignes « nature de culture » : pas de bâti)
    code_nature_culture         TEXT,
    nature_culture              TEXT,
    code_nature_culture_speciale TEXT,
    nature_culture_speciale     TEXT,
    surface_terrain             NUMERIC(12,2),

    -- Géolocalisation (parcelle, fournie par geo-dvf)
    longitude                   DOUBLE PRECISION,
    latitude                    DOUBLE PRECISION,

    -- Traçabilité
    source_annee                SMALLINT    NOT NULL,
    imported_at                 TIMESTAMPTZ DEFAULT NOW(),

    -- TimescaleDB impose que la clé primaire contienne la colonne de
    -- partitionnement. ligne_id (séquence) reste unique, la PK garantit
    -- l'unicité en mode PostgreSQL standard comme en hypertable.
    PRIMARY KEY (ligne_id, date_mutation)
);

CREATE INDEX IF NOT EXISTS idx_dvf_lignes_commune_date
    ON dvf.lignes_mutation (code_commune, date_mutation DESC);
CREATE INDEX IF NOT EXISTS idx_dvf_lignes_mutation
    ON dvf.lignes_mutation (id_mutation);
CREATE INDEX IF NOT EXISTS idx_dvf_lignes_type_date
    ON dvf.lignes_mutation (type_local, date_mutation DESC);

COMMENT ON TABLE dvf.lignes_mutation IS
    'Lignes brutes DVF géolocalisées (une ligne = un local/parcelle d''une mutation).';

-- ───────────────────────────────────────────────────────────────────────────
--  3. dvf.transactions — une ligne par acte, prêt pour l'analyse
-- ───────────────────────────────────────────────────────────────────────────
--  Construite par DVF_data.py à partir de dvf.lignes_mutation.
--
--  type_bien :
--    'Appartement' / 'Maison' → l'acte porte sur UN SEUL logement
--                               (dépendances — cave, garage, parking —
--                               autorisées : elles font partie du lot vendu) ;
--    'Autre'                  → pas de logement, plusieurs logements, ou
--                               mélange logement + local commercial.
--                               Conservé pour le comptage, exclu des prix.
--
--  prix_m2_fiable : FALSE si le prix au m² n'a pas de sens économique
--  (vente d'une quote-part, lot isolé, surface aberrante, prix symbolique).
--  Les séries de prix n'utilisent QUE les lignes fiables ; les volumes
--  comptent TOUTES les transactions.
-- ───────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS dvf.transactions (
    id_mutation             TEXT        NOT NULL,
    date_mutation           DATE        NOT NULL,
    annee                   SMALLINT    NOT NULL,
    mois                    SMALLINT    NOT NULL,
    nature_mutation         TEXT        NOT NULL,
    marche                  TEXT        NOT NULL,   -- 'ancien' | 'neuf' (VEFA)
    type_bien               TEXT        NOT NULL,   -- 'Appartement' | 'Maison' | 'Autre'

    valeur_fonciere         NUMERIC(14,2),
    surface_bati            NUMERIC(10,2),
    surface_terrain         NUMERIC(12,2),
    surface_dependances     NUMERIC(10,2),
    nb_locaux               SMALLINT,
    nb_dependances          SMALLINT,
    nb_pieces               SMALLINT,

    prix_m2                 NUMERIC(10,2),
    prix_m2_fiable          BOOLEAN     NOT NULL DEFAULT FALSE,
    motif_non_fiable        TEXT,

    adresse                 TEXT,
    code_postal             VARCHAR(5),
    code_commune            VARCHAR(5)  NOT NULL,
    longitude               DOUBLE PRECISION,
    latitude                DOUBLE PRECISION,

    -- Millésime du fichier source dont la ligne est issue. Sert de clé de
    -- remplacement : chaque import supprime le millésime entier avant de le
    -- réinsérer (les fichiers DVF sont des instantanés annuels complets, ils
    -- peuvent donc contenir des corrections ou des suppressions).
    source_annee            SMALLINT    NOT NULL,
    imported_at             TIMESTAMPTZ DEFAULT NOW(),

    PRIMARY KEY (id_mutation, date_mutation)
);

CREATE INDEX IF NOT EXISTS idx_dvf_tx_commune_date
    ON dvf.transactions (code_commune, date_mutation DESC);
CREATE INDEX IF NOT EXISTS idx_dvf_tx_type_date
    ON dvf.transactions (type_bien, date_mutation DESC)
    WHERE prix_m2_fiable;
CREATE INDEX IF NOT EXISTS idx_dvf_tx_voie
    ON dvf.transactions (adresse, date_mutation DESC);

COMMENT ON TABLE dvf.transactions IS
    'Une ligne par acte DVF (vente), avec prix au m² et indicateur de fiabilité.';
COMMENT ON COLUMN dvf.transactions.prix_m2_fiable IS
    'FALSE = prix au m² non exploitable (quote-part, lot isolé, surface/prix aberrants).';

-- ───────────────────────────────────────────────────────────────────────────
--  4. Conversion en hypertables TimescaleDB (si l'extension est présente)
-- ───────────────────────────────────────────────────────────────────────────
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
        RAISE NOTICE 'TimescaleDB absent : les tables restent des tables PostgreSQL standard.';
        RETURN;
    END IF;

    PERFORM create_hypertable('dvf.lignes_mutation', 'date_mutation',
                              chunk_time_interval => INTERVAL '1 year',
                              if_not_exists => TRUE);
    PERFORM create_hypertable('dvf.transactions', 'date_mutation',
                              chunk_time_interval => INTERVAL '1 year',
                              if_not_exists => TRUE);
    RAISE NOTICE 'Hypertables dvf.lignes_mutation et dvf.transactions prêtes (chunk = 1 an).';
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'Création des hypertables ignorée : %', SQLERRM;
END $$;

-- ───────────────────────────────────────────────────────────────────────────
--  5. dvf.marche_mensuel — agrégats mensuels + tendance du marché
-- ───────────────────────────────────────────────────────────────────────────
--  Vue (et non table matérialisée) : le volume est faible (≈ 21 000 lignes
--  brutes, ≈ 6 300 transactions exploitables sur 2021-2025), le recalcul est
--  instantané et la vue ne peut donc jamais être périmée après un import.
--
--  Lecture des indicateurs :
--    * prix_m2_median          médiane des prix au m² du mois (robuste aux
--                              valeurs extrêmes, contrairement à la moyenne) ;
--    * prix_m2_median_3m       moyenne des 3 dernières médianes mensuelles
--                              (lissage : un mois creux ne fait pas la tendance) ;
--    * prix_m2_median_12m      moyenne des 12 dernières médianes mensuelles ;
--    * momentum_pct            3m vs 12m — le « thermomètre » de court terme ;
--    * variation_yoy_pct       3m vs les mêmes 3 mois un an plus tôt ;
--    * tendance                classification : un momentum supérieur à
--                              +2 % = marché qui accélère, inférieur à
--                              -2 % = marché qui ralentit, entre les deux
--                              = stabilité. Le seuil de 2 % correspond à
--                              l'écart-type observé du momentum mensuel
--                              (≈ 2,4 % sur les appartements, 3,4 % sur les
--                              maisons) : en deçà, la variation n'est pas
--                              distinguable du bruit d'échantillonnage.
--
--  La grille mensuelle est complète (generate_series) : les mois sans vente
--  apparaissent avec nb_ventes = 0, ce qui évite de fausser les moyennes
--  glissantes par simple suppression des lignes vides.
-- ───────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW dvf.marche_mensuel AS
WITH habitat AS (
    SELECT date_trunc('month', date_mutation)::date AS mois,
           type_bien,
           prix_m2,
           prix_m2_fiable,
           valeur_fonciere,
           surface_bati,
           marche
    FROM dvf.transactions
    WHERE type_bien IN ('Appartement', 'Maison')
),
-- Agrégat par (mois, type) puis par (mois, 'Global') empilé verticalement.
agg AS (
    SELECT mois,
           type_bien,
           count(*)                                              AS nb_ventes,
           count(*) FILTER (WHERE prix_m2_fiable)                AS nb_ventes_prix,
           count(*) FILTER (WHERE marche = 'neuf')               AS nb_ventes_neuf,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY prix_m2)
               FILTER (WHERE prix_m2_fiable)                     AS prix_m2_median,
           percentile_cont(0.25) WITHIN GROUP (ORDER BY prix_m2)
               FILTER (WHERE prix_m2_fiable)                     AS prix_m2_p25,
           percentile_cont(0.75) WITHIN GROUP (ORDER BY prix_m2)
               FILTER (WHERE prix_m2_fiable)                     AS prix_m2_p75,
           avg(prix_m2) FILTER (WHERE prix_m2_fiable)            AS prix_m2_moyen,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY valeur_fonciere)
               FILTER (WHERE prix_m2_fiable)                     AS prix_median,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY surface_bati)
               FILTER (WHERE prix_m2_fiable)                     AS surface_mediane
    FROM habitat
    GROUP BY mois, type_bien

    UNION ALL

    SELECT mois,
           'Global',
           count(*),
           count(*) FILTER (WHERE prix_m2_fiable),
           count(*) FILTER (WHERE marche = 'neuf'),
           percentile_cont(0.5) WITHIN GROUP (ORDER BY prix_m2)
               FILTER (WHERE prix_m2_fiable),
           percentile_cont(0.25) WITHIN GROUP (ORDER BY prix_m2)
               FILTER (WHERE prix_m2_fiable),
           percentile_cont(0.75) WITHIN GROUP (ORDER BY prix_m2)
               FILTER (WHERE prix_m2_fiable),
           avg(prix_m2) FILTER (WHERE prix_m2_fiable),
           percentile_cont(0.5) WITHIN GROUP (ORDER BY valeur_fonciere)
               FILTER (WHERE prix_m2_fiable),
           percentile_cont(0.5) WITHIN GROUP (ORDER BY surface_bati)
               FILTER (WHERE prix_m2_fiable)
    FROM habitat
    GROUP BY mois
),
grille AS (
    SELECT g.mois::date AS mois, t.type_bien
    FROM (SELECT generate_series(min(mois), max(mois), INTERVAL '1 month') AS mois
          FROM agg) g
    CROSS JOIN (VALUES ('Appartement'), ('Maison'), ('Global')) AS t(type_bien)
),
serie AS (
    SELECT gr.mois,
           gr.type_bien,
           COALESCE(a.nb_ventes, 0)          AS nb_ventes,
           COALESCE(a.nb_ventes_prix, 0)     AS nb_ventes_prix,
           COALESCE(a.nb_ventes_neuf, 0)     AS nb_ventes_neuf,
           a.prix_m2_median,
           a.prix_m2_p25,
           a.prix_m2_p75,
           a.prix_m2_moyen,
           a.prix_median,
           a.surface_mediane
    FROM grille gr
    LEFT JOIN agg a ON a.mois = gr.mois AND a.type_bien = gr.type_bien
),
lisse AS (
    SELECT s.*,
           avg(prix_m2_median) OVER w3                       AS prix_m2_median_3m,
           avg(prix_m2_median) OVER w12                      AS prix_m2_median_12m,
           sum(nb_ventes)      OVER w12                      AS nb_ventes_12m,
           count(prix_m2_median) OVER w12                    AS mois_avec_prix_12m,
           row_number() OVER (PARTITION BY type_bien ORDER BY mois) AS rang
    FROM serie s
    WINDOW
        w3  AS (PARTITION BY type_bien ORDER BY mois
                ROWS BETWEEN 2 PRECEDING AND CURRENT ROW),
        w12 AS (PARTITION BY type_bien ORDER BY mois
                ROWS BETWEEN 11 PRECEDING AND CURRENT ROW)
)
SELECT
    mois,
    type_bien,
    nb_ventes,
    nb_ventes_prix,
    nb_ventes_neuf,
    round(prix_m2_median)::int                                  AS prix_m2_median,
    round(prix_m2_p25)::int                                     AS prix_m2_p25,
    round(prix_m2_p75)::int                                     AS prix_m2_p75,
    round(prix_m2_moyen)::int                                   AS prix_m2_moyen,
    round(prix_m2_median_3m)::int                               AS prix_m2_median_3m,
    round(prix_m2_median_12m)::int                              AS prix_m2_median_12m,
    round(prix_median)::int                                     AS prix_median,
    round(surface_mediane)::int                                 AS surface_mediane,
    nb_ventes_12m,
    round((100.0 * (prix_m2_median_3m / NULLIF(prix_m2_median_12m, 0) - 1))::numeric, 2)
                                                                AS momentum_pct,
    round((100.0 * (prix_m2_median_3m
                    / NULLIF(lag(prix_m2_median_3m, 12) OVER (PARTITION BY type_bien ORDER BY mois), 0)
                    - 1))::numeric, 2)                          AS variation_yoy_pct,
    round((100.0 * (nb_ventes_12m
                    / NULLIF(lag(nb_ventes_12m, 12) OVER (PARTITION BY type_bien ORDER BY mois), 0)
                    - 1))::numeric, 2)                          AS volume_yoy_pct,
    CASE
        WHEN rang < 12 OR prix_m2_median_12m IS NULL THEN NULL
        WHEN prix_m2_median_3m > prix_m2_median_12m * 1.02 THEN 'Accélération'
        WHEN prix_m2_median_3m < prix_m2_median_12m * 0.98 THEN 'Ralentissement'
        ELSE 'Stabilité'
    END                                                         AS tendance
FROM lisse
ORDER BY type_bien, mois;

COMMENT ON VIEW dvf.marche_mensuel IS
    'Agrégats mensuels DVF par type de bien (Appartement/Maison/Global) : prix médian au m², '
    'dispersion, volumes, momentum 3 mois vs 12 mois et classification de la tendance.';

-- ───────────────────────────────────────────────────────────────────────────
--  6. dvf.prix_par_voie — classement des rues (panel Grafana « Top rues »)
-- ───────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW dvf.prix_par_voie AS
SELECT code_postal,
       adresse                              AS voie,
       type_bien,
       count(*)                             AS nb_ventes,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY prix_m2))::int AS prix_m2_median,
       round(percentile_cont(0.25) WITHIN GROUP (ORDER BY prix_m2))::int AS prix_m2_p25,
       round(percentile_cont(0.75) WITHIN GROUP (ORDER BY prix_m2))::int AS prix_m2_p75,
       min(date_mutation)                   AS premiere_vente,
       max(date_mutation)                   AS derniere_vente
FROM dvf.transactions
WHERE prix_m2_fiable
  AND adresse IS NOT NULL
GROUP BY code_postal, adresse, type_bien;

COMMENT ON VIEW dvf.prix_par_voie IS
    'Prix médian au m² par voie (adresse) et par type de bien, pour les transactions fiables.';

-- ───────────────────────────────────────────────────────────────────────────
--  7. Droits de lecture pour le rôle Grafana
-- ───────────────────────────────────────────────────────────────────────────
--  Mêmes principes que pour NDBC (voir grafana_setup.md) :
--    1. droits immédiats sur les objets existants ;
--    2. privilèges par défaut pour les objets créés plus tard ;
--    3. recharger la datasource dans Grafana.
--  Sans le point 2, toute table créée après ce script provoque l'erreur
--  « permission denied for table … (SQLSTATE 42501) ».
-- ───────────────────────────────────────────────────────────────────────────
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grafana') THEN
        RAISE NOTICE 'Rôle "grafana" introuvable — bloc GRANT ignoré (voir grafana_setup.md §2).';
        RETURN;
    END IF;

    EXECUTE 'GRANT USAGE ON SCHEMA dvf TO grafana';
    EXECUTE 'GRANT SELECT ON ALL TABLES IN SCHEMA dvf TO grafana';
    RAISE NOTICE 'GRANT SELECT accordé à "grafana" sur le schéma dvf.';
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'GRANT immédiat ignoré : %', SQLERRM;
END $$;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grafana')
       AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pi') THEN
        EXECUTE 'ALTER DEFAULT PRIVILEGES FOR ROLE pi IN SCHEMA dvf GRANT SELECT ON TABLES TO grafana';
        RAISE NOTICE 'Privilèges par défaut posés pour les futurs objets de "pi" dans dvf.';
    ELSE
        RAISE NOTICE 'Rôles "grafana" et/ou "pi" absents — privilèges par défaut NON modifiés.';
    END IF;
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'Privilèges par défaut ignorés : %', SQLERRM;
END $$;
