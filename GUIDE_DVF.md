# Guide DVF — Comprendre les données des transactions immobilières

Document de référence à destination des utilisateurs, chercheurs et curieux souhaitant comprendre
l'origine, la pertinence et les limites des données collectées par ce projet.

---

## Qu'est-ce que DVF ?

DVF signifie **Demandes de valeurs foncières**. C'est le nom donné à la publication en open data
du **fichier des mutations immobilières** tenu par la **Direction générale des finances publiques
(DGFiP)**.

Une « mutation immobilière » est le terme administratif pour une **transaction portant sur un bien
immobilier** : une vente, une adjudication, un échange. Chaque fois qu'un acte est enregistré chez
un notaire puis traité par l'administration fiscale, il alimente ce fichier.

Depuis 2019, ces informations sont publiées en **open data**, gratuitement, sans inscription et
sans clé d'API. C'est une ouverture rare : peu de pays publient le prix réel de chaque transaction
immobilière, à l'adresse près.

**Ce que DVF n'est pas :** ce n'est ni un fichier cadastral, ni un registre de propriété. Il dit
« tel bien a été vendu tel jour pour tel prix », jamais « telle personne possède tel bien ».

---

## D'où viennent les données ?

La chaîne est longue, et chaque étape explique une caractéristique du jeu de données :

```
Acte signé chez le notaire
        │
        ▼
Formalités d'enregistrement auprès de l'administration fiscale
        │
        ▼
Fichier des mutations immobilières (DGFiP)  ← données fiscales, exhaustives
        │
        ▼
Retraitement, anonymisation, agrégation géographique
        │
        ├──▶ DVF brut        : un fichier TXT par année, France entière
        │
        └──▶ DVF géolocalisé : un CSV par commune et par année   ← utilisé ici
                    │
                    ▼
        dvf.lignes_mutation  →  dvf.transactions  →  Grafana
```

**Ce projet utilise la version géolocalisée** (`geo-dvf`, produite par Etalab). Elle ajoute trois
choses décisives par rapport aux fichiers bruts :

1. un **CSV propre** (UTF-8, séparateur virgule, colonnes en `snake_case`, décimales normalisées) ;
2. la **géolocalisation** de chaque parcelle (`longitude`, `latitude`) ;
3. l'**identifiant cadastral** (`id_parcelle`), qui permet de rattacher la vente à une parcelle
   précise.

En contrepartie, elle n'est disponible que **par commune** et ne remonte pas avant 2021 dans le
dossier `latest`. C'est amplement suffisant pour suivre un marché local.

---

## Ce que contient une ligne

Chaque ligne du fichier décrit **un local** rattaché à un acte. Les colonnes utiles :

| Colonne | Signification | Piège |
|---|---|---|
| `id_mutation` | Identifiant de l'acte | Une même valeur apparaît sur plusieurs lignes |
| `date_mutation` | Date de la vente | C'est la date de l'acte, pas celle de la publication |
| `nature_mutation` | `Vente`, `Vente en l'état futur d'achèvement` (VEFA), `Adjudication`, `Échange`… | Toutes ne sont pas des ventes classiques |
| `valeur_fonciere` | Prix déclaré dans l'acte (€) | **Répété sur chaque ligne de l'acte** |
| `type_local` | `Appartement`, `Maison`, `Dépendance`, `Local industriel. commercial ou assimilé` | Peut être vide (terrains) |
| `surface_reelle_bati` | Surface bâtie du local (m²) | Vient du **cadastre**, pas d'un mesurage |
| `surface_terrain` | Surface de la parcelle (m²) | Répétée pour chaque ligne d'une même parcelle |
| `nombre_pieces_principales` | Pièces principales du logement | Vide pour les dépendances |
| `lot1_surface_carrez` … | Surfaces Carrez des lots de copropriété | Souvent partiellement renseigné |
| `adresse_numero`, `adresse_nom_voie`, `code_postal` | Adresse déclarative | Numéros parfois absents |
| `id_parcelle`, `longitude`, `latitude` | Parcelle cadastrale et position | 1 à 2 % des lignes sans coordonnées |

---

## Les pièges à connaître

### 1. La valeur foncière est répétée

C'est **le** piège du jeu de données. Un acte portant sur un appartement, sa cave et son parking
occupe trois lignes, et la `valeur_fonciere` est **identique sur les trois**. Sommer cette colonne
sur la table brute revient donc à compter le prix trois fois.

> Dans ce projet, la table `dvf.transactions` reconstruit **un enregistrement par acte**. C'est la
> seule table sur laquelle il faut faire des statistiques de prix.

Concrètement, sur les 21 553 lignes correspondant à Saint-Maur-des-Fossés (2021-2025), il n'y a
que **8 614 actes**.

### 2. Tous les actes ne portent pas sur un logement unique

Un acte peut vendre :

- un **logement unique** (le cas normal pour un prix au m²) ;
- un logement **avec ses dépendances** — cave, garage, parking : parfaitement exploitable ;
- **plusieurs logements** (immeuble entier, deux appartements loués) : le prix au m² n'est plus
  rattachable à un logement ;
- un **mélange logement + local commercial** : le prix mélange deux marchés ;
- **aucun logement** (terrain, garage isolé, local commercial) : hors périmètre.

Seul le premier et le deuxième cas donnent un prix au m² interprétable. Les autres sont conservés
en base, comptés dans les volumes, mais exclus des séries de prix.

### 3. Un prix peut ne pas représenter un marché

Certaines valeurs foncières ne correspondent pas à une transaction de marché :

- **quote-part** vendue entre héritiers ou ex-époux : le prix porte sur la moitié, le quart, le
  dixième d'un bien ;
- **vente d'un lot isolé** (cave, parking, grenier) mal typé comme logement ;
- **valeur symbolique** (1 €, 100 €) dans un contexte familial ;
- **dation en paiement**, apport en société, soulte.

Ces cas produisent des prix au m² de 30 € ou de 40 000 €. Les filtres de plausibilité du projet
(500 à 30 000 €/m², surface ≥ 9 m²) en écartent l'essentiel, et la **médiane** absorbe le reste.

### 4. La surface cadastrale n'est pas la surface Carrez

`surface_reelle_bati` provient du cadastre. Elle diffère de la surface loi Carrez utilisée dans
les annonces, de 5 à 10 % couramment (murs, gaines, trémies d'escalier, sous-pentes). Un prix au
m² calculé à partir de DVF n'est donc pas directement comparable à un prix d'annonce.

### 5. DVF ignore l'état du bien

Aucune information sur les travaux, l'étage, l'exposition, la vue, le DPE, les parties communes.
Deux ventes au même prix au m² le même mois peuvent porter sur des biens très différents. C'est
une raison supplémentaire de raisonner en distributions (médiane, quartiles) plutôt qu'en valeurs
ponctuelles.

### 6. La publication a plusieurs mois de retard

Les actes sont publiés **deux fois par an**, en avril et en octobre, avec un décalage de plusieurs
mois sur la signature. Le « dernier mois » de la série est donc souvent incomplet, et la vente la
plus récente peut avoir **plus de six mois**.

> Conséquence pratique : lire les **volumes sur 12 mois glissants**, jamais sur le seul dernier
> mois, et ne pas interpréter une baisse de prix sur 30 ventes.

### 7. Une vente peut être rectifiée ou annulée

Une mutation peut disparaître ou changer de montant d'une publication à l'autre (correction,
annulation). C'est pourquoi chaque import **remplace intégralement le millésime** concerné au lieu
d'ajouter les nouvelles lignes : la base ne peut pas conserver de transactions fantômes.

---

## Comment un prix au m² est calculé ici

Le principe, en trois étapes :

1. **Regrouper** toutes les lignes d'un même `id_mutation`.
2. **Classer** l'acte : un seul logement (`Appartement` ou `Maison`), ou `Autre`. Les dépendances
   (caves, garages, parkings) sont acceptées et n'entrent pas dans la surface du logement.
3. **Calculer** `prix_m2 = valeur_fonciere / surface_reelle_bati du logement`, puis marquer le
   résultat **fiable ou non** selon les bornes de plausibilité.

Ce choix a un coût : environ **25 % des actes** sont marqués non fiables (actes multi-logements,
locaux commerciaux, terrains, valeurs aberrantes). C'est le prix à payer pour que la médiane
affiche signifie quelque chose. Ces actes restent visibles dans les volumes.

### Ordres de grandeur observés à Saint-Maur-des-Fossés

Prix médian au m² des transactions fiables :

| Année | Appartement | Maison |
|---|---|---|
| 2021 | 6 044 € | 7 733 € |
| 2022 | 6 186 € | 8 103 € |
| 2023 | 5 817 € | 7 857 € |
| 2024 | 5 406 € | 7 095 € |
| 2025 | 5 328 € | 6 764 € |

Quartiles 2025 :

| Type | P25 | Médiane | P75 |
|---|---|---|---|
| Appartement | 4 554 € | 5 328 € | 6 213 € |
| Maison | 5 449 € | 6 764 € | 8 058 € |

Écart entre les deux bureaux distributeurs (2021-2025, tous millésimes) :

| Code postal | Appartement | Maison |
|---|---|---|
| 94100 — Saint-Maur | 5 736 € | 7 463 € |
| 94210 — La Varenne-Saint-Hilaire | 6 021 € | 7 636 € |

Ces chiffres sont **calculés sur les données importées**, pas repris d'une source extérieure : ils
évolueront à chaque publication DVF. Le millésime 2025 est le dernier complet disponible au moment
de la rédaction.

---

## Comprendre le marché local

Quelques repères pour lire le dashboard de Saint-Maur-des-Fossés :

- **Un marché de maisons anciennes.** Sur la période, les appartements représentent environ 70 %
  des ventes de logements, mais les maisons pèsent lourd en valeur. Le neuf (VEFA) concerne
  presque uniquement les appartements.
- **Deux quartiers, deux marchés.** 94100 (Saint-Maur, autour du centre et du RER A) et 94210
  (La Varenne, plus à l'est) affichent un écart de prix de l'ordre de 3 à 5 % en faveur de
  La Varenne. L'écart mérite d'être suivi : il se resserre ou s'ouvre selon les programmes neufs.
- **Un marché qui a refroidi après 2022.** Le pic de prix est atteint en 2022 ; 2024 et 2025 sont
  en repli. Le momentum du dashboard permet de repérer le moment où cette baisse s'arrête — c'est
  précisément l'information qu'une série annuelle ne montre pas.
- **Une saisonnalité marquée.** Les ventes se concentrent au printemps et à l'automne. Un mois de
  février faible n'est pas un signal de retournement.

---

## Forces du jeu de données

### Exhaustivité

DVF couvre **toutes** les transactions à titre onéreux enregistrées par l'administration fiscale,
pas seulement celles publiées par les agences ou les réseaux de notaires. Aucun biais de
sélection commercial.

### Finesse géographique

Grâce à la version géolocalisée, chaque vente est rattachée à une parcelle cadastrale, donc à une
rue, à un quartier. C'est ce qui permet la carte des transactions du dashboard.

### Gratuité et absence de restriction

Pas de clé d'API, pas de quota, pas d'inscription. Licence Ouverte 2.0 : réutilisation libre, y
compris commerciale, avec mention de la source.

### Historique et régularité

Les publications d'avril et d'octobre sont attendues et documentées. Les millésimes antérieurs
sont corrigés au fil des campagnes : l'historique s'améliore avec le temps.

### Comparabilité nationale

Les mêmes colonnes existent pour toutes les communes de France (hors Mayotte et Alsace-Moselle
pour certaines périodes). Un projet équivalent peut être déployé ailleurs en changeant un code
INSEE.

---

## Limites et précautions

| Limite | Conséquence pratique |
|---|---|
| **Pas de données sur l'état du bien** | Impossible d'expliquer un écart de prix entre deux ventes |
| **Surface cadastrale, pas Carrez** | Non comparable aux prix d'annonce |
| **Ventes non notariées absentes** | Mutations gratuites, ventes de gré à gré non couvertes |
| **Quote-parts non signalées** | Des prix aberrants subsistent malgré les filtres |
| **Environ 25 % d'actes inexploitables pour le prix au m²** | Les séries reposent sur ~1 000 à 1 200 ventes par an, pas sur 1 800 |
| **Délai de publication** | Le dernier mois est partiel ; la vente la plus récente a plusieurs mois |
| **Millésimes antérieurs à 2021 non disponibles dans `latest`** | Profondeur d'historique limitée à cinq ans, mais croissante |
| **Anonymisation** | Aucune information sur l'acquéreur, le vendeur, le financement |
| **Adresses déclaratives** | Quelques numéros manquants ou mal orthographiés, d'où le regroupement par voie |

### Ce que DVF ne permet pas de faire

- **Estimer un bien précis.** DVF donne des transactions passées, pas une expertise : l'état du
  bien, l'étage, l'exposition et les travaux ne sont pas connus.
- **Conclure sur un mois isolé.** 20 à 40 ventes par mois pour un type de bien : c'est peu.
- **Suivre le marché locatif.** DVF ne concerne que les mutations à titre onéreux (ventes).
- **Prédire.** DVF est un jeu d'observations, pas un modèle. La tendance décrit où l'on en est,
  pas où l'on va.

---

## Vocabulaire

| Terme | Définition |
|---|---|
| **Acte / mutation** | Enregistrement juridique d'une transaction immobilière ; identifié par `id_mutation` |
| **Adjudication** | Vente aux enchères (judiciaire ou volontaire) |
| **Carrez** | Surface légale d'un lot de copropriété, utilisée dans les annonces |
| **Dépendance** | Cave, garage, parking, grenier rattaché au logement |
| **DVF** | Demandes de valeurs foncières — publication open data du fichier des mutations immobilières |
| **geo-dvf** | Version géolocalisée et normalisée de DVF, produite par Etalab |
| **Hypertable** | Table PostgreSQL partitionnée automatiquement par intervalles de temps (TimescaleDB) |
| **Médiane** | Valeur qui coupe une distribution en deux : 50 % au-dessous, 50 % au-dessus |
| **Momentum** | Ici : rapport entre le prix lissé sur 3 mois et le prix lissé sur 12 mois |
| **Mutation** | Voir *acte* |
| **P25 / P75** | Premier et troisième quartiles : bornes du « cœur » de la distribution |
| **Quote-part** | Fraction indivise d'un bien (divorce, succession) — source de prix aberrants |
| **VEFA** | Vente en l'état futur d'achèvement — achat sur plan, comptabilisé comme « neuf » |
| **Valeur foncière** | Prix déclaré dans l'acte, base des droits de mutation |

---

## Ressources pour aller plus loin

- [DVF géolocalisées sur data.gouv.fr](https://www.data.gouv.fr/datasets/demandes-de-valeurs-foncieres-geolocalisees)
- [Fichiers geo-dvf (CSV par commune)](https://files.data.gouv.fr/geo-dvf/latest/csv/)
- [Visualiseur DVF d'Etalab](https://app.dvf.etalab.gouv.fr/)
- [Foire aux questions DVF (PDF)](https://static.data.gouv.fr/resources/demandes-de-valeurs-foncieres/20221017-153257/faq-20221017.pdf)
- [Notice descriptive du fichier DVF (PDF)](https://static.data.gouv.fr/resources/demandes-de-valeurs-foncieres/20221017-153319/notice-descriptive-du-fichier-dvf-20221017.pdf)
- [Cerema — Datafoncier](https://datafoncier.cerema.fr/)
- [INSEE — Saint-Maur-des-Fossés (94068)](https://www.insee.fr/fr/statistiques/7725600?geo=COM-94068)
