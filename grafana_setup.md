# Guide d'installation Grafana — DVF Tracker

> Installation native de Grafana sur Raspberry Pi (ARM64), connexion à PostgreSQL/TimescaleDB,
> et import du dashboard « DVF — Marché immobilier Saint-Maur-des-Fossés ».

Ce document suppose que la base `marine_database` est **déjà en place** (elle est partagée avec
`NDBC_tracker`). Si Grafana n'est pas encore installé, la procédure d'installation complète est
décrite dans [`../NDBC_tracker/grafana_setup.md`](../NDBC_tracker/grafana_setup.md) §1 ; les étapes
sont rappelées brièvement ci-dessous.

---

## 1. Rappel : installation de Grafana

```bash
sudo apt-get update
sudo apt-get install -y apt-transport-https wget gnupg software-properties-common

sudo mkdir -p /etc/apt/keyrings
sudo wget -O /etc/apt/keyrings/grafana.asc https://apt.grafana.com/gpg-full.key
sudo chmod 644 /etc/apt/keyrings/grafana.asc
echo "deb [signed-by=/etc/apt/keyrings/grafana.asc] https://apt.grafana.com stable main" \
  | sudo tee /etc/apt/sources.list.d/grafana.list

sudo apt-get update
sudo apt-get install -y grafana

sudo systemctl enable --now grafana-server
```

Accès : `http://<IP_DE_LA_MACHINE>:3000` — identifiants par défaut `admin` / `admin`
(Grafana impose de changer le mot de passe à la première connexion).

---

## 2. Préparer le schéma `dvf`

### 2.1 Créer le schéma et les tables

En tant que propriétaire de la base (utilisateur `pi`) :

```bash
psql -U pi -d marine_database -f dvf_schema.sql
```

Le script crée le schéma `dvf`, les tables `communes`, `lignes_mutation`, `transactions`, les
vues `marche_mensuel` et `prix_par_voie`, ainsi que les index. Il est **idempotent**.

> `DVF_data.py` rejoue ce script à chaque import : cette étape est facultative, mais elle permet de
> vérifier la base avant le premier import et de poser les droits tout de suite.

### 2.2 Créer (si besoin) le rôle de lecture Grafana

> **Le rôle est probablement déjà créé** si `NDBC_tracker` est en service. Dans ce cas, passer
> directement au §2.3.

À lancer en superutilisateur (`postgres`) :

```bash
sudo -u postgres psql -d marine_database -c "CREATE USER grafana WITH PASSWORD 'grafana_secure_password';"
sudo -u postgres psql -d marine_database -c "GRANT CONNECT ON DATABASE marine_database TO grafana;"
```

### 2.3 Accorder les droits sur le schéma `dvf`

> **⚠️ Ordre important.** `GRANT ... ON ALL TABLES IN SCHEMA dvf` n'accorde de droits que sur les
> objets **déjà existants**. Toute table ou vue créée plus tard (par exemple après un `git pull`
> qui ajoute une vue) ne sera **pas** couverte, ce qui produit dans Grafana l'erreur
> `permission denied for table transactions (SQLSTATE 42501)`.
>
> Il faut donc corriger les **trois volets** : droits immédiats, privilèges par défaut, et
> rechargement de la datasource.

```bash
# 1. Usage du schéma
sudo -u postgres psql -d marine_database -c "GRANT USAGE ON SCHEMA dvf TO grafana;"

# 2. Lecture de tous les objets existants (tables ET vues)
sudo -u postgres psql -d marine_database -c "GRANT SELECT ON ALL TABLES IN SCHEMA dvf TO grafana;"

# 3. Privilèges par défaut pour les objets créés plus tard, AU NOM DU PROPRIÉTAIRE
sudo -u postgres psql -d marine_database -c \
  "ALTER DEFAULT PRIVILEGES FOR ROLE pi IN SCHEMA dvf GRANT SELECT ON TABLES TO grafana;"
```

Le bloc `DO $$ … $$` en fin de `dvf_schema.sql` rejoue exactement ces GRANT : il peut être utilisé
pour réparer une base existante sans repartir de zéro (voir §8).

Vérification :

```bash
psql -U pi -d marine_database -c "\dp dvf.*"
psql -U pi -d marine_database -c "SELECT schemaname, tablename, tableowner FROM pg_tables WHERE schemaname = 'dvf';"
```

---

## 3. Configurer la Data Source PostgreSQL

1. Dans Grafana : **⚙️ (Administration)** → **Data sources**
2. **Add new data source** → **PostgreSQL**

| Paramètre | Valeur |
|---|---|
| **Name** | `Marine-TimescaleDB` *(réutiliser la datasource de NDBC : la base est la même)* |
| **Host** | `localhost:5432` |
| **Database** | `marine_database` |
| **User** | `grafana` (ou `pi`) |
| **Password** | *(mot de passe du rôle)* |
| **SSL Mode** | `disable` (local) |
| **TimescaleDB** | ✅ cocher |

Cliquer sur **Save & test** : « Database Connection OK » doit s'afficher.

> Le dashboard DVF déclare sa datasource via une variable de type *datasource* (`DS_POSTGRESQL`) :
> il utilisera celle que tu sélectionnes à l'import, sans modification du JSON.

---

## 4. Importer le dashboard

1. **+ (Create)** → **Import**
2. **Upload dashboard JSON file** → sélectionner `dashboard_dvf.json`
3. Laisser l'`uid` proposé : `dvf-saint-maur`
4. Choisir la datasource `Marine-TimescaleDB`
5. **Import**

Méthode alternative : copier-coller le contenu du JSON dans la zone de texte, puis **Load** →
**Import**.

Le dashboard s'ouvre sur une plage de temps **2020 → maintenant**, ce qui couvre tous les
millésimes disponibles.

---

## 5. Structure du dashboard

### Indicateurs clés (haut de page)

| Panneau | Type | Description |
|---|---|---|
| 💶 **Prix médian — Appartement** | Stat | Médiane des prix au m² sur les 12 derniers mois de données |
| 💶 **Prix médian — Maison** | Stat | Idem pour les maisons |
| 📊 **Écart Maison / Appartement** | Stat | Prime de prix de la maison individuelle, en % |
| 🔢 **Ventes de logements — 12 mois** | Stat | Taille actuelle du marché |
| 🎯 **Tendance du marché** | Stat | 🔴 Accélération · 🟢 Stabilité · 🔵 Ralentissement |

### Panneaux demandés

| Panneau | Type | Description |
|---|---|---|
| 📈 **Prix au m² — évolution dans le temps (appartement vs maison)** | Série temporelle | Médiane mensuelle lissée sur 3 mois, deux courbes |
| 📊 **Volume de ventes par mois** | Barres empilées | Nombre d'actes par mois et par type |
| 🌡️ **Tendance du marché — momentum 3 mois vs 12 mois (%)** | Série temporelle seuillée | Bleu < −2 %, vert entre, rouge > +2 % |
| 📋 **Lecture détaillée de la tendance** | Tableau | Prix lissés, momentum, variations annuelles, volumes |

### Panneaux complémentaires

| Panneau | Type | Description |
|---|---|---|
| 📉 **Dispersion des prix — appartements / maisons** | Séries temporelles | Bandes P25 / médiane / P75 |
| 🗺️ **Carte des transactions (prix au m²)** | Geomap | Chaque acte géolocalisé, coloré par prix au m² |
| 🍂 **Saisonnalité — ventes moyennes par mois calendaire** | Barres | Profil saisonnier toutes années confondues |
| 🏗️ **Neuf (VEFA) vs ancien** | Barres empilées | Volume annuel par marché |
| 📐 **Surface médiane des logements vendus** | Série temporelle | Indicateur du type d'acquéreur |
| 🏆 **Voies les plus chères** | Tableau | Prix médian au m² par rue (≥ 5 ventes) |
| 🧾 **Dernières transactions** | Tableau | 50 derniers actes exploitables |
| ℹ️ **Méthodologie et limites** | Texte | Rappel des filtres et des précautions de lecture |

`$__timeFilter()` est appliqué à `date_mutation` ou `mois` : **le sélecteur de temps en haut à
droite filtre tous les panneaux**, sauf la saisonnalité et le comparatif neuf/ancien, qui sont par
construction des vues pluriannuelles.

---

## 6. Rafraîchissement

Le dashboard se rafraîchit toutes les **heures** (`refresh: 1h`). Les données ne changeant qu'une
fois par mois, une valeur plus élevée suffit largement — mais elle évite de relire une page
ouverte après un import Jenkins.

---

## 7. Alertes (optionnel)

Deux alertes ont du sens sur ce projet :

### 7.1 Alerte « le pipeline n'a pas tourné »

Sur le panneau **Ventes de logements — 12 mois**, ou via une règle d'alerte dédiée :

```sql
SELECT max(imported_at) AS time, extract(day FROM now() - max(imported_at)) AS value
FROM dvf.transactions;
```

Condition : `WHEN last() OF query(A) IS ABOVE 35` → « l'import DVF a plus de 35 jours ».

### 7.2 Alerte « retournement de marché »

Sur le panneau **Tendance du marché — momentum** :

Condition : `WHEN last() OF query(A) IS BELOW -4` → « les prix reculent de plus de 4 % sous leur
tendance longue ».

> ⚠️ Le momentum est un indicateur **mensuel** : une alerte trop sensible produirait du bruit.
> Laisser le seuil à ±4 % et exiger deux évaluations consécutives.

---

## 8. Dépannage

| Problème | Solution |
|---|---|
| `permission denied for table transactions (SQLSTATE 42501)` | Le rôle Grafana n'a pas le `SELECT` sur le schéma `dvf` : rejouer les GRANT du §2.3, puis **Save & test** sur la datasource. |
| `permission denied for schema dvf` | Il manque `GRANT USAGE ON SCHEMA dvf TO grafana;` — le `GRANT SELECT` seul ne suffit pas. |
| `relation "dvf.marche_mensuel" does not exist` | `dvf_schema.sql` n'a pas été appliqué : `psql -U pi -d marine_database -f dvf_schema.sql`, ou lancer un import. |
| Panneaux vides | Vérifier qu'il y a des données : `SELECT count(*) FROM dvf.transactions;`. Si 0, lancer `python3 DVF_data.py`. |
| Le panneau de tendance est vide | `tendance` vaut `NULL` pour les **11 premiers mois** de la série : il faut 12 mois d'historique pour calculer un momentum 12 mois. |
| Le dernier mois semble incomplet | C'est normal : DVF est publié avec plusieurs mois de retard et le dernier mois peut être partiel. Lire les volumes sur 12 mois. |
| `Connection refused` | PostgreSQL n'écoute pas : `sudo ss -tlnp \| grep 5432`. |
| Grafana ne démarre pas | `sudo journalctl -u grafana-server -f`. |

---

## 9. Commandes utiles

```bash
# Redémarrer Grafana
sudo systemctl restart grafana-server

# Logs en temps réel
sudo journalctl -u grafana-server -f

# Vérifier l'écoute sur le port 3000
sudo ss -tlnp | grep 3000

# Contrôler les données présentes
psql -U pi -d marine_database -c "SELECT count(*) FROM dvf.transactions;"
psql -U pi -d marine_database -c "SELECT * FROM dvf.marche_mensuel WHERE type_bien = 'Global' ORDER BY mois DESC LIMIT 3;"

# Vérifier les droits
psql -U pi -d marine_database -c "\dp dvf.*"
```

---

## Récapitulatif des fichiers

| Fichier | Rôle |
|---|---|
| `dvf_schema.sql` | Schéma `dvf` : tables, hypertables, vues, index, droits |
| `dashboard_dvf.json` | Dashboard Grafana prêt à importer (uid `dvf-saint-maur`) |
| `DVF_data.py` | Script Python ETL |
| `verify_dvf_freshness.py` | Contrôle de fraîcheur post-import |
| `Jenkinsfile` | Pipeline d'automatisation mensuel |
| `GUIDE_DVF.md` | Comprendre la donnée DVF et ses limites |
