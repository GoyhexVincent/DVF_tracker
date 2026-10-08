pipeline {
    agent any

    options {
        buildDiscarder(logRotator(numToKeepStr: "12"))   // un an d'historique de builds
        // L'import DVF est léger (5 fichiers de ~1 Mo, quelques milliers de
        // lignes) : 15 minutes suffisent largement, même sur Raspberry Pi.
        timeout(time: 15, unit: "MINUTES")
        disableConcurrentBuilds()
    }

    parameters {
        booleanParam(
            name: "FORCE_IMPORT",
            defaultValue: false,
            description: "Force la réimportation même si le garde-fou de fréquence " +
                         "(MIN_IMPORT_INTERVAL_HOURS = 600 h) s'y oppose."
        )
    }

    // ⚠️  Jenkins n'applique le bloc `triggers` d'un Jenkinsfile qu'à partir
    //     du PROCHAIN build du job. Tant qu'aucun build n'a eu lieu depuis la
    //     modification, l'ancien planning reste actif. De plus, si le job est
    //     défini en « Pipeline script » (script inline) au lieu de
    //     « Pipeline script from SCM », ce fichier est purement ignoré : le
    //     planning doit alors être corrigé dans la configuration du job
    //     (Build Triggers → Build periodically).
    //
    //     Ce job est SÉPARÉ de celui de NDBC_tracker : cadence différente
    //     (mensuelle contre quotidienne), périmètre différent. Les deux
    //     partagent en revanche la même base, le même utilisateur `pi` et le
    //     même identifiant Jenkins `pg-password-marine`.
    triggers {
        cron("H 5 1 * *")        // le 1er de chaque mois, vers 5 h du matin
    }

    environment {
        PGHOST     = "localhost"
        PGPORT     = "5432"
        PGDATABASE = "marine_database"
        PGUSER     = "pi"
        PGPASSWORD = credentials("pg-password-marine")

        // ── Garde-fou de fréquence ──
        // 600 h ≈ 25 jours : même si le job est déclenché plusieurs fois dans
        // le mois (webhook, build manuel, trigger résiduel), l'import ne
        // s'exécute qu'une fois. FORCE_IMPORT=1 pour passer outre.
        MIN_IMPORT_INTERVAL_HOURS = "600"

        // Premier millésime disponible dans le dossier `latest` de geo-dvf.
        // Les millésimes absents (404) sont ignorés automatiquement, et un
        // nouveau millésime publié est récupéré sans modification du code.
        DVF_START_YEAR = "2021"

        // Bornes de plausibilité du prix au m² (€/m²), utilisées pour marquer
        // les transactions non exploitables (quote-part, lot isolé, valeur
        // symbolique). Les transactions restent en base, seuls les prix sont
        // exclus des séries.
        DVF_PRIX_M2_MIN = "500"
        DVF_PRIX_M2_MAX = "30000"
        DVF_SURFACE_MIN = "9"

        // ── Seuils du contrôle de fraîcheur ──
        MAX_IMPORT_AGE_DAYS   = "35"    // le job mensuel doit avoir tourné
        MAX_MUTATION_AGE_DAYS = "400"   // DVF est publié avec ~10 mois de retard
    }

    stages {
        stage("Setup Python Environment") {
            steps {
                sh """
                    python3 -m venv .venv
                    . .venv/bin/activate
                    pip install --upgrade pip
                    pip install -r requirements.txt
                """
            }
        }

        stage("Run DVF Import") {
            steps {
                sh """
                    . .venv/bin/activate
                    FORCE_IMPORT=${params.FORCE_IMPORT ? '1' : '0'} python3 DVF_data.py
                """
            }
        }

        stage("Verify DVF Freshness") {
            steps {
                sh """
                    . .venv/bin/activate
                    python3 verify_dvf_freshness.py
                """
            }
        }

        stage("Run Unit Tests") {
            steps {
                sh """
                    . .venv/bin/activate
                    python3 -m unittest -v test_dvf_etl
                """
            }
        }
    }

    post {
        always {
            cleanWs(cleanWhenNotBuilt: false, deleteDirs: true)
        }
        success {
            echo "✅ Marché immobilier DVF (Saint-Maur-des-Fossés) mis à jour."
        }
        failure {
            echo "❌ Import DVF en échec : consulter les logs du stage rouge " +
                 "et vérifier l'accès à files.data.gouv.fr."
        }
    }
}
