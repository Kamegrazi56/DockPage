#!/usr/bin/env python3
"""Tableau de bord alimenté par l'API de Portainer (bibliothèque standard uniquement)."""
import json
import os
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

lock = threading.Lock()
cache = {"environments": [], "updated": None, "error": None}


def api(path):
    req = urllib.request.Request(PORTAINER_URL + "/api" + path, headers={"X-API-Key": TOKEN})
    ctx = None if VERIFY_TLS else ssl._create_unverified_context()
    with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
        return json.load(r)


def load_items():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f).get("items", {})
    except (OSError, ValueError):
        return {}


def save_items(items):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"items": items}, f, indent=2, ensure_ascii=False)
    os.replace(tmp, CONFIG_PATH)


def env_host(ep):
    if ep["Name"] in HOSTS:
        return HOSTS[ep["Name"]]
    for raw in (ep.get("PublicURL"), ep.get("URL")):
        if raw:
            host = urllib.parse.urlparse(raw if "://" in raw else "tcp://" + raw).hostname
            if host:
                return host
    return urllib.parse.urlparse(PORTAINER_URL).hostname or "localhost"


def collect():
    envs = []
    for ep in api("/endpoints"):
        if ep.get("Type") not in (1, 2, 4):  # Docker, agent Docker, Edge agent Docker
            continue
        entry = {"name": ep["Name"], "online": ep.get("Status") == 1, "containers": []}
        if entry["online"]:
            try:
                host = env_host(ep)
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
                    })
            except Exception as e:  # un environnement en erreur n'empêche pas les autres
                entry["error"] = str(e)
        envs.append(entry)
    return envs


def refresher():
    while True:
        try:
            if not PORTAINER_URL or not TOKEN:
                raise RuntimeError("PORTAINER_URL et PORTAINER_TOKEN doivent être définis")
            envs = collect()
            with lock:
                cache.update(environments=envs, updated=time.strftime("%H:%M:%S"), error=None)
        except Exception as e:
            with lock:
                cache["error"] = str(e)
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
            with lock:
                payload = dict(cache, title=TITLE, items=load_items())
            self.send(200, json.dumps(payload))
        elif self.path.split("?")[0] == "/":
            self.send(200, PAGE.replace("__TITLE__", TITLE), "text/html")
        else:
            self.send(404, "{}")

    def do_POST(self):
        if self.path != "/api/item":
            return self.send(404, "{}")
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            key, data = body["key"], body.get("data")
            items = load_items()
            if data:
                items[key] = {k: v for k, v in data.items()
                              if k in ("name", "url", "group", "icon", "hidden") and v not in ("", None, False)}
            else:
                items.pop(key, None)
            save_items(items)
            self.send(200, "{}")
        except Exception as e:
            self.send(400, json.dumps({"error": str(e)}))


PAGE = r"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--bg:#f4f5f7;--card:#fff;--text:#1c2330;--muted:#6b7585;--line:#dde1e8;--accent:#2f6fed;--ok:#1f9d55;--bad:#d64545}
@media (prefers-color-scheme:dark){:root{--bg:#12151b;--card:#1b2029;--text:#e6e9ef;--muted:#8d97a8;--line:#2b3240;--accent:#6aa0ff;--ok:#3ecf7b;--bad:#ff6b6b}}
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
.top{display:flex;align-items:center;gap:8px}.ico{font-size:20px}.name{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.dot{width:9px;height:9px;border-radius:50%;background:var(--bad);margin-left:auto;flex:none}.dot.ok{background:var(--ok)}
.meta{color:var(--muted);font-size:12px;margin-top:4px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ports{display:flex;flex-wrap:wrap;gap:4px;margin-top:6px}.ports a{font-size:12px;padding:1px 7px;border:1px solid var(--line);border-radius:99px;color:var(--accent);text-decoration:none}
.edit{position:absolute;right:6px;bottom:6px;padding:1px 7px;font-size:12px}
.msg{padding:10px 14px;border:1px solid var(--bad);border-radius:8px;margin-bottom:12px;color:var(--bad)}
dialog{border:1px solid var(--line);border-radius:12px;background:var(--card);color:var(--text);width:min(420px,92vw)}
dialog::backdrop{background:#0008}dialog label{display:block;margin:10px 0 4px;color:var(--muted);font-size:13px}dialog input[type=text]{width:100%}
.row{display:flex;gap:8px;justify-content:flex-end;margin-top:16px}
</style></head><body>
<header><h1>__TITLE__</h1>
<input id="q" type="search" placeholder="Rechercher…">
<label class="chk"><input id="nolink" type="checkbox">Sans port</label>
<button id="edit">Personnaliser</button><span id="upd" class="meta"></span></header>
<main><div id="msg"></div><div id="out"></div></main>
<dialog id="dlg"><form method="dialog" id="f">
<strong id="dt"></strong>
<label>Nom affiché</label><input type="text" id="f_name">
<label>Lien (URL complète)</label><input type="text" id="f_url" placeholder="http://192.168.1.210:3210">
<label>Groupe</label><input type="text" id="f_group">
<label>Icône (emoji)</label><input type="text" id="f_icon" maxlength="4">
<label class="chk" style="margin-top:12px"><input type="checkbox" id="f_hidden">Masquer ce conteneur</label>
<div class="row"><button type="button" id="reset">Réinitialiser</button><button type="button" id="cancel">Annuler</button><button id="save" value="ok">Enregistrer</button></div>
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
      if(q&&!(c.name+' '+c.image+' '+(o.name||'')).toLowerCase().includes(q))continue;
      const g=o.group||env.name;(groups[g]=groups[g]||[]).push({c,o,url});
    }
  }
  const out=$('#out');out.replaceChildren();
  for(const g of Object.keys(groups).sort((a,b)=>a.localeCompare(b))){
    out.append(el('h2',null,g));const grid=el('div','grid');
    for(const {c,o,url} of groups[g].sort((a,b)=>(a.o.name||a.c.name).localeCompare(b.o.name||b.c.name))){
      const card=el(url?'a':'div','card'+(o.hidden?' dim':'')+(url?'':' nolink'));
      if(url){card.href=url;card.target='_blank';card.rel='noopener'}
      const top=el('div','top');top.append(el('span','ico',o.icon||'🐳'),el('span','name',o.name||c.name),el('span','dot'+(c.state==='running'?' ok':'')));
      card.append(top,el('div','meta',c.image),el('div','meta',c.status));
      if(c.ports.length>1){const p=el('div','ports');for(const x of c.ports){const a=el('a',null,':'+x.port);a.href=x.url;a.target='_blank';a.rel='noopener';p.append(a)}card.append(p)}
      if(edit){const b=el('button','edit','Modifier');b.onclick=e=>{e.preventDefault();open(c,o)};card.append(b)}
      grid.append(card);
    }
    out.append(grid);
  }
}
function open(c,o){cur=c;$('#dt').textContent=c.key;
  for(const k of ['name','url','group','icon'])$('#f_'+k).value=o[k]||'';
  $('#f_hidden').checked=!!o.hidden;$('#dlg').showModal()}
async function post(data){await fetch('api/item',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key:cur.key,data})});$('#dlg').close();load()}
$('#save').onclick=e=>{e.preventDefault();const d={hidden:$('#f_hidden').checked};for(const k of ['name','url','group','icon'])d[k]=$('#f_'+k).value.trim();post(d)};
$('#reset').onclick=()=>post(null);$('#cancel').onclick=()=>$('#dlg').close();
$('#edit').onclick=()=>{edit=!edit;$('#edit').classList.toggle('on',edit);render()};
$('#q').oninput=render;$('#nolink').onchange=render;
load();setInterval(load,30000);
</script></body></html>"""

if __name__ == "__main__":
    threading.Thread(target=refresher, daemon=True).start()
    print("Tableau de bord sur le port %d" % PORT, flush=True)
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
