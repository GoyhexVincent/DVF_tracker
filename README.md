# DVF Tracker — Marché immobilier de Saint-Maur-des-Fossés

Pipeline automatisé de collecte, de stockage et de visualisation des **Demandes de valeurs
foncières (DVF)** publiées en open data par la DGFiP, appliqué à la commune de
**Saint-Maur-des-Fossés** (Val-de-Marne, INSEE 94068).

Le projet suit la même architecture que [`NDBC_tracker`](../NDBC_tracker) — source ouverte →
Jenkins → PostgreSQL/TimescaleDB → Grafana — mais il ne stocke pas des mesures physiques :
il reconstitue un **marché immobilier** (prix au m², volumes, tendance) à partir des actes notariés.

---

## 📋 Table des matières

- [Vue d'ensemble](#vue-densemble)
- [Architecture](#architecture)
- [Stack technique](#stack-technique)
- [Sources de données](#sources-de-données)
- [Périmètre : Saint-Maur-des-Fossés](#périmètre--saint-maur-des-fossés)
- [Schéma de base de données](#schéma-de-base-de-données)
- [Indicateurs et méthodologie](#indicateurs-et-méthodologie)
- [Installation](#installation)
- [Configuration](#configuration)
- [Utilisation](#utilisation)
- [Pipeline Jenkins](#pipeline-jenkins)
- [Dashboard Grafana](#dashboard-grafana)
- [Limites du jeu de données](#limites-du-jeu-de-données)
- [Licence](#licence)

---

## Vue d'ensemble

Ce projet automatise la récupération **mensuelle** des transactions immobilières enregistrées à
Saint-Maur-des-Fossés, leur stockage dans le schéma `dvf` d'une base PostgreSQL/TimescaleDB, puis
leur restitution dans Grafana.

L'objectif est de répondre à trois questions simples :

1. **Combien coûte le mètre carré, et comment évolue-t-il ?** — série mensuelle des prix médians,
   distinguant appartements et maisons.
2. **Quel est le volume du marché ?** — nombre d'actes par mois et sur 12 mois glissants.
3. **Le marché accélère-t-il ou ralentit-il ?** — un indicateur de *momentum* comparant la
   tendance de court terme (3 mois) à celle de long terme (12 mois).

Le projet est **volontairement séparé** de `NDBC_tracker` : cadence différente (mensuelle contre
quotidienne), source différente, responsable potentiellement différent. Il partage en revanche la
même base (`marine_database`), le même utilisateur (`pi`) et le même identifiant Jenkins.

---

## Architecture

```
┌──────────────────────┐     ┌──────────────┐     ┌─────────────────────┐     ┌──────────┐
│  DGFiP / Etalab      │────▶│   Jenkins    │────▶│   PostgreSQL 16 +   │────▶│ Grafana  │
│  DVF géolocalisées   │     │  (mensuel)   │     │   TimescaleDB       │     │(dashboard)│
│  geo-dvf (CSV)       │     │              │     │   schéma  dvf       │     │          │
└──────────────────────┘     └──────────────┘     └─────────────────────┘     └──────────┘
                                       │                     ▲
                                       └──── 1×/mois ────────┘
```

**Flux de données :**

1. Jenkins déclenche le pipeline le **1er de chaque mois**.
2. `DVF_data.py` interroge le dossier `latest` de geo-dvf pour chaque millésime disponible
   (2021 → année courante) et télécharge le CSV de la commune (~0,8 Mo par millésime).
3. Les lignes brutes sont insérées dans `dvf.lignes_mutation`, puis les actes reconstitués dans
   `dvf.transactions` (prix au m², type de bien, fiabilité).
4. `verify_dvf_freshness.py` vérifie que l'import est récent et que la source remonte bien.
5. Grafana interroge la vue `dvf.marche_mensuel` pour produire les visualisations.

---

## Stack technique

| Couche | Technologie | Rôle |
|---|---|---|
| Orchestration | Jenkins | Planification mensuelle et exécution du pipeline |
| Langage | Python 3.11+ | Scripting ETL (Extract, Transform, Load) |
| Librairie DB | `psycopg2-binary` | Connexion et insertion batch dans PostgreSQL |
| Base de données | PostgreSQL 16 | Stockage relationnel des transactions |
| Extension TS | TimescaleDB *(optionnelle)* | Hypertables partitionnées par année |
| Visualisation | Grafana | Dashboard « DVF — Marché immobilier Saint-Maur-des-Fossés » |
| Versioning | Git + GitHub | Gestion du code source |

Aucune autre dépendance Python : les CSV sont lus avec le module standard `csv`, les
téléchargements avec `urllib`. `pandas` n'est pas nécessaire (≈ 21 500 lignes pour cinq ans).

---

## Sources de données

### DVF — Demandes de valeurs foncières

| Élément | Détail |
|---|---|
| Producteur | **DGFiP** (Direction générale des finances publiques) |
| Diffuseur | **Etalab / data.gouv.fr** |
| Page du jeu de données | <https://www.data.gouv.fr/datasets/demandes-de-valeurs-foncieres-geolocalisees> |
| Format utilisé | CSV géolocalisé (`geo-dvf`), séparateur virgule, UTF-8 |
| Accès | <https://files.data.gouv.fr/geo-dvf/latest/csv/> |
| Gabarit d'URL | `…/latest/csv/{annee}/communes/{departement}/{code_insee}.csv` |
| Clé API | **aucune** — open data |
| Licence | Licence Ouverte 2.0 (Etalab) |
| Fréquence de publication | **semestrielle** (avril et octobre) |

`geo-dvf` est une **version enrichie** des fichiers DVF bruts : colonnes renommées en
`snake_case`, encodage UTF-8, codes INSEE/COG normalisés, et surtout **géolocalisation** de chaque
parcelle (`longitude`, `latitude`) ainsi que l'identifiant cadastral (`id_parcelle`). C'est cette
version qui est utilisée ici, car elle évite d'avoir à manipuler les fichiers France entière
(plusieurs centaines de Mo par millésime).

### Contenu d'une ligne DVF

Une ligne décrit **un local** (ou une parcelle de terrain) rattaché à un acte :

| Colonne source | Signification |
|---|---|
| `id_mutation` | Identifiant de l'acte notarié — **la clé de regroupement** |
| `date_mutation` | Date de la vente |
| `nature_mutation` | `Vente`, `Vente en l'état futur d'achèvement` (VEFA), `Adjudication`, `Échange`… |
| `valeur_fonciere` | Prix déclaré dans l'acte, en euros (**répété sur chaque ligne de l'acte**) |
| `type_local` | `Appartement`, `Maison`, `Dépendance`, `Local industriel. commercial ou assimilé` |
| `surface_reelle_bati` | Surface bâtie issue du cadastre (m²) |
| `surface_terrain` | Surface de la parcelle (m²) |
| `nombre_pieces_principales` | Nombre de pièces principales du logement |
| `adresse_*`, `code_postal` | Adresse déclarative |
| `id_parcelle`, `longitude`, `latitude` | Parcelle cadastrale et sa géolocalisation |

> ⚠️ **Le piège classique de DVF.** Un acte (un `id_mutation`) occupe presque toujours
> **plusieurs lignes** : l'appartement, sa cave, son parking… La `valeur_fonciere` est
> **répétée à l'identique** sur chacune d'elles. Sommer cette colonne revient donc à compter le
> prix deux, trois, dix fois. C'est la raison d'être de la table `dvf.transactions`, qui
> reconstruit un enregistrement unique par acte.

### Publications

DVF est diffusé **deux fois par an**, avec un décalage de plusieurs mois :

| Campagne | Contenu publié |
|---|---|
| Avril | Année N-1 complète (et corrections des millésimes antérieurs) |
| Octobre | Mise à jour des millésimes, ajout des premiers mois de l'année en cours selon les campagnes |

Un import **mensuel** ne cherche donc pas des données nouvelles chaque mois : il garantit que la
base absorbe les corrections et la nouvelle campagne dans le mois qui suit leur publication.

---

## Périmètre : Saint-Maur-des-Fossés

| Élément | Valeur |
|---|---|
| Code INSEE | **94068** |
| Département | 94 — Val-de-Marne |
| Codes postaux | **94100** (Saint-Maur) et **94210** (La Varenne-Saint-Hilaire) |
| Population légale | 76 572 habitants (INSEE, millésime 2023) |
| Coordonnées | 48,7994 N — 2,4997 E |

Deux codes postaux pour une même commune : c'est normal, et sans conséquence ici — le fichier
`94068.csv` contient bien les deux. Le schéma stocke le code postal de chaque acte, ce qui permet
de comparer les deux quartiers dans Grafana.

**Pour étendre le périmètre**, il suffit d'ajouter une entrée dans la constante `COMMUNES` de
`DVF_data.py` : le pipeline télécharge alors un fichier supplémentaire par millésime.

### Volumétrie constatée (millésimes 2021 → 2025)

| Mesure | Valeur |
|---|---|
| Lignes brutes importées | 21 553 |
| Actes (toutes natures) | 8 614 |
| Actes de vente conservés dans `dvf.transactions` | 8 582 |
| Actes avec un prix au m² exploitable | 6 437 |
| Taille téléchargée | ≈ 4 Mo pour cinq millésimes |

---

## Schéma de base de données

Tout est créé par `dvf_schema.sql`, rejoué automatiquement à chaque import par `DVF_data.py`.
Le script est **idempotent** : on peut le lancer autant de fois que nécessaire.

```
┌────────────────────┐       ┌──────────────────────────┐       ┌───────────────────────┐
│   dvf.communes     │       │  dvf.lignes_mutation     │       │   dvf.transactions    │
│  (référentiel)     │       │  (brut, 1 ligne = 1 local)│─────▶│ (1 ligne = 1 acte)    │
└────────────────────┘       └──────────────────────────┘       └───────────┬───────────┘
                                                                           │
                                                     ┌─────────────────────┴──────────────────┐
                                                     ▼                                        ▼
                                          ┌──────────────────────┐              ┌────────────────────┐
                                          │ dvf.marche_mensuel   │              │ dvf.prix_par_voie  │
                                          │ (vue : prix, volume, │              │ (vue : prix médian │
                                          │  momentum, tendance) │              │  par rue)          │
                                          └──────────────────────┘              └────────────────────┘
```

### `dvf.communes` — référentiel

Une ligne par commune suivie : code INSEE, nom, département, codes postaux, coordonnées,
population, URL source. Alimentée depuis la constante `COMMUNES` de `DVF_data.py`.

### `dvf.lignes_mutation` — copies fidèles des CSV

Une ligne = une ligne du fichier geo-dvf, avec les 40 colonnes d'origine plus
`source_annee` (millésime du fichier) et `imported_at`. Table **TimescaleDB** partitionnée par
année sur `date_mutation`.

C'est la table de vérité : toute analyse peut être refaite à partir d'elle sans retélécharger.

### `dvf.transactions` — une ligne par acte

Construite à partir des lignes, avec les colonnes d'analyse :

| Colonne | Signification |
|---|---|
| `type_bien` | `Appartement`, `Maison` (si l'acte porte sur **un seul** logement) ou `Autre` |
| `marche` | `ancien` ou `neuf` (vente en l'état futur d'achèvement) |
| `valeur_fonciere` | Prix de l'acte, une seule fois |
| `surface_bati` | Surface du/des logement(s) |
| `surface_terrain` | Surface des parcelles (sans double comptage) |
| `surface_dependances`, `nb_dependances` | Caves, garages, parkings |
| `prix_m2` | `valeur_fonciere / surface_bati` |
| `prix_m2_fiable` | `FALSE` si le prix au m² n'a pas de sens économique |
| `motif_non_fiable` | Explication du rejet, en clair |
| `adresse`, `code_postal`, `longitude`, `latitude` | Localisation de l'acte |
| `source_annee`, `imported_at` | Traçabilité |

### `dvf.marche_mensuel` — agrégats et tendance

Vue (et non table matérialisée : le volume est trop faible pour justifier un rafraîchissement) qui
produit, pour chaque mois et pour chacun des trois axes `Appartement` / `Maison` / `Global` :

| Colonne | Description |
|---|---|
| `nb_ventes` | Nombre d'actes du mois (toutes transactions) |
| `nb_ventes_prix` | Nombre d'actes contribuant réellement au prix |
| `prix_m2_median` | Médiane des prix au m² du mois |
| `prix_m2_p25`, `prix_m2_p75` | Premier et troisième quartiles (dispersion) |
| `prix_m2_median_3m` | Moyenne des trois dernières médianes mensuelles (**prix lissé court terme**) |
| `prix_m2_median_12m` | Moyenne des douze dernières médianes mensuelles (**prix lissé long terme**) |
| `prix_median` | Médiane des prix de vente en euros |
| `surface_mediane` | Surface médiane des logements vendus |
| `nb_ventes_12m` | Volume sur 12 mois glissants |
| `momentum_pct` | `3m / 12m − 1`, en % |
| `variation_yoy_pct` | `3m` comparé aux mêmes trois mois un an plus tôt |
| `volume_yoy_pct` | `nb_ventes_12m` comparé aux douze mois précédents |
| `tendance` | `Accélération`, `Stabilité` ou `Ralentissement` |

La grille mensuelle est **complète** : les mois sans vente apparaissent avec `nb_ventes = 0`, ce
qui évite de fausser les moyennes glissantes.

### `dvf.prix_par_voie` — classement des rues

Prix médian au m² par voie et par type de bien, avec le nombre de ventes et les quartiles.

---

## Indicateurs et méthodologie

### Le prix au m² n'est calculé que sur des actes « propres »

Un prix au m² n'a de sens que si l'acte porte sur **un seul logement**, avec une surface et un prix
crédibles. Les filtres appliqués sont les suivants — tout acte qui échoue est **conservé en base**
mais exclu des séries de prix (il reste compté dans les volumes) :

| Filtre | Valeur par défaut | Variable |
|---|---|---|
| Un seul logement dans l'acte (dépendances tolérées) | — | — |
| Pas de local commercial ni de local de type indéterminé | — | — |
| Surface bâtie minimale | 9 m² | `DVF_SURFACE_MIN` |
| Prix au m² minimal | 500 €/m² | `DVF_PRIX_M2_MIN` |
| Prix au m² maximal | 30 000 €/m² | `DVF_PRIX_M2_MAX` |

Ces bornes écartent les cas les plus fréquents de prix non représentatifs : vente d'une
**quote-part** (divorce, succession), vente d'un **lot isolé** (cave, parking) mal typé, ou valeur
symbolique. Elles ne prétendent pas tout attraper : c'est la médiane, et non la moyenne, qui
absorbe le reste.

### Pourquoi la médiane

Saint-Maur mélange, dans la même rue, des studios et des maisons de 200 m². La moyenne est tirée
par les extrêmes ; la **médiane** décrit le vendeur typique. Elle est en plus **lissée sur trois
mois**, car un mois isolé ne compte parfois que 20 à 30 ventes — insuffisant pour conclure.

### Comment la tendance est calculée

```
momentum = prix lissé 3 mois / prix lissé 12 mois − 1
```

| Momentum | Tendance affichée |
|---|---|
| > +2 % | 🔴 **Accélération** — les prix récents dépassent nettement la tendance longue |
| −2 % … +2 % | 🟢 **Stabilité** — la variation n'est pas distinguable du bruit mensuel |
| < −2 % | 🔵 **Ralentissement** — les prix récents passent sous la tendance longue |

Le seuil de **±2 %** n'est pas arbitraire : il correspond à l'écart-type observé du momentum
mensuel sur la commune (≈ 2,4 % pour les appartements, 3,4 % pour les maisons). En deçà, un
changement de couleur relèverait du bruit d'échantillonnage.

Deux indicateurs complémentaires sont fournis et affichés dans le dashboard :

- `variation_yoy_pct` : vue **annuelle**, moins réactive, utile pour confirmer une inflexion ;
- `volume_yoy_pct` : le **volume** peut ralentir alors que les prix tiennent (marché qui se
  contracte sans se retourner) — les deux se lisent ensemble.

---

## Installation

### Prérequis

- Python 3.11 ou plus récent
- PostgreSQL 14+ (TimescaleDB optionnel — le pipeline fonctionne sans)
- Jenkins (optionnel, pour l'automatisation)
- Grafana (optionnel, pour la visualisation)

### 1. Récupérer le code

```bash
git clone git@github.com:GoyhexVincent/DVF_tracker.git
cd DVF_tracker
```

### 2. Installer les dépendances Python

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Préparer la base (une seule fois)

La base et l'utilisateur sont **ceux de NDBC_tracker** : rien à recréer, seul un schéma
supplémentaire est ajouté.

```bash
# En tant que propriétaire de la base (utilisateur « pi »)
psql -U pi -d marine_database -f dvf_schema.sql
```

Cela crée le schéma `dvf`, ses quatre objets, et accorde les droits de lecture au rôle `grafana`
s'il existe. `DVF_data.py` rejoue ce script à chaque import : cette étape est donc facultative,
mais elle permet de vérifier la base avant le premier import.

---

## Configuration

### Variables d'environnement

| Variable | Défaut | Description |
|---|---|---|
| `PGHOST` | `localhost` | Hôte PostgreSQL |
| `PGPORT` | `5432` | Port PostgreSQL |
| `PGDATABASE` | `marine_database` | Nom de la base (partagée avec NDBC) |
| `PGUSER` | `pi` | Utilisateur PostgreSQL |
| `PGPASSWORD` | *(obligatoire)* | Mot de passe — aucune valeur par défaut |
| `DVF_START_YEAR` | `2021` | Premier millésime interrogé |
| `DVF_SCHEMA_FILE` | `dvf_schema.sql` | Chemin du script de schéma |
| `DVF_SURFACE_MIN` | `9` | Surface minimale d'un logement (m²) |
| `DVF_PRIX_M2_MIN` | `500` | Prix au m² minimal retenu (€/m²) |
| `DVF_PRIX_M2_MAX` | `30000` | Prix au m² maximal retenu (€/m²) |
| `MIN_IMPORT_INTERVAL_HOURS` | `0` (désactivé) | Garde-fou : ne rien réimporter si la dernière importation a moins de N heures (le Jenkinsfile le positionne à 600) |
| `FORCE_IMPORT` | *(non défini)* | `1` / `true` / `oui` pour ignorer le garde-fou |
| `MAX_IMPORT_AGE_DAYS` | `35` | Contrôle de fraîcheur : âge maximal du dernier import |
| `MAX_MUTATION_AGE_DAYS` | `400` | Contrôle de fraîcheur : âge maximal de la vente la plus récente |

### Fichier `.env` pour le développement local

```bash
PGHOST=localhost
PGPORT=5432
PGDATABASE=marine_database
PGUSER=pi
PGPASSWORD=votre_mot_de_passe
```

`.env` est déjà présent dans `.gitignore`.

---

## Utilisation

### Import manuel

```bash
export PGHOST=localhost PGPORT=5432 PGDATABASE=marine_database PGUSER=pi
export PGPASSWORD=votre_mot_de_passe

source .venv/bin/activate
python3 DVF_data.py
```

Sortie attendue :

```
2026-10-08 10:08:42 [INFO] Schéma dvf prêt (dvf_schema.sql).
2026-10-08 10:08:42 [INFO] Millésimes interrogés : 2021, 2022, 2023, 2024, 2025, 2026, 2027
2026-10-08 10:08:43 [INFO] 94068 2021 : 4830 lignes brutes, 2158 actes importés.
2026-10-08 10:08:44 [INFO] 94068 2022 : 4931 lignes brutes, 1939 actes importés.
2026-10-08 10:08:45 [INFO] 94068 2023 : 3699 lignes brutes, 1479 actes importés.
2026-10-08 10:08:46 [INFO] 94068 2024 : 3687 lignes brutes, 1376 actes importés.
2026-10-08 10:08:47 [INFO] 94068 2025 : 4406 lignes brutes, 1630 actes importés.
2026-10-08 10:08:47 [INFO] 94068 — millésime 2026 indisponible (404), ignoré.
2026-10-08 10:08:47 [INFO] 94068 — millésime 2027 indisponible (404), ignoré.
2026-10-08 10:08:47 [INFO] TOTAL : 5 millésimes, 21553 lignes brutes, 8582 actes.
```

Les millésimes futurs sont testés puis ignorés : **un nouveau millésime publié est récupéré sans
aucune modification du code**.

### Vérification des données

```bash
python3 verify_dvf_freshness.py
```

```
Lignes brutes (dvf.lignes_mutation) : 21553
Actes (dvf.transactions)            : 8582 dont 6437 avec prix fiable
Période couverte                    : 2021-01-04 → 2025-12-31
Dernier import                      : 2026-10-08 10:08:47+02:00
Âge du dernier import               : 0.0 j (seuil 35 j)
Âge de la vente la plus récente     : 281 j (seuil 400 j)

Dernier mois de la série dvf.marche_mensuel :
  2025-12-01  Appartement  prix 3 mois glissants = 5393 €/m² | momentum = 1.53 % | tendance = Accélération | ventes 12 mois = 852
  2025-12-01  Global       prix 3 mois glissants = 5648 €/m² | momentum = 0.64 % | tendance = Stabilité | ventes 12 mois = 1203
  2025-12-01  Maison       prix 3 mois glissants = 6687 €/m² | momentum = -2.39 % | tendance = Ralentissement | ventes 12 mois = 351

OK : données DVF présentes, import récent et source à jour.
```

### Tests

```bash
python3 -m unittest -v test_dvf_etl
```

Les tests sont **hors ligne** : ni réseau, ni base de données. Ils couvrent les conversions, le
parsing, la construction des actes (dépendances, terrains, actes multi-lots, filtres de
plausibilité) et vérifient qu'aucune dérive ne s'est installée entre `dvf_schema.sql`, l'ETL et
`dashboard_dvf.json`.

### Requêtes utiles

```sql
-- Dernier état du marché par type de bien
SELECT DISTINCT ON (type_bien) *
FROM dvf.marche_mensuel
ORDER BY type_bien, mois DESC;

-- Prix médian au m² par année
SELECT date_trunc('year', date_mutation) AS annee,
       type_bien,
       count(*) AS ventes,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY prix_m2)) AS prix_m2_median
FROM dvf.transactions
WHERE prix_m2_fiable
GROUP BY 1, 2
ORDER BY 1, 2;

-- Pourquoi des actes sont-ils exclus des prix ?
SELECT motif_non_fiable, count(*)
FROM dvf.transactions
WHERE NOT prix_m2_fiable
GROUP BY 1
ORDER BY 2 DESC;
```

---

## Pipeline Jenkins

Le job est **indépendant** de celui de NDBC_tracker.

### Configuration du job

| Paramètre | Valeur |
|---|---|
| Type | Pipeline |
| Définition | Pipeline script from SCM |
| SCM | Git |
| URL | `git@github.com:GoyhexVincent/DVF_tracker.git` |
| Branche | `*/main` |
| Script Path | `Jenkinsfile` |

### Credentials requis

Ce sont **les mêmes que pour NDBC_tracker** :

| Identifiant | Type | Contenu |
|---|---|---|
| `pg-password-marine` | Secret text | Mot de passe PostgreSQL de l'utilisateur `pi` |
| `github-ssh-key` | SSH Username with private key | Clé de déploiement GitHub |

### Planification

```groovy
triggers {
    cron("H 5 1 * *")     // le 1er de chaque mois, vers 5 h
}
```

> ⚠️ Jenkins n'applique le bloc `triggers` d'un Jenkinsfile **qu'à partir du prochain build** du
> job. Si le job est défini en « Pipeline script » (script inline) au lieu de « Pipeline script
> from SCM », le Jenkinsfile est purement ignoré et le planning doit être corrigé dans la
> configuration du job (Build Triggers → Build periodically).

**Garde-fou :** le Jenkinsfile exporte `MIN_IMPORT_INTERVAL_HOURS=600` (≈ 25 jours). Même si le
job est déclenché plusieurs fois dans le mois (build manuel, webhook, trigger résiduel),
`DVF_data.py` n'insère rien si un import a déjà eu lieu. `FORCE_IMPORT=1` outrepasse ce garde-fou.

### Stages

| Stage | Rôle |
|---|---|
| `Setup Python Environment` | Création du venv et installation des dépendances |
| `Run DVF Import` | Téléchargement et chargement des millésimes |
| `Verify DVF Freshness` | Contrôle de l'âge de l'import et de la source |
| `Run Unit Tests` | Tests hors ligne de l'ETL |

---

## Dashboard Grafana

Import du fichier `dashboard_dvf.json` (uid `dvf-saint-maur`) : voir **[grafana_setup.md](grafana_setup.md)**.

### Panneaux demandés

| Panneau | Type | Répond à |
|---|---|---|
| **Prix au m² — évolution dans le temps (appartement vs maison)** | Série temporelle | Évolution des prix, avec distinction appartement / maison |
| **Volume de ventes par mois** | Barres empilées | Volume mensuel par type de bien |
| **Tendance du marché — momentum 3 mois vs 12 mois** | Série temporelle seuillée | Accélération / stabilité / ralentissement |
| **Lecture détaillée de la tendance** | Tableau | Prix lissés, momentum, variation annuelle, volumes |
| **Tendance du marché** *(indicateur clé)* | Stat | La tendance du dernier mois en un coup d'œil |

### Panneaux proposés en complément

| Panneau | Intérêt |
|---|---|
| **Prix médian — Appartement / Maison** (stats) | Le niveau de prix actuel, sans lire la courbe |
| **Écart Maison / Appartement** | Mesure la prime de la maison individuelle |
| **Ventes de logements — 12 mois** | Taille actuelle du marché |
| **Dispersion des prix (P25 / médiane / P75)** | Un prix médian stable peut cacher un parc qui se resserre ou s'écarte |
| **Carte des transactions (prix au m²)** | Repère les micro-marchés : bords de Marne, centre, La Varenne… |
| **Saisonnalité — ventes moyennes par mois calendaire** | Distingue un creux conjoncturel d'un creux saisonnier |
| **Neuf (VEFA) vs ancien** | Le neuf ne suit pas la même dynamique que l'ancien |
| **Surface médiane des logements vendus** | Révèle le type d'acquéreur (investisseurs → petites surfaces) |
| **Voies les plus chères** | Situe une adresse ; complète la carte |
| **Dernières transactions** | Contrôle visuel rapide sur la qualité des données |

---

## Limites du jeu de données

| Limite | Conséquence |
|---|---|
| La **valeur foncière** est le prix *déclaré* de l'acte | Une vente d'une quote-part (divorce, succession) produit un prix au m² aberrant ; d'où les filtres de plausibilité et l'usage de la médiane |
| La **surface réelle bâtie** vient du cadastre | Elle diffère de la surface Carrez ; l'écart atteint couramment 5 à 10 % |
| DVF ignore l'**état du bien** | À prix égal, deux logements peuvent être incomparables (travaux, étage, exposition) |
| Le **délai de publication** est de plusieurs mois | Le « dernier mois » de la série est souvent partiel : lire les volumes sur 12 mois glissants |
| Les **ventes non notariées** sont absentes | Ventes de gré à gré, sciages, mutations à titre gratuit n'y figurent pas |
| Les **millésimes antérieurs à 2021** ne sont plus diffusés | La profondeur d'historique est limitée à cinq ans dans `latest` ; elle s'allongera automatiquement au fil des publications |
| Une vente peut être **rectifiée ou annulée** | Chaque import **remplace** intégralement le millésime concerné plutôt que d'ajouter des lignes — la base ne peut pas conserver de fantômes |

Pour une lecture détaillée du jeu de données, de son vocabulaire et de ses usages, voir
**[GUIDE_DVF.md](GUIDE_DVF.md)**.

---

## Licence

Les données DVF sont diffusées sous **Licence Ouverte 2.0 (Etalab)** : réutilisation libre, y
compris commerciale, sous réserve de mention de la source.

> Source : DGFiP — *Demandes de valeurs foncières*, diffusées par Etalab sur
> <https://www.data.gouv.fr/datasets/demandes-de-valeurs-foncieres-geolocalisees>

Le code source est fourni à titre éducatif et de recherche.

## Ressources complémentaires

- [DVF sur data.gouv.fr](https://www.data.gouv.fr/datasets/demandes-de-valeurs-foncieres-geolocalisees)
- [Fichiers geo-dvf](https://files.data.gouv.fr/geo-dvf/latest/csv/)
- [Visualiseur DVF (Etalab)](https://app.dvf.etalab.gouv.fr/)
- [Documentation TimescaleDB](https://docs.timescale.com/)
- [Site de l'INSEE — commune de Saint-Maur-des-Fossés](https://www.insee.fr/fr/statistiques/7725600?geo=COM-94068)
