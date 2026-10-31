#!/usr/bin/env python3
"""Tableau de bord alimenté par l'API de Portainer (bibliothèque standard uniquement)."""
import json
import os
import re
import ssl
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORTAINER_URL = os.environ.get("PORTAINER_URL", "").rstrip("/")
TOKEN = os.environ.get("PORTAINER_TOKEN", "")
VERIFY_TLS = os.environ.get("VERIFY_TLS", "true").lower() == "true"
REFRESH = int(os.environ.get("REFRESH_SECONDS", "30"))
PORT = int(os.environ.get("PORT", "8080"))
TITLE = os.environ.get("TITLE", "Homelab")
CONFIG_PATH = os.environ.get("CONFIG_PATH", "/data/config.json")
# "NomEnvironnement=192.168.1.20,Autre=nas.local"
HOSTS = dict(p.split("=", 1) for p in os.environ.get("HOSTS", "").split(",") if "=" in p)
HTTPS_PORTS = (443, 8443, 9443)
# Collection d'icônes libre « dashboard-icons » (dossiers svg/ et png/), mise en cache dans /data/icons
ICON_BASE = os.environ.get("ICON_BASE", "https://raw.githubusercontent.com/homarr-labs/dashboard-icons/main").rstrip("/")
ICON_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,60}$")
GENERIC_OWNERS = {"library", "linuxserver", "lscr.io", "ghcr.io", "docker.io", "quay.io", "gcr.io"}

lock = threading.Lock()
cache = {"environments": [], "updated": None, "error": None}


def api(path):
    req = urllib.request.Request(PORTAINER_URL + "/api" + path, headers={"X-API-Key": TOKEN})
    ctx = None if VERIFY_TLS else ssl._create_unverified_context()
    with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
        return json.load(r)


def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.replace(tmp, CONFIG_PATH)


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
        entry = {"name": ep["Name"], "label": saved.get(ep["Name"], {}).get("label", ""),
                 "type": ep.get("Type"), "url": ep.get("URL", ""), "host": host, "hostSource": source,
                 "online": ep.get("Status") == 1, "containers": []}
        if entry["online"]:
            try:
                for c in api("/endpoints/%s/docker/containers/json?all=1" % ep["Id"]):
                    name = (c.get("Names") or ["/?"])[0].lstrip("/")
                    ports, seen = [], set()
                    for p in c.get("Ports") or []:
                        pub = p.get("PublicPort")
                        if not pub or p.get("Type", "tcp") != "tcp" or pub in seen:
                            continue
                        seen.add(pub)
                        https = pub in HTTPS_PORTS or p.get("PrivatePort") in HTTPS_PORTS
                        ports.append({"port": pub, "url": "%s://%s:%s" % ("https" if https else "http", host, pub)})
                    entry["containers"].append({
                        "key": "%s/%s" % (ep["Name"], name), "name": name, "image": c.get("Image", ""),
                        "state": c.get("State", ""), "status": c.get("Status", ""), "ports": ports,
                        "icons": icon_candidates(c.get("Image", ""), name),
                    })
            except Exception as e:  # un environnement en erreur n'empêche pas les autres
                entry["error"] = str(e)
        envs.append(entry)
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
                               categories=categories_of(cfg))
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
        if self.path not in ("/api/item", "/api/envs", "/api/order", "/api/categories", "/api/move"):
            return self.send(404, "{}")
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
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
                if data:
                    items[key] = {k: v for k, v in data.items()
                                  if k in ("name", "url", "group", "icon", "hidden") and v not in ("", None, False)}
                else:
                    items.pop(key, None)
            else:
                envs = {n: {k: v.strip() for k, v in e.items()
                            if k in ("host", "label") and isinstance(v, str) and v.strip()}
                        for n, e in body["envs"].items()}
                cfg["envs"] = {n: e for n, e in envs.items() if e}
            save_config(cfg)
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
  --bg:#f7f3ec;--surface:#fff;--surface2:#f1ebe0;--tile:#f3eee5;--text:#2b2a33;--muted:#6b6774;--line:#e6dfd2;
  --accent:#d9480f;--accent-ink:#fff;--accent-soft:#fdebe2;
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
body{margin:0;background:var(--bg);color:var(--text);font:16px/1.5 ui-rounded,"SF Pro Rounded","Segoe UI",system-ui,-apple-system,Roboto,sans-serif;-webkit-font-smoothing:antialiased}
button,input,select{font:inherit;color:inherit}
:focus-visible{outline:3px solid color-mix(in srgb,var(--accent) 55%,transparent);outline-offset:2px}

/* Barre du haut */
header{position:sticky;top:0;z-index:5;display:flex;flex-wrap:wrap;gap:14px 20px;align-items:center;justify-content:space-between;padding:14px max(16px,calc((100% - 1240px)/2 + 24px));background:color-mix(in srgb,var(--bg) 82%,transparent);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);border-bottom:1px solid var(--line)}
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

main{max-width:1240px;margin:0 auto;padding:22px 24px 80px}

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
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,270px),1fr));gap:14px}
.grid>*{min-width:0}
.group.collapsed .grid{display:none}

/* Cartes */
.card{position:relative;display:flex;flex-direction:column;gap:12px;padding:16px;background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);box-shadow:var(--shadow);color:inherit;text-decoration:none;transition:transform .15s,box-shadow .15s,border-color .15s}
a.card:hover{transform:translateY(-3px);box-shadow:var(--shadow-hover);border-color:color-mix(in srgb,var(--accent) 45%,var(--line))}
.card.nolink{background:transparent;box-shadow:none;border-style:dashed}
.card.dim{opacity:.5}
.head{display:flex;align-items:center;gap:13px}
.tile{width:48px;height:48px;border-radius:15px;background:var(--tile);display:grid;place-items:center;font-size:25px;flex:none}
.tile img{width:32px;height:32px;object-fit:contain}
.body{min-width:0;flex:1}
.name{font-weight:700;font-size:1.04rem;line-height:1.3;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sub{color:var(--muted);font-size:.8rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
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
.card.editing{cursor:grab;border-style:dashed;border-color:color-mix(in srgb,var(--accent) 40%,var(--line))}
.card.drag{opacity:.35}
.over{outline:3px dashed var(--accent);outline-offset:3px;border-radius:14px}
.hint{grid-column:1/-1;padding:22px;text-align:center;color:var(--muted);border:2px dashed var(--line);border-radius:var(--radius)}

/* États vides et chargement */
.empty{text-align:center;padding:64px 20px;color:var(--muted)}
.empty .big{font-size:3.2rem;line-height:1.2}.empty h2{margin:10px 0 6px;color:var(--text);font-size:1.3rem}.empty p{margin:0 auto;max-width:460px}
.skel{height:116px;background:linear-gradient(100deg,var(--surface) 30%,var(--surface2) 50%,var(--surface) 70%);background-size:200% 100%;animation:sh 1.4s linear infinite;border:1px solid var(--line);border-radius:var(--radius)}
@keyframes sh{to{background-position:-200% 0}}

/* Fenêtres */
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
  <div id="msg"></div>
  <div id="summary" class="summary"></div>
  <div id="editbar" class="editbar" hidden>
    <p><strong>Mode personnalisation.</strong> Glisse les cartes pour les déplacer (ou utilise les flèches), et pose-les sur une catégorie pour les y ranger. « Modifier » change le nom, l'icône ou la catégorie.</p>
    <div class="acts"><button class="btn sm" id="catbtn">🗂️ Catégories</button><button class="btn sm" id="envbtn">🖥️ Environnements</button></div>
  </div>
  <div id="out"><div class="grid"><div class="skel"></div><div class="skel"></div><div class="skel"></div></div></div>
</main>

<dialog id="dlg"><form method="dialog" onsubmit="return false">
  <h3>Modifier la carte</h3><div class="lead key" id="dt"></div>
  <label>Nom affiché<input type="text" id="f_name"></label>
  <label>Lien à ouvrir<small>Laisser vide pour utiliser le port détecté.</small><input type="text" id="f_url" placeholder="http://192.168.1.210:3210"></label>
  <label>Catégorie<select id="f_group"></select></label>
  <label>Icône<small>Vide = automatique. Sinon : un nom de la collection (ex. jellyfin), une URL d'image ou un emoji.</small><input type="text" id="f_icon" placeholder="jellyfin, https://…/logo.png ou 🎬"></label>
  <label class="chk"><input type="checkbox" id="f_hidden">Masquer ce conteneur</label>
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
const $=s=>document.querySelector(s);let D=null,edit=false,cur=null;
const el=(t,c,x)=>{const e=document.createElement(t);if(c)e.className=c;if(x!=null)e.textContent=x;return e};
const store={get(k,d){try{const v=localStorage.getItem(k);return v==null?d:JSON.parse(v)}catch(e){return d}},set(k,v){try{localStorage.setItem(k,JSON.stringify(v))}catch(e){}}};
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

async function load(){try{const r=await fetch('api/data');D=await r.json();render()}catch(e){$('#msg').replaceChildren(banner('bad','Impossible de joindre le tableau de bord','Vérifie que le conteneur DockPage est bien démarré.'))}}

function render(){
  const msg=$('#msg');msg.replaceChildren();
  if(D.error)msg.append(banner('bad','Impossible de lire Portainer',friendly(D.error),D.error));
  const q=$('#q').value.trim().toLowerCase(),groups={};let nOk=0,nStop=0,nWarn=0;
  for(const env of D.environments){
    if(env.error)msg.append(banner('','Problème avec l\'environnement « '+(env.label||env.name)+' »',null,env.error));
    else if(!env.online)msg.append(banner('','L\'environnement « '+(env.label||env.name)+' » est hors ligne','Ses conteneurs ne sont pas affichés pour le moment.'));
    for(const c of env.containers){
      const o=D.items[c.key]||{},url=o.url||(c.ports[0]&&c.ports[0].url)||'',st=stateInfo(c);
      if(!o.hidden){if(st.cls==='ok')nOk++;else if(st.cls==='bad')nStop++;else if(st.cls==='warn')nWarn++}
      if(o.hidden&&!edit)continue;
      if(!url&&!showAll&&!edit)continue;
      if(!edit&&q&&!(c.name+' '+c.image+' '+(o.name||'')).toLowerCase().includes(q))continue;
      const g=o.group||env.label||env.name;(groups[g]=groups[g]||[]).push({c,o,url,env,st});
    }
  }
  /* Résumé */
  const sm=$('#summary');sm.replaceChildren();
  if(D.environments.length){
    const all=nOk+nStop+nWarn;
    sm.append(el('span','chip '+(nStop||nWarn?'warn':'ok'),all?(nStop||nWarn?'Quelques services demandent un coup d\'œil':'✨ Tout fonctionne bien'):'Aucun conteneur détecté'));
    if(nOk)sm.append(el('span','chip ok',nOk+' en marche'));
    if(nWarn)sm.append(el('span','chip warn',nWarn+' à surveiller'));
    if(nStop)sm.append(el('span','chip bad',nStop+' arrêté'+(nStop>1?'s':'')));
    const t=el('label','chip toggle'),cb=el('input');cb.type='checkbox';cb.id='nolink';cb.checked=showAll;
    cb.onchange=()=>{showAll=cb.checked;store.set('dp-all',showAll);render()};t.append(cb,document.createTextNode('Afficher aussi les conteneurs sans lien'));sm.append(t);
    if(D.updated)sm.append(el('span','upd','Mis à jour à '+D.updated));
  }
  const cats=D.categories||[];
  if(edit)for(const k of cats)groups[k]=groups[k]||[];  // catégories vides visibles pour y glisser des conteneurs
  const out=$('#out');out.replaceChildren();
  const go=D.groupOrder||[],pos=(arr,k)=>{const i=arr.indexOf(k);return i<0?1e9:i};
  const rank=g=>{let i=go.indexOf(g);if(i>=0)return i;i=cats.indexOf(g);return i>=0?1e6+i:1e9};
  const names=Object.keys(groups).sort((a,b)=>rank(a)-rank(b)||a.localeCompare(b));
  if(!names.length){
    const e=el('div','empty');
    if(!D.environments.length&&D.error){e.append(el('div','big','🔌'),el('h2',null,'En attente de Portainer'),el('p',null,'Dès que la connexion fonctionnera, tes conteneurs apparaîtront ici.'))}
    else if(q){e.append(el('div','big','🔍'),el('h2',null,'Aucun résultat'),el('p',null,'Rien ne correspond à « '+q+' ». Essaie un autre mot.'))}
    else{e.append(el('div','big','🌱'),el('h2',null,'Rien à afficher pour le moment'),el('p',null,'Aucun conteneur n\'a de port accessible. Coche « Afficher aussi les conteneurs sans lien » ou clique sur Personnaliser.'))}
    out.append(e);return;
  }
  names.forEach((g,gi)=>{
    const open_=edit||!collapsed.has(g);
    const sec=el('section','group'+(open_?'':' collapsed'));sec.style.setProperty('--h',hue(g));
    const h=el('h2','gh');
    const tg=el('button','gtoggle');tg.type='button';tg.setAttribute('aria-expanded',open_?'true':'false');tg.disabled=edit;
    tg.innerHTML=CHEV;const dot=el('span','gdot');tg.append(dot,document.createTextNode(g),el('span','count',String(groups[g].length)));
    tg.onclick=()=>{collapsed.has(g)?collapsed.delete(g):collapsed.add(g);store.set('dp-collapsed',[...collapsed]);render()};
    h.append(tg);
    if(edit){for(const [txt,d,tip] of [['▲',-1,'Monter la catégorie'],['▼',1,'Descendre la catégorie']]){const b=el('button','btn sm',txt);b.title=tip;b.disabled=gi+d<0||gi+d>=names.length;
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
      if(!list.length)grid.append(el('div','hint','Glisse un conteneur ici, ou utilise « Modifier » sur une carte.'));
    }
    list.forEach(({c,o,url,env,st},i)=>{
      const card=el(url?'a':'div','card'+(o.hidden?' dim':'')+(url?'':' nolink')+(edit?' editing':''));
      if(url){card.href=url;card.target='_blank';card.rel='noopener'}
      const head=el('div','head'),tile=el('div','tile'),body=el('div','body');
      fillIcon(tile,c,o);
      body.append(el('div','name',o.name||c.name),el('div','sub',c.image));
      head.append(tile,body);
      let since=fr(c.status);if(c.state==='running')since=since.replace(/^En marche /,'');
      const foot=el('div','foot');foot.append(el('span','pill '+st.cls,st.txt),el('span','since',since));
      if(o.group)foot.append(el('span','tag',env.label||env.name));  // d'où vient un conteneur rangé dans une catégorie
      card.append(head,foot);
      if(c.ports.length>1){const p=el('div','ports');for(const x of c.ports){const a=el('a',null,'Port '+x.port);a.href=x.url;a.target='_blank';a.rel='noopener';a.onclick=e=>e.stopPropagation();p.append(a)}card.append(p)}
      if(edit){
        card.onclick=e=>e.preventDefault();
        const ctl=el('div','ctl'),mv=el('div','mv');
        for(const [txt,d,tip] of [['◀',-1,'Avancer'],['▶',1,'Reculer']]){const m=el('button','btn sm',txt);m.title=tip;m.disabled=i+d<0||i+d>=keys.length;
          m.onclick=e=>{e.preventDefault();const k=keys.slice();[k[i],k[i+d]]=[k[i+d],k[i]];save(k)};mv.append(m)}
        const b=el('button','btn sm','✏️ Modifier');b.onclick=e=>{e.preventDefault();openItem(c,o)};
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
  for(const k of ['name','url','group','icon'])$('#f_'+k).value=o[k]||'';
  $('#f_name').placeholder=c.name;
  $('#f_hidden').checked=!!o.hidden;$('#dlg').showModal()}
async function post(data){await fetch('api/item',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key:cur.key,data})});$('#dlg').close();load()}
$('#save').onclick=e=>{e.preventDefault();const d={hidden:$('#f_hidden').checked};for(const k of ['name','url','group','icon'])d[k]=$('#f_'+k).value.trim();post(d)};
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
$('#edit').onclick=()=>{edit=!edit;$('#edit').textContent=edit?'✓ Terminer':'✏️ Personnaliser';$('#edit').classList.toggle('primary',!edit);$('#editbar').hidden=!edit;render()};
$('#q').oninput=render;
$('#refresh').onclick=async()=>{
  const b=$('#refresh'),l=$('#rlbl');b.disabled=true;b.classList.add('spin');l.textContent='Actualisation…';
  try{await fetch('api/refresh',{method:'POST'});await load();l.textContent='À jour ✓'}
  catch(e){l.textContent='Échec'}
  finally{b.disabled=false;b.classList.remove('spin');setTimeout(()=>{l.textContent='Actualiser'},2000)}};
$('#theme').onclick=()=>{const cur=document.documentElement.dataset.theme||(matchMedia('(prefers-color-scheme:dark)').matches?'dark':'light'),next=cur==='dark'?'light':'dark';
  document.documentElement.dataset.theme=next;store.set('dp-theme',next)};
document.addEventListener('keydown',e=>{
  if(e.key==='/'&&!/input|select|textarea/i.test(document.activeElement.tagName)){e.preventDefault();$('#q').focus()}
  else if(e.key==='Escape'&&document.activeElement===$('#q')){$('#q').value='';render();$('#q').blur()}});
hello();setInterval(hello,30000);
load();setInterval(load,30000);
</script></body></html>"""

if __name__ == "__main__":
    threading.Thread(target=refresher, daemon=True).start()
    print("Tableau de bord sur le port %d" % PORT, flush=True)
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
