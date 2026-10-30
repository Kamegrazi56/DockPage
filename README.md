# DockPage

Tableau de bord web alimenté par l'API de Portainer : il liste automatiquement les conteneurs
de tous tes environnements (serveur, NAS…) avec un lien cliquable vers chacun, et reste personnalisable
(nom, lien, groupe, icône, masquage). Aucune dépendance : Python (bibliothèque standard) dans un conteneur.

## Installation

1. Dans Portainer : *My account* → *Access tokens* → créer un jeton.
2. Sur le serveur :
   ```bash
   sudo mkdir -p /opt/portainer-dashboard/data
   ```
   puis copier `app.py` dans `/opt/portainer-dashboard/app.py`.
3. Créer une stack Portainer avec `docker-compose.yml` et adapter les variables :

| Variable | Rôle |
|---|---|
| `PORTAINER_URL` | Adresse de Portainer (ex. `https://192.168.1.210:9443`) |
| `PORTAINER_TOKEN` | Jeton d'accès Portainer (ne jamais le commiter) |
| `VERIFY_TLS` | `false` pour un certificat auto-signé |
| `HOSTS` | `NomEnvironnement=IP,…` : adresse utilisée pour les liens de chaque environnement |
| `TITLE` | Titre de la page |
| `REFRESH_SECONDS` | Intervalle de rafraîchissement (30 par défaut) |

4. Ouvrir `http://IP_DU_SERVEUR:8088`.

## Sécurité

Le tableau de bord n'a pas d'authentification : à garder sur le réseau local, sans l'exposer sur Internet.
Le jeton Portainer donne un large accès : le stocker uniquement dans les variables de la stack.
Les réglages de personnalisation sont enregistrés dans `/data/config.json`.
