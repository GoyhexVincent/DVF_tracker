#!/usr/bin/env python3
"""
DVF Tracker — import des « Demandes de valeurs foncières » dans PostgreSQL.

Source : DVF géolocalisées (geo-dvf, Etalab), dérivé open data des fichiers
         DVF de la DGFiP — https://www.data.gouv.fr/datasets/demandes-de-valeurs-foncieres-geolocalisees
         Un CSV par commune et par millésime :
         https://files.data.gouv.fr/geo-dvf/latest/csv/{annee}/communes/{dept}/{insee}.csv

Périmètre : Saint-Maur-des-Fossés (INSEE 94068, Val-de-Marne), millésimes
            2021 → année courante (le dossier « latest » ne remonte pas avant
            2021 ; DVF_START_YEAR permet de changer la borne).

Deux tables alimentées, dans le schéma `dvf` de la base `marine_database` :
  * dvf.lignes_mutation : copie fidèle du fichier source (1 ligne = 1 local
    ou 1 parcelle rattaché à un acte) ;
  * dvf.transactions    : 1 ligne = 1 acte, avec prix au m² et indicateur de
    fiabilité, construit à partir des lignes.

Stratégie d'import — REMPLACEMENT PAR MILLÉSIME :
  Les fichiers DVF sont des instantanés annuels complets, qui peuvent être
  corrigés d'une publication à l'autre (mutations rectifiées, supprimées).
  Un simple ON CONFLICT DO NOTHING laisserait donc des lignes fantômes.
  Chaque millésime est supprimé puis réinséré dans une seule transaction :
  la base est à tout instant le reflet exact de la source.

Garde-fou : si MIN_IMPORT_INTERVAL_HOURS > 0, le script s'arrête sans rien
  insérer lorsqu'une importation a déjà eu lieu il y a moins de N heures.
  Le Jenkinsfile positionne cette variable à 600 h (≈ 25 jours) pour garantir
  une seule importation par mois. FORCE_IMPORT=1 outrepasse ce garde-fou.

Le script est autonome : il crée le schéma, les tables, les index, la vue
  dvf.marche_mensuel et les droits Grafana en exécutant dvf_schema.sql.
"""

from __future__ import annotations

import csv
import io
import logging
import os
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import psycopg2
from psycopg2.extras import execute_values

# ───────────────────────────── CONFIG ─────────────────────────────
# Communes suivies. Une entrée = une commune = un fichier CSV par millésime.
# Pour étendre le périmètre : ajouter une entrée ici, puis relancer.
COMMUNES = [
    {
        "code_insee": "94068",
        "nom": "Saint-Maur-des-Fossés",
        "code_departement": "94",
        # La commune compte deux bureaux distributeurs : 94100 (Saint-Maur)
        # et 94210 (La Varenne-Saint-Hilaire). Les deux sont couverts par le
        # même fichier INSEE 94068.
        "codes_postaux": ("94100", "94210"),
        "latitude": 48.7994,
        "longitude": 2.4997,
        "population": 76572,        # population légale 2023 (INSEE)
    },
]

# `latest` est un alias stable : les millésimes publiés y apparaissent au fur
# et à mesure (dossier 2021 → 2025 aujourd'hui). Les années absentes
# renvoient une 404, simplement ignorée.
DVF_BASE_URL = (
    "https://files.data.gouv.fr/geo-dvf/latest/csv/"
    "{annee}/communes/{departement}/{insee}.csv"
)

SOURCE_LABEL = "geo-dvf (Etalab) — https://files.data.gouv.fr/geo-dvf/latest/csv/"
USER_AGENT = "DVF-Tracker/1.0 (+https://github.com/GoyhexVincent/DVF_tracker)"

# Bornes de plausibilité du prix au m² (€/m²). En dehors, la transaction est
# conservée en base mais marquée non fiable : il s'agit presque toujours d'une
# quote-part vendue, d'un lot isolé (cave, parking) mal typé, ou d'une
# libéralité déguisée. Ces bornes ne servent qu'au calcul des prix.
PRIX_M2_MIN_DEFAUT = 500.0
PRIX_M2_MAX_DEFAUT = 30000.0
# Surface minimale d'un logement (loi Carrez : un lot de moins de 9 m² n'est
# pas un logement autonome). En dessous, le prix au m² n'a pas de sens.
SURFACE_MIN_DEFAUT = 9.0

# Millésime le plus ancien disponible dans le dossier `latest` de geo-dvf.
START_YEAR_DEFAUT = 2021

# Natures de mutation conservées comme « ventes » dans dvf.transactions.
# Adjudication et Échange restent visibles dans dvf.lignes_mutation seulement.
NATURES_VENTE = (
    "Vente",
    "Vente en l'état futur d'achèvement",
    "Vente terrain à bâtir",
)

TYPES_HABITATION = ("Appartement", "Maison")

FETCH_TIMEOUT = 120         # secondes
FETCH_RETRIES = 3

SCHEMA_FILE_DEFAUT = Path(__file__).resolve().parent / "dvf_schema.sql"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("dvf")


# ───────────────────────────── OPTIONS ─────────────────────────────
def _env_float(name: str, defaut: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return defaut
    try:
        return float(raw)
    except ValueError:
        raise SystemExit(f"ERROR : {name} doit être un nombre (reçu : {raw!r}).")


def load_options() -> dict:
    """Options d'exécution, lues dans l'environnement (testable sans env)."""
    return {
        "start_year": int(_env_float("DVF_START_YEAR", START_YEAR_DEFAUT)),
        "min_import_interval_hours": _env_float("MIN_IMPORT_INTERVAL_HOURS", 0.0),
        "force_import": os.getenv("FORCE_IMPORT", "").strip().lower()
        in ("1", "true", "yes", "oui"),
        "prix_m2_min": _env_float("DVF_PRIX_M2_MIN", PRIX_M2_MIN_DEFAUT),
        "prix_m2_max": _env_float("DVF_PRIX_M2_MAX", PRIX_M2_MAX_DEFAUT),
        "surface_min": _env_float("DVF_SURFACE_MIN", SURFACE_MIN_DEFAUT),
        "schema_file": Path(os.getenv("DVF_SCHEMA_FILE") or SCHEMA_FILE_DEFAUT),
    }


def db_config() -> dict:
    return {
        "host": os.getenv("PGHOST", "localhost"),
        "port": os.getenv("PGPORT", "5432"),
        "dbname": os.getenv("PGDATABASE", "marine_database"),
        "user": os.getenv("PGUSER", "pi"),
        "password": os.getenv("PGPASSWORD"),
    }


# ───────────────────────────── CONVERSIONS ─────────────────────────────
def to_float(value):
    """Chaîne vide / None → None ; « 1 234,5 » ou « 1234.5 » → float."""
    if value is None:
        return None
    value = str(value).strip()
    if not value:
        return None
    try:
        return float(value.replace(" ", "").replace(",", "."))
    except ValueError:
        return None


def to_int(value):
    if value is None:
        return None
    value = str(value).strip()
    if not value:
        return None
    try:
        return int(float(value.replace(" ", "").replace(",", ".")))
    except ValueError:
        return None


def to_str(value):
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def to_date(value):
    if value is None:
        return None
    value = str(value).strip()
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


# ───────────────────────────── FETCH ─────────────────────────────
def source_url(commune: dict, annee: int) -> str:
    return DVF_BASE_URL.format(
        annee=annee,
        departement=commune["code_departement"],
        insee=commune["code_insee"],
    )


def years_to_fetch(start_year: int, reference: date | None = None) -> list[int]:
    """Millésimes à interroger : de start_year à l'année courante + 1.

    La borne +1 évite de rater un dossier publié en avance ; les millésimes
    inexistants renvoient une 404, ignorée silencieusement.
    """
    reference = reference or datetime.now(timezone.utc).date()
    return list(range(start_year, reference.year + 2))


def fetch_csv(url: str) -> str | None:
    """Télécharge un CSV. Renvoie None si la ressource n'existe pas (404)."""
    for tentative in range(1, FETCH_RETRIES + 1):
        request = Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urlopen(request, timeout=FETCH_TIMEOUT) as resp:
                return resp.read().decode("utf-8-sig")
        except HTTPError as exc:
            if exc.code == 404:
                return None
            logger.warning("HTTP %s sur %s (tentative %s/%s)",
                           exc.code, url, tentative, FETCH_RETRIES)
        except (URLError, TimeoutError, OSError) as exc:
            logger.warning("Téléchargement impossible (%s) sur %s (tentative %s/%s)",
                           exc, url, tentative, FETCH_RETRIES)
        if tentative < FETCH_RETRIES:
            time.sleep(2 * tentative)
    return None


# ───────────────────────────── PARSE / TRANSFORM ─────────────────────────────
def parse_csv(contenu: str) -> list[dict]:
    """Parse un CSV geo-dvf en liste de dictionnaires (une entrée = une ligne)."""
    reader = csv.DictReader(io.StringIO(contenu))
    lignes = []
    for ligne in reader:
        if to_date(ligne.get("date_mutation")) is None:
            continue
        if not to_str(ligne.get("id_mutation")):
            continue
        lignes.append(ligne)
    return lignes


LIGNES_COLONNES = (
    "id_mutation", "date_mutation", "numero_disposition", "nature_mutation",
    "valeur_fonciere", "adresse_numero", "adresse_suffixe", "adresse_nom_voie",
    "adresse_code_voie", "code_postal", "code_commune", "nom_commune",
    "code_departement", "ancien_code_commune", "ancien_nom_commune",
    "id_parcelle", "ancien_id_parcelle", "numero_volume",
    "lot1_numero", "lot1_surface_carrez", "lot2_numero", "lot2_surface_carrez",
    "lot3_numero", "lot3_surface_carrez", "lot4_numero", "lot4_surface_carrez",
    "lot5_numero", "lot5_surface_carrez", "nombre_lots",
    "code_type_local", "type_local", "surface_reelle_bati",
    "nombre_pieces_principales", "code_nature_culture", "nature_culture",
    "code_nature_culture_speciale", "nature_culture_speciale",
    "surface_terrain", "longitude", "latitude",
)

# Colonnes NUMERIC côté PostgreSQL (les autres colonnes texte acceptent la
# chaîne brute telle quelle).
LIGNES_NUMERIQUES = {
    "valeur_fonciere", "lot1_surface_carrez", "lot2_surface_carrez",
    "lot3_surface_carrez", "lot4_surface_carrez", "lot5_surface_carrez",
    "surface_reelle_bati", "surface_terrain", "longitude", "latitude",
}
LIGNES_ENTIERES = {"nombre_lots", "nombre_pieces_principales"}


def ligne_to_tuple(ligne: dict, code_commune_repli: str, annee: int) -> tuple:
    """Convertit une ligne CSV en tuple prêt pour INSERT (ordre = LIGNES_COLONNES)."""
    valeurs = []
    for colonne in LIGNES_COLONNES:
        brut = ligne.get(colonne)
        if colonne in LIGNES_NUMERIQUES:
            valeurs.append(to_float(brut))
        elif colonne in LIGNES_ENTIERES:
            valeurs.append(to_int(brut))
        elif colonne == "date_mutation":
            valeurs.append(to_date(brut))
        elif colonne == "code_commune":
            valeurs.append(to_str(brut) or code_commune_repli)
        elif colonne == "code_postal":
            valeurs.append(to_str(brut))
        else:
            valeurs.append(to_str(brut))
    valeurs.append(annee)          # source_annee
    return tuple(valeurs)


def group_by_mutation(lignes: list[dict]) -> dict[str, list[dict]]:
    """Regroupe les lignes DVF par identifiant d'acte (id_mutation)."""
    actes: dict[str, list[dict]] = {}
    for ligne in lignes:
        actes.setdefault(ligne["id_mutation"], []).append(ligne)
    return actes


def _format_adresse(ligne: dict) -> str | None:
    numero = to_str(ligne.get("adresse_numero")) or ""
    suffixe = to_str(ligne.get("adresse_suffixe")) or ""
    voie = to_str(ligne.get("adresse_nom_voie")) or ""
    adresse = f"{numero}{suffixe} {voie}".strip()
    return adresse or None


def build_transaction(lignes: list[dict], options: dict,
                      code_commune_repli: str = "") -> dict | None:
    """Construit l'acte (une ligne de dvf.transactions) à partir de ses lignes.

    Renvoie None si la mutation n'est pas une vente (échange, adjudication…)
    ou si sa date est inexploitable.
    """
    entete = lignes[0]
    nature = to_str(entete.get("nature_mutation")) or ""
    if nature not in NATURES_VENTE:
        return None

    date_mutation = to_date(entete.get("date_mutation"))
    if date_mutation is None:
        return None

    id_mutation = to_str(entete.get("id_mutation"))
    if not id_mutation:
        return None

    valeur_fonciere = to_float(entete.get("valeur_fonciere"))
    # Contrôle de cohérence : la valeur foncière est répétée à l'identique sur
    # chaque ligne d'un même acte. Une divergence signale une ligne corrompue.
    for ligne in lignes[1:]:
        if to_float(ligne.get("valeur_fonciere")) != valeur_fonciere:
            logger.debug("Valeur foncière divergente dans la mutation %s", id_mutation)
            valeur_fonciere = None
            break

    habitations = [lg for lg in lignes if to_str(lg.get("type_local")) in TYPES_HABITATION]
    dependances = [lg for lg in lignes if to_str(lg.get("type_local")) == "Dépendance"]
    commerces = [lg for lg in lignes
                 if to_str(lg.get("type_local")) == "Local industriel. commercial ou assimilé"]
    # Une ligne sans type_local mais avec une nature de culture est une
    # parcelle de terrain (jardin, sols…) vendue avec le logement : c'est
    # parfaitement normal et ne doit pas rendre l'acte non fiable.
    indetermines = [
        lg for lg in lignes
        if not to_str(lg.get("type_local"))
        and not to_str(lg.get("nature_culture"))
        and not to_str(lg.get("nature_culture_speciale"))
    ]

    motifs: list[str] = []
    if not habitations:
        motifs.append("aucun logement dans l'acte")
        type_bien = "Autre"
    elif len(habitations) > 1:
        motifs.append("plusieurs logements dans l'acte")
        type_bien = "Autre"
    else:
        type_bien = to_str(habitations[0].get("type_local"))
    if commerces:
        motifs.append("local commercial dans l'acte")
    if indetermines:
        motifs.append("local de type indéterminé dans l'acte")

    surface_bati = None
    surface_dependances = None
    nb_pieces = None
    if habitations:
        surfaces = [to_float(lg.get("surface_reelle_bati")) for lg in habitations]
        surfaces = [s for s in surfaces if s and s > 0]
        surface_bati = round(sum(surfaces), 2) if surfaces else None
        pieces = [to_int(lg.get("nombre_pieces_principales")) for lg in habitations]
        pieces = [p for p in pieces if p]
        nb_pieces = sum(pieces) if pieces else None
    if dependances:
        surfaces = [to_float(lg.get("surface_reelle_bati")) for lg in dependances]
        surfaces = [s for s in surfaces if s and s > 0]
        surface_dependances = round(sum(surfaces), 2) if surfaces else None

    # La surface de terrain est répétée sur chaque ligne d'une même parcelle :
    # on retient le maximum par parcelle avant de sommer les parcelles.
    par_parcelle: dict[str, float] = {}
    for lg in lignes:
        surface = to_float(lg.get("surface_terrain"))
        if not surface or surface <= 0:
            continue
        cle = to_str(lg.get("id_parcelle")) or f"__sans_parcelle_{id(lg)}"
        par_parcelle[cle] = max(par_parcelle.get(cle, 0.0), surface)
    surface_terrain = round(sum(par_parcelle.values()), 2) if par_parcelle else None

    prix_m2 = None
    if valeur_fonciere and surface_bati and surface_bati > 0:
        prix_m2 = round(valeur_fonciere / surface_bati, 2)

    if valeur_fonciere is None or valeur_fonciere <= 0:
        motifs.append("valeur foncière absente ou nulle")
    if surface_bati is None:
        motifs.append("surface bâtie absente")
    elif surface_bati < options["surface_min"]:
        motifs.append(f"surface bâtie < {options['surface_min']:g} m²")
    if prix_m2 is not None:
        if prix_m2 < options["prix_m2_min"]:
            motifs.append(f"prix/m² < {options['prix_m2_min']:g} €")
        elif prix_m2 > options["prix_m2_max"]:
            motifs.append(f"prix/m² > {options['prix_m2_max']:g} €")

    reference = habitations[0] if habitations else entete
    coordonnees = next(
        ((to_float(lg.get("longitude")), to_float(lg.get("latitude")))
         for lg in ([reference] + lignes)
         if to_float(lg.get("longitude")) is not None and to_float(lg.get("latitude")) is not None),
        (None, None),
    )

    return {
        "id_mutation": id_mutation,
        "date_mutation": date_mutation,
        "annee": date_mutation.year,
        "mois": date_mutation.month,
        "nature_mutation": nature,
        "marche": "neuf" if nature.startswith("Vente en l'état futur") else "ancien",
        "type_bien": type_bien,
        "valeur_fonciere": valeur_fonciere,
        "surface_bati": surface_bati,
        "surface_terrain": surface_terrain,
        "surface_dependances": surface_dependances,
        "nb_locaux": sum(1 for lg in lignes if to_str(lg.get("type_local"))),
        "nb_dependances": len(dependances),
        "nb_pieces": nb_pieces,
        "prix_m2": prix_m2,
        "prix_m2_fiable": not motifs,
        "motif_non_fiable": "; ".join(motifs) if motifs else None,
        "adresse": _format_adresse(reference),
        "code_postal": to_str(reference.get("code_postal")),
        "code_commune": to_str(reference.get("code_commune"))
        or to_str(entete.get("code_commune"))
        or code_commune_repli,
        "longitude": coordonnees[0],
        "latitude": coordonnees[1],
    }


def build_transactions(lignes: list[dict], options: dict,
                       code_commune_repli: str = "") -> list[dict]:
    """Construit la liste des actes (ventes) à partir des lignes d'un millésime."""
    transactions = []
    for id_mutation, groupe in group_by_mutation(lignes).items():
        acte = build_transaction(groupe, options, code_commune_repli)
        if acte is not None:
            transactions.append(acte)
    return transactions


TRANSACTIONS_COLONNES = (
    "id_mutation", "date_mutation", "annee", "mois", "nature_mutation",
    "marche", "type_bien", "valeur_fonciere", "surface_bati", "surface_terrain",
    "surface_dependances", "nb_locaux", "nb_dependances", "nb_pieces",
    "prix_m2", "prix_m2_fiable", "motif_non_fiable", "adresse", "code_postal",
    "code_commune", "longitude", "latitude",
)


def transaction_to_tuple(acte: dict, annee: int) -> tuple:
    return tuple(acte[colonne] for colonne in TRANSACTIONS_COLONNES) + (annee,)


# ───────────────────────────── DB ─────────────────────────────
def init_db(conn, schema_file: Path) -> None:
    """Applique dvf_schema.sql (idempotent) : schéma, tables, vue, droits."""
    if not schema_file.exists():
        raise FileNotFoundError(f"Fichier de schéma introuvable : {schema_file}")
    sql = schema_file.read_text(encoding="utf-8")
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()
    logger.info("Schéma dvf prêt (%s).", schema_file.name)


def upsert_communes(conn, communes: list[dict]) -> None:
    with conn.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO dvf.communes (
                code_commune, nom_commune, code_departement, codes_postaux,
                latitude, longitude, population, source_url
            ) VALUES %s
            ON CONFLICT (code_commune) DO UPDATE SET
                nom_commune      = EXCLUDED.nom_commune,
                code_departement = EXCLUDED.code_departement,
                codes_postaux    = EXCLUDED.codes_postaux,
                latitude         = EXCLUDED.latitude,
                longitude        = EXCLUDED.longitude,
                population       = EXCLUDED.population,
                source_url       = EXCLUDED.source_url,
                updated_at       = NOW();
            """,
            [
                (
                    c["code_insee"], c["nom"], c["code_departement"],
                    list(c.get("codes_postaux") or []), c.get("latitude"),
                    c.get("longitude"), c.get("population"), SOURCE_LABEL,
                )
                for c in communes
            ],
        )
    conn.commit()


def hours_since_last_import(conn) -> float | None:
    """Âge (en heures) de la dernière importation, None si la base est vide."""
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(imported_at) FROM dvf.lignes_mutation;")
        row = cur.fetchone()
    if not row or row[0] is None:
        return None
    return (datetime.now(timezone.utc) - row[0]).total_seconds() / 3600.0


def replace_year(conn, commune: dict, annee: int,
                 lignes: list[tuple], transactions: list[tuple]) -> tuple[int, int]:
    """Remplace intégralement un millésime, dans une seule transaction.

    La suppression est volontairement double :

      * `source_annee = annee` retire la version précédente du même fichier,
        où que ses dates tombent (un fichier annuel peut contenir quelques
        actes enregistrés tardivement et datés de l'année précédente) ;
      * la borne temporelle `[annee-01-01, annee+01-01[` retire tout reliquat
        de cette année civile, quelle que soit son origine — c'est ce qui
        évite les doublons si un même acte apparaît dans deux millésimes.

    Renvoie (nb_lignes_insérées, nb_transactions_insérées). En cas d'erreur,
    tout est annulé : la base conserve l'état précédent du millésime.
    """
    code_commune = commune["code_insee"]
    debut, fin = date(annee, 1, 1), date(annee + 1, 1, 1)
    cible = """
        DELETE FROM {table}
        WHERE code_commune = %s
          AND (source_annee = %s OR (date_mutation >= %s AND date_mutation < %s));
    """
    with conn.cursor() as cur:
        cur.execute(cible.format(table="dvf.transactions"),
                    (code_commune, annee, debut, fin))
        cur.execute(cible.format(table="dvf.lignes_mutation"),
                    (code_commune, annee, debut, fin))
        if lignes:
            execute_values(
                cur,
                f"""
                INSERT INTO dvf.lignes_mutation ({", ".join(LIGNES_COLONNES)}, source_annee)
                VALUES %s;
                """,
                lignes,
                page_size=1000,
            )
        if transactions:
            execute_values(
                cur,
                f"""
                INSERT INTO dvf.transactions ({", ".join(TRANSACTIONS_COLONNES)}, source_annee)
                VALUES %s
                ON CONFLICT (id_mutation, date_mutation) DO UPDATE SET
                    annee               = EXCLUDED.annee,
                    mois                = EXCLUDED.mois,
                    nature_mutation     = EXCLUDED.nature_mutation,
                    marche              = EXCLUDED.marche,
                    type_bien           = EXCLUDED.type_bien,
                    valeur_fonciere     = EXCLUDED.valeur_fonciere,
                    surface_bati        = EXCLUDED.surface_bati,
                    surface_terrain     = EXCLUDED.surface_terrain,
                    surface_dependances = EXCLUDED.surface_dependances,
                    nb_locaux           = EXCLUDED.nb_locaux,
                    nb_dependances      = EXCLUDED.nb_dependances,
                    nb_pieces           = EXCLUDED.nb_pieces,
                    prix_m2             = EXCLUDED.prix_m2,
                    prix_m2_fiable      = EXCLUDED.prix_m2_fiable,
                    motif_non_fiable    = EXCLUDED.motif_non_fiable,
                    adresse             = EXCLUDED.adresse,
                    code_postal         = EXCLUDED.code_postal,
                    longitude           = EXCLUDED.longitude,
                    latitude            = EXCLUDED.latitude,
                    source_annee        = EXCLUDED.source_annee,
                    imported_at         = NOW();
                """,
                transactions,
                page_size=1000,
            )
    conn.commit()
    return len(lignes), len(transactions)


# ───────────────────────────── MAIN ─────────────────────────────
def main() -> int:
    options = load_options()
    config = db_config()
    if not config["password"]:
        logger.error("La variable d'environnement PGPASSWORD n'est pas définie.")
        return 1

    conn = psycopg2.connect(**config)
    try:
        init_db(conn, options["schema_file"])
        upsert_communes(conn, COMMUNES)

        if options["min_import_interval_hours"] > 0 and not options["force_import"]:
            age = hours_since_last_import(conn)
            if age is not None and age < options["min_import_interval_hours"]:
                logger.warning(
                    "Import ignoré : la dernière importation date de %.1f h "
                    "(seuil MIN_IMPORT_INTERVAL_HOURS = %.0f h). "
                    "Utiliser FORCE_IMPORT=1 pour passer outre.",
                    age, options["min_import_interval_hours"],
                )
                return 0
            if age is not None:
                logger.info("Dernière importation il y a %.1f h — import autorisé.", age)

        annees = years_to_fetch(options["start_year"])
        logger.info("Millésimes interrogés : %s", ", ".join(str(a) for a in annees))

        total_lignes = 0
        total_actes = 0
        millesimes_ok = 0

        for commune in COMMUNES:
            for annee in annees:
                url = source_url(commune, annee)
                contenu = fetch_csv(url)
                if contenu is None:
                    logger.info("%s — millésime %s indisponible (404), ignoré.",
                                commune["code_insee"], annee)
                    continue

                lignes_brutes = parse_csv(contenu)
                tuples_lignes = [
                    ligne_to_tuple(lg, commune["code_insee"], annee) for lg in lignes_brutes
                ]
                actes = build_transactions(lignes_brutes, options, commune["code_insee"])
                tuples_actes = [transaction_to_tuple(a, annee) for a in actes]

                nb_lignes, nb_actes = replace_year(
                    conn, commune, annee, tuples_lignes, tuples_actes
                )
                total_lignes += nb_lignes
                total_actes += nb_actes
                millesimes_ok += 1
                logger.info(
                    "%s %s : %s lignes brutes, %s actes importés.",
                    commune["code_insee"], annee, nb_lignes, nb_actes,
                )

        if millesimes_ok == 0:
            logger.error("Aucun millésime téléchargé : vérifier l'accès à %s", DVF_BASE_URL)
            return 1

        logger.info(
            "TOTAL : %s millésimes, %s lignes brutes, %s actes.",
            millesimes_ok, total_lignes, total_actes,
        )
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
