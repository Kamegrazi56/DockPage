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
| `UPDATE_CHECK_SECONDS` | Intervalle de vérification des images (3600 par défaut, minimum 300 secondes) |
| `UBUNTU_STATUS_PATH` | Fichier de relevé APT partagé avec l’hôte (`/data/ubuntu-updates.json` par défaut) |

3. Ouvrir `http://IP_DU_SERVEUR:8088`.

## Interface

- Présentation « Lanceur et détails » : fond clair chaleureux (ou thème sombre), icônes sans tuiles et panneau de détails. Cliquer sur une icône sélectionne le service ; **Ouvrir** dans le panneau ouvre son lien dans un nouvel onglet. Sur petit écran, les détails passent au-dessus de la liste. **✕** ou `Échap` ferme le panneau.
- Filtres par groupe et par environnement, combinables avec la recherche. La sélection est conservée lors des rafraîchissements, et fermée si le service disparaît des résultats.
- Les états du conteneur et du service restent séparés sous chaque icône. Versions, dates, ports, contrôles et mises à jour sont regroupés dans le panneau.
- Accueil avec salutation, résumé de l'état des services (en marche, à surveiller, arrêtés) et statuts en français.
- Sections repliables (l'état est mémorisé dans le navigateur) et recherche rapide (touche `/` pour y accéder, `Échap` pour l'effacer).
- Thème clair ou sombre : suit le système par défaut, modifiable avec le bouton en haut à droite (choix mémorisé).
- Messages d'erreur explicites (jeton refusé, Portainer injoignable, environnement hors ligne).
- Le bouton **Actualiser** relit Portainer immédiatement (sinon, la lecture automatique a lieu toutes les `REFRESH_SECONDS` secondes, 30 par défaut).
- Le bouton **Réglages** du panneau ouvre les réglages du service sélectionné : modifier « Lien à ouvrir », puis **Enregistrer** pour changer l'adresse de redirection. Laisser le champ vide rétablit le lien détecté automatiquement.
- Toute la personnalisation (catégories, environnements, ordre, icônes) est regroupée derrière le bouton **Personnaliser**.

## Environnements (serveur, NAS…)

Le bouton **Environnements** liste ceux détectés dans Portainer, avec l'adresse actuellement utilisée pour les liens
et sa provenance. Pour chacun, on peut définir un nom affiché et une adresse IP (ou nom d'hôte). Ces réglages sont
prioritaires sur la variable `HOSTS`, qui reste utilisable comme valeur par défaut.

Pour que Portainer puisse télécharger l'image sans identifiants, le paquet doit être public
(GitHub → profil → *Packages* → `dockpage` → *Package settings* → *Change visibility*).

## Onglet Serveur Ubuntu

L’onglet **Serveur Ubuntu** indique si des mises à jour de paquets APT sont disponibles,
leur nombre, le nom des paquets concernés, leurs versions installées et proposées,
leur source APT et l’heure du dernier contrôle. Un champ de recherche permet de
retrouver un paquet dans la liste. Il ne lance aucune installation. Si le
relevé manque, est illisible, date de plus de 24 heures ou signale une erreur APT,
l’onglet affiche **Vérification impossible** au lieu de déclarer le serveur à jour.
Ce sont des paquets Ubuntu : certains sont des services, d’autres des bibliothèques
ou le noyau. La liste ne prétend pas identifier les services à redémarrer.
Le bouton **Relire** charge le dernier relevé ; il ne déclenche pas un contrôle APT.
L’onglet affiche aussi les commandes `sudo apt update` et `sudo apt upgrade` à saisir
dans le terminal du serveur après vérification d’une sauvegarde du serveur. La seconde commande
demande confirmation et peut redémarrer des services. DockPage affiche ces commandes
comme aide ; il ne les exécute pas.
Un ancien relevé contenant seulement le nombre reste lisible, mais il faut relancer
le nouveau script sur l’hôte pour obtenir le détail des paquets.

Portainer ne fournit pas l’état des paquets du système hôte. Sur le serveur Ubuntu,
installer le script `ubuntu_status.py` et le lancer régulièrement. Le chemin ci-dessous
correspond au volume de données de `docker-compose.yml` ; l’adapter si votre stack
utilise un autre dossier :

```sh
sudo install -m 755 ubuntu_status.py /usr/local/sbin/dockpage-ubuntu-status.py
sudo python3 /usr/local/sbin/dockpage-ubuntu-status.py /home/kamegrazi/docker/dockpage/data/ubuntu-updates.json
sudo crontab -e
```

Ajouter cette ligne à la crontab de `root` pour un contrôle toutes les six heures :

```cron
0 */6 * * * /usr/bin/python3 /usr/local/sbin/dockpage-ubuntu-status.py /home/kamegrazi/docker/dockpage/data/ubuntu-updates.json >> /var/log/dockpage-ubuntu-status.log 2>&1
```

Le script exécute `apt-get update`, puis compte les paquets signalés par
`apt list --upgradable`. Il écrit la date, le nombre et la liste des paquets (ou une erreur)
dans le fichier partagé, sans installer de paquets. Il faut que ce dossier soit
monté dans DockPage sous `/data`, comme dans la stack fournie. Le contrôle concerne
l’hôte sur lequel le script s’exécute, et non les autres environnements Portainer.
Après une mise à jour du code de DockPage, relancer les deux commandes `sudo install`
et `sudo python3` ci-dessus pour remplacer le script déjà installé et produire
un nouveau relevé ; la ligne de la crontab ne change pas.

## Catégories

- Le bouton **Catégories** permet de créer, renommer et supprimer des catégories (par exemple Média, IA, Outils).
- Un conteneur se range dans une catégorie avec **Personnaliser** → *Modifier* → menu « Catégorie », ou en glissant son icône en mode **Personnaliser**
  sur une catégorie (même vide). Glisser une icône sur un groupe d'environnement la retire de sa catégorie.
- Les conteneurs sans catégorie restent regroupés par environnement. Une étiquette indique l'environnement d'un conteneur rangé.
- Supprimer une catégorie remet ses conteneurs dans leur environnement. Les groupes saisis avec une ancienne version
  sont repris automatiquement comme catégories.

## Ordre d'affichage et icônes

- **Personnaliser** active le mode édition : chaque service peut être déplacé par glisser-déposer ou avec les flèches ◀ ▶,
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

## Versions des images et mises à jour manuelles

Le panneau de détails indique la **Version** déclarée par le label `org.opencontainers.image.version`
(ou `org.label-schema.version`). À défaut, elle affiche le tag et l'identifiant abrégé de
l'image utilisée : un tag comme `latest` n'est pas un numéro de version.
La **Dernière mise à jour observée** indique quand DockPage a constaté un changement
d'identifiant d'image, y compris un retour à une ancienne image. La date exacte de l'opération
reste inconnue : un changement survenu pendant un arrêt sera daté à sa découverte.
La première découverte affiche « Indisponible », sans inventer d'historique.
Une recréation avec la même image ou un simple redémarrage ne change pas cette information.
Les observations sont conservées à côté de la configuration dans `config.json.images.json`
(`CONFIG_PATH` suivi de `.images.json`), dans le volume persistant. Elles sont associées
à l'identifiant d'environnement et au nom du conteneur ; un renommage crée un nouveau suivi.
Une erreur de lecture ou d'écriture de ce fichier est signalée sans publier une nouvelle
observation non sauvegardée. Ne pas partager ce fichier entre plusieurs instances DockPage.
Le **Dernier déploiement**, affiché séparément, correspond à la création du conteneur actuel :
une recréation, même avec la même image, le change. Les dates utilisent le fuseau du navigateur.

Le panneau de détails affiche **Image à jour**, **Mise à jour disponible**, **Image figée** ou
**Vérification impossible**. Survoler l'indicateur donne le détail et la date de vérification.
DockPage compare le digest de l'image utilisée par le conteneur avec celui du **tag configuré**
dans le registre : `nginx:1.26` reste sur `1.26`, sans rechercher une nouvelle version majeure.
Les vérifications sont mises en cache pendant `UPDATE_CHECK_SECONDS`, même avec **Actualiser**.
Les images locales sans digest et les registres privés non accessibles restent en état inconnu ;
aucune mise à jour n'est déclenchée dans ce cas.

Le panneau de détails possède un bouton **Mettre à jour**, actif lorsqu'une nouvelle image est
confirmée et que le conteneur est pris en charge. Une confirmation rappelle l'interruption
du service et la perte des données hors volumes. DockPage demande alors à Portainer de
télécharger le **tag actuel** et de recréer ce seul conteneur avec ses paramètres existants.
Le nom de la carte et ses réglages restent associés au nom du conteneur.
Le bouton indique l'opération en cours, puis son résultat ou son erreur dans le panneau de détails.
La liste reflète le nouveau conteneur au prochain rafraîchissement.

Les mises à jour automatiques sont supprimées : les anciens réglages `autoUpdate`
éventuellement présents sont ignorés et disparaissent à la prochaine sauvegarde de la carte.
Les vérifications périodiques du registre restent actives, sans déclencher de recréation.
La demande manuelle revérifie la cible et le registre avant d'agir. Une opération simultanée
est refusée ; si une lecture de Portainer est en cours, réessayer après son achèvement.
Après l'envoi d'une demande de recréation, une deuxième demande pour le même identifiant est
refusée jusqu'au redémarrage de DockPage, même si la réponse a été perdue. Aucun nouvel essai
automatique n'a lieu : vérifier d'abord le résultat dans Portainer.

Les conteneurs arrêtés, images figées par digest, services Swarm, DockPage et Portainer/agents
ne sont pas recréés par ce bouton. La raison du blocage est affichée dans le panneau de détails.
Un réseau `container:…` est également refusé après inspection.
Pour ces cas, utiliser Portainer ou l'outil de déploiement. Pour une stack Compose, l'opération est
individuelle : elle ne modifie pas le fichier Compose et ne coordonne pas les services dépendants.
La fonctionnalité nécessite que la version de Portainer expose son action de recréation et que
le jeton dispose des droits correspondants ; sinon la tuile affiche l'échec.

Références techniques : [proxy Docker de Portainer](https://docs.portainer.io/api/examples),
[action de recréation Portainer](https://github.com/portainer/portainer/blob/develop/api/http/handler/docker/containers/recreate.go),
[lecture du digest distant Docker](https://docs.docker.com/reference/api/engine/version/v1.57/operations/DistributionInspect/).

Tests locaux (sans accès à un Docker réel) : `python3 -m unittest -v test_app`.

## Surveillance de disponibilité

Dans **Icône du service → Réglages → Surveillance du service**, choisir HTTP / HTTPS ou TCP, puis enregistrer.
Le contrôle est désactivé par défaut. L'adresse peut être distincte du lien de la carte
(par exemple `https://service/health` ou `tcp://192.168.1.210:5432`). Vide, elle reprend
le lien de la carte, sinon le premier port détecté ; en TCP, son hôte et son port sont utilisés.

DockPage effectue les contrôles depuis son serveur même lorsque le navigateur est fermé,
avec au plus huit contrôles en parallèle. Une réponse HTTP finale 200–399 après redirection,
ou une connexion TCP réussie, indique **En ligne**. Les erreurs HTTP (dont 401/403), réseau,
certificat HTTPS et délais dépassés indiquent **Hors ligne**. Le délai réseau est de cinq secondes ;
les certificats HTTPS sont vérifiés. Le jeton Portainer n'est jamais envoyé à ces services.

Le panneau de détails affiche la latence, les 60 derniers contrôles et leur pourcentage de réussite
(ce n'est pas une disponibilité calculée sur 24 heures). Survoler une barre affiche sa date et
son résultat. La date et le détail du dernier contrôle (réponse HTTP ou erreur), l'intervalle
et l'heure prévue du prochain contrôle sont visibles dans le panneau de détails. Les cartes
surveillées restent visibles même sans lien à ouvrir. Un résultat ancien repasse en attente.
L'état du conteneur reste affiché séparément :
un conteneur en marche peut héberger un service inaccessible. Le résumé signale aussi les échecs
pour les services surveillés dont l'environnement Portainer est indisponible.

Dans **Icône du service → Réglages → Intervalle de vérification (secondes)**, régler chaque service de 10 à 86400 secondes.
Laisser le champ vide utilise `MONITOR_INTERVAL_SECONDS` (60 secondes par défaut, minimum 10).
Chaque service est contrôlé indépendamment lorsque son intervalle depuis le dernier résultat
est écoulé ; changer l'intervalle conserve l'historique. Le serveur recherche les contrôles dus
chaque seconde, avec un délai possible lorsque les contrôles précédents sont encore en cours.
Le premier contrôle est lancé dès le prochain passage ; le navigateur relit les résultats toutes
les 5 secondes. **Actualiser** relit Portainer, sans forcer un contrôle de disponibilité.
Les réglages sont persistants, mais l'historique est conservé en mémoire et repart à zéro après
un redémarrage ou un changement de cible. Cette première version ne comprend ni notifications,
ni stockage longue durée, ni intégration avec une instance Uptime Kuma.

Tests de disponibilité et de non-régression sans Docker :
`python3 -m unittest -v test_app test_monitor`.

### Déploiement de test du code local

Le fichier autonome `compose.test.yml` construit le `Dockerfile` du dépôt pour le projet
`dockpage-grazi19-test`. Il utilise son propre volume nommé `test-data` et expose l'application
sur `http://127.0.0.1:18019` (port modifiable par `DOCKPAGE_TEST_PORT`). Il ne reprend ni le
volume de production ni la stack existante `dockpage-test`.

Le Gestionnaire Docker effectue le déploiement avec
`docker compose -f compose.test.yml up -d --build` depuis le dépôt.
Les variables `PORTAINER_URL`, `PORTAINER_TOKEN`, `VERIFY_TLS` et `HOSTS` peuvent être fournies
par l'environnement ou un fichier `.env` local ignoré par Git. Aucun jeton n'est inclus.
Sans accès Portainer configuré, le serveur et son API peuvent être testés, mais la liste
des conteneurs ne sera pas disponible. L'intervalle par défaut des contrôles de disponibilité
est de 10 secondes dans cette configuration de test.

Pour arrêter la stack en conservant ses réglages, le Gestionnaire Docker utilise
`docker compose -f compose.test.yml down` (sans suppression du volume).
