#!/usr/bin/env python3
"""Tableau de bord alimenté par l'API de Portainer (bibliothèque standard uniquement)."""
import json
import os
import re
import ssl
import socket
from concurrent.futures import ThreadPoolExecutor
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORTAINER_URL = os.environ.get("PORTAINER_URL", "").rstrip("/")
TOKEN = os.environ.get("PORTAINER_TOKEN", "")
VERIFY_TLS = os.environ.get("VERIFY_TLS", "true").lower() == "true"
REFRESH = int(os.environ.get("REFRESH_SECONDS", "30"))
UPDATE_INTERVAL = max(300, int(os.environ.get("UPDATE_CHECK_SECONDS", "3600")))
MONITOR_INTERVAL = max(10, int(os.environ.get("MONITOR_INTERVAL_SECONDS", "60")))
MONITOR_TIMEOUT = 5
PORT = int(os.environ.get("PORT", "8080"))
TITLE = os.environ.get("TITLE", "Homelab")
CONFIG_PATH = os.environ.get("CONFIG_PATH", "/data/config.json")
UBUNTU_STATUS_PATH = os.environ.get("UBUNTU_STATUS_PATH", "/data/ubuntu-updates.json")
UBUNTU_STATUS_MAX_AGE = 24 * 3600
# "NomEnvironnement=192.168.1.20,Autre=nas.local"
HOSTS = dict(p.split("=", 1) for p in os.environ.get("HOSTS", "").split(",") if "=" in p)
HTTPS_PORTS = (443, 8443, 9443)
# Collection d'icônes libre « dashboard-icons » (dossiers svg/ et png/), mise en cache dans /data/icons
ICON_BASE = os.environ.get("ICON_BASE", "https://raw.githubusercontent.com/homarr-labs/dashboard-icons/main").rstrip("/")
ICON_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,60}$")
GENERIC_OWNERS = {"library", "linuxserver", "lscr.io", "ghcr.io", "docker.io", "quay.io", "gcr.io"}

lock = threading.Lock()
cache = {"environments": [], "updated": None, "error": None}
versions = {}
update_attempts = {}
monitors = {}


def ubuntu_status():
    """Lit le relevé APT produit sur l'hôte, sans exécuter de commande système dans Docker."""
    try:
        with open(UBUNTU_STATUS_PATH, "r", encoding="utf-8") as f:
            report = json.load(f)
        if not isinstance(report, dict):
            raise ValueError("Format du relevé invalide")
        checked = report.get("checkedAt")
        if type(checked) not in (int, float) or not 0 < checked <= time.time() + 300:
            raise ValueError("Date du relevé invalide")
        if time.time() - checked > UBUNTU_STATUS_MAX_AGE:
            return {"state": "unknown", "detail": "Le dernier relevé a plus de 24 heures.", "checkedAt": checked}
        if report.get("error"):
            return {"state": "unknown", "detail": "La vérification APT a échoué.", "checkedAt": checked}
        count = report.get("count")
        if type(count) is not int or not 0 <= count <= 100000:
            raise ValueError("Nombre de mises à jour invalide")
        packages = report.get("packages")
        if packages is not None:
            fields = ("name", "installed", "candidate", "source", "architecture")
            if (not isinstance(packages, list) or len(packages) != count
                    or any(not isinstance(package, dict)
                           or any(type(package.get(field)) is not str
                                  or not package[field] or len(package[field]) > 300
                                  for field in fields)
                           for package in packages)):
                raise ValueError("Liste de mises à jour invalide")
        return {"state": "available" if count else "current", "count": count,
                "packages": packages, "checkedAt": checked}
    except FileNotFoundError:
        return {"state": "unknown", "detail": "Aucun relevé Ubuntu disponible. Configurer le contrôle sur le serveur."}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {"state": "unknown", "detail": "Le relevé Ubuntu est illisible ou invalide."}


def api(path, data=None):
    req = urllib.request.Request(PORTAINER_URL + "/api" + path,
                                 data=json.dumps(data).encode() if data is not None else None,
                                 headers={"X-API-Key": TOKEN, "Content-Type": "application/json"})
    ctx = None if VERIFY_TLS else ssl._create_unverified_context()
    with urllib.request.urlopen(req, timeout=300 if data is not None else 15, context=ctx) as r:
        return json.load(r)


def image_version(endpoint, c):
    """Compare l'image réellement utilisée au digest du tag distant, sans téléchargement."""
    image = c.get("Image", "")
    if "@" in image or image.startswith("sha256:") or not image:
        return {"state": "pinned", "detail": "Image figée par digest ou sans tag vérifiable."}
    key = (endpoint, c.get("ImageID"), image)
    now = time.time()
    if key in versions and now - versions[key][0] < UPDATE_INTERVAL:
        return dict(versions[key][1])
    try:
        base = "/endpoints/%s/docker" % endpoint
        local = api(base + "/images/%s/json" % urllib.parse.quote(c["ImageID"], safe=""))
        digests = {d.rsplit("@", 1)[-1] for d in local.get("RepoDigests") or []}
        if not digests:
            raise ValueError("Image locale sans digest de registre.")
        remote = api(base + "/distribution/%s/json" % urllib.parse.quote(image, safe=""))
        digest = remote.get("Descriptor", {}).get("digest")
        if not digest:
            raise ValueError("Le registre ne fournit pas de digest.")
        result = {"state": "current" if digest in digests else "available",
                  "detail": "Comparaison avec le tag configuré : " + image}
    except Exception as e:
        result = {"state": "unknown", "detail": "Vérification impossible : " + str(e)}
    result["checked"] = time.strftime("%d/%m %H:%M")
    versions[key] = (now, result)
    return dict(result)


def deployed_version(c):
    """Version déclarée, sinon référence identifiable sans consulter le registre."""
    labels = c.get("Labels") or {}
    declared = labels.get("org.opencontainers.image.version") or labels.get("org.label-schema.version")
    if declared:
        return declared
    image = c.get("Image") or ""
    image_id = c.get("ImageID") or (image.split("@", 1)[-1] if "@" in image or image.startswith("sha256:") else "")
    identity = image_id[:19] if image_id.startswith("sha256:") else image_id
    tag = image.rsplit("/", 1)[-1].rsplit(":", 1)[-1] if ":" in image.rsplit("/", 1)[-1] else "latest"
    if "@" in image or image.startswith("sha256:") or not image:
        return identity or "Indisponible"
    return " · ".join(part for part in ("Tag " + tag, identity) if part)


def update_reason(c):
    image = c.get("Image", "").lower()
    labels = c.get("Labels") or {}
    if "@" in image or image.startswith("sha256:") or not image:
        return "Image figée : modifier sa référence dans Portainer."
    if "com.docker.swarm.service.id" in labels:
        return "Service Swarm : mettre à jour la stack dans Portainer."
    if "portainer" in image or "dockpage" in image or (
            os.environ.get("HOSTNAME") and c.get("Id", "").startswith(os.environ["HOSTNAME"])):
        return "Mettre à jour DockPage et Portainer depuis un outil externe."
    if c.get("State") != "running":
        return "Mise à jour réservée aux conteneurs en marche."
    return ""


def manual_update(endpoint, container_id):
    """Action explicite uniquement ; relit la cible et interdit les doubles demandes."""
    if type(endpoint) is not int or endpoint < 1 or not isinstance(container_id, str) or not re.fullmatch(r"[a-zA-Z0-9]+", container_id):
        raise ValueError("Conteneur ou environnement invalide.")
    if not refresh_lock.acquire(blocking=False):
        raise ValueError("Une opération est en cours. Réessayer après son achèvement.")
    try:
        containers = api("/endpoints/%s/docker/containers/json?all=1" % endpoint)
        c = next((c for c in containers if c["Id"] == container_id), None)
        if c is None:
            raise ValueError("Le conteneur a changé ou a disparu. Actualiser la page.")
        reason = update_reason(c)
        if reason:
            raise ValueError(reason)
        attempt_key = (endpoint, container_id)
        if attempt_key in update_attempts:
            raise ValueError("Une demande a déjà été envoyée pour ce conteneur. Vérifier son état dans Portainer avant toute autre intervention.")
        versions.pop((endpoint, c.get("ImageID"), c.get("Image", "")), None)
        version = image_version(endpoint, c)
        if version["state"] != "available":
            raise ValueError("Aucune mise à jour confirmée : " + version["detail"])
        inspected = api("/endpoints/%s/docker/containers/%s/json" % (endpoint, container_id))
        if not inspected.get("State", {}).get("Running"):
            raise ValueError("Le conteneur n'est plus en marche.")
        if inspected.get("HostConfig", {}).get("NetworkMode", "").startswith("container:"):
            raise ValueError("Réseau partagé avec un autre conteneur : utiliser Portainer.")
        # Une réponse perdue peut cacher une recréation réussie : ne jamais relancer automatiquement.
        update_attempts[attempt_key] = time.time()
        try:
            api("/docker/%s/containers/%s/recreate" % (endpoint, container_id), {"PullImage": True})
        except Exception as error:
            raise ValueError("Résultat de mise à jour incertain : %s. Vérifier le conteneur dans Portainer." % error)
        versions.pop((endpoint, c.get("ImageID"), c.get("Image", "")), None)
        return {"message": "Recréation effectuée par Portainer. La liste sera actualisée au prochain cycle."}
    finally:
        refresh_lock.release()


def validate_monitor(data):
    interval = data.get("monitorInterval")
    if interval is not None and (type(interval) is not int or not 10 <= interval <= 86400):
        raise ValueError("Intervalle de surveillance : entier entre 10 et 86400 secondes")
    mode = data.get("monitor", "")
    if mode not in ("", "http", "tcp"):
        raise ValueError("Surveillance : choisir HTTP, TCP ou désactivée")
    target = data.get("monitorUrl", "")
    if not isinstance(target, str):
        raise ValueError("Adresse de surveillance invalide")
    if not target or not mode:
        return
    parsed = urllib.parse.urlsplit(target)
    if (parsed.scheme not in (("tcp",) if mode == "tcp" else ("http", "https"))
            or not parsed.hostname or parsed.username or parsed.password
            or (mode == "tcp" and (not parsed.port or parsed.path or parsed.query or parsed.fragment))):
        raise ValueError("Adresse attendue : http(s)://hôte:port ou tcp://hôte:port")
    parsed.port  # vérifie aussi la plage et le format du port HTTP


def probe_service(mode, target):
    started = time.monotonic()
    try:
        validate_monitor({"monitor": mode, "monitorUrl": target})
        if not target:
            raise ValueError("Aucune adresse : renseigner une cible dans les réglages")
        if mode == "tcp":
            parsed = urllib.parse.urlsplit(target)
            with socket.create_connection((parsed.hostname, parsed.port), timeout=MONITOR_TIMEOUT):
                pass
            detail = "Connexion TCP établie"
        else:
            # Ne transmet jamais le jeton Portainer aux services surveillés.
            req = urllib.request.Request(target, headers={"User-Agent": "DockPage-monitor"})
            with urllib.request.urlopen(req, timeout=MONITOR_TIMEOUT) as response:
                detail = "HTTP %s" % response.status
                if not 200 <= response.status < 400:
                    raise ValueError(detail)
        return {"state": "up", "detail": detail, "ms": round((time.monotonic() - started) * 1000)}
    except Exception as error:
        if isinstance(error, urllib.error.HTTPError):
            error.close()
        return {"state": "down", "detail": str(error), "ms": None}


def monitor_targets():
    items = load_items()
    with lock:
        containers = {c["key"]: c for env in cache["environments"] for c in env["containers"]}
    targets = {}
    for key, item in items.items():
        mode = item.get("monitor")
        if mode not in ("http", "tcp"):
            continue
        ports = containers.get(key, {}).get("ports", [])
        target = item.get("monitorUrl") or item.get("url") or (ports[0]["url"] if ports else "")
        if mode == "tcp" and not item.get("monitorUrl") and target:
            try:
                parsed = urllib.parse.urlsplit(target)
                if parsed.scheme not in ("http", "https") or not parsed.hostname:
                    raise ValueError("Lien sans hôte HTTP")
                target = "tcp://%s:%s" % (parsed.hostname if ":" not in parsed.hostname else "[" + parsed.hostname + "]",
                                             parsed.port or (443 if parsed.scheme == "https" else 80))
            except ValueError:
                target = ""  # Une cible invalide ne bloque pas les autres services.
        targets[key] = (mode, target)
    return targets


def check_monitors():
    targets = monitor_targets()
    items = load_items()
    with lock:
        targets = {key: pair for key, pair in targets.items()
                   if (monitors.get(key, {}).get("mode"), monitors.get(key, {}).get("target")) != pair
                   or time.time() >= monitors[key]["checked"] + items.get(key, {}).get("monitorInterval", MONITOR_INTERVAL)}
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda pair: probe_service(*pair), targets.values()))
    current = monitor_targets()
    with lock:
        for key in list(monitors):
            if key not in current:
                del monitors[key]
        for (key, (mode, target)), result in zip(targets.items(), results):
            if current.get(key) != (mode, target):
                continue
            previous = monitors.get(key, {})
            history = previous.get("history", []) if previous.get("target") == target and previous.get("mode") == mode else []
            sample = dict(result, checked=time.time())
            history = (history + [sample])[-60:]
            monitors[key] = dict(sample, target=target, mode=mode, history=history,
                                 uptime=round(100 * sum(h["state"] == "up" for h in history) / len(history), 1))


def monitor_loop():
    while True:
        try:
            check_monitors()
        except Exception as error:
            print("Surveillance : %s" % error, flush=True)
        time.sleep(1)


def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(cfg, path=None):
    path = path or CONFIG_PATH
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def record_image_changes(envs):
    """Conserve l'heure d'observation, jamais une date de mise à jour supposée."""
    path = CONFIG_PATH + ".images.json"
    try:
        with open(path, encoding="utf-8") as f:
            history = json.load(f)
    except FileNotFoundError:
        history = {}
    changed = False
    for env in envs:
        for c in env["containers"]:
            c["imageChangedAt"] = None
            image_id = c.get("imageId")
            if not image_id:
                continue
            key = "%s/%s" % (env["id"], c["name"])
            previous = history.get(key)
            if not previous or previous["imageId"] != image_id:
                history[key] = {"imageId": image_id,
                                "observedAt": time.time() if previous else None}
                changed = True
            c["imageChangedAt"] = history[key]["observedAt"]
    if changed:
        save_config(history, path)


def load_items():
    return load_config().get("items", {})


def categories_of(cfg):
    """Catégories créées à la main, plus les anciens groupes saisis dans « Modifier »."""
    cats = [c for c in cfg.get("categories", []) if isinstance(c, str)]
    for it in cfg.get("items", {}).values():
        g = it.get("group")
        if g and g not in cats:
            cats.append(g)
    return cats


def apply_categories(cfg, entries):
    """entries : [{"name": nouveau nom, "old": ancien nom ou None}] dans l'ordre voulu."""
    previous = set(categories_of(cfg))
    names, renames, kept = [], {}, set()
    for e in entries:
        name = (e.get("name") or "").strip()[:40]
        if not name or name.lower() in (n.lower() for n in names):
            continue
        names.append(name)
        old = e.get("old") or name
        kept.add(old)
        if old != name:
            renames[old] = name
    deleted = previous - kept
    items = cfg.setdefault("items", {})
    for key in list(items):
        g = items[key].get("group")
        if g in renames:
            items[key]["group"] = renames[g]
        elif g in deleted:
            del items[key]["group"]
        if not items[key]:
            del items[key]
    order = cfg.get("order", {})
    cfg["order"] = {renames.get(g, g): k for g, k in order.items() if g not in deleted}
    cfg["groupOrder"] = [renames.get(g, g) for g in cfg.get("groupOrder", []) if g not in deleted]
    cfg["categories"] = names


def clean_host(raw):
    raw = raw.strip()
    return urllib.parse.urlparse(raw if "://" in raw else "//" + raw).hostname or raw


def env_host(ep, saved):
    """Adresse utilisée pour les liens d'un environnement, et d'où elle vient."""
    name = ep["Name"]
    if saved.get(name, {}).get("host"):
        return clean_host(saved[name]["host"]), "réglage du tableau de bord"
    if HOSTS.get(name):
        return clean_host(HOSTS[name]), "variable HOSTS"
    for raw, source in ((ep.get("PublicURL"), "Public URL de Portainer"),
                        (ep.get("URL"), "adresse de l'environnement dans Portainer")):
        if raw:
            host = urllib.parse.urlparse(raw if "://" in raw else "tcp://" + raw).hostname
            if host:
                return host, source
    return urllib.parse.urlparse(PORTAINER_URL).hostname or "localhost", "adresse de Portainer (par défaut)"


def collect():
    envs = []
    saved = load_config().get("envs", {})
    for ep in api("/endpoints"):
        if ep.get("Type") not in (1, 2, 4):  # Docker, agent Docker, Edge agent Docker
            continue
        host, source = env_host(ep, saved)
        entry = {"id": ep["Id"], "name": ep["Name"], "label": saved.get(ep["Name"], {}).get("label", ""),
                 "type": ep.get("Type"), "url": ep.get("URL", ""), "host": host, "hostSource": source,
                 "online": ep.get("Status") == 1, "containers": []}
        if entry["online"]:
            try:
                for c in api("/endpoints/%s/docker/containers/json?all=1" % ep["Id"]):
                    name = (c.get("Names") or ["/?"])[0].lstrip("/")
                    key = "%s/%s" % (ep["Name"], name)
                    version = image_version(ep["Id"], c)
                    version["updateBlocked"] = update_reason(c)
                    ports, seen = [], set()
                    for p in c.get("Ports") or []:
                        pub = p.get("PublicPort")
                        if not pub or p.get("Type", "tcp") != "tcp" or pub in seen:
                            continue
                        seen.add(pub)
                        https = pub in HTTPS_PORTS or p.get("PrivatePort") in HTTPS_PORTS
                        ports.append({"port": pub, "url": "%s://%s:%s" % ("https" if https else "http", host, pub)})
                    entry["containers"].append({
                        "key": key, "id": c["Id"], "name": name, "image": c.get("Image", ""), "version": version,
                        "deployedVersion": deployed_version(c), "deployedAt": c.get("Created"),
                        "imageId": c.get("ImageID"),
                        "state": c.get("State", ""), "status": c.get("Status", ""), "ports": ports,
                        "icons": icon_candidates(c.get("Image", ""), name),
                    })
            except Exception as e:  # un environnement en erreur n'empêche pas les autres
                entry["error"] = str(e)
        envs.append(entry)
    record_image_changes(envs)
    return envs


def icon_candidates(image, name):
    """Noms d'icônes à essayer, du plus probable au moins probable."""
    ref = image.split("@")[0]
    if ":" in ref.rsplit("/", 1)[-1]:
        ref = ref.rsplit(":", 1)[0]
    parts = [p.lower() for p in ref.split("/") if p]
    last = parts[-1] if parts else ""
    name = name.lower()
    owner = parts[-2] if len(parts) > 1 and parts[-2] not in GENERIC_OWNERS and "." not in parts[-2] else ""
    found = []
    for c in (last, re.sub(r"-(ce|ee|server|app)$", "", last), name, re.sub(r"[-_]\d+$", "", name), owner):
        if c and ICON_NAME.match(c) and c not in found:
            found.append(c)
    return found


def icon_file(cands):
    """Chemin local d'une icône (téléchargée au besoin), ou None si aucune n'existe."""
    folder = os.path.join(os.path.dirname(CONFIG_PATH), "icons")
    os.makedirs(folder, exist_ok=True)
    for c in cands[:6]:
        if not ICON_NAME.match(c):
            continue
        for ext in ("svg", "png"):
            path = os.path.join(folder, "%s.%s" % (c, ext))
            if os.path.exists(path):
                return path
        none = os.path.join(folder, c + ".none")
        if os.path.exists(none) and time.time() - os.path.getmtime(none) < 7 * 86400:
            continue
        missing = True  # 404 sur toutes les tentatives : on retient l'absence ; une panne réseau n'est pas retenue
        for ext in ("svg", "png"):
            try:
                with urllib.request.urlopen("%s/%s/%s.%s" % (ICON_BASE, ext, c, ext), timeout=6) as r:
                    data = r.read()
                if data:
                    path = os.path.join(folder, "%s.%s" % (c, ext))
                    with open(path, "wb") as f:
                        f.write(data)
                    return path
            except urllib.error.HTTPError as e:
                missing = missing and e.code == 404
            except Exception:
                missing = False
        if missing:
            open(none, "w").close()
    return None


refresh_lock = threading.Lock()  # une seule lecture de Portainer à la fois (cycle automatique ou bouton)


def refresh():
    with refresh_lock:
        try:
            if not PORTAINER_URL or not TOKEN:
                raise RuntimeError("PORTAINER_URL et PORTAINER_TOKEN doivent être définis")
            envs = collect()
            with lock:
                cache.update(environments=envs, updated=time.strftime("%H:%M:%S"), error=None)
        except Exception as e:
            with lock:
                cache["error"] = str(e)


def refresher():
    while True:
        refresh()
        time.sleep(REFRESH)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.split("?")[0] == "/api/data":
            cfg = load_config()
            with lock:
                payload = dict(cache, title=TITLE, items=cfg.get("items", {}), envs=cfg.get("envs", {}),
                               order=cfg.get("order", {}), groupOrder=cfg.get("groupOrder", []),
                               categories=categories_of(cfg), monitors=dict(monitors), monitorInterval=MONITOR_INTERVAL)
            payload["ubuntu"] = ubuntu_status()
            self.send(200, json.dumps(payload))
        elif self.path.split("?")[0] == "/icon":
            path = icon_file(urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("n", []))
            if not path:
                self.send_response(404)
                self.send_header("Cache-Control", "max-age=300")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            with open(path, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml" if path.endswith(".svg") else "image/png")
            self.send_header("Cache-Control", "public, max-age=86400")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif self.path.split("?")[0] == "/":
            self.send(200, PAGE.replace("__TITLE__", TITLE), "text/html")
        else:
            self.send(404, "{}")

    def do_POST(self):
        if self.path == "/api/refresh":  # actualisation manuelle : relit Portainer maintenant
            refresh()
            return self.send(200, "{}")
        if self.path not in ("/api/item", "/api/envs", "/api/order", "/api/categories", "/api/move", "/api/update"):
            return self.send(404, "{}")
        if self.headers.get_content_type() != "application/json":
            return self.send(415, json.dumps({"error": "Content-Type application/json requis"}))
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            if self.path == "/api/update":
                result = manual_update(body.get("endpoint"), body.get("id"))
                return self.send(200, json.dumps(result))
            cfg = load_config()
            if self.path == "/api/categories":
                apply_categories(cfg, body["categories"])
            elif self.path == "/api/move":  # range un conteneur dans une catégorie (ou l'en retire si group est vide)
                items, group = cfg.setdefault("items", {}), body.get("group")
                item = items.setdefault(body["key"], {})
                if group:
                    item["group"] = group
                    if group not in categories_of(cfg):
                        cfg.setdefault("categories", []).append(group)
                    if body.get("keys"):
                        cfg.setdefault("order", {})[group] = [k for k in body["keys"] if isinstance(k, str)]
                else:
                    item.pop("group", None)
                if not item:
                    del items[body["key"]]
            elif self.path == "/api/order":
                if "groups" in body:  # ordre des groupes
                    cfg["groupOrder"] = [g for g in body["groups"] if isinstance(g, str)]
                else:  # ordre des conteneurs d'un groupe
                    cfg.setdefault("order", {})[body["group"]] = [k for k in body["keys"] if isinstance(k, str)]
            elif self.path == "/api/item":
                key, data = body["key"], body.get("data")
                items = cfg.setdefault("items", {})
                old_item = items.get(key, {})
                if data:
                    validate_monitor(data)
                    items[key] = {k: v for k, v in data.items()
                                  if k in ("name", "url", "group", "icon", "hidden", "monitor", "monitorUrl", "monitorInterval") and v not in ("", None, False)}
                else:
                    items.pop(key, None)
            else:
                envs = {n: {k: v.strip() for k, v in e.items()
                            if k in ("host", "label") and isinstance(v, str) and v.strip()}
                        for n, e in body["envs"].items()}
                cfg["envs"] = {n: e for n, e in envs.items() if e}
            save_config(cfg)
            if self.path == "/api/item" and any(old_item.get(k) != items.get(key, {}).get(k)
                                                for k in ("monitor", "monitorUrl", "url")):
                with lock:
                    monitors.pop(key, None)
            if self.path == "/api/envs":
                refresh()  # les liens sont recalculés tout de suite
            self.send(200, "{}")
        except Exception as e:
            self.send(400, json.dumps({"error": str(e)}))


PAGE = r"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Ctext y='.9em' font-size='90'%3E%F0%9F%8F%A0%3C/text%3E%3C/svg%3E">
<script>try{var t=JSON.parse(localStorage.getItem('dp-theme'));if(t)document.documentElement.dataset.theme=t}catch(e){}</script>
<style>
:root{
  --bg:#faf6f0;--surface:#fff;--surface2:#f1ebe0;--tile:#f3eee5;--text:#2b2a33;--muted:#6b6774;--line:#e6dfd2;
  --accent:#a84d32;--accent-ink:#fff;--accent-soft:#fdebe2;
  --ok:#1e7a44;--ok-bg:#e3f5ea;--bad:#b4281f;--bad-bg:#fde8e6;--warn:#8f5a00;--warn-bg:#fff1d6;--mute-bg:#ece9e3;
  --shadow:0 1px 2px rgba(80,55,30,.07),0 10px 28px -14px rgba(80,55,30,.25);
  --shadow-hover:0 2px 4px rgba(80,55,30,.08),0 18px 36px -16px rgba(80,55,30,.35);
  --radius:18px}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){
  --bg:#15141a;--surface:#1f1e26;--surface2:#292833;--tile:#ecebf3;--text:#f2f0f7;--muted:#a7a2b3;--line:#34323f;
  --accent:#ff8a5b;--accent-ink:#1d1109;--accent-soft:#38271f;
  --ok:#6ee7a0;--ok-bg:#16301f;--bad:#ff8f87;--bad-bg:#3a1c1b;--warn:#fbbf24;--warn-bg:#35290f;--mute-bg:#2c2b35;
  --shadow:0 1px 2px rgba(0,0,0,.4),0 10px 28px -14px rgba(0,0,0,.6);--shadow-hover:0 2px 4px rgba(0,0,0,.4),0 18px 36px -16px rgba(0,0,0,.7)}}
:root[data-theme=dark]{
  --bg:#15141a;--surface:#1f1e26;--surface2:#292833;--tile:#ecebf3;--text:#f2f0f7;--muted:#a7a2b3;--line:#34323f;
  --accent:#ff8a5b;--accent-ink:#1d1109;--accent-soft:#38271f;
  --ok:#6ee7a0;--ok-bg:#16301f;--bad:#ff8f87;--bad-bg:#3a1c1b;--warn:#fbbf24;--warn-bg:#35290f;--mute-bg:#2c2b35;
  --shadow:0 1px 2px rgba(0,0,0,.4),0 10px 28px -14px rgba(0,0,0,.6);--shadow-hover:0 2px 4px rgba(0,0,0,.4),0 18px 36px -16px rgba(0,0,0,.7)}

*{box-sizing:border-box}
html{color-scheme:light dark}
:root[data-theme=light]{color-scheme:light}:root[data-theme=dark]{color-scheme:dark}
body{margin:0;background:var(--bg);color:var(--text);font:16px/1.5 "Segoe UI",system-ui,-apple-system,Roboto,sans-serif;-webkit-font-smoothing:antialiased}
button,input,select{font:inherit;color:inherit}
:focus-visible{outline:3px solid color-mix(in srgb,var(--accent) 55%,transparent);outline-offset:2px}

.update-action{padding:8px 12px 12px;display:grid;gap:6px}
/* Barre du haut */
header{position:sticky;top:0;z-index:5;display:flex;flex-wrap:wrap;gap:14px 20px;align-items:center;justify-content:space-between;padding:14px max(16px,calc((100% - 1480px)/2 + 24px));background:color-mix(in srgb,var(--bg) 82%,transparent);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);border-bottom:1px solid var(--line)}
.brand{display:flex;align-items:center;gap:14px;min-width:0}
.logo{width:46px;height:46px;border-radius:15px;background:var(--accent-soft);display:grid;place-items:center;font-size:25px;flex:none}
h1{margin:0;font-size:1.45rem;line-height:1.2;letter-spacing:-.01em}
#hello{color:var(--muted);font-size:.9rem}
.tools{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.search{position:relative;display:block}
.search svg{position:absolute;left:13px;top:50%;transform:translateY(-50%);width:18px;height:18px;color:var(--muted);pointer-events:none}
.search input{width:min(320px,70vw);padding:10px 14px 10px 40px;border:1px solid var(--line);border-radius:99px;background:var(--surface);box-shadow:var(--shadow)}
.search input::placeholder{color:var(--muted)}
.btn{display:inline-flex;align-items:center;gap:8px;padding:9px 16px;border:1px solid var(--line);border-radius:99px;background:var(--surface);cursor:pointer;font-weight:600;transition:background .15s,border-color .15s,transform .1s}
.btn:hover{border-color:color-mix(in srgb,var(--accent) 50%,var(--line))}
.btn:active{transform:scale(.97)}
.btn.primary{background:var(--accent);border-color:var(--accent);color:var(--accent-ink)}
.btn.primary:hover{filter:brightness(1.07)}
.btn.danger{color:var(--bad)}
.btn.sm{padding:4px 11px;font-size:.82rem}
.btn:disabled{opacity:.4;cursor:default}
#refresh.spin svg{animation:spin .8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.icon-btn{width:42px;height:42px;padding:0;justify-content:center}
.icon-btn svg{width:20px;height:20px}

main{max-width:1480px;margin:0 auto;padding:22px 24px 80px}

/* Résumé */
.summary{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:22px}
.chip{display:inline-flex;align-items:center;gap:7px;padding:6px 14px;border-radius:99px;font-size:.9rem;font-weight:600;background:var(--mute-bg);color:var(--text)}
.chip.ok{background:var(--ok-bg);color:var(--ok)}.chip.bad{background:var(--bad-bg);color:var(--bad)}.chip.warn{background:var(--warn-bg);color:var(--warn)}
.chip.toggle{cursor:pointer;background:transparent;border:1px dashed var(--line);color:var(--muted);font-weight:500}
.chip.toggle:has(input:checked){border-style:solid;border-color:var(--accent);color:var(--accent);background:var(--accent-soft)}
.chip.toggle input{accent-color:var(--accent);margin:0}
.upd{margin-left:auto;color:var(--muted);font-size:.82rem}

/* Bandeaux */
.banner{display:flex;flex-direction:column;gap:2px;padding:12px 16px;border-radius:14px;margin-bottom:12px;background:var(--warn-bg);color:var(--warn);border:1px solid color-mix(in srgb,currentColor 25%,transparent)}
.banner.bad{background:var(--bad-bg);color:var(--bad)}
.banner strong{font-size:.98rem}.banner p{margin:0;font-size:.9rem;color:var(--text)}.banner small{opacity:.75;word-break:break-all}

/* Barre de personnalisation */
.editbar{display:flex;flex-wrap:wrap;gap:10px 14px;align-items:center;padding:14px 16px;margin-bottom:22px;background:var(--accent-soft);border:1px solid color-mix(in srgb,var(--accent) 35%,var(--line));border-radius:16px}
.editbar[hidden]{display:none}
.editbar p{margin:0;flex:1 1 320px;font-size:.92rem}
.editbar .acts{display:flex;gap:8px;flex-wrap:wrap}

/* Sections */
.group{margin-bottom:30px}
.gh{display:flex;align-items:center;gap:8px;margin:0 0 12px;font-size:1.12rem}
.gtoggle{display:flex;align-items:center;gap:10px;padding:4px 10px 4px 6px;margin-left:-6px;background:none;border:0;border-radius:12px;cursor:pointer;font-weight:700;font-size:1.12rem;letter-spacing:-.01em}
.gtoggle:hover{background:var(--surface2)}
.gdot{width:12px;height:12px;border-radius:50%;background:hsl(var(--h) 72% 56%);box-shadow:0 0 0 4px hsl(var(--h) 72% 56%/.18)}
.chev{width:16px;height:16px;color:var(--muted);transition:transform .2s}
.gtoggle[aria-expanded=false] .chev{transform:rotate(-90deg)}
.count{font-size:.8rem;font-weight:600;color:var(--muted);background:var(--surface2);border-radius:99px;padding:1px 10px}
.gh .btn.sm{padding:2px 9px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,185px),1fr));gap:18px 24px}
.grid>*{min-width:0}
.group.collapsed .grid{display:none}

/* Lanceur sans tuiles */
.launcher{min-width:0;position:relative}
.launcher.dim{opacity:.5}
.launch{width:100%;position:relative;display:flex;flex-direction:column;align-items:center;gap:7px;padding:28px 10px 24px;border:0;border-radius:6px;background:transparent;color:var(--text);cursor:pointer;text-align:center}
.launch:hover{background:var(--surface2)}
.launch.selected::after{content:"";position:absolute;bottom:7px;width:44px;height:3px;border-radius:2px;background:var(--accent)}
.launch .tile{width:76px;height:76px;margin-bottom:8px;background:transparent;font-size:48px}
.launch .tile img{width:68px;height:68px}
.launch .name{font-family:Georgia,serif;font-size:1.3rem;white-space:normal;overflow-wrap:anywhere}
.launch .sub{white-space:normal;overflow-wrap:anywhere;margin-bottom:8px}
.launch .pill{background:transparent;padding:4px 11px;max-width:100%;font-weight:400;white-space:normal;text-align:left;font-size:.85rem}
.launch .pill::before{flex:none}
.launcher.editing{outline:1px dashed var(--line);border-radius:8px;cursor:grab}
.launcher.drag{opacity:.35}
.head{display:flex;align-items:center;gap:13px}
.tile{width:48px;height:48px;border-radius:15px;background:var(--tile);display:grid;place-items:center;font-size:25px;flex:none}
.tile img{width:32px;height:32px;object-fit:contain}
.body{min-width:0;flex:1}
.name{font-weight:700;font-size:1.04rem;line-height:1.3;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sub{color:var(--muted);font-size:.8rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sub.metadata{white-space:normal;overflow-wrap:anywhere;margin-top:3px}
.pill{display:inline-flex;align-items:center;gap:6px;padding:2px 10px;border-radius:99px;font-size:.76rem;font-weight:700;white-space:nowrap;background:var(--mute-bg);color:var(--muted)}
.pill::before{content:"";width:7px;height:7px;border-radius:50%;background:currentColor}
.pill.ok{background:var(--ok-bg);color:var(--ok)}.pill.bad{background:var(--bad-bg);color:var(--bad)}.pill.warn{background:var(--warn-bg);color:var(--warn)}
.foot{display:flex;flex-wrap:wrap;align-items:center;gap:6px 10px;color:var(--muted);font-size:.82rem}
.foot .since{min-width:0;flex:1 1 auto}
.foot .pill{flex:none}
.tag{flex:none;margin-left:auto;background:var(--surface2);border-radius:99px;padding:1px 10px;font-size:.74rem;font-weight:600}
.ports{display:flex;flex-wrap:wrap;gap:6px}
.ports a{font-size:.78rem;font-weight:600;padding:2px 10px;border:1px solid var(--line);border-radius:99px;color:var(--accent);text-decoration:none;background:var(--surface)}
.ports a:hover{background:var(--accent-soft)}
.ctl{display:flex;align-items:center;justify-content:space-between;gap:8px;padding-top:10px;border-top:1px dashed var(--line)}
.ctl .mv{display:flex;gap:6px}
.over{outline:3px dashed var(--accent);outline-offset:3px;border-radius:14px}
.hint{grid-column:1/-1;padding:22px;text-align:center;color:var(--muted);border:2px dashed var(--line);border-radius:var(--radius)}

/* États vides et chargement */
.empty{text-align:center;padding:64px 20px;color:var(--muted)}
.empty .big{font-size:3.2rem;line-height:1.2}.empty h2{margin:10px 0 6px;color:var(--text);font-size:1.3rem}.empty p{margin:0 auto;max-width:460px}
.skel{height:116px;background:linear-gradient(100deg,var(--surface) 30%,var(--surface2) 50%,var(--surface) 70%);background-size:200% 100%;animation:sh 1.4s linear infinite;border:1px solid var(--line);border-radius:var(--radius)}
@keyframes sh{to{background-position:-200% 0}}

/* Fenêtres */
.monitor{display:flex;flex-wrap:wrap;gap:8px;padding:0 16px 14px}.monitor .since{width:100%;overflow-wrap:anywhere;font-size:.82rem;color:var(--muted)}.heartbeats{display:flex;gap:3px;width:100%;height:18px}.heartbeats span{flex:1;max-width:10px;border-radius:3px}.heartbeats .up{background:var(--ok,#22a06b)}.heartbeats .down{background:var(--bad,#d54848)}
dialog{border:1px solid var(--line);border-radius:22px;background:var(--surface);color:var(--text);width:min(480px,94vw);padding:0;box-shadow:0 30px 80px -20px rgba(0,0,0,.45);max-height:90vh}
dialog::backdrop{background:rgba(20,15,10,.45);backdrop-filter:blur(3px)}
dialog form{padding:22px 24px;display:flex;flex-direction:column;gap:4px}
dialog h3{margin:0;font-size:1.2rem}
dialog .lead{margin:2px 0 10px;color:var(--muted);font-size:.9rem}
dialog label{margin-top:12px;font-weight:600;font-size:.88rem}
dialog label small{display:block;font-weight:400;color:var(--muted)}
dialog input[type=text],dialog select{width:100%;margin-top:5px;padding:10px 12px;border:1px solid var(--line);border-radius:12px;background:var(--bg)}
dialog input[type=text]:focus,dialog select:focus{border-color:var(--accent);outline:3px solid color-mix(in srgb,var(--accent) 25%,transparent);outline-offset:0}
dialog .chk{display:flex;gap:10px;align-items:center;font-weight:500}
dialog .chk input{accent-color:var(--accent);width:18px;height:18px;margin:0}
.row{display:flex;gap:10px;justify-content:flex-end;margin-top:20px}
.crow{display:flex;gap:8px;margin-top:8px}.crow input{flex:1;margin:0!important}
.envbox{margin-top:14px;padding:14px;border:1px solid var(--line);border-radius:14px;background:var(--bg)}
.envbox .meta{color:var(--muted);font-size:.82rem}
.envbox .used{margin:2px 0 4px}
.key{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:.8rem;color:var(--muted)}

@media (max-width:640px){
  main{padding:16px 16px 72px}
  .search input{width:100%}.search{flex:1 1 100%}.tools{width:100%}
  .upd{margin-left:0;flex-basis:100%}
}

/* Présentation inspirée du lanceur validé, détails à droite sur grand écran. */
header{position:relative;background:var(--bg);backdrop-filter:none;padding-top:26px;padding-bottom:24px}
h1{font-family:Georgia,serif;font-size:2rem;color:var(--accent)}
.brand .logo{display:none}
.btn,.search input{border-radius:7px;box-shadow:none}
.search input{background:transparent}
.summary{padding-bottom:20px;border-bottom:1px solid var(--line)}
.summary .chip:not(.toggle){background:transparent;padding-left:0;font-weight:500}
.browse-bar{display:flex;align-items:center;justify-content:space-between;gap:20px;flex-wrap:wrap;margin-bottom:28px}
.main-tabs{display:flex;gap:24px;border-bottom:1px solid var(--line);margin-bottom:24px}
.main-tab{border:0;border-bottom:3px solid transparent;background:transparent;color:var(--muted);padding:12px 2px;cursor:pointer;font-size:1rem}
.main-tab[aria-selected=true]{color:var(--accent);border-bottom-color:var(--accent);font-weight:700}
.ubuntu-card{max-width:1000px;padding:24px;border:1px solid var(--line);border-radius:var(--radius);background:var(--surface);box-shadow:var(--shadow)}
.ubuntu-card h2{font:700 1.8rem/1.2 Georgia,serif;margin:0 0 16px}
.ubuntu-card p{color:var(--muted);line-height:1.5}
.ubuntu-card .pill{font-size:.9rem;padding:7px 12px}
.ubuntu-card .metadata{margin-top:20px;padding-top:16px;border-top:1px solid var(--line);font-size:.85rem}
.ubuntu-filter{display:block;margin:24px 0 12px;font-weight:600}
.ubuntu-filter input{display:block;width:min(100%,400px);margin-top:6px;padding:10px 12px;border:1px solid var(--line);border-radius:7px;background:var(--bg);color:var(--text)}
.ubuntu-table-wrap{overflow-x:auto}
.ubuntu-table{width:100%;min-width:560px;border-collapse:collapse;text-align:left;font-size:.9rem}
.ubuntu-table th,.ubuntu-table td{padding:12px 10px;border-bottom:1px solid var(--line);vertical-align:top;overflow-wrap:anywhere}
.ubuntu-table th{color:var(--muted);font-size:.8rem}
.ubuntu-table td:first-child{font-weight:700}
.ubuntu-table td small{display:block;margin-top:3px;color:var(--muted);font-weight:400}
.ubuntu-manual{margin-top:24px;padding:18px;border:1px solid var(--line);border-radius:var(--radius);background:var(--bg)}
.ubuntu-manual h3{margin:0 0 8px}
.ubuntu-manual p{margin:8px 0}
.ubuntu-manual pre{margin:12px 0;padding:12px;overflow-x:auto;border:1px solid var(--line);border-radius:7px;background:var(--surface)}
[hidden]{display:none!important}
#group-filters{display:flex;gap:24px;overflow-x:auto;max-width:100%;border-bottom:1px solid var(--line)}
.group-filter{border:0;border-bottom:3px solid transparent;background:transparent;color:var(--muted);padding:12px 2px;cursor:pointer;white-space:nowrap;font-size:1rem}
.group-filter[aria-pressed=true]{color:var(--accent);border-bottom-color:var(--accent);font-weight:700}
.environment-label{font-size:.85rem;color:var(--muted)}
#environment{max-width:100%;padding:8px;border:1px solid var(--line);border-radius:6px;background:var(--bg)}
#workspace{display:grid;grid-template-columns:minmax(0,1fr);gap:32px;align-items:start}
#workspace.has-details{grid-template-columns:minmax(0,1.8fr) minmax(320px,1fr)}
#out{min-width:0}
.gh{border-bottom:1px solid var(--line);padding-bottom:10px}
.gdot{display:none}
#inspector{min-width:0;border-left:1px solid var(--line);padding-left:30px;position:sticky;top:20px;max-height:calc(100vh - 40px);overflow:auto;scroll-margin-top:16px}
#inspector[hidden]{display:none}
.inspector-heading{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:22px}
.inspector-heading h2{font-size:.9rem;color:var(--muted);font-weight:500;margin:0}
.service-details{display:flex;flex-direction:column;gap:22px}
.service-details .head{align-items:flex-start;flex-wrap:wrap}
.service-details .tile{width:60px;height:60px;background:transparent;font-size:40px}
.service-details .tile img{width:56px;height:56px}
.service-details .name{font:700 1.8rem/1.2 Georgia,serif;white-space:normal;overflow-wrap:anywhere;margin:0 0 8px}
.service-details .body{min-width:min(200px,100%)}
.service-details .sub{white-space:normal;overflow-wrap:anywhere}
.service-details .metadata{padding-top:10px;margin-top:10px;border-top:1px solid var(--line);font-size:.85rem}
.service-details .foot{padding-top:16px;border-top:1px solid var(--line)}
.service-details .monitor{padding:0;gap:12px}
.service-details .pill{white-space:normal}
.service-details .heartbeats{gap:2px}
.detail-actions{display:flex;gap:12px;flex-wrap:wrap}
.detail-actions .btn{flex:1;justify-content:center;text-decoration:none}
.service-details .update-action{padding:0 0 20px}
@media(min-width:1200px){#workspace.has-details .grid{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media(max-width:900px){
  #workspace.has-details{grid-template-columns:minmax(0,1fr)}
  #inspector{position:static;max-height:none;border-left:0;border-bottom:1px solid var(--line);padding:0 0 24px;grid-row:1}
}
@media(max-width:480px){
  .grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:16px 8px}
  .launch{padding:18px 4px 24px}.launch .name{font-size:1.05rem}.launch .pill{font-size:.75rem;padding:4px 8px}
  .ctl{flex-wrap:wrap}.browse-bar{gap:12px}header{padding:20px 16px}
}

@media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}
</style></head><body>
<header>
  <div class="brand"><div class="logo" aria-hidden="true">🏠</div><div><h1>__TITLE__</h1><div id="hello"></div></div></div>
  <div class="tools">
    <label class="search"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/></svg>
      <input id="q" type="search" placeholder="Rechercher un service…  ( / )" aria-label="Rechercher un service"></label>
    <button class="btn" id="refresh" title="Relire Portainer maintenant" aria-label="Actualiser la liste des conteneurs">
      <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12a9 9 0 1 1-3-6.7"/><path d="M21 4v5h-5"/></svg><span id="rlbl">Actualiser</span></button>
    <button class="btn icon-btn" id="theme" title="Changer le thème (clair / sombre)" aria-label="Changer le thème">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg></button>
    <button class="btn primary" id="edit">✏️ Personnaliser</button>
  </div>
</header>
<main>
  <nav class="main-tabs" role="tablist" aria-label="Pages du tableau de bord">
    <button type="button" class="main-tab" id="services-tab" role="tab" aria-controls="services-view" aria-selected="true" data-focus="tab-services">Services</button>
    <button type="button" class="main-tab" id="ubuntu-tab" role="tab" aria-controls="ubuntu-view" aria-selected="false" data-focus="tab-ubuntu">Serveur Ubuntu</button>
  </nav>
  <div id="services-view" role="tabpanel" aria-labelledby="services-tab">
  <div id="msg"></div>
  <div id="summary" class="summary"></div>
  <div id="editbar" class="editbar" hidden>
    <p><strong>Mode personnalisation.</strong> Glisse les services pour les déplacer (ou utilise les flèches), et pose-les sur une catégorie pour les y ranger. « Modifier » change le nom, l'icône ou la catégorie.</p>
    <div class="acts"><button class="btn sm" id="catbtn">🗂️ Catégories</button><button class="btn sm" id="envbtn">🖥️ Environnements</button></div>
  </div>
  <div class="browse-bar"><nav id="group-filters" aria-label="Filtrer les groupes"></nav><label class="environment-label">Environnement <select id="environment" aria-label="Filtrer les environnements"><option value="">Tous les environnements</option></select></label></div>
  <div id="workspace">
  <div id="out"><div class="grid"><div class="skel"></div><div class="skel"></div><div class="skel"></div></div></div>
  <aside id="inspector" aria-labelledby="details-title" hidden>
    <div class="inspector-heading"><h2 id="details-title" tabindex="-1">Détails du service</h2><button type="button" class="btn icon-btn" id="close-details" data-focus="close-details" aria-label="Fermer les détails">✕</button></div>
    <div id="details-body"></div>
  </aside>
  </div>
  </div>
  <section id="ubuntu-view" role="tabpanel" aria-labelledby="ubuntu-tab" hidden>
    <div class="ubuntu-card">
      <h2>Mises à jour du serveur Ubuntu</h2>
      <div id="ubuntu-status"></div>
      <section class="ubuntu-manual" aria-labelledby="ubuntu-manual-title">
        <h3 id="ubuntu-manual-title">Mettre à jour manuellement</h3>
        <p>Dans le terminal du serveur Ubuntu, après avoir vérifié une sauvegarde du serveur, lance ces commandes l’une après l’autre :</p>
        <pre><code>sudo apt update
sudo apt upgrade</code></pre>
        <p>La seconde commande affiche les changements et demande confirmation. Des services peuvent redémarrer pendant la mise à jour.</p>
      </section>
      <label class="ubuntu-filter" id="ubuntu-filter-wrap" hidden>Rechercher un paquet
        <input id="ubuntu-filter" type="search" placeholder="Nom du paquet…">
      </label>
      <div id="ubuntu-packages"></div>
    </div>
  </section>
</main>

<dialog id="dlg"><form method="dialog" onsubmit="return false">
  <h3>Modifier le service</h3><div class="lead key" id="dt"></div>
  <label>Nom affiché<input type="text" id="f_name"></label>
  <label>Lien à ouvrir<small>Laisser vide pour utiliser le port détecté.</small><input type="text" id="f_url" placeholder="http://192.168.1.210:3210"></label>
  <label>Catégorie<select id="f_group"></select></label>
  <label>Icône<small>Vide = automatique. Sinon : un nom de la collection (ex. jellyfin), une URL d'image ou un emoji.</small><input type="text" id="f_icon" placeholder="jellyfin, https://…/logo.png ou 🎬"></label>
  <label class="chk"><input type="checkbox" id="f_hidden">Masquer ce conteneur</label>
  <label>Surveillance du service<select id="f_monitor"><option value="">Désactivée</option><option value="http">HTTP / HTTPS</option><option value="tcp">Connexion TCP</option></select></label>
  <label>Adresse à surveiller<small>Vide = lien de la carte ou premier port détecté. HTTP : réponse 200–399 ; TCP : connexion au port.</small><input type="text" id="f_monitorUrl" placeholder="https://service/health ou tcp://192.168.1.210:5432"></label>
  <label>Intervalle de vérification (secondes)<small>De 10 à 86400 secondes. Vide = valeur par défaut du serveur. Appliqué après le dernier contrôle.</small><input type="number" id="f_monitorInterval" min="10" max="86400" step="1"></label>
  <p id="itemError" role="alert"></p>
  <div class="row"><button type="button" class="btn danger" id="reset">Réinitialiser</button><button type="button" class="btn" id="cancel">Annuler</button><button class="btn primary" id="save" value="ok">Enregistrer</button></div>
</form></dialog>

<dialog id="cdlg"><form method="dialog" onsubmit="return false">
  <h3>Catégories</h3>
  <p class="lead">Crée tes catégories, renomme-les ou supprime-les. Supprimer une catégorie remet ses conteneurs dans leur environnement.</p>
  <div id="clist"></div>
  <div class="crow"><input type="text" id="cnew" placeholder="Nouvelle catégorie…" maxlength="40"><button type="button" class="btn" id="cadd">Ajouter</button></div>
  <div class="row"><button type="button" class="btn" id="cclose">Fermer</button><button type="button" class="btn primary" id="csave">Enregistrer</button></div>
</form></dialog>

<dialog id="edlg"><form method="dialog" onsubmit="return false">
  <h3>Environnements Portainer</h3>
  <p class="lead">Pour chaque environnement : le nom affiché et l'adresse IP (ou nom d'hôte) utilisée pour les liens. Laisse vide pour l'adresse détectée.</p>
  <div id="elist"></div>
  <div class="row"><button type="button" class="btn" id="eclose">Fermer</button><button type="button" class="btn primary" id="esave">Enregistrer</button></div>
</form></dialog>

<script>
const $=s=>document.querySelector(s);let D=null,edit=false,cur=null,selectedKey,activeGroup='',activeEnv='';
const updating=new Set(),updateResults=new Map();
const el=(t,c,x)=>{const e=document.createElement(t);if(c)e.className=c;if(x!=null)e.textContent=x;return e};
const store={get(k,d){try{const v=localStorage.getItem(k);return v==null?d:JSON.parse(v)}catch(e){return d}},set(k,v){try{localStorage.setItem(k,JSON.stringify(v))}catch(e){}}};
let activePage=store.get('dp-page','services')==='ubuntu'?'ubuntu':'services';
let showAll=!!store.get('dp-all',false);const collapsed=new Set(store.get('dp-collapsed',[]));
const CHEV='<svg class="chev" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 9l6 6 6-6"/></svg>';

/* Textes en français */
function hello(){const d=new Date(),h=d.getHours();
  $('#hello').textContent=(h<5?'Bonne nuit':h<12?'Bonjour':h<18?'Bon après-midi':'Bonsoir')+' · '+d.toLocaleDateString('fr-FR',{weekday:'long',day:'numeric',month:'long'})+' · '+d.toLocaleTimeString('fr-FR',{hour:'2-digit',minute:'2-digit'})}
function fr(s){
  if(!s)return '';
  const U={second:'seconde',seconds:'secondes',minute:'minute',minutes:'minutes',hour:'heure',hours:'heures',day:'jour',days:'jours',week:'semaine',weeks:'semaines',month:'mois',months:'mois',year:'an',years:'ans'};
  const dur=t=>t.replace(/^About an? /i,'environ 1 ').replace(/^An? /i,'1 ').replace(/^Less than a second/i,'moins d\'une seconde').replace(/\b[a-z]+\b/g,m=>U[m]||m);
  let m;
  if(/^Up .*\(Paused\)$/i.test(s))return 'En pause';
  if(m=s.match(/^Up (.+?)(?: \((.+)\))?$/i)){const h=m[2]==='healthy'?' · en bonne santé':m[2]==='unhealthy'?'':m[2]?' · '+m[2].replace('health: starting','démarrage'):'';return 'En marche depuis '+dur(m[1])+h}
  if(m=s.match(/^Exited \((\d+)\) (.+) ago$/i))return 'Arrêté il y a '+dur(m[2])+' (code '+m[1]+')';
  if(m=s.match(/^Restarting \((\d+)\) (.+) ago$/i))return 'Redémarre en boucle (code '+m[1]+')';
  if(/^Created$/i.test(s))return 'Créé, pas encore démarré';
  if(/^Up .*\(Paused\)$/i.test(s))return 'En pause';
  return s}
function stateInfo(c){
  if(c.state==='running')return /unhealthy/i.test(c.status)?{cls:'warn',txt:'À surveiller'}:{cls:'ok',txt:'En marche'};
  if(c.state==='exited'||c.state==='dead')return {cls:'bad',txt:'Arrêté'};
  if(c.state==='paused')return {cls:'warn',txt:'En pause'};
  if(c.state==='restarting')return {cls:'warn',txt:'Redémarre'};
  if(c.state==='created')return {cls:'',txt:'Créé'};
  return {cls:'',txt:c.state||'Inconnu'}}
function friendly(err){
  if(/PORTAINER_URL et PORTAINER_TOKEN/.test(err))return 'Il manque l\'adresse ou le jeton de Portainer dans la configuration du conteneur.';
  if(/\b40[13]\b/.test(err))return 'Portainer refuse le jeton d\'accès. Vérifie la variable PORTAINER_TOKEN.';
  if(/refused|timed out|unreachable|Name or service|urlopen/i.test(err))return 'Portainer ne répond pas. Vérifie l\'adresse PORTAINER_URL.';
  return 'Une erreur est survenue en contactant Portainer.'}
function banner(kind,title,text,detail){const b=el('div','banner'+(kind?' '+kind:''));b.append(el('strong',null,title));if(text)b.append(el('p',null,text));if(detail)b.append(el('small',null,detail));return b}
const hue=s=>{let h=0;for(const ch of s)h=(h*31+ch.charCodeAt(0))%360;return h};

async function load(){try{const r=await fetch('api/data');if(!r.ok)throw Error('Réponse du serveur invalide');D=await r.json();render();return true}
  catch(e){if(D)D.ubuntu={state:'unknown',detail:'Impossible de joindre DockPage.'};
    if(activePage==='ubuntu')render();
    else $('#msg').replaceChildren(banner('bad','Impossible de joindre le tableau de bord','Vérifie que le conteneur DockPage est bien démarré.'));
    return false}}

function renderUbuntu(){
  const box=$('#ubuntu-status'),list=$('#ubuntu-packages');
  const u=D?.ubuntu||{state:'unknown',detail:'Impossible de joindre DockPage.'};
  box.replaceChildren();list.replaceChildren();
  const available=u.state==='available',current=u.state==='current';
  const packages=available&&Array.isArray(u.packages)?u.packages:null;
  $('#ubuntu-filter-wrap').hidden=!packages?.length;
  const label=available?u.count+' mise'+(u.count>1?'s':'')+' à jour disponible'+(u.count>1?'s':''):
    current?'Serveur à jour':'Vérification impossible';
  box.append(el('span','pill '+(available?'warn':current?'ok':'bad'),label));
  if(!available&&!current)box.append(el('p',null,u.detail||'Le relevé du serveur est indisponible.'));
  else box.append(el('p',null,'État des paquets APT du serveur Ubuntu. Aucune mise à jour n’est lancée depuis DockPage.'));
  box.append(el('p','metadata',u.checkedAt?'Dernière vérification : '+new Date(u.checkedAt*1000).toLocaleString('fr-FR'):'Aucune vérification enregistrée.'));
  if(available&&!packages){
    list.append(el('p',null,'Le relevé actuel contient seulement le nombre de mises à jour. Réinstalle le script de contrôle sur le serveur, puis relance-le pour voir les paquets.'));
    return;
  }
  if(!packages)return;
  const q=$('#ubuntu-filter').value.trim().toLocaleLowerCase('fr-FR');
  const visible=packages.filter(p=>!q||(p.name+' '+p.installed+' '+p.candidate).toLocaleLowerCase('fr-FR').includes(q));
  list.append(el('h3',null,'Paquets à mettre à jour'));
  if(q)list.append(el('p',null,visible.length+' paquet'+(visible.length>1?'s':'')+' affiché'+(visible.length>1?'s':'')+' sur '+packages.length));
  if(!visible.length){list.append(el('p',null,'Aucun paquet ne correspond à la recherche.'));return}
  const wrap=el('div','ubuntu-table-wrap'),table=el('table','ubuntu-table'),head=el('thead'),row=el('tr');
  for(const title of ['Paquet','Version installée','Version proposée']){const th=el('th',null,title);th.scope='col';row.append(th)}
  head.append(row);table.append(head);
  const body=el('tbody');
  for(const p of visible){
    const tr=el('tr'),name=el('td');
    name.append(document.createTextNode(p.name),el('small',null,'Source APT : '+p.source+' · '+p.architecture));
    tr.append(name,el('td',null,p.installed),el('td',null,p.candidate));body.append(tr);
  }
  table.append(body);wrap.append(table);list.append(wrap);
}

function render(){
  const focused=document.activeElement?.dataset.focus;
  const restoreFocus=()=>{if(focused){const target=[...document.querySelectorAll('[data-focus]')].find(e=>e.dataset.focus===focused);if(target)target.focus({preventScroll:true})}};
  const services=activePage==='services';
  $('#services-view').hidden=!services;$('#ubuntu-view').hidden=services;
  $('#services-tab').setAttribute('aria-selected',String(services));$('#ubuntu-tab').setAttribute('aria-selected',String(!services));
  $('#services-tab').tabIndex=services?0:-1;$('#ubuntu-tab').tabIndex=services?-1:0;
  $('.search').hidden=!services;$('#edit').hidden=!services;
  $('#refresh').title=services?'Relire Portainer maintenant':'Relire le dernier relevé du serveur';
  $('#refresh').setAttribute('aria-label',services?'Actualiser la liste des conteneurs':'Relire le dernier relevé du serveur Ubuntu');
  $('#rlbl').textContent=services?'Actualiser':'Relire';
  if(!services){renderUbuntu();restoreFocus();return}
  if(!D){$('#msg').replaceChildren(banner('bad','Impossible de joindre le tableau de bord','Vérifie que le conteneur DockPage est bien démarré.'));restoreFocus();return}
  const envSelect=$('#environment');
  if(activeEnv&&!D.environments.some(e=>String(e.id)===activeEnv))activeEnv='';
  const envOptions=JSON.stringify(D.environments.map(env=>[String(env.id),env.label||env.name]));
  if(envSelect.dataset.options!==envOptions){
    envSelect.replaceChildren(new Option('Tous les environnements',''));
    for(const env of D.environments)envSelect.append(new Option(env.label||env.name,String(env.id)));
    envSelect.dataset.options=envOptions;
  }
  envSelect.value=activeEnv;

  const msg=$('#msg');msg.replaceChildren();
  if(D.error)msg.append(banner('bad','Impossible de lire Portainer',friendly(D.error),D.error));
  const q=$('#q').value.trim().toLowerCase(),groups={};let nOk=0,nStop=0,nWarn=0;
  for(const env of D.environments){
    if(env.error)msg.append(banner('','Problème avec l\'environnement « '+(env.label||env.name)+' »',null,env.error));
    else if(!env.online)msg.append(banner('','L\'environnement « '+(env.label||env.name)+' » est hors ligne','Ses conteneurs ne sont pas affichés pour le moment.'));
    for(const c of env.containers){
      const o=D.items[c.key]||{},url=o.url||(c.ports[0]&&c.ports[0].url)||'',st=stateInfo(c);
      if(!o.hidden){if(st.cls==='ok')nOk++;else if(st.cls==='bad')nStop++;else if(st.cls==='warn')nWarn++}
      if(!edit&&activeEnv&&String(env.id)!==activeEnv)continue;
      if(o.hidden&&!edit)continue;
      if(!url&&!o.monitor&&!showAll&&!edit)continue;
      if(!edit&&q&&!(c.name+' '+c.image+' '+(o.name||'')).toLowerCase().includes(q))continue;
      const g=o.group||env.label||env.name;(groups[g]=groups[g]||[]).push({c,o,url,env,st});
    }
  }
  /* Résumé */
  const sm=$('#summary');sm.replaceChildren();
  if(D.environments.length){
    const monitored=Object.entries(D.items).filter(([key,o])=>o.monitor&&!o.hidden);
    const recent=monitored.filter(([key,o])=>{const m=(D.monitors||{})[key];return m&&m.mode===o.monitor&&(!o.monitorUrl||m.target===o.monitorUrl)&&Date.now()/1000-m.checked<Math.max(30,(o.monitorInterval||D.monitorInterval)*3)});
    const unavailable=recent.filter(([key])=>D.monitors[key].state==='down').length,pending=monitored.length-recent.length;
    if(unavailable||pending)nWarn+=unavailable+pending;
    const all=nOk+nStop+nWarn;
    sm.append(el('span','chip '+(nStop||nWarn?'warn':'ok'),all?(nStop||nWarn?'Quelques services demandent un coup d\'œil':'✨ Tout fonctionne bien'):'Aucun conteneur détecté'));
    if(monitored.length)sm.append(el('span','chip '+(unavailable?'bad':''),unavailable+' service(s) hors ligne / '+monitored.length+' surveillé(s)'+(pending?' · '+pending+' en attente':'')));
    if(nOk)sm.append(el('span','chip ok',nOk+' en marche'));
    if(nWarn)sm.append(el('span','chip warn',nWarn+' à surveiller'));
    if(nStop)sm.append(el('span','chip bad',nStop+' arrêté'+(nStop>1?'s':'')));
    const t=el('label','chip toggle'),cb=el('input');cb.type='checkbox';cb.id='nolink';cb.dataset.focus='show-all';cb.checked=showAll;
    cb.onchange=()=>{showAll=cb.checked;store.set('dp-all',showAll);render()};t.append(cb,document.createTextNode('Afficher aussi les conteneurs sans lien'));sm.append(t);
    if(D.updated)sm.append(el('span','upd','Mis à jour à '+D.updated));
  }
  const cats=D.categories||[];
  if(edit)for(const k of cats)groups[k]=groups[k]||[];  // catégories vides visibles pour y glisser des conteneurs
  const out=$('#out');out.replaceChildren();
  const go=D.groupOrder||[],pos=(arr,k)=>{const i=arr.indexOf(k);return i<0?1e9:i};
  const rank=g=>{let i=go.indexOf(g);if(i>=0)return i;i=cats.indexOf(g);return i>=0?1e6+i:1e9};
  const names=Object.keys(groups).sort((a,b)=>rank(a)-rank(b)||a.localeCompare(b));
  const filters=$('#group-filters');filters.replaceChildren();
  if(activeGroup&&!names.includes(activeGroup))activeGroup='';
  for(const g of ['',...names]){
    const tab=el('button','group-filter',g||'Tous');tab.type='button';tab.dataset.focus='group:'+g;
    tab.setAttribute('aria-pressed',String(activeGroup===g));tab.disabled=edit;
    tab.onclick=()=>{activeGroup=g;render()};filters.append(tab);
  }
  const visible=names.filter(g=>edit||!activeGroup||g===activeGroup).flatMap(g=>groups[g]);
  if(selectedKey===undefined&&visible.length)selectedKey=visible[0].c.key;
  const selected=visible.find(x=>x.c.key===selectedKey);
  if(!selected)selectedKey=null;
  renderInspector(selected);
  if(!names.length){
    const e=el('div','empty');
    if(!D.environments.length&&D.error){e.append(el('div','big','🔌'),el('h2',null,'En attente de Portainer'),el('p',null,'Dès que la connexion fonctionnera, tes conteneurs apparaîtront ici.'))}
    else if(q){e.append(el('div','big','🔍'),el('h2',null,'Aucun résultat'),el('p',null,'Rien ne correspond à « '+q+' ». Essaie un autre mot.'))}
    else{e.append(el('div','big','🌱'),el('h2',null,'Rien à afficher pour le moment'),el('p',null,'Aucun conteneur n\'a de port accessible. Coche « Afficher aussi les conteneurs sans lien » ou clique sur Personnaliser.'))}
    out.append(e);restoreFocus();return;
  }
  names.forEach((g,gi)=>{
    if(!edit&&activeGroup&&g!==activeGroup)return;
    const open_=edit||!collapsed.has(g);
    const sec=el('section','group'+(open_?'':' collapsed'));sec.style.setProperty('--h',hue(g));
    const h=el('h2','gh');
    const tg=el('button','gtoggle');tg.type='button';tg.dataset.focus='section:'+g;tg.setAttribute('aria-expanded',open_?'true':'false');tg.disabled=edit;
    tg.innerHTML=CHEV;const dot=el('span','gdot');tg.append(dot,document.createTextNode(g),el('span','count',String(groups[g].length)));
    tg.onclick=()=>{collapsed.has(g)?collapsed.delete(g):collapsed.add(g);store.set('dp-collapsed',[...collapsed]);render()};
    h.append(tg);
    if(edit){for(const [txt,d,tip] of [['▲',-1,'Monter la catégorie'],['▼',1,'Descendre la catégorie']]){const b=el('button','btn sm',txt);b.title=tip;b.dataset.focus='move-group:'+g+':'+d;b.disabled=gi+d<0||gi+d>=names.length;
      b.onclick=()=>{const n=names.slice();[n[gi],n[gi+d]]=[n[gi+d],n[gi]];post2('api/order',{groups:n})};h.append(b)}}
    sec.append(h);const grid=el('div','grid');
    const ord=(D.order||{})[g]||[];
    const list=groups[g].sort((a,b)=>pos(ord,a.c.key)-pos(ord,b.c.key)||(a.o.name||a.c.name).localeCompare(b.o.name||b.c.name));
    const keys=list.map(x=>x.c.key),save=k=>post2('api/order',{group:g,keys:k});
    // déplace un conteneur vers ce groupe : rangé dans la catégorie, ou retiré de sa catégorie si c'est un groupe d'environnement
    const moveTo=(key,idx)=>{const isCat=cats.includes(g),k=keys.filter(x=>x!==key);k.splice(idx==null?k.length:idx,0,key);
      post2('api/move',{key,group:isCat?g:null,keys:isCat?k:null})};
    if(edit){
      for(const t of [h,grid]){t.ondragover=e=>{e.preventDefault();t.classList.add('over')};t.ondragleave=()=>t.classList.remove('over');
        t.ondrop=e=>{e.preventDefault();t.classList.remove('over');const dk=e.dataTransfer.getData('text/plain');if(dk&&!keys.includes(dk))moveTo(dk)}}
      if(!list.length)grid.append(el('div','hint','Glisse un conteneur ici, ou utilise « Modifier » sous une icône.'));
    }
    list.forEach(({c,o,url,env,st},i)=>{
      const card=el('div','launcher'+(o.hidden?' dim':'')+(edit?' editing':''));
      const launch=el('button','launch'+(selectedKey===c.key?' selected':''));launch.type='button';
      launch.dataset.key=c.key;launch.dataset.focus='service:'+c.key;
      launch.setAttribute('aria-label','Afficher les détails de '+(o.name||c.name));
      launch.setAttribute('aria-controls','inspector');launch.setAttribute('aria-expanded',String(selectedKey===c.key));
      const tile=el('span','tile');fillIcon(tile,c,o);
      const health=serviceHealth(c,o);
      launch.append(tile,el('span','name',o.name||c.name),el('span','sub',env.label||env.name),
        el('span','pill '+st.cls,'Conteneur : '+st.txt),el('span','pill '+health.cls,'Service : '+health.txt));
      launch.onclick=()=>{selectedKey=c.key;render();$('#details-title').focus();
        if(matchMedia('(max-width:900px)').matches)$('#inspector').scrollIntoView({block:'start'})};
      card.append(launch);
      if(edit){
        const ctl=el('div','ctl'),mv=el('div','mv');
        for(const [txt,d,tip] of [['◀',-1,'Avancer'],['▶',1,'Reculer']]){const m=el('button','btn sm',txt);m.title=tip;m.dataset.focus='move-service:'+c.key+':'+d;m.disabled=i+d<0||i+d>=keys.length;
          m.onclick=e=>{e.preventDefault();const k=keys.slice();[k[i],k[i+d]]=[k[i+d],k[i]];save(k)};mv.append(m)}
        const b=el('button','btn sm','✏️ Modifier');b.dataset.focus='edit-service:'+c.key;b.onclick=e=>{e.preventDefault();openItem(c,o)};
        ctl.append(mv,b);card.append(ctl);
        card.draggable=true;
        card.ondragstart=e=>{e.dataTransfer.setData('text/plain',c.key);e.dataTransfer.effectAllowed='move';card.classList.add('drag')};
        card.ondragend=()=>card.classList.remove('drag');
        card.ondragover=e=>{e.preventDefault();card.classList.add('over')};
        card.ondragleave=()=>card.classList.remove('over');
        card.ondrop=e=>{e.preventDefault();e.stopPropagation();card.classList.remove('over');
          const dk=e.dataTransfer.getData('text/plain'),from=keys.indexOf(dk);
          if(!dk||dk===c.key)return;
          if(from<0)return moveTo(dk,i);  // vient d'un autre groupe
          const k=keys.slice();k.splice(i,0,k.splice(from,1)[0]);save(k)};
      }
      grid.append(card);
    });
    sec.append(grid);out.append(sec);
  });
  restoreFocus();
}

function renderServiceDetails({c,o,url,env,st}){
      const card=el('div','service-details');
      const head=el('div','head'),tile=el('div','tile'),body=el('div','body');
      fillIcon(tile,c,o);
      body.append(el('h2','name',o.name||c.name),el('div','sub',env.label||env.name));
      const metadata=el('div','detail-metadata');
      const release=el('div','sub metadata','Version : '+(c.deployedVersion||'Indisponible'));
      release.title=release.textContent;
      const formatDate=value=>typeof value==='number'&&value>0&&Number.isFinite(new Date(value*1000).getTime())
        ?new Date(value*1000).toLocaleString('fr-FR'):'Indisponible';
      const updated=el('div','sub metadata','Dernière mise à jour observée : '+formatDate(c.imageChangedAt));
      updated.title='Heure à laquelle DockPage a constaté un changement d’image, dans votre fuseau horaire. La date exacte de l’opération est inconnue. Indisponible avant le premier changement observé.';
      const deployed=el('div','sub metadata','Dernier déploiement : '+formatDate(c.deployedAt));
      deployed.title='Création du conteneur actuel. Une recréation avec la même image change cette date, un simple redémarrage ne la change pas.';
      metadata.append(release,updated,deployed,el('div','sub metadata','Image : '+c.image));
      head.append(tile,body);
      let since=fr(c.status);if(c.state==='running')since=since.replace(/^En marche /,'');
      const foot=el('div','foot');foot.append(el('span','pill '+st.cls,st.txt),el('span','since',since));
      const v=c.version||{},vs={current:['ok','Image à jour'],available:['warn','Mise à jour disponible'],unknown:['','Vérification impossible'],pinned:['','Image figée'],updated:['ok','Mise à jour effectuée'],error:['bad','Échec de mise à jour']};
      const [vc,vt]=vs[v.state]||['','Vérification en attente'];
      const badge=el('span','pill '+vc,vt);badge.title=(v.detail||'')+(v.checked?' — Vérifié le '+v.checked:'');foot.append(badge);
      if(o.group)foot.append(el('span','tag',env.label||env.name));  // d'où vient un conteneur rangé dans une catégorie
      card.append(head,foot);
      if(o.monitor){
        const m=(D.monitors||{})[c.key],fresh=m&&m.mode===o.monitor&&(!o.monitorUrl||m.target===o.monitorUrl)&&Date.now()/1000-m.checked<Math.max(30,(o.monitorInterval||D.monitorInterval)*3);
        const status=el('span','pill '+(fresh?(m.state==='up'?'ok':'bad'):''),fresh?(m.state==='up'?'En ligne':'Hors ligne'):'Contrôle en attente');
        status.title=fresh?m.detail+' — '+new Date(m.checked*1000).toLocaleString(): 'En attente d’un contrôle récent';
        const box=el('div','monitor'),interval=o.monitorInterval||D.monitorInterval;box.append(status);
        box.append(el('span','since',o.monitor.toUpperCase()+' · Toutes les '+interval+' s'));
        if(m){
          box.append(el('span','since','Dernier contrôle : '+new Date(m.checked*1000).toLocaleString()+' · '+m.detail));
          const next=m.checked+interval;
          box.append(el('span','since',next>Date.now()/1000?'Prochain contrôle prévu : '+new Date(next*1000).toLocaleTimeString():'Prochain contrôle attendu · résultat en attente'));
        }else box.append(el('span','since','Premier contrôle en attente'));
        if(m)box.title=m.target;
        if(fresh){
          box.append(el('span','since',(m.ms===null?'':m.ms+' ms · ')+m.uptime+' % sur '+m.history.length+' contrôles'));
          const bars=el('div','heartbeats');bars.setAttribute('aria-label','Historique des contrôles, du plus ancien au plus récent');
          for(const h of m.history){const b=el('span',h.state==='up'?'up':'down');b.title=new Date(h.checked*1000).toLocaleString()+' — '+(h.state==='up'?'En ligne':'Hors ligne')+' — '+h.detail;bars.append(b)}
          box.append(bars);
        }
        card.append(box);
      }
      if(!o.monitor)card.append(el('div','sub','Service : non surveillé'));
      card.append(metadata);
      if(c.ports.length){const p=el('div','ports');for(const x of c.ports){const a=el('a',null,'Port '+x.port);a.href=x.url;a.target='_blank';a.rel='noopener';a.onclick=e=>e.stopPropagation();p.append(a)}card.append(p)}

      const settings=el('button','btn','Réglages');settings.type='button';
      settings.dataset.focus='settings';settings.onclick=()=>openItem(c,o);
      const links=el('div','detail-actions');
      if(url){const open=el('a','btn primary','Ouvrir ↗');open.href=url;open.target='_blank';open.rel='noopener';open.dataset.focus='open-service';links.append(open)}
      links.append(settings);card.append(links);
      const action=el('div','update-action'),button=el('button','btn sm',updating.has(c.key)?'Mise à jour en cours…':'Mettre à jour');
      button.type='button';
      const blocked=v.updateBlocked||(v.state==='current'?'Image déjà à jour':v.state!=='available'?'Aucune mise à jour confirmée':'');
      button.disabled=updating.has(c.key)||!!blocked;
      button.title=blocked||'Télécharger le tag actuel et recréer ce conteneur';
      button.setAttribute('aria-label','Mettre à jour '+(o.name||c.name));
      button.onclick=()=>updateContainer(env,c,o);
      const result=el('div','sub metadata',updateResults.get(c.key)||blocked);
      result.setAttribute('role','status');action.append(button,result);

      button.dataset.focus='update-service';card.append(action);return card;
}
function serviceHealth(c,o){
  if(!o.monitor)return {cls:'',txt:'Non surveillé'};
  const m=(D.monitors||{})[c.key];
  const fresh=m&&m.mode===o.monitor&&(!o.monitorUrl||m.target===o.monitorUrl)&&Date.now()/1000-m.checked<Math.max(30,(o.monitorInterval||D.monitorInterval)*3);
  return fresh?{cls:m.state==='up'?'ok':'bad',txt:m.state==='up'?'En ligne':'Hors ligne'}:{cls:'',txt:'En attente'};
}
function focusLauncher(key){
  const target=[...document.querySelectorAll('.launch')].find(b=>b.dataset.key===key);
  if(target)target.focus();
}
function closeDetails(){const key=selectedKey;selectedKey=null;render();focusLauncher(key)}
function renderInspector(entry){
  const panel=$('#inspector');panel.hidden=!entry;$('#workspace').classList.toggle('has-details',!!entry);
  $('#details-body').replaceChildren();
  if(entry)$('#details-body').append(renderServiceDetails(entry));
}

async function updateContainer(env,c,o){
  if(updating.has(c.key))return;
  if(!confirm('Mettre à jour '+(o.name||c.name)+' ? Le tag actuel sera conservé. Le service sera interrompu pendant la recréation. Les données hors volumes seront perdues.'))return;
  updating.add(c.key);updateResults.delete(c.key);render();
  try{
    const r=await fetch('api/update',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({endpoint:env.id,id:c.id})});
    const result=await r.json();
    if(!r.ok)throw Error(result.error||'Mise à jour impossible');
    updateResults.set(c.key,result.message);
  }catch(e){updateResults.set(c.key,e.message+' Aucune nouvelle tentative automatique.')}
  finally{updating.delete(c.key);render();load()}
}
// Icône : champ « icône » du conteneur (URL, nom de la collection ou emoji), sinon détection automatique
function fillIcon(box,c,o){
  const fallback=()=>box.replaceChildren(document.createTextNode('🐳'));
  const img=src=>{const i=document.createElement('img');i.alt='';i.loading='lazy';i.onerror=fallback;i.src=src;box.replaceChildren(i)};
  const v=(o.icon||'').trim();
  if(/^https?:\/\//i.test(v))img(v);
  else if(/^[a-z0-9][a-z0-9._-]*$/i.test(v))img('icon?n='+encodeURIComponent(v.toLowerCase()));
  else if(v)box.textContent=v;
  else if(c.icons&&c.icons.length)img('icon?'+c.icons.map(n=>'n='+encodeURIComponent(n)).join('&'));
  else fallback();
}
async function post2(url,body){await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});load()}

/* Fenêtre « Modifier » */
function openItem(c,o){cur=c;$('#dt').textContent=c.key;
  const sel=$('#f_group');sel.replaceChildren(new Option('Aucune (selon l\'environnement)',''));
  for(const k of D.categories||[])sel.append(new Option(k,k));
  for(const k of ['name','url','group','icon','monitor','monitorUrl'])$('#f_'+k).value=o[k]||'';
  $('#f_monitorInterval').value=o.monitorInterval||'';
  $('#f_monitorInterval').placeholder=D.monitorInterval+' (défaut)';
  $('#f_name').placeholder=c.name;
  $('#itemError').textContent='';
  $('#f_hidden').checked=!!o.hidden;$('#dlg').showModal()}
async function post(data){try{const r=await fetch('api/item',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key:cur.key,data})});if(!r.ok)throw Error((await r.json()).error||'Enregistrement impossible');$('#dlg').close();load()}catch(e){$('#itemError').textContent=e.message}}
$('#save').onclick=e=>{e.preventDefault();if(!$('#f_monitorInterval').reportValidity())return;const d={monitorInterval:$('#f_monitorInterval').value?Number($('#f_monitorInterval').value):null,hidden:$('#f_hidden').checked};for(const k of ['name','url','group','icon','monitor','monitorUrl'])d[k]=$('#f_'+k).value.trim();post(d)};
$('#reset').onclick=()=>post(null);$('#cancel').onclick=()=>$('#dlg').close();

/* Fenêtre « Catégories » */
function catRow(name,old){
  const row=el('div','crow'),i=el('input');i.type='text';i.value=name;i.maxLength=40;i.dataset.old=old||'';i.setAttribute('aria-label','Nom de la catégorie');
  const d=el('button','btn sm','✕');d.type='button';d.title='Supprimer cette catégorie';d.onclick=()=>row.remove();row.append(i,d);return row}
$('#catbtn').onclick=()=>{const l=$('#clist');l.replaceChildren();for(const k of D.categories||[])l.append(catRow(k,k));$('#cnew').value='';$('#cdlg').showModal()};
function addCat(){const v=$('#cnew').value.trim();if(v){$('#clist').append(catRow(v,''));$('#cnew').value=''}$('#cnew').focus()}
$('#cadd').onclick=addCat;$('#cnew').onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();addCat()}};
$('#cclose').onclick=()=>$('#cdlg').close();
$('#csave').onclick=async()=>{addCat();
  const categories=[...document.querySelectorAll('#clist input')].map(i=>({name:i.value.trim(),old:i.dataset.old||null}));
  await fetch('api/categories',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({categories})});
  $('#cdlg').close();load()};

/* Fenêtre « Environnements » */
const TYPES={1:'Docker local',2:'Agent Portainer',4:'Edge Agent'};
function openEnvs(){
  const list=$('#elist');list.replaceChildren();
  for(const env of D.environments){
    const s=(D.envs||{})[env.name]||{},box=el('div','envbox');
    box.append(el('strong',null,env.name+(env.online?'':' (hors ligne)')));
    box.append(el('div','meta',(TYPES[env.type]||'Type '+env.type)+(env.url?' – '+env.url:'')));
    box.append(el('div','meta used','Adresse utilisée : '+env.host+' ('+env.hostSource+')'));
    for(const [k,lab,ph] of [['label','Nom affiché',env.name],['host','Adresse IP ou nom d\'hôte pour les liens',env.host]]){
      const l=el('label',null,lab),i=el('input');i.type='text';i.value=s[k]||'';i.placeholder=ph;i.dataset.env=env.name;i.dataset.k=k;
      l.append(i);box.append(l);
    }
    list.append(box);
  }
  $('#edlg').showModal();
}
$('#envbtn').onclick=openEnvs;$('#eclose').onclick=()=>$('#edlg').close();
$('#esave').onclick=async()=>{
  const envs={};
  for(const i of document.querySelectorAll('#elist input')){(envs[i.dataset.env]=envs[i.dataset.env]||{})[i.dataset.k]=i.value.trim()}
  $('#esave').disabled=true;
  try{await fetch('api/envs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({envs})})}finally{$('#esave').disabled=false}
  $('#edlg').close();load();
};

/* Barre du haut */
function openPage(page){activePage=page;store.set('dp-page',page);render()}
$('#services-tab').onclick=()=>openPage('services');$('#ubuntu-tab').onclick=()=>openPage('ubuntu');
$('.main-tabs').onkeydown=e=>{if(e.key==='ArrowLeft'||e.key==='ArrowRight'){
  e.preventDefault();const page=activePage==='services'?'ubuntu':'services';openPage(page);$('#'+page+'-tab').focus()}};
$('#edit').onclick=()=>{edit=!edit;$('#environment').disabled=edit;$('#edit').textContent=edit?'✓ Terminer':'✏️ Personnaliser';$('#edit').classList.toggle('primary',!edit);$('#editbar').hidden=!edit;render()};
$('#q').oninput=render;
$('#ubuntu-filter').oninput=renderUbuntu;
$('#environment').onchange=e=>{activeEnv=e.target.value;render()};
$('#close-details').onclick=closeDetails;
$('#refresh').onclick=async()=>{
  const b=$('#refresh'),l=$('#rlbl');b.disabled=true;b.classList.add('spin');l.textContent='Actualisation…';
  try{if(activePage==='services'){const r=await fetch('api/refresh',{method:'POST'});if(!r.ok)throw Error('Actualisation impossible')}
    if(!await load())throw Error('Lecture impossible');l.textContent=activePage==='services'?'Actualisé ✓':'Relevé relu ✓'}
  catch(e){l.textContent='Échec'}
  finally{b.disabled=false;b.classList.remove('spin');setTimeout(()=>{l.textContent=activePage==='services'?'Actualiser':'Relire'},2000)}};
$('#theme').onclick=()=>{const cur=document.documentElement.dataset.theme||(matchMedia('(prefers-color-scheme:dark)').matches?'dark':'light'),next=cur==='dark'?'light':'dark';
  document.documentElement.dataset.theme=next;store.set('dp-theme',next)};
document.addEventListener('keydown',e=>{
  if(e.key==='/'&&!/input|select|textarea/i.test(document.activeElement.tagName)){e.preventDefault();$('#q').focus()}
  else if(e.key==='Escape'&&document.activeElement===$('#q')){$('#q').value='';render();$('#q').blur()}
  else if(e.key==='Escape'&&selectedKey&&!document.querySelector('dialog[open]'))closeDetails();});
hello();setInterval(hello,30000);
if(activePage==='ubuntu')render();
load();setInterval(load,5000);
</script></body></html>"""

if __name__ == "__main__":
    threading.Thread(target=refresher, daemon=True).start()
    threading.Thread(target=monitor_loop, daemon=True).start()
    print("Tableau de bord sur le port %d" % PORT, flush=True)
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
