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


def refresh():
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
                               order=cfg.get("order", {}), groupOrder=cfg.get("groupOrder", []))
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
        if self.path not in ("/api/item", "/api/envs", "/api/order"):
            return self.send(404, "{}")
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            cfg = load_config()
            if self.path == "/api/order":
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
<style>
:root{--bg:#f4f5f7;--card:#fff;--text:#1c2330;--muted:#6b7585;--line:#dde1e8;--accent:#2f6fed;--ok:#1f9d55;--bad:#d64545}
@media (prefers-color-scheme:dark){:root{--bg:#12151b;--card:#1b2029;--text:#e6e9ef;--muted:#8d97a8;--line:#2b3240;--accent:#6aa0ff;--ok:#3ecf7b;--bad:#ff6b6b}.ico img{background:#fff;border-radius:6px;padding:2px}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.4 system-ui,sans-serif}
header{display:flex;flex-wrap:wrap;gap:12px;align-items:center;padding:16px;border-bottom:1px solid var(--line)}
h1{font-size:20px;margin:0 auto 0 0}
input[type=search],input[type=text],button{font:inherit;color:var(--text);background:var(--card);border:1px solid var(--line);border-radius:8px;padding:7px 10px}
button{cursor:pointer}button.on{border-color:var(--accent);color:var(--accent)}
label.chk{display:flex;gap:6px;align-items:center;color:var(--muted)}
main{padding:16px;max-width:1300px;margin:auto}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:22px 0 10px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:10px}
.card{position:relative;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px;display:block;color:inherit;text-decoration:none}
a.card:hover{border-color:var(--accent)}.card.dim{opacity:.45}.card.nolink{opacity:.8}
.top{display:flex;align-items:center;gap:8px}.ico{width:26px;height:26px;display:flex;align-items:center;justify-content:center;font-size:20px;flex:none}.ico img{max-width:26px;max-height:26px;object-fit:contain}
.card.editing{padding-bottom:36px;cursor:grab}.card.over{outline:2px dashed var(--accent)}.card.drag{opacity:.4}
.arrows{position:absolute;left:8px;bottom:6px;display:flex;gap:4px}.arrows button,h2 button{padding:0 7px;font-size:12px;line-height:20px}h2 button{margin-left:6px}.name{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.dot{width:9px;height:9px;border-radius:50%;background:var(--bad);margin-left:auto;flex:none}.dot.ok{background:var(--ok)}
.meta{color:var(--muted);font-size:12px;margin-top:4px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ports{display:flex;flex-wrap:wrap;gap:4px;margin-top:6px}.ports a{font-size:12px;padding:1px 7px;border:1px solid var(--line);border-radius:99px;color:var(--accent);text-decoration:none}
.edit{position:absolute;right:6px;bottom:6px;padding:1px 7px;font-size:12px}
.msg{padding:10px 14px;border:1px solid var(--bad);border-radius:8px;margin-bottom:12px;color:var(--bad)}
dialog{border:1px solid var(--line);border-radius:12px;background:var(--card);color:var(--text);width:min(420px,92vw)}
dialog::backdrop{background:#0008}dialog label{display:block;margin:10px 0 4px;color:var(--muted);font-size:13px}dialog input[type=text]{width:100%}
.row{display:flex;gap:8px;justify-content:flex-end;margin-top:16px}
.envbox{border-top:1px solid var(--line);margin-top:12px;padding-top:4px}.envbox .meta{white-space:normal}
</style></head><body>
<header><h1>__TITLE__</h1>
<input id="q" type="search" placeholder="Rechercher…">
<label class="chk"><input id="nolink" type="checkbox">Sans port</label>
<button id="envbtn">Environnements</button><button id="edit">Personnaliser</button><span id="upd" class="meta"></span></header>
<main><div id="msg"></div><div id="out"></div></main>
<dialog id="dlg"><form method="dialog" id="f">
<strong id="dt"></strong>
<label>Nom affiché</label><input type="text" id="f_name">
<label>Lien (URL complète)</label><input type="text" id="f_url" placeholder="http://192.168.1.210:3210">
<label>Groupe</label><input type="text" id="f_group">
<label>Icône (vide = automatique ; sinon nom de la collection, URL d'image ou emoji)</label><input type="text" id="f_icon" placeholder="ex. jellyfin, https://…/logo.png ou 🎬">
<label class="chk" style="margin-top:12px"><input type="checkbox" id="f_hidden">Masquer ce conteneur</label>
<div class="row"><button type="button" id="reset">Réinitialiser</button><button type="button" id="cancel">Annuler</button><button id="save" value="ok">Enregistrer</button></div>
</form></dialog>
<dialog id="edlg"><form method="dialog" onsubmit="return false">
<strong>Environnements Portainer</strong>
<div class="meta" style="white-space:normal;margin-top:4px">Pour chaque environnement : le nom affiché dans le tableau de bord et l'adresse IP (ou nom d'hôte) utilisée pour construire les liens. Laisser vide pour l'adresse détectée.</div>
<div id="elist"></div>
<div class="row"><button type="button" id="eclose">Fermer</button><button type="button" id="esave">Enregistrer</button></div>
</form></dialog>
<script>
const $=s=>document.querySelector(s);let D=null,edit=false,cur=null;
const el=(t,c,x)=>{const e=document.createElement(t);if(c)e.className=c;if(x!=null)e.textContent=x;return e};
async function load(){try{const r=await fetch('api/data');D=await r.json();render()}catch(e){$('#msg').replaceChildren(el('div','msg','Impossible de joindre le tableau de bord'))}}
function render(){
  const msg=$('#msg');msg.replaceChildren();
  if(D.error)msg.append(el('div','msg','Erreur Portainer : '+D.error));
  $('#upd').textContent=D.updated?'Mis à jour '+D.updated:'';
  const q=$('#q').value.toLowerCase(),nolink=$('#nolink').checked,groups={};
  for(const env of D.environments){
    if(env.error)msg.append(el('div','msg',env.name+' : '+env.error));
    if(!env.online)msg.append(el('div','msg',env.name+' : environnement hors ligne'));
    for(const c of env.containers){
      const o=D.items[c.key]||{},url=o.url||(c.ports[0]&&c.ports[0].url)||'';
      if(o.hidden&&!edit)continue;
      if(!url&&!nolink&&!edit)continue;
      if(!edit&&q&&!(c.name+' '+c.image+' '+(o.name||'')).toLowerCase().includes(q))continue;
      const g=o.group||env.label||env.name;(groups[g]=groups[g]||[]).push({c,o,url});
    }
  }
  const out=$('#out');out.replaceChildren();
  const go=D.groupOrder||[],pos=(arr,k)=>{const i=arr.indexOf(k);return i<0?1e9:i};
  const names=Object.keys(groups).sort((a,b)=>pos(go,a)-pos(go,b)||a.localeCompare(b));
  names.forEach((g,gi)=>{
    const h=el('h2',null,g);
    if(edit){for(const [txt,d] of [['▲',-1],['▼',1]]){const b=el('button',null,txt);b.disabled=gi+d<0||gi+d>=names.length;
      b.onclick=()=>{const n=names.slice();[n[gi],n[gi+d]]=[n[gi+d],n[gi]];post2('api/order',{groups:n})};h.append(b)}}
    out.append(h);const grid=el('div','grid');
    const ord=(D.order||{})[g]||[];
    const list=groups[g].sort((a,b)=>pos(ord,a.c.key)-pos(ord,b.c.key)||(a.o.name||a.c.name).localeCompare(b.o.name||b.c.name));
    const keys=list.map(x=>x.c.key),save=k=>post2('api/order',{group:g,keys:k});
    list.forEach(({c,o,url},i)=>{
      const card=el(url?'a':'div','card'+(o.hidden?' dim':'')+(url?'':' nolink')+(edit?' editing':''));
      if(url){card.href=url;card.target='_blank';card.rel='noopener'}
      const top=el('div','top');top.append(el('span','ico',null),el('span','name',o.name||c.name),el('span','dot'+(c.state==='running'?' ok':'')));
      fillIcon(top.firstChild,c,o);
      card.append(top,el('div','meta',c.image),el('div','meta',c.status));
      if(c.ports.length>1){const p=el('div','ports');for(const x of c.ports){const a=el('a',null,':'+x.port);a.href=x.url;a.target='_blank';a.rel='noopener';p.append(a)}card.append(p)}
      if(edit){
        card.onclick=e=>e.preventDefault();
        const b=el('button','edit','Modifier');b.onclick=e=>{e.preventDefault();open(c,o)};
        const ar=el('div','arrows');
        for(const [txt,d] of [['◀',-1],['▶',1]]){const m=el('button',null,txt);m.disabled=i+d<0||i+d>=keys.length;
          m.onclick=e=>{e.preventDefault();const k=keys.slice();[k[i],k[i+d]]=[k[i+d],k[i]];save(k)};ar.append(m)}
        card.append(ar,b);
        card.draggable=true;
        card.ondragstart=e=>{e.dataTransfer.setData('text/plain',c.key);e.dataTransfer.effectAllowed='move';card.classList.add('drag')};
        card.ondragend=()=>card.classList.remove('drag');
        card.ondragover=e=>{e.preventDefault();card.classList.add('over')};
        card.ondragleave=()=>card.classList.remove('over');
        card.ondrop=e=>{e.preventDefault();card.classList.remove('over');const from=keys.indexOf(e.dataTransfer.getData('text/plain'));
          if(from<0||from===i)return;const k=keys.slice();k.splice(i,0,k.splice(from,1)[0]);save(k)};
      }
      grid.append(card);
    });
    out.append(grid);
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
function open(c,o){cur=c;$('#dt').textContent=c.key;
  for(const k of ['name','url','group','icon'])$('#f_'+k).value=o[k]||'';
  $('#f_hidden').checked=!!o.hidden;$('#dlg').showModal()}
async function post(data){await fetch('api/item',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key:cur.key,data})});$('#dlg').close();load()}
$('#save').onclick=e=>{e.preventDefault();const d={hidden:$('#f_hidden').checked};for(const k of ['name','url','group','icon'])d[k]=$('#f_'+k).value.trim();post(d)};
$('#reset').onclick=()=>post(null);$('#cancel').onclick=()=>$('#dlg').close();
const TYPES={1:'Docker local',2:'Agent Portainer',4:'Edge Agent'};
function openEnvs(){
  const list=$('#elist');list.replaceChildren();
  for(const env of D.environments){
    const s=(D.envs||{})[env.name]||{},box=el('div','envbox');
    box.append(el('strong',null,env.name+(env.online?'':' (hors ligne)')));
    box.append(el('div','meta',(TYPES[env.type]||'Type '+env.type)+(env.url?' – '+env.url:'')));
    box.append(el('div','meta','Adresse utilisée : '+env.host+' ('+env.hostSource+')'));
    for(const [k,lab,ph] of [['label','Nom affiché',env.name],['host','Adresse IP ou nom d\'hôte pour les liens',env.host]]){
      const i=el('input');i.type='text';i.value=s[k]||'';i.placeholder=ph;i.dataset.env=env.name;i.dataset.k=k;
      box.append(el('label',null,lab),i);
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
$('#edit').onclick=()=>{edit=!edit;$('#edit').classList.toggle('on',edit);render()};
$('#q').oninput=render;$('#nolink').onchange=render;
load();setInterval(load,30000);
</script></body></html>"""

if __name__ == "__main__":
    threading.Thread(target=refresher, daemon=True).start()
    print("Tableau de bord sur le port %d" % PORT, flush=True)
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
