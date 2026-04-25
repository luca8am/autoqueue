"""
AutoQueue - Auto-acepta partidas de League of Legends usando la LCU API.
Sistema de conexion por niveles con escalada automatica de permisos.

Niveles de conexion LCU:
  Nivel 1 - lcu-driver (deteccion automatica via psutil + WebSocket)
  Nivel 2 - Lockfile en rutas conocidas de Riot Games + polling REST
  Nivel 3 - Escaneo de todas las unidades buscando el lockfile + polling REST
  Nivel 4 - Solicitud de permisos de Administrador via UAC (ultimo recurso)

Niveles del servidor web:
  Nivel A - 0.0.0.0 (accesible desde la red local)
  Nivel B - 127.0.0.1 (solo localhost)
  Nivel C - Sin servidor web (solo consola)
"""

from lcu_driver import Connector
import threading
from flask import Flask, jsonify, request, make_response
import logging
import asyncio
import sys
import os
import socket
import time
import ctypes
import string
import base64
import msvcrt
import requests
import urllib3
import psutil
import json

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

if sys.stdout.encoding.lower() != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

# ==========================================
# Logging
# ==========================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%H:%M:%S',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# ==========================================
# Estado global compartido
# ==========================================
estado_partida = {
    "estado": "Iniciando...",
    "encontrada": False,
    "intentos_partida_actual": 0,
    "bloqueo_aceptacion": False,
    "ultimo_estado_lcu": None,
    "nivel_conexion": "Detectando...",
    "fase": None,
    "modo_juego": None,
    "posiciones": None,
    "miembros_lobby": [],
    "ban_en_progreso": False,
    "tipo_queue": None,
}

MAX_INTENTOS = 3
SEGUNDOS_DECISION = 2.5       # sin nadie en la web
SEGUNDOS_DECISION_WEB = 3.0   # con alguien conectado via web

web_config = {
    "velocidad_maxima": False,
    "ultimo_acceso": 0.0,
}
web_accion = {"pendiente": None}
web_info = {"url": None}

champ_select_state = {
    "local_cell_id": None,
    "action_id_ban": None,
}

lcu_conn = {
    "port": None,
    "token": None,
}

# ==========================================
# Configuracion persistente
# ==========================================
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')
_config_lock = threading.Lock()

def config_por_defecto():
    return {
        "picks": {"TOP": 0, "JGL": 0, "MID": 0, "ADC": 0, "SUP": 0},
        "bans": {"ban1": 0, "ban2": 0}
    }

def cargar_config():
    try:
        with _config_lock:
            with open(CONFIG_PATH, 'r') as f:
                return json.load(f)
    except Exception:
        return config_por_defecto()

def guardar_config(config):
    with _config_lock:
        with open(CONFIG_PATH, 'w') as f:
            json.dump(config, f, indent=2)

# ==========================================
# Web UI
# ==========================================
HTML_PAGE = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>AutoQueue</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
    background: #111;
    color: #f0f0f0;
    min-height: 100vh;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: 16px;
    gap: 16px;
  }
  .title {
    font-size: 0.7rem;
    letter-spacing: 0.3em;
    color: #555;
    text-transform: uppercase;
  }
  .card {
    background: #1a1a1a;
    border: 1px solid #2a2a2a;
    border-radius: 12px;
    padding: 16px;
    width: 100%;
    max-width: 280px;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .status-row {
    display: flex;
    align-items: center;
    gap: 8px;
    min-height: 20px;
  }
  .dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: #333;
    flex-shrink: 0;
    transition: background 0.4s, box-shadow 0.4s;
  }
  .dot.on { background: #4caf50; box-shadow: 0 0 6px #4caf5077; animation: blink 1.4s infinite; }
  @keyframes blink { 0%, 100% { opacity: 1; } 50% { opacity: 0.45; } }
  #estado-text { font-size: 0.85rem; color: #888; line-height: 1.3; }
  #estado-text.found { color: #f0f0f0; }
  .actions { display: flex; gap: 8px; }
  .btn { flex: 1; padding: 10px; border: none; border-radius: 8px; font-size: 0.8rem; font-weight: 600; cursor: pointer; transition: opacity 0.15s; }
  .btn:active { opacity: 0.65; }
  .btn-accept { background: #2a5c3a; color: #a8e6bb; }
  .btn-reject { background: #5c2a2a; color: #e6a8a8; }
  .divider { border: none; border-top: 1px solid #222; margin: 8px 0; }
  .config-row { display: flex; align-items: center; justify-content: space-between; font-size: 0.75rem; color: #999; }
  .toggle { position: relative; width: 32px; height: 16px; }
  .toggle input { opacity: 0; width: 0; height: 0; }
  .slider { position: absolute; cursor: pointer; inset: 0; background: #222; border-radius: 20px; transition: background 0.2s; }
  .slider::before { content: ''; position: absolute; width: 12px; height: 12px; left: 2px; top: 2px; background: #555; border-radius: 50%; transition: transform 0.2s, background 0.2s; }
  input:checked + .slider { background: #2a5c3a; }
  input:checked + .slider::before { transform: translateX(16px); background: #4caf50; }
  .badge { display: inline-block; background: #222; border-radius: 4px; padding: 2px 6px; font-size: 0.65rem; color: #888; }
  .badge.lobby { color: #7ec8e3; }
  .badge.champ { color: #e3c87e; }
  .members-list { font-size: 0.75rem; color: #999; line-height: 1.6; }
  .config-section { margin-top: 8px; }
  .config-section-title { font-size: 0.65rem; color: #555; text-transform: uppercase; letter-spacing: 0.1em; margin-bottom: 6px; }
  .role-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; margin-bottom: 8px; }
  .role-select { background: #0f0f0f; border: 1px solid #2a2a2a; border-radius: 6px; padding: 6px; color: #ccc; font-size: 0.75rem; width: 100%; }
  .search-box { background: #0f0f0f; border: 1px solid #2a2a2a; border-radius: 6px; padding: 6px; color: #ccc; font-size: 0.75rem; width: 100%; margin-bottom: 8px; }
  .search-results { background: #0f0f0f; border: 1px solid #2a2a2a; border-radius: 6px; max-height: 120px; overflow-y: auto; font-size: 0.75rem; }
  .search-result { padding: 6px; border-bottom: 1px solid #1a1a1a; cursor: pointer; color: #7ec8e3; }
  .search-result:hover { background: #1a1a1a; }
  .search-result:last-child { border-bottom: none; }
  .collapsible-header { cursor: pointer; display: flex; justify-content: space-between; align-items: center; user-select: none; font-size: 0.85rem; }
  .collapsible-content { display: none; max-height: 0; overflow: hidden; transition: max-height 0.3s; }
  .collapsible-content.open { display: block; max-height: 400px; }
  .btn-guardar { background: #2a5c3a; color: #a8e6bb; width: 100%; padding: 8px; border: none; border-radius: 6px; font-weight: 600; cursor: pointer; font-size: 0.75rem; }
  .btn-guardar:active { opacity: 0.7; }
  .client-status { font-size: 0.7rem; color: #666; text-align: center; margin-top: 4px; }
  .client-status.online { color: #4caf50; }
  .client-status.offline { color: #d64545; }
</style>
</head>
<body>
<span class="title">AutoQueue</span>

<div class="card">
  <div class="status-row">
    <div class="dot" id="dot"></div>
    <span id="estado-text">Conectando...</span>
  </div>
  <div id="client-status" class="client-status offline">cliente offline</div>
  <div class="actions" id="actions" style="display:none">
    <button class="btn btn-accept" onclick="accion('aceptar')">Aceptar</button>
    <button class="btn btn-reject" onclick="accion('rechazar')">Rechazar</button>
  </div>
  <hr class="divider">
  <div class="config-row">
    <span>insta-accept</span>
    <label class="toggle">
      <input type="checkbox" id="velmax" onchange="setVelMax(this.checked)">
      <span class="slider"></span>
    </label>
  </div>
</div>

<div class="card" id="info-card" style="display:none">
  <div style="display:flex; gap:6px; flex-wrap:wrap;">
    <span class="badge" id="fase-badge"></span>
    <span class="badge" id="queue-badge" style="color:#a8e6bb;"></span>
  </div>

  <div id="ranked-picks" style="display:none;">
    <div class="config-section-title">Tus picks en ranked</div>
    <div style="display:flex; gap:6px; margin-bottom:8px;">
      <button id="role-1-btn" class="btn" style="flex:1; background:#2a3a5c; color:#7ec8e3; padding:6px; border-radius:4px; border:2px solid #2a5c3a; font-size:0.7rem;" onclick="selectRole(0)">-</button>
      <button id="role-2-btn" class="btn" style="flex:1; background:#2a3a5c; color:#999; padding:6px; border-radius:4px; border:none; font-size:0.7rem;" onclick="selectRole(1)">-</button>
    </div>
    <input id="champ-search" class="search-box" placeholder="Buscar campeón..." onkeyup="buscarChampion()" style="display:none;">
    <div id="search-results" class="search-results" style="display:none;"></div>
  </div>

  <div id="aram-info" style="display:none;">
    <div class="config-section-title">Picks disponibles (ARAM)</div>
    <div id="aram-picks" style="font-size:0.8rem; color:#7ec8e3; line-height:1.6;"></div>
  </div>

  <div id="posiciones-text" style="font-size:0.8rem; color:#7ec8e3; margin-bottom:4px;"></div>
  <div class="config-section-title" id="jugadores-label">En lobby</div>
  <div class="members-list" id="miembros-list">-</div>
</div>

<div class="card">
  <div class="collapsible-header" onclick="toggleConfig()">
    <span>Configuración</span>
    <span id="config-toggle">▼</span>
  </div>
  <div class="collapsible-content" id="config-content">
    <div class="config-section">
      <div class="config-section-title">Auto-ban</div>
      <div class="role-grid">
        <div><label style="font-size:0.7rem;">Ban 1</label><select id="ban-1" class="role-select" style="margin-top:3px;"></select></div>
        <div><label style="font-size:0.7rem;">Ban 2</label><select id="ban-2" class="role-select" style="margin-top:3px;"></select></div>
      </div>
    </div>
    <div class="config-section">
      <div class="config-section-title">Picks por rol</div>
      <div class="role-grid">
        <div><label style="font-size:0.7rem;">TOP</label><select id="pick-TOP" class="role-select" style="margin-top:3px;"></select></div>
        <div><label style="font-size:0.7rem;">JGL</label><select id="pick-JGL" class="role-select" style="margin-top:3px;"></select></div>
        <div><label style="font-size:0.7rem;">MID</label><select id="pick-MID" class="role-select" style="margin-top:3px;"></select></div>
        <div><label style="font-size:0.7rem;">ADC</label><select id="pick-ADC" class="role-select" style="margin-top:3px;"></select></div>
        <div><label style="font-size:0.7rem;">SUP</label><select id="pick-SUP" class="role-select" style="margin-top:3px;"></select></div>
      </div>
    </div>
    <button class="btn-guardar" onclick="guardarConfig()">Guardar</button>
  </div>
</div>

<script>
  let championsData = [];
  let selectedRoleIndex = 0;

  function poll() {
    fetch('/api/status').then(r => r.json()).then(d => {
      const el = document.getElementById('estado-text');
      const dot = document.getElementById('dot');
      const act = document.getElementById('actions');
      const infoCard = document.getElementById('info-card');
      const clientStatus = document.getElementById('client-status');

      el.textContent = d.estado;
      el.className = d.encontrada ? 'found' : '';
      dot.className = 'dot' + (d.encontrada ? ' on' : '');
      act.style.display = d.encontrada ? 'flex' : 'none';
      document.getElementById('velmax').checked = !!d.velocidad_maxima;

      const enFase = d.fase === 'Lobby' || d.fase === 'Matchmaking' || d.fase === 'ChampSelect';
      clientStatus.textContent = enFase ? 'cliente online' : 'cliente offline';
      clientStatus.className = 'client-status ' + (enFase ? 'online' : 'offline');

      if (enFase) {
        infoCard.style.display = 'flex';
        const fase_txt = d.fase === 'Lobby' ? 'En lobby' : (d.fase === 'Matchmaking' ? 'En cola' : 'Selección');
        document.getElementById('fase-badge').textContent = fase_txt;
        document.getElementById('fase-badge').className = 'badge ' + (d.fase === 'Lobby' ? 'lobby' : 'champ');
        document.getElementById('queue-badge').textContent = d.tipo_queue || '';

        const isRanked = d.tipo_queue && d.tipo_queue.includes('Ranked');
        const isAram = d.tipo_queue && d.tipo_queue.includes('ARAM');

        document.getElementById('ranked-picks').style.display = isRanked ? 'block' : 'none';
        document.getElementById('aram-info').style.display = isAram ? 'block' : 'none';

        if (isRanked && d.posiciones) {
          const [role1, role2] = d.posiciones.split('/');
          document.getElementById('role-1-btn').textContent = role1 || '?';
          document.getElementById('role-2-btn').textContent = role2 || '?';
        }

        if (isAram && d.posiciones) {
          document.getElementById('aram-picks').textContent = d.posiciones;
        }

        document.getElementById('posiciones-text').textContent = d.posiciones ? 'Pos: ' + d.posiciones : '';
        const miembros = d.miembros_lobby || [];
        const label = document.getElementById('jugadores-label');
        if (miembros.length === 0) {
          document.getElementById('miembros-list').textContent = '-';
          label.textContent = 'En lobby';
        } else {
          label.textContent = `En lobby (${miembros.length})`;
          document.getElementById('miembros-list').innerHTML = miembros.slice(0, 3).map(m => '<div>' + m + '</div>').join('');
        }
      } else {
        infoCard.style.display = 'none';
      }
    }).catch(() => {});
  }

  function accion(tipo) {
    fetch('/api/accion/' + tipo, {method: 'POST'}).then(() => poll());
  }

  function setVelMax(val) {
    fetch('/api/config/velocidad_maxima', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({valor: val})
    });
  }

  async function cargarChampions() {
    try {
      const r = await fetch('/api/champions');
      championsData = await r.json();
      llenarSelectores();
    } catch (e) {
      console.error('Error:', e);
    }
  }

  function llenarSelectores() {
    const roles = ['ban-1', 'ban-2', 'pick-TOP', 'pick-JGL', 'pick-MID', 'pick-ADC', 'pick-SUP'];
    roles.forEach(id => {
      const sel = document.getElementById(id);
      if (!sel) return;
      sel.innerHTML = '<option value="0">---</option>';
      championsData.forEach(c => {
        const opt = document.createElement('option');
        opt.value = c.id;
        opt.textContent = c.name.substring(0, 12);
        sel.appendChild(opt);
      });
    });
  }

  function buscarChampion() {
    const query = document.getElementById('champ-search').value.toLowerCase();
    const resultsDiv = document.getElementById('search-results');
    if (!query) {
      resultsDiv.style.display = 'none';
      return;
    }

    const filtered = championsData.filter(c => c.name.toLowerCase().includes(query));
    const sorted = filtered.sort((a, b) => {
      const aStarts = a.name.toLowerCase().startsWith(query);
      const bStarts = b.name.toLowerCase().startsWith(query);
      if (aStarts && !bStarts) return -1;
      if (!aStarts && bStarts) return 1;
      return a.name.localeCompare(b.name);
    });

    resultsDiv.innerHTML = sorted.slice(0, 8).map(c =>
      `<div class="search-result" onclick="selectChampion('${selectedRoleIndex}', ${c.id})">${c.name}</div>`
    ).join('');
    resultsDiv.style.display = sorted.length > 0 ? 'block' : 'none';
  }

  function selectChampion(roleIndex, champId) {
    if (roleIndex === 0) {
      document.getElementById('role-1-btn').textContent = championsData.find(c => c.id === champId)?.name || '?';
    } else {
      document.getElementById('role-2-btn').textContent = championsData.find(c => c.id === champId)?.name || '?';
    }
    document.getElementById('champ-search').value = '';
    document.getElementById('search-results').style.display = 'none';
  }

  async function cargarConfig() {
    try {
      const cfg = await (await fetch('/api/config')).json();
      document.getElementById('ban-1').value = cfg.bans?.ban1 || 0;
      document.getElementById('ban-2').value = cfg.bans?.ban2 || 0;
      ['TOP', 'JGL', 'MID', 'ADC', 'SUP'].forEach(r => {
        document.getElementById('pick-' + r).value = cfg.picks?.[r] || 0;
      });
    } catch (e) {
      console.error('Error:', e);
    }
  }

  function guardarConfig() {
    const config = {
      picks: {
        TOP: parseInt(document.getElementById('pick-TOP').value) || 0,
        JGL: parseInt(document.getElementById('pick-JGL').value) || 0,
        MID: parseInt(document.getElementById('pick-MID').value) || 0,
        ADC: parseInt(document.getElementById('pick-ADC').value) || 0,
        SUP: parseInt(document.getElementById('pick-SUP').value) || 0
      },
      bans: {
        ban1: parseInt(document.getElementById('ban-1').value) || 0,
        ban2: parseInt(document.getElementById('ban-2').value) || 0
      }
    };
    fetch('/api/config', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(config)})
      .then(() => alert('Guardado'));
  }

  function toggleConfig() {
    const content = document.getElementById('config-content');
    const toggle = document.getElementById('config-toggle');
    content.classList.toggle('open');
    toggle.textContent = content.classList.contains('open') ? '▲' : '▼';
  }

  function selectRole(index) {
    selectedRoleIndex = index;
    document.getElementById('role-1-btn').style.borderColor = index === 0 ? '#2a5c3a' : '#555';
    document.getElementById('role-2-btn').style.borderColor = index === 1 ? '#2a5c3a' : '#555';
    document.getElementById('champ-search').style.display = 'block';
    document.getElementById('champ-search').focus();
  }

  async function inicializar() {
    await cargarChampions();
    await cargarConfig();
    const content = document.getElementById('config-content');
    const toggle = document.getElementById('config-toggle');
    content.classList.add('open');
    toggle.textContent = '▲';
  }

  inicializar();
  poll();
  setInterval(poll, 1500);
</script>
</body>
</html>"""

# ==========================================
# Flask - Monitor Web
# ==========================================
app = Flask(__name__)

@app.route('/')
def index():
    web_config["ultimo_acceso"] = time.time()
    resp = make_response(HTML_PAGE)
    resp.headers['Content-Type'] = 'text/html; charset=utf-8'
    return resp

@app.route('/api/status')
def api_status():
    web_config["ultimo_acceso"] = time.time()
    return jsonify({**estado_partida, "velocidad_maxima": web_config["velocidad_maxima"]})

@app.route('/api/accion/<tipo>', methods=['POST'])
def api_accion(tipo):
    if tipo in ('aceptar', 'rechazar'):
        web_accion["pendiente"] = tipo
    return jsonify({"ok": True})

@app.route('/api/config/velocidad_maxima', methods=['POST'])
def api_config_velmax():
    data = request.get_json(silent=True) or {}
    web_config["velocidad_maxima"] = bool(data.get("valor", False))
    estado = "activo" if web_config["velocidad_maxima"] else "desactivado"
    logger.info(f"Insta-accept {estado} (via web).")
    return jsonify({"ok": True})

@app.route('/api/config', methods=['GET'])
def api_get_config():
    web_config["ultimo_acceso"] = time.time()
    return jsonify(cargar_config())

@app.route('/api/config', methods=['POST'])
def api_set_config():
    web_config["ultimo_acceso"] = time.time()
    data = request.get_json(silent=True) or {}
    config = config_por_defecto()
    for rol in config["picks"]:
        v = data.get("picks", {}).get(rol, 0)
        config["picks"][rol] = int(v) if isinstance(v, (int, float)) else 0
    for ban in ("ban1", "ban2"):
        v = data.get("bans", {}).get(ban, 0)
        config["bans"][ban] = int(v) if isinstance(v, (int, float)) else 0
    guardar_config(config)
    return jsonify({"ok": True})

CHAMPIONS_HARDCODED = [
    {"id": 1, "name": "Annie"}, {"id": 2, "name": "Olaf"}, {"id": 3, "name": "Galio"}, {"id": 4, "name": "Twisted Fate"},
    {"id": 5, "name": "Xin Zhao"}, {"id": 6, "name": "Urgot"}, {"id": 7, "name": "LeBlanc"}, {"id": 8, "name": "Vladimir"},
    {"id": 9, "name": "Fiddlesticks"}, {"id": 10, "name": "Kayle"}, {"id": 11, "name": "Master Yi"}, {"id": 12, "name": "Alistar"},
    {"id": 13, "name": "Ryze"}, {"id": 14, "name": "Sion"}, {"id": 15, "name": "Sivir"}, {"id": 16, "name": "Soraka"},
    {"id": 17, "name": "Teemo"}, {"id": 18, "name": "Tristana"}, {"id": 19, "name": "Warwick"}, {"id": 20, "name": "Nunu"},
    {"id": 21, "name": "Miss Fortune"}, {"id": 22, "name": "Ashe"}, {"id": 23, "name": "Garen"}, {"id": 24, "name": "Jax"},
    {"id": 25, "name": "Morgana"}, {"id": 26, "name": "Zilean"}, {"id": 27, "name": "Singed"}, {"id": 28, "name": "Evelynn"},
    {"id": 29, "name": "Twitch"}, {"id": 30, "name": "Karthus"}, {"id": 31, "name": "Cho'Gath"}, {"id": 32, "name": "Amumu"},
    {"id": 33, "name": "Rammus"}, {"id": 34, "name": "Anivia"}, {"id": 35, "name": "Shaco"}, {"id": 36, "name": "Dr. Mundo"},
    {"id": 37, "name": "Sona"}, {"id": 38, "name": "Kassadin"}, {"id": 39, "name": "Irelia"}, {"id": 40, "name": "Janna"},
    {"id": 41, "name": "Gangplank"}, {"id": 42, "name": "Corki"}, {"id": 43, "name": "Karma"}, {"id": 44, "name": "Taric"},
    {"id": 45, "name": "Veigar"}, {"id": 48, "name": "Trundle"}, {"id": 50, "name": "Swain"}, {"id": 51, "name": "Caitlyn"},
    {"id": 53, "name": "Blitzcrank"}, {"id": 54, "name": "Malphite"}, {"id": 55, "name": "Katarina"}, {"id": 56, "name": "Nocturne"},
    {"id": 57, "name": "Maokai"}, {"id": 58, "name": "Renekton"}, {"id": 59, "name": "Jarvan IV"}, {"id": 60, "name": "Elise"},
    {"id": 61, "name": "Orianna"}, {"id": 62, "name": "Wukong"}, {"id": 63, "name": "Brand"}, {"id": 64, "name": "Lee Sin"},
    {"id": 67, "name": "Vayne"}, {"id": 68, "name": "Rumble"}, {"id": 69, "name": "Cassiopeia"}, {"id": 72, "name": "Skarner"},
    {"id": 74, "name": "Heimerdinger"}, {"id": 75, "name": "Nasus"}, {"id": 76, "name": "Nidalee"}, {"id": 77, "name": "Udyr"},
    {"id": 78, "name": "Poppy"}, {"id": 79, "name": "Gragas"}, {"id": 80, "name": "Pantheon"}, {"id": 81, "name": "Ezreal"},
    {"id": 82, "name": "Mordekaiser"}, {"id": 83, "name": "Yorick"}, {"id": 84, "name": "Akali"}, {"id": 85, "name": "Kennen"},
    {"id": 86, "name": "Gnar"}, {"id": 89, "name": "Leona"}, {"id": 90, "name": "Talon"}, {"id": 91, "name": "Tahmm Kench"},
    {"id": 92, "name": "Riven"}, {"id": 96, "name": "Kog'Maw"}, {"id": 98, "name": "Shen"}, {"id": 99, "name": "Lux"},
    {"id": 101, "name": "Xerath"}, {"id": 102, "name": "Shyvana"}, {"id": 103, "name": "Ahri"}, {"id": 104, "name": "Darius"},
    {"id": 105, "name": "Fizz"}, {"id": 106, "name": "Volibear"}, {"id": 107, "name": "Rengar"}, {"id": 110, "name": "Varus"},
    {"id": 111, "name": "Nautilus"}, {"id": 112, "name": "Viktor"}, {"id": 113, "name": "Sejuani"}, {"id": 114, "name": "Fiora"},
    {"id": 115, "name": "Zyra"}, {"id": 117, "name": "Lulu"}, {"id": 119, "name": "Draven"}, {"id": 120, "name": "Hecarim"},
    {"id": 121, "name": "Kha'Zix"}, {"id": 122, "name": "Darius"}, {"id": 126, "name": "Jayce"}, {"id": 127, "name": "Lissandra"},
    {"id": 131, "name": "Diana"}, {"id": 133, "name": "Quinn"}, {"id": 134, "name": "Syndra"}, {"id": 136, "name": "Aurelion Sol"},
    {"id": 141, "name": "Kayn"}, {"id": 142, "name": "Zoe"}, {"id": 143, "name": "Zyra"}, {"id": 145, "name": "Kai'Sa"},
    {"id": 147, "name": "Seraphine"}, {"id": 150, "name": "Gnar"}, {"id": 154, "name": "Zac"}, {"id": 157, "name": "Yasuo"},
    {"id": 161, "name": "Vel'Koz"}, {"id": 163, "name": "Taliyah"}, {"id": 164, "name": "Camille"}, {"id": 166, "name": "Yuumi"},
    {"id": 167, "name": "Akshan"}, {"id": 168, "name": "Hwei"}, {"id": 164, "name": "Camille"}, {"id": 245, "name": "Ekko"},
    {"id": 246, "name": "Qiyana"}, {"id": 247, "name": "Rell"}, {"id": 254, "name": "Vi"}, {"id": 266, "name": "Aatrox"},
    {"id": 267, "name": "Nami"}, {"id": 268, "name": "Azir"}, {"id": 269, "name": "Garen"}, {"id": 350, "name": "Yuumi"},
    {"id": 360, "name": "Samira"}, {"id": 412, "name": "Thresh"}, {"id": 420, "name": "Illaoi"}, {"id": 421, "name": "Rek'Sai"},
    {"id": 427, "name": "Ivern"}, {"id": 429, "name": "Kalista"}, {"id": 432, "name": "Bard"}, {"id": 497, "name": "Rakan"},
    {"id": 498, "name": "Xayah"}, {"id": 516, "name": "Ornn"}, {"id": 517, "name": "Sylas"}, {"id": 518, "name": "Neeko"},
    {"id": 523, "name": "Aphelios"}, {"id": 526, "name": "Rell"}, {"id": 555, "name": "Pyke"}, {"id": 875, "name": "Sett"},
    {"id": 876, "name": "Lillia"}, {"id": 887, "name": "Yone"}, {"id": 897, "name": "K'Sante"}, {"id": 902, "name": "Seraphine"},
]

@app.route('/api/champions', methods=['GET'])
def api_champions():
    web_config["ultimo_acceso"] = time.time()

    # Intentar traer desde LCU
    if lcu_conn["port"] and lcu_conn["token"]:
        try:
            creds = base64.b64encode(f"riot:{lcu_conn['token']}".encode()).decode()
            headers = {'Authorization': f'Basic {creds}'}
            r = requests.get(
                f"https://127.0.0.1:{lcu_conn['port']}/lol-game-data/assets/v1/champion-summary.json",
                headers=headers,
                verify=False,
                timeout=3
            )
            if r.status_code == 200:
                champs = r.json()
                lista = [{"id": c.get("id"), "name": c.get("name")} for c in champs if "id" in c and "name" in c]
                return jsonify(sorted(lista, key=lambda x: x["name"]))
        except Exception:
            pass

    # Fallback: lista hardcodeada
    return jsonify(sorted(CHAMPIONS_HARDCODED, key=lambda x: x["name"]))

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

def iniciar_servidor_web():
    logging.getLogger('werkzeug').disabled = True
    ip_local = get_local_ip()

    for puerto in range(5000, 5011):
        try:
            web_info["url"] = f"http://{ip_local}:{puerto}"
            logger.info(f"Monitor web: {web_info['url']}")
            app.run(host='0.0.0.0', port=puerto, debug=False, use_reloader=False)
            return
        except OSError:
            web_info["url"] = None
            continue
        except Exception as e:
            logger.error(f"Error inesperado en Flask: {e}")
            break

    for puerto in range(5000, 5011):
        try:
            web_info["url"] = f"http://127.0.0.1:{puerto}"
            logger.warning(f"[Web Nivel B] Monitor solo en localhost:{puerto}")
            app.run(host='127.0.0.1', port=puerto, debug=False, use_reloader=False)
            return
        except OSError:
            web_info["url"] = None
            continue
        except Exception:
            break

    logger.warning("[Web Nivel C] Monitor web desactivado. El bot sigue funcionando por consola.")

def mostrar_qr_web(url):
    try:
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.make(fit=True)
        print()
        qr.print_ascii(invert=True)
        print(f"  {url}")
        print()
    except Exception:
        pass


# ==========================================
# Permisos de Administrador
# ==========================================
def es_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False

def relanzar_como_admin():
    logger.info("[Nivel 4] Solicitando permisos de Administrador via UAC...")
    try:
        exe = sys.executable
        args = '' if getattr(sys, 'frozen', False) else ' '.join(f'"{a}"' for a in sys.argv)
        resultado = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, args, None, 1)
        if resultado > 32:
            logger.info("Proceso relanzado como Administrador. Cerrando instancia actual.")
            sys.exit(0)
        else:
            logger.error("Solicitud de permisos cancelada (UAC).")
    except Exception as e:
        logger.error(f"Error al solicitar admin: {e}")


# ==========================================
# Lockfile - Busqueda y parseo (Niveles 2 y 3)
# ==========================================
_RUTAS_LOCKFILE_CONOCIDAS = [
    os.path.join(os.getenv('LOCALAPPDATA', ''), 'Riot Games', 'League of Legends', 'lockfile'),
    os.path.join(os.getenv('PROGRAMFILES', r'C:\Program Files'), 'Riot Games', 'League of Legends', 'lockfile'),
    os.path.join(os.getenv('PROGRAMFILES(X86)', r'C:\Program Files (x86)'), 'Riot Games', 'League of Legends', 'lockfile'),
    r'C:\Riot Games\League of Legends\lockfile',
    r'D:\Riot Games\League of Legends\lockfile',
    r'E:\Riot Games\League of Legends\lockfile',
]

_DIRS_EXCLUIR_ESCANEO = {
    'Windows', 'System32', 'SysWOW64', 'WinSxS',
    '$Recycle.Bin', 'ProgramData', 'Recovery', 'Intel', 'AMD',
}

def _lockfile_valido(ruta):
    try:
        with open(ruta, 'r') as f:
            return f.read(12).startswith('LeagueClient')
    except Exception:
        return False

def parsear_lockfile(ruta):
    with open(ruta, 'r') as f:
        partes = f.read().strip().split(':')
    return {'port': int(partes[2]), 'token': partes[3]}

def buscar_lockfile_rutas_conocidas():
    logger.info("[Nivel 2] Buscando lockfile en rutas conocidas de Riot Games...")
    for ruta in _RUTAS_LOCKFILE_CONOCIDAS:
        if os.path.exists(ruta) and _lockfile_valido(ruta):
            logger.info(f"   Encontrado: {ruta}")
            return ruta
    logger.warning("   No encontrado en rutas conocidas.")
    return None

def buscar_lockfile_drives():
    logger.info("[Nivel 3] Escaneando todas las unidades... (puede tardar unos segundos)")
    unidades = [f"{d}:\\" for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")]

    for unidad in unidades:
        for raiz, dirs, archivos in os.walk(unidad, topdown=True):
            dirs[:] = [d for d in dirs if d not in _DIRS_EXCLUIR_ESCANEO]
            if 'lockfile' in archivos:
                ruta = os.path.join(raiz, 'lockfile')
                if _lockfile_valido(ruta):
                    logger.info(f"   Encontrado: {ruta}")
                    return ruta

    logger.warning("   No encontrado en ninguna unidad.")
    return None


# ==========================================
# Decision manual del usuario (countdown interactivo)
# ==========================================
def esperar_decision_usuario():
    """
    Countdown interactivo. Teclas: S/1/Enter/Espacio = Aceptar, N/2 = Rechazar.
    Tambien responde a acciones enviadas desde la web.
    Retorna: True (aceptar), False (rechazar), None (timeout -> auto-aceptar)
    """
    ACEPTAR  = {b's', b'S', b'1', b'\r', b' '}
    RECHAZAR = {b'n', b'N', b'2'}

    if web_config["velocidad_maxima"]:
        logger.info("Insta-accept activo. Aceptando de inmediato...")
        return None

    web_accion["pendiente"] = None

    segundos = SEGUNDOS_DECISION_WEB if time.time() - web_config["ultimo_acceso"] < 60 else SEGUNDOS_DECISION
    fin = time.time() + segundos

    while time.time() < fin:
        restante = fin - time.time()
        print(
            f"\r   Auto-aceptando en {restante:.1f}s  "
            f"[S / 1 / Enter = Aceptar   N / 2 = Rechazar]  ",
            end='', flush=True
        )

        pendiente = web_accion["pendiente"]
        if pendiente == "aceptar":
            web_accion["pendiente"] = None
            print()
            logger.info("Accion desde web: ACEPTAR")
            return True
        elif pendiente == "rechazar":
            web_accion["pendiente"] = None
            print()
            logger.info("Accion desde web: RECHAZAR")
            return False

        if msvcrt.kbhit():
            tecla = msvcrt.getch()
            if tecla in (b'\x00', b'\xe0'):
                msvcrt.getch()
                continue
            print()
            if tecla in ACEPTAR:
                logger.info("Teclado: ACEPTAR")
                return True
            elif tecla in RECHAZAR:
                logger.info("Teclado: RECHAZAR")
                return False

        time.sleep(0.05)

    print()
    return None


# ==========================================
# Helpers de estado de matchmaking
# ==========================================
def _on_partida_detectada():
    intento = estado_partida["intentos_partida_actual"] + 1
    logger.info(f"Partida encontrada. Intento {intento}/{MAX_INTENTOS}...")
    estado_partida["estado"] = "Partida encontrada - Esperando decision"
    estado_partida["encontrada"] = True
    estado_partida["bloqueo_aceptacion"] = True

def _on_aceptacion_ok():
    logger.info("Partida aceptada.")
    estado_partida["estado"] = "Partida aceptada - Esperando confirmacion"

def _on_aceptacion_error(detalle):
    logger.warning(f"Error al aceptar ({detalle}). Reintentando...")
    estado_partida["intentos_partida_actual"] += 1

def _on_rechazo():
    logger.info("Partida rechazada.")
    estado_partida["estado"] = "Partida rechazada"
    estado_partida.update({
        "encontrada": False,
        "intentos_partida_actual": 0,
        "bloqueo_aceptacion": False,
    })


# ==========================================
# Logica de matchmaking compartida
# ==========================================
def evaluar_estado_matchmaking(estado, respuesta_jugador):
    if estado and estado != estado_partida["ultimo_estado_lcu"]:
        logger.info(f"[Matchmaking] Estado: {estado} | Respuesta: {respuesta_jugador}")
        estado_partida["ultimo_estado_lcu"] = estado

    if estado == 'InProgress':
        if respuesta_jugador == 'Accepted':
            return None
        if estado_partida["bloqueo_aceptacion"]:
            return None
        if estado_partida["intentos_partida_actual"] >= MAX_INTENTOS:
            if estado_partida["estado"] != "Error - Limite de intentos alcanzado":
                logger.error(f"Limite de {MAX_INTENTOS} reintentos. Deteniendo spam para esta partida.")
                estado_partida["estado"] = "Error - Limite de intentos alcanzado"
            return None
        return 'aceptar'

    elif estado == 'EveryoneReady':
        if estado_partida["estado"] != "Todos aceptaron":
            logger.info("Todos aceptaron. Entrando a seleccion de campeones.")
            estado_partida["estado"] = "Todos aceptaron"

    elif estado in [None, 'None', 'Error', 'Declined']:
        if estado_partida["intentos_partida_actual"] > 0 or estado_partida["encontrada"]:
            logger.info("Matchmaking finalizado. Reseteando para la siguiente cola.")
            estado_partida.update({
                "intentos_partida_actual": 0,
                "encontrada": False,
                "bloqueo_aceptacion": False,
            })

    return None


# ==========================================
# Helpers para lobby y champ-select
# ==========================================
def _actualizar_fase(fase):
    estado_partida["fase"] = fase
    if fase == "Lobby":
        estado_partida["estado"] = "En lobby"
    elif fase == "Matchmaking":
        estado_partida["estado"] = "En cola"
    elif fase == "ChampSelect":
        estado_partida["estado"] = "Selección de campeones"
    elif fase not in ("ReadyCheck", "InProgress"):
        _resetear_estado_lobby()

def _resetear_estado_lobby():
    estado_partida.update({"miembros_lobby": [], "posiciones": None, "modo_juego": None})
    champ_select_state.update({"local_cell_id": None, "action_id_ban": None})

def _procesar_lobby(data):
    modo = data.get("gameConfig", {}).get("gameMode", "")
    queue_id = data.get("gameConfig", {}).get("queueId", 0)
    estado_partida["modo_juego"] = modo

    QUEUE_NAMES = {
        0: "Práctica",
        2: "Normal 5v5",
        4: "Reclutamiento",
        6: "Rankeds 3v3",
        7: "Normal 3v3",
        9: "Rankeds Flex",
        14: "Normal Draft",
        25: "Rankeds Solo/Duo",
        31: "Normal Bot",
        32: "Rankeds Flex",
        33: "Normal ARAM",
        65: "Rankeds ARAM",
        70: "Normal Clash",
        400: "Reclutamiento",
        420: "Rankeds Solo/Duo",
        440: "Rankeds Flex",
        450: "ARAM",
        480: "Casual",
        1700: "Arena",
        2300: "Pelea",
        2400: "ARAM Caos",
    }
    tipo_queue = QUEUE_NAMES.get(queue_id, f"Queue {queue_id}")
    estado_partida["tipo_queue"] = tipo_queue

    miembros = []
    for m in data.get("members", []):
        tag = m.get("gameNameTag") or m.get("summonerName", "?")
        miembros.append(tag)
    estado_partida["miembros_lobby"] = miembros

    if modo == "CLASSIC":
        lm = data.get("localMember", {})
        p1 = lm.get("firstPositionPreference", "")
        p2 = lm.get("secondPositionPreference", "")
        if p1 and p1 != "UNSELECTED":
            ALIAS = {"MIDDLE":"MID", "BOTTOM":"ADC", "UTILITY":"SUP", "JUNGLE":"JGL", "TOP":"TOP", "FILL":"FILL"}
            pos1 = ALIAS.get(p1, p1)
            pos2 = ALIAS.get(p2, p2) if p2 and p2 != "UNSELECTED" else ""
            estado_partida["posiciones"] = f"{pos1}/{pos2}" if pos2 else pos1
        else:
            estado_partida["posiciones"] = None
    elif modo == "ARAM":
        estado_partida["posiciones"] = "Espera en champ select"
    else:
        estado_partida["posiciones"] = None

def _detectar_turno_ban(data):
    local_cell = data.get("localPlayerCellId")
    for grupo in data.get("actions", []):
        for action in grupo:
            if (action.get("type") == "ban"
                    and action.get("actorCellId") == local_cell
                    and action.get("isInProgress") is True
                    and not action.get("completed", True)):
                return action["id"]
    return None

def _elegir_campeon_ban(data):
    bans = data.get("bans", {})
    ya_baneados = set(bans.get("myTeamBans", []) + bans.get("theirTeamBans", []))
    config = cargar_config()
    for key in ("ban1", "ban2"):
        cid = config["bans"].get(key, 0)
        if cid and cid not in ya_baneados:
            return cid
    return None

async def _ejecutar_ban_ws(connection, action_id, champion_id):
    if estado_partida["ban_en_progreso"]:
        return
    estado_partida["ban_en_progreso"] = True
    try:
        r = await connection.request('patch',
            f'/lol-champ-select/v1/session/actions/{action_id}',
            data={"championId": champion_id, "completed": True})
        if r.status in (200, 204):
            logger.info(f"[AutoBan] Campeón ID {champion_id} baneado.")
        else:
            logger.warning(f"[AutoBan] Error HTTP {r.status} al banear.")
    except Exception as e:
        logger.warning(f"[AutoBan] Error: {e}")
    finally:
        estado_partida["ban_en_progreso"] = False

def _ejecutar_ban_polling(session, port, action_id, champion_id):
    if estado_partida["ban_en_progreso"]:
        return
    estado_partida["ban_en_progreso"] = True
    try:
        r = session.patch(
            f"https://127.0.0.1:{port}/lol-champ-select/v1/session/actions/{action_id}",
            json={"championId": champion_id, "completed": True}, timeout=3)
        if r.status_code in (200, 204):
            logger.info(f"[AutoBan] Campeón ID {champion_id} baneado.")
        else:
            logger.warning(f"[AutoBan] Error HTTP {r.status_code} al banear.")
    except Exception as e:
        logger.warning(f"[AutoBan] Error: {e}")
    finally:
        estado_partida["ban_en_progreso"] = False


# ==========================================
# Nivel 1: lcu-driver (WebSocket, deteccion automatica)
# ==========================================
connector = Connector()

@connector.ready
async def connect(connection):
    logger.info("Cliente conectado (lcu-driver + WebSocket).")
    estado_partida["estado"] = "Buscando partida"
    estado_partida["nivel_conexion"] = "lcu-driver + WebSocket"
    try:
        if hasattr(connection, 'port'):
            lcu_conn["port"] = connection.port
        if hasattr(connection, 'auth_key'):
            lcu_conn["token"] = connection.auth_key

        r = await connection.request('get', '/lol-gameflow/v1/gameflow-phase')
        data = await r.json()
        _actualizar_fase(data)
    except Exception as e:
        logger.error(f"Error en connect: {e}")

@connector.ws.register('/lol-matchmaking/v1/ready-check', event_types=('UPDATE',))
async def match_found(connection, event):
    accion = evaluar_estado_matchmaking(
        event.data.get('state'),
        event.data.get('playerResponse')
    )
    if accion != 'aceptar':
        return

    _on_partida_detectada()

    loop = asyncio.get_event_loop()
    decision = await loop.run_in_executor(None, esperar_decision_usuario)

    if decision is False:
        try:
            await connection.request('post', '/lol-matchmaking/v1/ready-check/decline')
            _on_rechazo()
        except Exception as e:
            logger.error(f"Error al rechazar: {e}")
            estado_partida["bloqueo_aceptacion"] = False
        return

    if decision is None:
        logger.info("Tiempo agotado. Auto-aceptando...")
        estado_partida["estado"] = "Auto-aceptando partida..."
    else:
        estado_partida["estado"] = "Aceptando partida..."

    try:
        response = await connection.request('post', '/lol-matchmaking/v1/ready-check/accept')
        if response.status in [200, 204]:
            _on_aceptacion_ok()
        else:
            _on_aceptacion_error(f"HTTP {response.status}")
    except Exception as e:
        _on_aceptacion_error(e)
    finally:
        await asyncio.sleep(0.5)
        estado_partida["bloqueo_aceptacion"] = False

@connector.close
async def disconnect(connection):
    logger.info("Cliente cerrado.")
    estado_partida["estado"] = "Cliente cerrado"
    estado_partida.update({"encontrada": False, "intentos_partida_actual": 0})

@connector.ws.register('/lol-gameflow/v1/gameflow-phase', event_types=('UPDATE',))
async def on_gameflow_phase(connection, event):
    fase = event.data
    _actualizar_fase(fase)
    if fase == "Lobby":
        try:
            r = await connection.request('get', '/lol-lobby/v2/lobby')
            data = await r.json()
            _procesar_lobby(data)
        except Exception:
            pass

@connector.ws.register('/lol-lobby/v2/lobby', event_types=('UPDATE',))
async def on_lobby_update(connection, event):
    if estado_partida["fase"] is None:
        try:
            r = await connection.request('get', '/lol-gameflow/v1/gameflow-phase')
            fase = await r.json()
            _actualizar_fase(fase)
        except Exception:
            pass

    if estado_partida["fase"] == "Lobby":
        _procesar_lobby(event.data)

@connector.ws.register('/lol-champ-select/v1/session', event_types=('UPDATE',))
async def on_champ_select(connection, event):
    data = event.data
    champ_select_state["local_cell_id"] = data.get("localPlayerCellId")
    action_id = _detectar_turno_ban(data)
    if action_id:
        champion_id = _elegir_campeon_ban(data)
        if champion_id:
            await _ejecutar_ban_ws(connection, action_id, champion_id)


# ==========================================
# Funciones de polling para nuevos endpoints
# ==========================================
def _poll_gameflow(session, port):
    try:
        r = session.get(
            f"https://127.0.0.1:{port}/lol-gameflow/v1/gameflow-phase",
            timeout=2
        )
        if r.status_code == 200:
            fase = r.json()
            _actualizar_fase(fase)
    except Exception:
        pass

def _poll_lobby(session, port):
    try:
        r = session.get(
            f"https://127.0.0.1:{port}/lol-lobby/v2/lobby",
            timeout=2
        )
        if r.status_code == 200:
            data = r.json()
            _procesar_lobby(data)
    except Exception:
        pass

def _poll_champ_select(session, port):
    try:
        r = session.get(
            f"https://127.0.0.1:{port}/lol-champ-select/v1/session",
            timeout=2
        )
        if r.status_code == 200:
            data = r.json()
            champ_select_state["local_cell_id"] = data.get("localPlayerCellId")
            action_id = _detectar_turno_ban(data)
            if action_id:
                champion_id = _elegir_campeon_ban(data)
                if champion_id:
                    _ejecutar_ban_polling(session, port, action_id, champion_id)
    except Exception:
        pass


# ==========================================
# Niveles 2/3: Polling REST via lockfile
# ==========================================
def iniciar_modo_polling(lockfile_path):
    estado_partida["nivel_conexion"] = "lockfile + polling REST"

    session = requests.Session()
    session.verify = False

    def _actualizar_auth(token):
        creds = base64.b64encode(f"riot:{token}".encode()).decode()
        session.headers.update({
            'Authorization': f'Basic {creds}',
            'Content-Type': 'application/json',
        })

    logger.info(f"Modo polling activo. Lockfile: {lockfile_path}")

    while True:
        try:
            datos = parsear_lockfile(lockfile_path)
            port = datos['port']
            _actualizar_auth(datos['token'])

            logger.info(f"Cliente conectado en puerto {port} (polling).")
            estado_partida["estado"] = "Buscando partida (polling)"
            lcu_conn["port"] = port
            lcu_conn["token"] = datos['token']

            _t_fase = _t_lobby = _t_cs = 0.0

            while True:
                if not os.path.exists(lockfile_path):
                    logger.warning("Lockfile eliminado. Cliente cerrado.")
                    estado_partida["estado"] = "Cliente cerrado"
                    estado_partida.update({"encontrada": False, "intentos_partida_actual": 0})
                    break

                try:
                    r = session.get(
                        f"https://127.0.0.1:{port}/lol-matchmaking/v1/ready-check",
                        timeout=2
                    )
                    if r.status_code == 200:
                        data = r.json()
                        accion = evaluar_estado_matchmaking(data.get('state'), data.get('playerResponse'))

                        if accion == 'aceptar':
                            _on_partida_detectada()

                            decision = esperar_decision_usuario()

                            if decision is False:
                                try:
                                    session.post(
                                        f"https://127.0.0.1:{port}/lol-matchmaking/v1/ready-check/decline",
                                        timeout=3
                                    )
                                    _on_rechazo()
                                except Exception as e:
                                    logger.error(f"Error al rechazar: {e}")
                                    estado_partida["bloqueo_aceptacion"] = False
                            else:
                                if decision is None:
                                    logger.info("Tiempo agotado. Auto-aceptando...")
                                    estado_partida["estado"] = "Auto-aceptando partida..."
                                else:
                                    estado_partida["estado"] = "Aceptando partida..."

                                try:
                                    ra = session.post(
                                        f"https://127.0.0.1:{port}/lol-matchmaking/v1/ready-check/accept",
                                        timeout=3
                                    )
                                    if ra.status_code in [200, 204]:
                                        _on_aceptacion_ok()
                                    else:
                                        _on_aceptacion_error(f"HTTP {ra.status_code}")
                                except Exception as e:
                                    _on_aceptacion_error(e)
                                finally:
                                    time.sleep(0.5)
                                    estado_partida["bloqueo_aceptacion"] = False

                except requests.exceptions.ConnectionError:
                    logger.warning("Conexion perdida. Reconectando...")
                    break
                except requests.exceptions.Timeout:
                    logger.warning("Timeout en la peticion. Reintentando...")
                except Exception as e:
                    logger.error(f"Error en polling: {e}")

                ahora = time.time()
                if ahora - _t_fase > 2.0:
                    _poll_gameflow(session, port)
                    _t_fase = ahora
                if estado_partida["fase"] == "Lobby" and ahora - _t_lobby > 3.0:
                    _poll_lobby(session, port)
                    _t_lobby = ahora
                if estado_partida["fase"] == "ChampSelect" and ahora - _t_cs > 1.0:
                    _poll_champ_select(session, port)
                    _t_cs = ahora

                time.sleep(0.5)

        except FileNotFoundError:
            logger.info("Lockfile no encontrado. Esperando...")
            estado_partida["estado"] = "Esperando al cliente..."
            time.sleep(3)
            continue
        except PermissionError:
            logger.error("Sin permisos para leer el lockfile.")
            return
        except Exception as e:
            logger.error(f"Error inesperado: {e}")
            time.sleep(3)
            continue

        logger.info("Esperando que el cliente se reinicie...")
        time.sleep(5)


# ==========================================
# Deteccion del nivel de conexion a usar
# ==========================================
def detectar_proceso_lol():
    for proc in psutil.process_iter(['name']):
        try:
            if proc.info['name'] in ('LeagueClient.exe', 'LeagueClientUx.exe'):
                return proc
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return None

def determinar_estrategia():
    if es_admin():
        logger.info("Corriendo como Administrador.")

    proceso = detectar_proceso_lol()

    if not proceso:
        logger.info("Cliente no detectado. Esperando cliente...(lcu-driver)")
        return ('lcu-driver', None)

    try:
        proceso.cmdline()
        logger.info("Proceso accesible (psutil).")
        return ('lcu-driver', None)
    except psutil.AccessDenied:
        logger.warning("Acceso denegado al proceso. Intentando nivel 2...")

    lockfile = buscar_lockfile_rutas_conocidas()
    if lockfile:
        return ('manual', lockfile)

    lockfile = buscar_lockfile_drives()
    if lockfile:
        return ('manual', lockfile)

    if not es_admin():
        logger.error("[Nivel 4] No se puede conectar con los permisos actuales.")
        return ('admin', None)

    logger.warning("Corriendo como admin pero no se encontro lockfile. Usando lcu-driver.")
    return ('lcu-driver', None)


# ==========================================
# Main
# ==========================================
if __name__ == '__main__':
    logger.info("=========================================")
    logger.info("AutoQueue  ∞  dev by 8AM")
    logger.info("=========================================")

    hilo_web = threading.Thread(target=iniciar_servidor_web, daemon=True)
    hilo_web.start()

    # Esperar a que el servidor determine su URL
    for _ in range(20):
        if web_info["url"]:
            break
        time.sleep(0.05)

    if web_info["url"]:
        mostrar_qr_web(web_info["url"])
    else:
        time.sleep(0.4)

    estrategia, lockfile_path = determinar_estrategia()

    if estrategia == 'lcu-driver':
        estado_partida["nivel_conexion"] = "lcu-driver + WebSocket"
        try:
            connector.start()
        except KeyboardInterrupt:
            logger.info("\nApagando AutoQueue...")
        except Exception as e:
            logger.error(f"Error critico: {e}")

    elif estrategia == 'manual':
        try:
            iniciar_modo_polling(lockfile_path)
        except KeyboardInterrupt:
            logger.info("\nApagando AutoQueue...")

    elif estrategia == 'admin':
        print()
        print("=" * 50)
        print("  No se pudo conectar con los permisos actuales.")
        print("  Relanzar como Administrador? (S/N): ", end='', flush=True)
        try:
            if input().strip().upper() == 'S':
                relanzar_como_admin()
            else:
                logger.info("Saliendo. Ejecuta manualmente como Administrador.")
        except (KeyboardInterrupt, EOFError):
            pass
