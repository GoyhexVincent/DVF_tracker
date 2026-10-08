#!/usr/bin/env python3
"""Tests hors-ligne de l'ETL DVF : aucun accès réseau, aucune base de données.

Lancement :
    python3 -m unittest test_dvf_etl -v
"""

import csv
import io
import json
import re
import sys
import types
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# psycopg2 n'est pas indispensable pour tester la logique pure : on le remplace
# par un bouchon si le paquet n'est pas installé dans l'environnement de test.
try:  # pragma: no cover
    import psycopg2  # noqa: F401
except ImportError:  # pragma: no cover
    _pg = types.ModuleType("psycopg2")
    _pg.Error = Exception
    _pg.OperationalError = Exception
    _extras = types.ModuleType("psycopg2.extras")
    _extras.execute_values = lambda *args, **kwargs: None
    _pg.extras = _extras
    sys.modules["psycopg2"] = _pg
    sys.modules["psycopg2.extras"] = _extras

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import DVF_data as dvf  # noqa: E402
import verify_dvf_freshness as freshness  # noqa: E402

SCHEMA_SQL = (ROOT / "dvf_schema.sql").read_text(encoding="utf-8")
DASHBOARD_JSON = (ROOT / "dashboard_dvf.json").read_text(encoding="utf-8")

OPTIONS = {
    "start_year": 2021,
    "min_import_interval_hours": 0.0,
    "force_import": False,
    "prix_m2_min": 500.0,
    "prix_m2_max": 30000.0,
    "surface_min": 9.0,
}

HEADER = (
    "id_mutation,date_mutation,numero_disposition,nature_mutation,valeur_fonciere,"
    "adresse_numero,adresse_suffixe,adresse_nom_voie,adresse_code_voie,code_postal,"
    "code_commune,nom_commune,code_departement,ancien_code_commune,ancien_nom_commune,"
    "id_parcelle,ancien_id_parcelle,numero_volume,lot1_numero,lot1_surface_carrez,"
    "lot2_numero,lot2_surface_carrez,lot3_numero,lot3_surface_carrez,lot4_numero,"
    "lot4_surface_carrez,lot5_numero,lot5_surface_carrez,nombre_lots,code_type_local,"
    "type_local,surface_reelle_bati,nombre_pieces_principales,code_nature_culture,"
    "nature_culture,code_nature_culture_speciale,nature_culture_speciale,"
    "surface_terrain,longitude,latitude"
)


def table_columns(sql, table):
    """Extrait les noms de colonnes d'un bloc CREATE TABLE IF NOT EXISTS."""
    match = re.search(
        rf"CREATE TABLE IF NOT EXISTS {table}\s*\((.*?)\n\);", sql, re.S
    )
    if not match:
        raise AssertionError(f"CREATE TABLE {table} introuvable dans dvf_schema.sql")
    columns = []
    for line in match.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("--"):
            continue
        if line.upper().startswith(("PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "COMMENT")):
            continue
        for chunk in line.split(","):
            name = re.match(r"\s*([a-z_][a-z0-9_]*)", chunk)
            if name:
                columns.append(name.group(1))
    return columns


def make_row(**overrides):
    """Ligne DVF minimale, complétée par les valeurs par défaut."""
    row = {col: "" for col in dvf.LIGNES_COLONNES}
    row.update(
        {
            "id_mutation": "2025-000001",
            "date_mutation": "2025-03-14",
            "nature_mutation": "Vente",
            "valeur_fonciere": "300000",
            "adresse_numero": "12",
            "adresse_nom_voie": "AV DU BAC",
            "code_postal": "94100",
            "code_commune": "94068",
            "nom_commune": "Saint-Maur-des-Fossés",
            "code_departement": "94",
            "id_parcelle": "94068000AB0001",
            "longitude": "2.4934",
            "latitude": "48.7989",
        }
    )
    row.update(overrides)
    return row


def acte(*lignes, options=None):
    return dvf.build_transaction(list(lignes), options or OPTIONS, "94068")


class TestConversions(unittest.TestCase):
    def test_to_float(self):
        self.assertEqual(dvf.to_float("1234.5"), 1234.5)
        self.assertEqual(dvf.to_float("1 234,5"), 1234.5)
        self.assertIsNone(dvf.to_float(""))
        self.assertIsNone(dvf.to_float(None))
        self.assertIsNone(dvf.to_float("n/a"))

    def test_to_int(self):
        self.assertEqual(dvf.to_int("3"), 3)
        self.assertEqual(dvf.to_int("3.0"), 3)
        self.assertIsNone(dvf.to_int(""))
        self.assertIsNone(dvf.to_int("abc"))

    def test_to_str_strips_and_nullifies(self):
        self.assertEqual(dvf.to_str("  x  "), "x")
        self.assertIsNone(dvf.to_str("   "))

    def test_to_date(self):
        self.assertEqual(dvf.to_date("2025-03-14"), date(2025, 3, 14))
        self.assertIsNone(dvf.to_date("14/03/2025"))
        self.assertIsNone(dvf.to_date(""))


class TestParsing(unittest.TestCase):
    def _csv(self, lignes):
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=HEADER.split(","))
        writer.writeheader()
        for ligne in lignes:
            writer.writerow(ligne)
        return buffer.getvalue()

    def test_parse_csv_roundtrip(self):
        contenu = self._csv([make_row()])
        lignes = dvf.parse_csv(contenu)
        self.assertEqual(len(lignes), 1)
        self.assertEqual(lignes[0]["id_mutation"], "2025-000001")

    def test_parse_csv_ignore_lignes_sans_date_ou_mutation(self):
        contenu = self._csv(
            [
                make_row(),
                make_row(id_mutation="2025-000002", date_mutation=""),
                make_row(id_mutation="", date_mutation="2025-01-05"),
            ]
        )
        self.assertEqual(len(dvf.parse_csv(contenu)), 1)

    def test_ligne_to_tuple_ordre_et_repli_commune(self):
        ligne = make_row(code_commune="", valeur_fonciere="250000,5")
        tup = dvf.ligne_to_tuple(ligne, "94068", 2025)
        self.assertEqual(len(tup), len(dvf.LIGNES_COLONNES) + 1)
        index = dvf.LIGNES_COLONNES.index
        self.assertEqual(tup[index("code_commune")], "94068")
        self.assertEqual(tup[index("valeur_fonciere")], 250000.5)
        self.assertEqual(tup[index("date_mutation")], date(2025, 3, 14))
        self.assertEqual(tup[-1], 2025)          # source_annee

    def test_group_by_mutation(self):
        lignes = [make_row(), make_row(type_local="Dépendance"), make_row(id_mutation="X")]
        groupes = dvf.group_by_mutation(lignes)
        self.assertEqual(sorted(groupes), ["2025-000001", "X"])
        self.assertEqual(len(groupes["2025-000001"]), 2)


class TestBuildTransaction(unittest.TestCase):
    def test_appartement_avec_cave_et_parking(self):
        resultat = acte(
            make_row(type_local="Appartement", code_type_local="2",
                     surface_reelle_bati="62", nombre_pieces_principales="3"),
            make_row(type_local="Dépendance", code_type_local="3",
                     surface_reelle_bati="12"),
            make_row(type_local="Dépendance", code_type_local="3",
                     surface_reelle_bati="0"),
        )
        self.assertEqual(resultat["type_bien"], "Appartement")
        self.assertEqual(resultat["surface_bati"], 62)
        self.assertEqual(resultat["surface_dependances"], 12)
        self.assertEqual(resultat["nb_dependances"], 2)
        self.assertEqual(resultat["nb_pieces"], 3)
        self.assertAlmostEqual(resultat["prix_m2"], round(300000 / 62, 2))
        self.assertTrue(resultat["prix_m2_fiable"])
        self.assertIsNone(resultat["motif_non_fiable"])
        self.assertEqual(resultat["marche"], "ancien")
        self.assertEqual(resultat["adresse"], "12 AV DU BAC")

    def test_vefa_marque_comme_neuf(self):
        resultat = acte(
            make_row(nature_mutation="Vente en l'état futur d'achèvement",
                     type_local="Appartement", surface_reelle_bati="45"),
        )
        self.assertEqual(resultat["marche"], "neuf")
        self.assertTrue(resultat["prix_m2_fiable"])

    def test_maison_avec_parcelle_de_terrain(self):
        """Une ligne de terrain (nature_culture renseignée) ne dégrade pas l'acte."""
        resultat = acte(
            make_row(type_local="Maison", code_type_local="1",
                     surface_reelle_bati="110", surface_terrain="300",
                     nombre_pieces_principales="5"),
            make_row(type_local="", nature_culture="sols", surface_terrain="300"),
        )
        self.assertEqual(resultat["type_bien"], "Maison")
        self.assertEqual(resultat["surface_bati"], 110)
        # La surface de terrain est répétée par parcelle : elle ne doit pas
        # être sommée deux fois.
        self.assertEqual(resultat["surface_terrain"], 300)
        self.assertTrue(resultat["prix_m2_fiable"])

    def test_terrain_reparti_sur_deux_parcelles(self):
        resultat = acte(
            make_row(type_local="Maison", surface_reelle_bati="100",
                     surface_terrain="400", id_parcelle="A"),
            make_row(type_local="", nature_culture="sols",
                     surface_terrain="400", id_parcelle="A"),
            make_row(type_local="", nature_culture="sols",
                     surface_terrain="150", id_parcelle="B"),
        )
        self.assertEqual(resultat["surface_terrain"], 550)

    def test_plaquette_de_bureaux_non_fiable(self):
        resultat = acte(
            make_row(type_local="Local industriel. commercial ou assimilé",
                     surface_reelle_bati="80"),
        )
        self.assertEqual(resultat["type_bien"], "Autre")
        self.assertFalse(resultat["prix_m2_fiable"])
        self.assertIn("aucun logement dans l'acte", resultat["motif_non_fiable"])

    def test_melange_logement_et_commerce_non_fiable(self):
        resultat = acte(
            make_row(type_local="Maison", surface_reelle_bati="120"),
            make_row(type_local="Local industriel. commercial ou assimilé",
                     surface_reelle_bati="60"),
        )
        self.assertEqual(resultat["type_bien"], "Maison")
        self.assertFalse(resultat["prix_m2_fiable"])
        self.assertIn("local commercial dans l'acte", resultat["motif_non_fiable"])

    def test_immeuble_entier_deux_logements_non_fiable(self):
        resultat = acte(
            make_row(type_local="Appartement", surface_reelle_bati="60"),
            make_row(type_local="Appartement", surface_reelle_bati="70"),
        )
        self.assertEqual(resultat["type_bien"], "Autre")
        self.assertFalse(resultat["prix_m2_fiable"])
        self.assertIn("plusieurs logements dans l'acte", resultat["motif_non_fiable"])

    def test_local_de_type_indetermine_non_fiable(self):
        resultat = acte(
            make_row(type_local="Appartement", surface_reelle_bati="60"),
            make_row(type_local="", nature_culture="", nature_culture_speciale=""),
        )
        self.assertFalse(resultat["prix_m2_fiable"])
        self.assertIn("local de type indéterminé", resultat["motif_non_fiable"])

    def test_prix_m2_hors_bornes_non_fiable(self):
        bas = acte(make_row(type_local="Appartement", valeur_fonciere="10000",
                            surface_reelle_bati="80"))
        self.assertFalse(bas["prix_m2_fiable"])
        self.assertIn("prix/m² < 500", bas["motif_non_fiable"])

        haut = acte(make_row(type_local="Appartement", valeur_fonciere="5000000",
                             surface_reelle_bati="80"))
        self.assertFalse(haut["prix_m2_fiable"])
        self.assertIn("prix/m² > 30000", haut["motif_non_fiable"])

    def test_surface_trop_petite_non_fiable(self):
        resultat = acte(make_row(type_local="Appartement", valeur_fonciere="12000",
                                 surface_reelle_bati="5"))
        self.assertFalse(resultat["prix_m2_fiable"])
        self.assertIn("surface bâtie < 9 m²", resultat["motif_non_fiable"])

    def test_valeur_fonciere_absente(self):
        resultat = acte(make_row(type_local="Maison", valeur_fonciere="",
                                 surface_reelle_bati="100"))
        self.assertIsNone(resultat["prix_m2"])
        self.assertFalse(resultat["prix_m2_fiable"])
        self.assertIn("valeur foncière absente", resultat["motif_non_fiable"])

    def test_echec_et_adjudication_exclus(self):
        self.assertIsNone(acte(make_row(nature_mutation="Echange")))
        self.assertIsNone(acte(make_row(nature_mutation="Adjudication")))

    def test_valeur_fonciere_divergente_ecartee(self):
        resultat = acte(
            make_row(type_local="Appartement", surface_reelle_bati="60",
                     valeur_fonciere="300000"),
            make_row(type_local="Dépendance", surface_reelle_bati="10",
                     valeur_fonciere="999999"),
        )
        self.assertIsNone(resultat["valeur_fonciere"])
        self.assertFalse(resultat["prix_m2_fiable"])

    def test_coordonnees_de_repli_sur_la_ligne_habitation(self):
        resultat = acte(
            make_row(type_local="Dépendance", longitude="", latitude=""),
            make_row(type_local="Maison", surface_reelle_bati="100",
                     longitude="2.5000", latitude="48.8000"),
        )
        self.assertEqual(resultat["longitude"], 2.5)
        self.assertEqual(resultat["latitude"], 48.8)

    def test_transaction_to_tuple_longueur(self):
        resultat = acte(make_row(type_local="Maison", surface_reelle_bati="100"))
        tup = dvf.transaction_to_tuple(resultat, 2025)
        self.assertEqual(len(tup), len(dvf.TRANSACTIONS_COLONNES) + 1)
        self.assertEqual(tup[-1], 2025)

    def test_build_transactions_compte_les_ventes(self):
        lignes = [
            make_row(id_mutation="A", type_local="Appartement", surface_reelle_bati="50"),
            make_row(id_mutation="B", type_local="Maison", surface_reelle_bati="90",
                     nature_mutation="Echange"),
            make_row(id_mutation="C", type_local="Maison", surface_reelle_bati="90"),
        ]
        actes = dvf.build_transactions(lignes, OPTIONS, "94068")
        self.assertEqual(sorted(a["id_mutation"] for a in actes), ["A", "C"])


class TestAnneeEtUrl(unittest.TestCase):
    def test_years_to_fetch(self):
        self.assertEqual(
            dvf.years_to_fetch(2021, date(2026, 10, 8)),
            [2021, 2022, 2023, 2024, 2025, 2026, 2027],
        )

    def test_years_to_fetch_start_year_dynamique(self):
        self.assertEqual(dvf.years_to_fetch(2025, date(2026, 1, 1)), [2025, 2026, 2027])

    def test_source_url(self):
        url = dvf.source_url(dvf.COMMUNES[0], 2025)
        self.assertEqual(
            url,
            "https://files.data.gouv.fr/geo-dvf/latest/csv/2025/communes/94/94068.csv",
        )

    def test_commune_suivie(self):
        self.assertEqual(dvf.COMMUNES[0]["code_insee"], "94068")
        self.assertEqual(dvf.COMMUNES[0]["code_departement"], "94")


class TestSchemaEtColonnes(unittest.TestCase):
    """Garde-fou anti-dérive entre dvf_schema.sql, l'ETL et le dashboard."""

    def test_colonnes_lignes_mutation(self):
        colonnes = set(table_columns(SCHEMA_SQL, "dvf.lignes_mutation"))
        attendues = set(dvf.LIGNES_COLONNES) | {"ligne_id", "source_annee", "imported_at"}
        self.assertEqual(colonnes, attendues)

    def test_colonnes_transactions(self):
        colonnes = set(table_columns(SCHEMA_SQL, "dvf.transactions"))
        attendues = set(dvf.TRANSACTIONS_COLONNES) | {"source_annee", "imported_at"}
        self.assertEqual(colonnes, attendues)

    def test_les_deux_tables_ont_une_colonne_de_temps(self):
        for table in ("dvf.lignes_mutation", "dvf.transactions"):
            self.assertIn("date_mutation", table_columns(SCHEMA_SQL, table))

    def test_primaire_inclut_la_colonne_de_partitionnement(self):
        """TimescaleDB exige que toute clé primaire contienne la colonne de temps."""
        for table in ("dvf.lignes_mutation", "dvf.transactions"):
            bloc = re.search(
                rf"CREATE TABLE IF NOT EXISTS {table}\s*\((.*?)\n\);", SCHEMA_SQL, re.S
            ).group(1)
            pk = re.search(r"PRIMARY KEY\s*\(([^)]*)\)", bloc).group(1)
            self.assertIn("date_mutation", pk)

    def test_vues_presentes(self):
        for vue in ("dvf.marche_mensuel", "dvf.prix_par_voie", "dvf.communes"):
            self.assertIn(vue, SCHEMA_SQL)

    def test_seuil_de_tendance_documente(self):
        vue = SCHEMA_SQL.split("CREATE OR REPLACE VIEW dvf.marche_mensuel")[1]
        self.assertIn("'Accélération'", vue)
        self.assertIn("'Ralentissement'", vue)
        self.assertIn("'Stabilité'", vue)


class TestDashboard(unittest.TestCase):
    def setUp(self):
        self.dashboard = json.loads(DASHBOARD_JSON)

    def test_json_valide_et_identite(self):
        self.assertEqual(self.dashboard["uid"], "dvf-saint-maur")
        self.assertIn("DVF", self.dashboard["title"])
        self.assertEqual(self.dashboard["schemaVersion"], 30)

    def test_datasource_template_presente(self):
        noms = [t["name"] for t in self.dashboard["templating"]["list"]]
        self.assertIn("DS_POSTGRESQL", noms)

    def test_panels_demandes_presents(self):
        titres = " | ".join(p.get("title", "") for p in self.dashboard["panels"])
        self.assertIn("Prix au m²", titres)          # évolution des prix
        self.assertIn("Volume", titres)              # volume de ventes
        self.assertIn("Tendance", titres)            # tendance du marché

    def test_panels_utilisent_la_datasource_postgres(self):
        for panel in self.dashboard["panels"]:
            if panel["type"] == "text":
                continue
            self.assertEqual(panel["datasource"]["uid"], "${DS_POSTGRESQL}")

    def test_sql_reference_des_objets_existants(self):
        """Toute table/vue dvf.* citée par un panel doit exister dans le schéma."""
        objets = set(re.findall(r"dvf\.([a-z_]+)", SCHEMA_SQL))
        for panel in self.dashboard["panels"]:
            for target in panel.get("targets", []):
                for reference in re.findall(r"dvf\.([a-z_]+)", target.get("rawSql", "")):
                    self.assertIn(
                        reference,
                        objets,
                        f"Le panel « {panel.get('title')} » cite dvf.{reference}, "
                        "absent de dvf_schema.sql.",
                    )

    def test_sql_utilise_le_filtre_temporel_grafana(self):
        sql = " ".join(
            target.get("rawSql", "")
            for panel in self.dashboard["panels"]
            for target in panel.get("targets", [])
        )
        self.assertIn("$__timeFilter", sql)


class TestFraicheur(unittest.TestCase):
    def test_env_float_defaut(self):
        self.assertEqual(freshness._env_float("VAR_ABSENTE_XYZ", 35), 35)

    def test_env_float_valeur(self):
        import os

        os.environ["VAR_TEST_DVF"] = "12.5"
        try:
            self.assertEqual(freshness._env_float("VAR_TEST_DVF", 35), 12.5)
        finally:
            del os.environ["VAR_TEST_DVF"]


if __name__ == "__main__":
    unittest.main(verbosity=2)
