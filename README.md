# DockPage

Tableau de bord web alimenté par l'API de Portainer : il liste automatiquement les conteneurs
de tous tes environnements (serveur, NAS…) avec un lien cliquable vers chacun, et reste personnalisable
(nom, lien, groupe, icône, masquage). Aucune dépendance : Python (bibliothèque standard) dans un conteneur.

## Installation (Portainer)

L'image est construite et publiée automatiquement par GitHub Actions sur `ghcr.io/kamegrazi56/dockpage:latest`
à chaque modification de `app.py` ou du `Dockerfile`.

1. Dans Portainer : *My account* → *Access tokens* → créer un jeton.
2. *Stacks* → *Add stack* → *Web editor* → coller le contenu de `docker-compose.yml`, puis adapter :

| Variable | Rôle |
|---|---|
| `PORTAINER_URL` | Adresse de Portainer (ex. `https://192.168.1.210:9443`) |
| `PORTAINER_TOKEN` | Jeton d'accès Portainer (à saisir dans Portainer, jamais dans le dépôt) |
| `HOSTS` | `NomEnvironnement=IP,…` : adresse utilisée pour les liens de chaque environnement |
| `VERIFY_TLS` | `false` pour un certificat auto-signé |
| `TITLE` | Titre de la page |
| `REFRESH_SECONDS` | Intervalle de rafraîchissement (30 par défaut) |

3. Ouvrir `http://IP_DU_SERVEUR:8088`.

## Interface

- Accueil avec salutation, résumé de l'état des services (en marche, à surveiller, arrêtés) et statuts en français.
- Sections repliables (l'état est mémorisé dans le navigateur) et recherche rapide (touche `/` pour y accéder, `Échap` pour l'effacer).
- Thème clair ou sombre : suit le système par défaut, modifiable avec le bouton en haut à droite (choix mémorisé).
- Messages d'erreur explicites (jeton refusé, Portainer injoignable, environnement hors ligne).
- Le bouton **Actualiser** relit Portainer immédiatement (sinon, la lecture automatique a lieu toutes les `REFRESH_SECONDS` secondes, 30 par défaut).
- Toute la personnalisation (catégories, environnements, ordre, icônes) est regroupée derrière le bouton **Personnaliser**.

## Environnements (serveur, NAS…)

Le bouton **Environnements** liste ceux détectés dans Portainer, avec l'adresse actuellement utilisée pour les liens
et sa provenance. Pour chacun, on peut définir un nom affiché et une adresse IP (ou nom d'hôte). Ces réglages sont
prioritaires sur la variable `HOSTS`, qui reste utilisable comme valeur par défaut.

Pour que Portainer puisse télécharger l'image sans identifiants, le paquet doit être public
(GitHub → profil → *Packages* → `dockpage` → *Package settings* → *Change visibility*).

## Catégories

- Le bouton **Catégories** permet de créer, renommer et supprimer des catégories (par exemple Média, IA, Outils).
- Un conteneur se range dans une catégorie avec **Personnaliser** → *Modifier* → menu « Catégorie », ou en glissant sa carte
  sur une catégorie (même vide). Glisser une carte sur un groupe d'environnement la retire de sa catégorie.
- Les conteneurs sans catégorie restent regroupés par environnement. Une étiquette indique l'environnement d'un conteneur rangé.
- Supprimer une catégorie remet ses conteneurs dans leur environnement. Les groupes saisis avec une ancienne version
  sont repris automatiquement comme catégories.

## Ordre d'affichage et icônes

- **Personnaliser** active le mode édition : chaque carte peut être déplacée par glisser-déposer ou avec les flèches ◀ ▶,
  et les groupes avec les flèches ▲ ▼. L'ordre est enregistré dans `/data/config.json`.
- **Icônes automatiques** : elles sont déduites du nom de l'image (puis du nom du conteneur) et téléchargées depuis la
  collection libre [dashboard-icons](https://github.com/homarr-labs/dashboard-icons), puis conservées dans `/data/icons`.
  Le conteneur a donc besoin d'accéder à `raw.githubusercontent.com` (modifiable avec la variable `ICON_BASE`).
- Dans *Modifier*, le champ « Icône » accepte un nom de la collection (ex. `jellyfin`), une URL d'image ou un emoji.
  Vide, la détection automatique s'applique ; sans icône trouvée, un 🐳 est affiché.

## Sécurité

Le tableau de bord n'a pas d'authentification : à garder sur le réseau local, sans l'exposer sur Internet.
Le jeton Portainer donne un large accès : le stocker uniquement dans les variables de la stack.
Les réglages de personnalisation sont enregistrés dans `/data/config.json`.
