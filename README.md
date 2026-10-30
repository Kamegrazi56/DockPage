# DockPage

Tableau de bord web alimenté par l'API de Portainer : il liste automatiquement les conteneurs
de tous tes environnements (serveur, NAS…) avec un lien cliquable vers chacun, et reste personnalisable
(nom, lien, groupe, icône, masquage). Aucune dépendance : Python (bibliothèque standard) dans un conteneur.

## Installation (Portainer, depuis ce dépôt)

1. Dans Portainer : *My account* → *Access tokens* → créer un jeton.
2. *Stacks* → *Add stack* → nom `dockpage` → méthode **Repository** :
   - Repository URL : `https://github.com/Kamegrazi56/DockPage`
   - Reference : `refs/heads/main`
   - Compose path : `docker-compose.yml`
   - (dépôt privé : activer *Authentication* et renseigner un jeton GitHub)
3. Dans *Environment variables*, ajouter :

| Variable | Rôle |
|---|---|
| `PORTAINER_URL` | Adresse de Portainer (ex. `https://192.168.1.210:9443`) |
| `PORTAINER_TOKEN` | Jeton d'accès Portainer (à saisir ici, jamais dans le dépôt) |
| `HOSTS` | `NomEnvironnement=IP,…` : adresse utilisée pour les liens de chaque environnement |
| `VERIFY_TLS` | `false` (défaut) pour un certificat auto-signé |
| `TITLE` | Titre de la page (défaut `Homelab`) |
| `DOCKPAGE_PORT` | Port d'accès (défaut `8088`) |

4. *Deploy the stack* (l'image est construite à partir du `Dockerfile`), puis ouvrir `http://IP_DU_SERVEUR:8088`.

## Sécurité

Le tableau de bord n'a pas d'authentification : à garder sur le réseau local, sans l'exposer sur Internet.
Le jeton Portainer donne un large accès : le stocker uniquement dans les variables de la stack.
Les réglages de personnalisation sont enregistrés dans `/data/config.json`.
