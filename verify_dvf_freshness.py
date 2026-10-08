#!/usr/bin/env python3
"""DVF Tracker — contrôle de fraîcheur et de cohérence des données en base.

Le pipeline est MENSUEL, mais la source DVF ne bouge que deux fois par an
(publications d'avril et d'octobre) : « fraîcheur » ne veut donc pas dire
« mesure de la veille », comme pour NDBC. Deux âges sont contrôlés :

  * MAX_IMPORT_AGE_DAYS   (défaut 35 j) : la date du dernier import réussi.
    C'est le vrai contrôle de bon fonctionnement du pipeline : au-delà de
    35 jours, le job mensuel n'a pas tourné ou n'a pas pu écrire.

  * MAX_MUTATION_AGE_DAYS (défaut 400 j) : l'âge de la vente la plus récente.
    DVF est diffusé avec un décalage de plusieurs mois (la campagne d'avril
    N publie l'année N-1) ; 400 jours laisse passer un cycle de publication
    complet avant de considérer que la source ne remonte plus.

Le script sort en code 1 dès qu'un contrôle échoue, ce qui fait échouer le
stage Jenkins correspondant.
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime, timezone

import psycopg2


def _env_float(name: str, defaut: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return defaut
    try:
        return float(raw)
    except ValueError:
        print(f"ERROR : {name} doit être un nombre (reçu : {raw!r}).")
        sys.exit(1)


def main() -> int:
    max_import_age = _env_float("MAX_IMPORT_AGE_DAYS", 35)
    max_mutation_age = _env_float("MAX_MUTATION_AGE_DAYS", 400)

    password = os.getenv("PGPASSWORD")
    if not password:
        print("ERROR : La variable d'environnement PGPASSWORD n'est pas définie.")
        return 1

    conn = psycopg2.connect(
        host=os.getenv("PGHOST", "localhost"),
        port=os.getenv("PGPORT", "5432"),
        dbname=os.getenv("PGDATABASE", "marine_database"),
        user=os.getenv("PGUSER", "pi"),
        password=password,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*)                                   AS nb_actes,
                       count(*) FILTER (WHERE prix_m2_fiable)      AS nb_actes_prix,
                       count(DISTINCT type_bien)                   AS nb_types,
                       min(date_mutation)                          AS premiere,
                       max(date_mutation)                          AS derniere,
                       max(imported_at)                            AS dernier_import
                FROM dvf.transactions;
                """
            )
            nb_actes, nb_actes_prix, nb_types, premiere, derniere, dernier_import = cur.fetchone()

            cur.execute("SELECT count(*) FROM dvf.lignes_mutation;")
            nb_lignes = cur.fetchone()[0]

            cur.execute(
                """
                SELECT mois, type_bien, prix_m2_median_3m, momentum_pct, tendance, nb_ventes_12m
                FROM dvf.marche_mensuel
                WHERE mois = (SELECT max(mois) FROM dvf.marche_mensuel)
                ORDER BY type_bien;
                """
            )
            derniers_marches = cur.fetchall()
    finally:
        conn.close()

    if not nb_actes:
        print("ERROR : dvf.transactions est vide — l'import n'a jamais abouti.")
        return 1

    now = datetime.now(timezone.utc)
    echecs = []

    print(f"Lignes brutes (dvf.lignes_mutation) : {nb_lignes}")
    print(f"Actes (dvf.transactions)            : {nb_actes} dont {nb_actes_prix} avec prix fiable")
    print(f"Période couverte                    : {premiere} → {derniere}")
    print(f"Dernier import                      : {dernier_import}")

    if dernier_import is None:
        echecs.append("Aucune date d'import enregistrée (imported_at).")
    else:
        age_import = (now - dernier_import).total_seconds() / 86400.0
        print(f"Âge du dernier import               : {age_import:.1f} j "
              f"(seuil {max_import_age:g} j)")
        if age_import > max_import_age:
            echecs.append(
                f"Dernier import trop ancien : {age_import:.1f} j "
                f"(seuil {max_import_age:g} j)."
            )

    age_mutation = (now.date() - derniere).days
    print(f"Âge de la vente la plus récente     : {age_mutation} j "
          f"(seuil {max_mutation_age:g} j)")
    if age_mutation > max_mutation_age:
        echecs.append(
            f"La source ne remonte plus : dernière vente il y a {age_mutation} j "
            f"(seuil {max_mutation_age:g} j). Vérifier la publication DVF."
        )

    if nb_types < 2:
        echecs.append("Aucune transaction exploitable : ni appartement ni maison.")

    print()
    print("Dernier mois de la série dvf.marche_mensuel :")
    for mois, type_bien, prix_3m, momentum, tendance, volume in derniers_marches:
        print(
            f"  {mois}  {type_bien:<12} prix 3 mois glissants = {prix_3m} €/m² | "
            f"momentum = {momentum} % | tendance = {tendance or 'n/d'} | "
            f"ventes 12 mois = {volume}"
        )

    if echecs:
        print()
        for echec in echecs:
            print(f"ERROR : {echec}")
        return 1

    print()
    print("OK : données DVF présentes, import récent et source à jour.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
