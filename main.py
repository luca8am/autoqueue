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
import requests
import urllib3
import psutil
import json

try:
    import msvcrt
except ImportError:
    msvcrt = None

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
logger.setLevel(logging.INFO)
logging.getLogger('lcu_driver').setLevel(logging.WARNING)

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
    "pick_en_progreso": False,
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
    "action_id_pick": None,
}

agent_events = []
agent_event_lock = threading.Lock()
agent_event_next_id = 1
agent_last_seen = time.time()

bestbuild_state = {
    "updated_at": None,
    "phase": None,
    "queue": None,
    "mode": None,
    "local_player": None,
    "lane_opponent": None,
    "allies": [],
    "enemies": [],
}

MOBALYTICS_GQL_URL = "https://widget.workers.mobalytics.gg/lol/graphql/v1/query"
MOBALYTICS_WIDGET_TOKEN = "2bd985cc-dc15-4cad-bea7-0c9669751e08"
MOBALYTICS_DYNAMIC_BUILD_QUERY = """
query LolChampionWidgetDynamicQuery(
  $champion: String!
  $role: Rolename
  $patch: String
  $region: Region
  $buildID: Int
  $buildType: LolChampionBuildType
  $gameMode: GameMode!
) {
  lol {
    champion(filters: { slug: $champion, role: $role, patch: $patch, region: $region, gameMode: $gameMode }) {
      build(filters: { buildId: $buildID, type: $buildType }) {
        id
        type
        name
        role
        patch
        championSlug
        vsChampionSlug
        spells
        skillOrder
        skillMaxOrder
        items {
          type
          items
        }
        perks {
          IDs
          style
          subStyle
        }
        stats {
          wins
          matchCount
        }
      }
      stats {
        tier
      }
    }
  }
}
"""

_ddragon_cache = {
    "loaded_at": 0,
    "version": None,
    "items": {},
    "summoners": {},
    "runes": {},
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

def registrar_evento(tipo, mensaje, data=None):
    global agent_event_next_id, agent_last_seen
    agent_last_seen = time.time()
    with agent_event_lock:
        event = {
            "id": agent_event_next_id,
            "ts": agent_last_seen,
            "type": tipo,
            "message": mensaje,
            "data": data or {},
        }
        agent_event_next_id += 1
        agent_events.append(event)
        del agent_events[:-80]
    return event

# ==========================================
# Web UI
# ==========================================
HTML_PAGE = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>AutoQueue</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'><rect width='64' height='64' fill='%23111'/><circle cx='32' cy='32' r='28' fill='%232a5c3a' opacity='0.3'/><circle cx='32' cy='32' r='20' fill='%232a5c3a'/><circle cx='32' cy='32' r='12' fill='%234caf50'/></svg>" type="image/svg+xml">
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

    <div style="background:#0f0f0f; border:1px solid #2a2a2a; border-radius:6px; padding:8px; margin-bottom:8px;">
      <div style="font-size:0.7rem; color:#666; margin-bottom:4px; text-transform:uppercase;">Rol 1</div>
      <div id="role-1-info" style="font-size:0.75rem; color:#7ec8e3; margin-bottom:6px;">- | -</div>
      <button id="role-1-btn" class="btn" style="width:100%; background:#2a3a5c; color:#999; padding:6px; border-radius:4px; border:1px solid #2a5c3a; font-size:0.7rem;" onclick="abrirBuscador(0)">Elegir</button>
      <input id="champ-search-0" class="search-box" placeholder="Buscar campeón..." onkeyup="buscarChampion(0)" style="display:none; margin-top:6px;">
      <div id="search-results-0" class="search-results" style="display:none; margin-top:6px;"></div>
    </div>

    <div style="background:#0f0f0f; border:1px solid #2a2a2a; border-radius:6px; padding:8px;">
      <div style="font-size:0.7rem; color:#666; margin-bottom:4px; text-transform:uppercase;">Rol 2</div>
      <div id="role-2-info" style="font-size:0.75rem; color:#7ec8e3; margin-bottom:6px;">- | -</div>
      <button id="role-2-btn" class="btn" style="width:100%; background:#2a3a5c; color:#999; padding:6px; border-radius:4px; border:1px solid #2a5c3a; font-size:0.7rem;" onclick="abrirBuscador(1)">Elegir</button>
      <input id="champ-search-1" class="search-box" placeholder="Buscar campeón..." onkeyup="buscarChampion(1)" style="display:none; margin-top:6px;">
      <div id="search-results-1" class="search-results" style="display:none; margin-top:6px;"></div>
    </div>

    <div id="bans-section" style="display:none; margin-top:8px;">
      <div style="background:#0f0f0f; border:1px solid #2a2a2a; border-radius:6px; padding:8px; margin-bottom:8px;">
        <div style="font-size:0.7rem; color:#666; margin-bottom:4px; text-transform:uppercase;">Ban 1</div>
        <div id="ban-1-info" style="font-size:0.75rem; color:#7ec8e3; margin-bottom:6px;">-</div>
        <button id="ban-1-btn" class="btn" style="width:100%; background:#2a3a5c; color:#999; padding:6px; border-radius:4px; border:1px solid #2a5c3a; font-size:0.7rem;" onclick="abrirBuscadorBan(0)">Elegir</button>
        <input id="ban-search-0" class="search-box" placeholder="Buscar campeón..." onkeyup="buscarBan(0)" style="display:none; margin-top:6px;">
        <div id="ban-results-0" class="search-results" style="display:none; margin-top:6px;"></div>
      </div>

      <div style="background:#0f0f0f; border:1px solid #2a2a2a; border-radius:6px; padding:8px;">
        <div style="font-size:0.7rem; color:#666; margin-bottom:4px; text-transform:uppercase;">Ban 2</div>
        <div id="ban-2-info" style="font-size:0.75rem; color:#7ec8e3; margin-bottom:6px;">-</div>
        <button id="ban-2-btn" class="btn" style="width:100%; background:#2a3a5c; color:#999; padding:6px; border-radius:4px; border:1px solid #2a5c3a; font-size:0.7rem;" onclick="abrirBuscadorBan(1)">Elegir</button>
        <input id="ban-search-1" class="search-box" placeholder="Buscar campeón..." onkeyup="buscarBan(1)" style="display:none; margin-top:6px;">
        <div id="ban-results-1" class="search-results" style="display:none; margin-top:6px;"></div>
      </div>
    </div>
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
  let rolePos = ["", ""];
  let rolePicks = {
    0: {champ1: null, champ2: null},
    1: {champ1: null, champ2: null}
  };
  let bansPicks = {
    0: null,
    1: null
  };

  // Cargar picks del localStorage
  function cargarPicksLocal() {
    const saved = localStorage.getItem('autoqueuePicks');
    if (saved) {
      try {
        rolePicks = JSON.parse(saved);
        actualizarVistaRol(0);
        actualizarVistaRol(1);
      } catch (e) {
        console.error('[Local] Error al cargar picks:', e);
      }
    }
  }

  function guardarPicksLocal() {
    localStorage.setItem('autoqueuePicks', JSON.stringify(rolePicks));
  }

  function poll() {
    fetch('/api/status').then(r => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return r.json();
    }).then(d => {
      console.log('[Poll] Data:', {
        estado: d.estado,
        fase: d.fase,
        tipo_queue: d.tipo_queue,
        posiciones: d.posiciones,
        miembros: d.miembros_lobby?.length || 0,
        encontrada: d.encontrada
      });

      const el = document.getElementById('estado-text');
      const dot = document.getElementById('dot');
      const act = document.getElementById('actions');
      const infoCard = document.getElementById('info-card');
      const clientStatus = document.getElementById('client-status');

      el.textContent = d.estado;
      el.className = d.encontrada ? 'found' : '';
      dot.className = 'dot' + (d.encontrada ? ' on' : '');

      // Solo mostrar botones si encontrada=true Y no ha sido aceptada aún
      const estadoAceptada = d.estado && (d.estado.includes('aceptada') || d.estado.includes('Aceptada') || d.estado.includes('confirmacion'));
      act.style.display = (d.encontrada && !estadoAceptada) ? 'flex' : 'none';

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
        document.getElementById('bans-section').style.display = isRanked ? 'block' : 'none';
        document.getElementById('aram-info').style.display = isAram ? 'block' : 'none';

        if (isRanked && d.posiciones) {
          const [role1, role2] = d.posiciones.split('/');
          rolePos[0] = role1 || '-';
          rolePos[1] = role2 || '-';
          actualizarVistaRol(0);
          actualizarVistaRol(1);
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
    }).catch(e => {
      console.error('[Poll] Error:', e);
    });
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
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      championsData = await r.json();
      console.log(`[Champions] Cargados ${championsData.length} campeones`);
      llenarSelectores();
    } catch (e) {
      console.error('[Champions] Error:', e);
      championsData = [];
    }
  }

  function llenarSelectores() {
    const roles = ['ban-1', 'ban-2', 'pick-TOP', 'pick-JGL', 'pick-MID', 'pick-ADC', 'pick-SUP'];
    let count = 0;
    roles.forEach(id => {
      const sel = document.getElementById(id);
      if (!sel) {
        console.warn(`[Selectores] No encontrado: ${id}`);
        return;
      }
      sel.innerHTML = '<option value="0">---</option>';
      championsData.forEach(c => {
        const opt = document.createElement('option');
        opt.value = c.id;
        opt.textContent = c.name.substring(0, 12);
        sel.appendChild(opt);
      });
      count++;
    });
    console.log(`[Selectores] Llenados ${count} selectores`);
  }

  function abrirBuscador(roleIdx) {
    const searchId = 'champ-search-' + roleIdx;
    const resultsId = 'search-results-' + roleIdx;
    const searchEl = document.getElementById(searchId);
    const resultsEl = document.getElementById(resultsId);

    if (searchEl.style.display === 'none' || !searchEl.style.display) {
      searchEl.style.display = 'block';
      resultsEl.style.display = 'none';
      searchEl.focus();
    } else {
      searchEl.style.display = 'none';
      resultsEl.style.display = 'none';
      searchEl.value = '';
    }
  }

  function buscarChampion(roleIdx) {
    const searchId = 'champ-search-' + roleIdx;
    const resultsId = 'search-results-' + roleIdx;
    const query = document.getElementById(searchId).value.toLowerCase();
    const resultsDiv = document.getElementById(resultsId);

    if (!query) {
      resultsDiv.style.display = 'none';
      return;
    }

    const filtered = championsData.filter(c => c.name.toLowerCase().includes(query));
    const sorted = filtered.sort((a, b) => a.name.localeCompare(b.name));

    resultsDiv.innerHTML = sorted.slice(0, 8).map(c =>
      `<div class="search-result" onclick="selectChampion(${roleIdx}, '${c.name}')">${c.name}</div>`
    ).join('');
    resultsDiv.style.display = sorted.length > 0 ? 'block' : 'none';
  }

  function selectChampion(roleIdx, champName) {
    if (!rolePicks[roleIdx].champ1) {
      rolePicks[roleIdx].champ1 = champName;
    } else if (!rolePicks[roleIdx].champ2) {
      rolePicks[roleIdx].champ2 = champName;
    } else {
      rolePicks[roleIdx].champ2 = champName;
    }
    actualizarVistaRol(roleIdx);
    guardarPicksLocal();
    document.getElementById('champ-search-' + roleIdx).value = '';
    document.getElementById('search-results-' + roleIdx).style.display = 'none';
  }

  function eliminarChampion(roleIdx, slot) {
    if (slot === 1) rolePicks[roleIdx].champ1 = null;
    else rolePicks[roleIdx].champ2 = null;
    actualizarVistaRol(roleIdx);
    guardarPicksLocal();
  }

  function actualizarVistaRol(roleIdx) {
    const picks = rolePicks[roleIdx];
    const c1 = picks.champ1 ? `<span style="background:#2a5c3a; padding:2px 4px; border-radius:3px; position:relative;">${picks.champ1}<span style="cursor:pointer; margin-left:4px;" onclick="eliminarChampion(${roleIdx}, 1)">×</span></span>` : '-';
    const c2 = picks.champ2 ? `<span style="background:#2a5c3a; padding:2px 4px; border-radius:3px; margin-left:4px; position:relative;">${picks.champ2}<span style="cursor:pointer; margin-left:4px;" onclick="eliminarChampion(${roleIdx}, 2)">×</span></span>` : '';
    const pos = rolePos[roleIdx] || '-';
    document.getElementById('role-' + (roleIdx + 1) + '-info').innerHTML = `${pos} | ${c1} ${c2}`;

    const btnText = picks.champ1 ? 'Elegir 2do pick' : 'Elegir';
    document.getElementById('role-' + (roleIdx + 1) + '-btn').textContent = btnText;
  }

  function abrirBuscadorBan(banIdx) {
    const searchId = 'ban-search-' + banIdx;
    const resultsId = 'ban-results-' + banIdx;
    const searchEl = document.getElementById(searchId);
    const resultsEl = document.getElementById(resultsId);

    if (searchEl.style.display === 'none' || !searchEl.style.display) {
      searchEl.style.display = 'block';
      resultsEl.style.display = 'none';
      searchEl.focus();
    } else {
      searchEl.style.display = 'none';
      resultsEl.style.display = 'none';
      searchEl.value = '';
    }
  }

  function buscarBan(banIdx) {
    const searchId = 'ban-search-' + banIdx;
    const resultsId = 'ban-results-' + banIdx;
    const query = document.getElementById(searchId).value.toLowerCase();
    const resultsDiv = document.getElementById(resultsId);

    if (!query) {
      resultsDiv.style.display = 'none';
      return;
    }

    const filtered = championsData.filter(c => c.name.toLowerCase().includes(query));
    const sorted = filtered.sort((a, b) => a.name.localeCompare(b.name));

    resultsDiv.innerHTML = sorted.slice(0, 8).map(c =>
      `<div class="search-result" onclick="selectBan(${banIdx}, '${c.name}')">${c.name}</div>`
    ).join('');
    resultsDiv.style.display = sorted.length > 0 ? 'block' : 'none';
  }

  function selectBan(banIdx, champName) {
    bansPicks[banIdx] = champName;
    actualizarVistaBan(banIdx);
    document.getElementById('ban-search-' + banIdx).value = '';
    document.getElementById('ban-results-' + banIdx).style.display = 'none';
  }

  function eliminarBan(banIdx) {
    bansPicks[banIdx] = null;
    actualizarVistaBan(banIdx);
  }

  function actualizarVistaBan(banIdx) {
    const champ = bansPicks[banIdx];
    const display = champ ? `<span style="background:#2a5c3a; padding:2px 4px; border-radius:3px;">${champ}<span style="cursor:pointer; margin-left:4px;" onclick="eliminarBan(${banIdx})">×</span></span>` : '-';
    document.getElementById('ban-' + (banIdx + 1) + '-info').innerHTML = display;
    document.getElementById('ban-' + (banIdx + 1) + '-btn').textContent = champ ? 'Cambiar' : 'Elegir';
  }

  async function cargarConfig() {
    try {
      const r = await fetch('/api/config');
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const cfg = await r.json();
      document.getElementById('ban-1').value = cfg.bans?.ban1 || 0;
      document.getElementById('ban-2').value = cfg.bans?.ban2 || 0;
      ['TOP', 'JGL', 'MID', 'ADC', 'SUP'].forEach(r => {
        const el = document.getElementById('pick-' + r);
        if (el) el.value = cfg.picks?.[r] || 0;
      });
      console.log('[Config] Cargada');
    } catch (e) {
      console.error('[Config] Error:', e);
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

  function clickRole(roleIdx) {
    const searchId = 'champ-search-' + (roleIdx + 1);
    const resultsId = 'search-results-' + (roleIdx + 1);
    const searchEl = document.getElementById(searchId);

    if (searchEl.style.display === 'none' || !searchEl.style.display) {
      searchEl.style.display = 'block';
      document.getElementById('champ-search-' + (3 - roleIdx)).style.display = 'none';
      document.getElementById('search-results-' + (3 - roleIdx)).style.display = 'none';
      searchEl.focus();
      selectedRoleIndex = roleIdx;
    } else {
      searchEl.style.display = 'none';
      document.getElementById(resultsId).style.display = 'none';
      selectedRoleIndex = -1;
    }
  }

  async function inicializar() {
    try {
      console.log('[Init] Cargando picks locales...');
      cargarPicksLocal();
      console.log('[Init] Picks locales cargados');

      console.log('[Init] Cargando campeones...');
      await cargarChampions();
      console.log('[Init] Campeones cargados');

      console.log('[Init] Cargando config...');
      await cargarConfig();
      console.log('[Init] Config cargada');

      const content = document.getElementById('config-content');
      const toggle = document.getElementById('config-toggle');
      if (content && toggle) {
        content.classList.add('open');
        toggle.textContent = '▲';
      }
      console.log('[Init] OK');
    } catch (e) {
      console.error('[Init] ERROR:', e);
    }
  }

  window.addEventListener('load', () => {
    console.log('[App] Página cargada');
    inicializar();
    poll();
  });
  setInterval(poll, 1500);
</script>
</body>
</html>"""

# ==========================================
# Flask - Monitor Web
# ==========================================
app = Flask(__name__)

@app.route('/test')
def test():
    return 'OK'

@app.route('/')
def index():
    web_config["ultimo_acceso"] = time.time()
    client_ip = request.remote_addr
    logger.info(f"📱 Acceso web desde: {client_ip}")
    resp = make_response(HTML_PAGE)
    resp.headers['Content-Type'] = 'text/html; charset=utf-8'
    return resp

@app.route('/api/status')
def api_status():
    web_config["ultimo_acceso"] = time.time()
    data = {**estado_partida, "velocidad_maxima": web_config["velocidad_maxima"]}
    return jsonify(data)

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

@app.route('/api/bestbuild/context', methods=['GET'])
def api_bestbuild_context():
    return jsonify(bestbuild_state)

@app.route('/api/bestbuild/recommendation', methods=['GET'])
def api_bestbuild_recommendation():
    return jsonify(_generar_recomendacion_basica(bestbuild_state))

@app.route('/api/bestbuild/manual', methods=['GET'])
def api_bestbuild_manual():
    champion_input = request.args.get("champion") or request.args.get("pick")
    opponent_input = request.args.get("opponent") or request.args.get("vs")
    role = normalizar_rol_usuario(request.args.get("role")) or None

    if not champion_input:
        return jsonify({"ok": False, "message": "Falta campeon. Ejemplo: /build yasuo vs ahri"}), 400

    champion_id, champion_name = resolver_campeon(champion_input)
    opponent_id, opponent_name = resolver_campeon(opponent_input) if opponent_input else (None, None)
    champion_name = champion_name or str(champion_input).strip().title()
    opponent_name = opponent_name or (str(opponent_input).strip().title() if opponent_input else None)

    context = {
        "updated_at": time.time(),
        "phase": "Manual",
        "queue": "Manual",
        "mode": "CLASSIC",
        "local_player": {
            "cell_id": 0,
            "champion_id": champion_id,
            "champion": champion_name,
            "position": role,
            "summoner_id": None,
        },
        "lane_opponent": {
            "cell_id": 1,
            "champion_id": opponent_id,
            "champion": opponent_name,
            "position": role,
            "summoner_id": None,
        } if opponent_name else None,
        "allies": [],
        "enemies": [{
            "cell_id": 1,
            "champion_id": opponent_id,
            "champion": opponent_name,
            "position": role,
            "summoner_id": None,
        }] if opponent_name else [],
    }
    return jsonify(_generar_recomendacion_basica(context))

@app.route('/api/agent/events', methods=['GET'])
def api_agent_events():
    since = request.args.get("since", "0")
    try:
        since_id = int(since)
    except ValueError:
        since_id = 0
    with agent_event_lock:
        events = [event for event in agent_events if event["id"] > since_id]
    return jsonify({"ok": True, "events": events, "last_seen": agent_last_seen})

@app.route('/api/agent/insta', methods=['POST'])
def api_agent_insta():
    data = request.get_json(silent=True) or {}
    enabled = bool(data.get("enabled", False))
    web_config["velocidad_maxima"] = enabled
    estado = "activado" if enabled else "desactivado"
    registrar_evento("insta_changed", f"Insta-accept {estado}.", {"enabled": enabled})
    return jsonify({"ok": True, "enabled": enabled})

@app.route('/api/agent/action', methods=['POST'])
def api_agent_action():
    data = request.get_json(silent=True) or {}
    action = data.get("action")
    if action not in ("aceptar", "rechazar"):
        return jsonify({"ok": False, "error": "accion invalida"}), 400
    web_accion["pendiente"] = action
    return jsonify({"ok": True, "action": action})

@app.route('/api/agent/ban', methods=['POST'])
def api_agent_ban():
    data = request.get_json(silent=True) or {}
    slot = int(data.get("slot") or 1)
    champion_id, champion_name = resolver_campeon(data.get("champion"))
    if slot not in (1, 2):
        return jsonify({"ok": False, "error": "slot invalido"}), 400
    if not champion_id:
        return jsonify({"ok": False, "error": "campeon no encontrado"}), 404

    config = cargar_config()
    config["bans"][f"ban{slot}"] = champion_id
    guardar_config(config)
    registrar_evento("ban_configured", f"Ban {slot}: {champion_name}.", {"slot": slot, "champion_id": champion_id})
    return jsonify({"ok": True, "slot": slot, "champion_id": champion_id, "champion": champion_name})

@app.route('/api/agent/pick', methods=['POST'])
def api_agent_pick():
    data = request.get_json(silent=True) or {}
    role = normalizar_rol_usuario(data.get("role"))
    champion_id, champion_name = resolver_campeon(data.get("champion"))
    if role not in ("TOP", "JGL", "MID", "ADC", "SUP"):
        return jsonify({"ok": False, "error": "rol invalido"}), 400
    if not champion_id:
        return jsonify({"ok": False, "error": "campeon no encontrado"}), 404

    config = cargar_config()
    config["picks"][role] = champion_id
    guardar_config(config)
    registrar_evento("pick_configured", f"Pick {role}: {champion_name}.", {"role": role, "champion_id": champion_id})
    return jsonify({"ok": True, "role": role, "champion_id": champion_id, "champion": champion_name})

@app.route('/api/agent/config-summary', methods=['GET'])
def api_agent_config_summary():
    config = cargar_config()
    bans = {
        key: {"id": value, "name": _champion_name(value)}
        for key, value in config.get("bans", {}).items()
        if value
    }
    picks = {
        key: {"id": value, "name": _champion_name(value)}
        for key, value in config.get("picks", {}).items()
        if value
    }
    return jsonify({"ok": True, "bans": bans, "picks": picks, "insta_accept": web_config["velocidad_maxima"]})

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
    {"id": 86, "name": "Gnar"}, {"id": 89, "name": "Leona"}, {"id": 90, "name": "Talon"}, {"id": 91, "name": "Tahm Kench"},
    {"id": 92, "name": "Riven"}, {"id": 96, "name": "Kog'Maw"}, {"id": 98, "name": "Shen"}, {"id": 99, "name": "Lux"},
    {"id": 101, "name": "Xerath"}, {"id": 102, "name": "Shyvana"}, {"id": 103, "name": "Ahri"}, {"id": 104, "name": "Darius"},
    {"id": 105, "name": "Fizz"}, {"id": 106, "name": "Volibear"}, {"id": 107, "name": "Rengar"}, {"id": 110, "name": "Varus"},
    {"id": 111, "name": "Nautilus"}, {"id": 112, "name": "Viktor"}, {"id": 113, "name": "Sejuani"}, {"id": 114, "name": "Fiora"},
    {"id": 115, "name": "Zyra"}, {"id": 117, "name": "Lulu"}, {"id": 119, "name": "Draven"}, {"id": 120, "name": "Hecarim"},
    {"id": 121, "name": "Kha'Zix"}, {"id": 126, "name": "Jayce"}, {"id": 127, "name": "Lissandra"}, {"id": 131, "name": "Diana"},
    {"id": 133, "name": "Quinn"}, {"id": 134, "name": "Syndra"}, {"id": 136, "name": "Aurelion Sol"}, {"id": 141, "name": "Kayn"},
    {"id": 142, "name": "Zoe"}, {"id": 145, "name": "Kai'Sa"}, {"id": 147, "name": "Seraphine"}, {"id": 154, "name": "Zac"},
    {"id": 157, "name": "Yasuo"}, {"id": 161, "name": "Vel'Koz"}, {"id": 163, "name": "Taliyah"}, {"id": 164, "name": "Camille"},
    {"id": 166, "name": "Yuumi"}, {"id": 167, "name": "Akshan"}, {"id": 168, "name": "Hwei"}, {"id": 245, "name": "Ekko"},
    {"id": 246, "name": "Qiyana"}, {"id": 247, "name": "Rell"}, {"id": 254, "name": "Vi"}, {"id": 266, "name": "Aatrox"},
    {"id": 267, "name": "Nami"}, {"id": 268, "name": "Azir"}, {"id": 360, "name": "Samira"}, {"id": 412, "name": "Thresh"},
    {"id": 420, "name": "Illaoi"}, {"id": 421, "name": "Rek'Sai"}, {"id": 427, "name": "Ivern"}, {"id": 429, "name": "Kalista"},
    {"id": 432, "name": "Bard"}, {"id": 497, "name": "Rakan"}, {"id": 498, "name": "Xayah"}, {"id": 516, "name": "Ornn"},
    {"id": 517, "name": "Sylas"}, {"id": 518, "name": "Neeko"}, {"id": 523, "name": "Aphelios"}, {"id": 555, "name": "Pyke"},
    {"id": 875, "name": "Sett"}, {"id": 876, "name": "Lillia"}, {"id": 887, "name": "Yone"}, {"id": 897, "name": "K'Sante"},
]

@app.route('/api/champions', methods=['GET'])
def api_champions():
    web_config["ultimo_acceso"] = time.time()

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

    return jsonify(sorted(CHAMPIONS_HARDCODED, key=lambda x: x["name"]))

_CHAMPION_NAMES_BY_ID = {c["id"]: c["name"] for c in CHAMPIONS_HARDCODED}
_champion_names_loaded_from_lcu = False
_champion_names_loaded_from_ddragon = False

def _refresh_champion_names_from_lcu():
    global _champion_names_loaded_from_lcu

    if _champion_names_loaded_from_lcu or not (lcu_conn["port"] and lcu_conn["token"]):
        return

    try:
        creds = base64.b64encode(f"riot:{lcu_conn['token']}".encode()).decode()
        headers = {'Authorization': f'Basic {creds}'}
        r = requests.get(
            f"https://127.0.0.1:{lcu_conn['port']}/lol-game-data/assets/v1/champion-summary.json",
            headers=headers,
            verify=False,
            timeout=2
        )
        if r.status_code == 200:
            for champion in r.json():
                champion_id = champion.get("id")
                name = champion.get("name")
                if isinstance(champion_id, int) and champion_id > 0 and name:
                    _CHAMPION_NAMES_BY_ID[champion_id] = name
            _champion_names_loaded_from_lcu = True
    except Exception:
        pass

def _refresh_champion_names_from_ddragon():
    global _champion_names_loaded_from_ddragon

    if _champion_names_loaded_from_ddragon:
        return

    try:
        version_response = requests.get("https://ddragon.leagueoflegends.com/api/versions.json", timeout=6)
        version_response.raise_for_status()
        version = version_response.json()[0]
        champion_response = requests.get(
            f"https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/champion.json",
            timeout=8,
        )
        champion_response.raise_for_status()
        for champion in champion_response.json().get("data", {}).values():
            champion_id = champion.get("key")
            name = champion.get("name")
            if champion_id and str(champion_id).isdigit() and name:
                _CHAMPION_NAMES_BY_ID[int(champion_id)] = name
        _champion_names_loaded_from_ddragon = True
    except Exception as e:
        logger.warning(f"[Champions] No se pudo cargar Data Dragon: {e}")

def _champion_name(champion_id):
    if not champion_id:
        return None
    _refresh_champion_names_from_lcu()
    _refresh_champion_names_from_ddragon()
    return _CHAMPION_NAMES_BY_ID.get(champion_id, f"Champion {champion_id}")

def _normalizar_nombre_campeon(value):
    value = str(value or "").lower()
    value = value.replace("&", "and")
    return "".join(ch for ch in value if ch.isalnum())

def resolver_campeon(value):
    if value is None:
        return None, None
    if isinstance(value, (int, float)) or str(value).isdigit():
        champion_id = int(value)
        return champion_id, _champion_name(champion_id)

    _refresh_champion_names_from_lcu()
    _refresh_champion_names_from_ddragon()
    target = _normalizar_nombre_campeon(value)
    for champion_id, name in _CHAMPION_NAMES_BY_ID.items():
        if _normalizar_nombre_campeon(name) == target:
            return champion_id, name

    aliases = {
        "wukong": "monkeyking",
        "nunu": "nunuandwillump",
        "drmundo": "drmundo",
        "jarvaniv": "jarvaniv",
        "ksante": "ksante",
        "chogath": "chogath",
        "khazix": "khazix",
        "kogmaw": "kogmaw",
        "kaisa": "kaisa",
        "reksai": "reksai",
        "velkoz": "velkoz",
    }
    alias_target = aliases.get(target, target)
    for champion_id, name in _CHAMPION_NAMES_BY_ID.items():
        if _normalizar_nombre_campeon(name) == alias_target:
            return champion_id, name
    return None, None

def normalizar_rol_usuario(role):
    aliases = {
        "TOP": "TOP",
        "JGL": "JGL",
        "JG": "JGL",
        "JUNGLE": "JGL",
        "JUNGLA": "JGL",
        "MID": "MID",
        "MIDDLE": "MID",
        "ADC": "ADC",
        "BOT": "ADC",
        "BOTTOM": "ADC",
        "SUP": "SUP",
        "SUPP": "SUP",
        "SUPPORT": "SUP",
        "UTILITY": "SUP",
    }
    return aliases.get(str(role or "").strip().upper())

def _normalize_position(position):
    aliases = {
        "TOP": "TOP",
        "JUNGLE": "JGL",
        "MIDDLE": "MID",
        "BOTTOM": "ADC",
        "UTILITY": "SUP",
    }
    return aliases.get(position or "", position or None)

def _summarize_champ_select_player(player):
    champion_id = player.get("championId") or player.get("championPickIntent") or 0
    return {
        "cell_id": player.get("cellId"),
        "champion_id": champion_id or None,
        "champion": _champion_name(champion_id),
        "position": _normalize_position(player.get("assignedPosition")),
        "summoner_id": player.get("summonerId"),
    }

def _procesar_bestbuild_context(data):
    local_cell = data.get("localPlayerCellId")
    allies = [_summarize_champ_select_player(p) for p in data.get("myTeam", [])]
    enemies = [_summarize_champ_select_player(p) for p in data.get("theirTeam", [])]
    local_player = next((p for p in allies if p["cell_id"] == local_cell), None)

    lane_opponent = None
    if local_player and local_player.get("position"):
        lane_opponent = next(
            (p for p in enemies if p.get("position") == local_player["position"] and p.get("champion")),
            None
        )

    bestbuild_state.update({
        "updated_at": time.time(),
        "phase": estado_partida.get("fase"),
        "queue": estado_partida.get("tipo_queue"),
        "mode": estado_partida.get("modo_juego"),
        "local_player": local_player,
        "lane_opponent": lane_opponent,
        "allies": allies,
        "enemies": enemies,
    })

def _generar_recomendacion_basica(context):
    local_player = context.get("local_player") or {}
    champion = local_player.get("champion")
    role = local_player.get("position")
    opponent = (context.get("lane_opponent") or {}).get("champion")
    enemies = [p.get("champion") for p in context.get("enemies", []) if p.get("champion")]

    if not champion:
        return {
            "ok": False,
            "message": "Todavia no detecte tu campeon. Entra a champ select o pickea/declara uno.",
            "context": context,
        }

    mobalytics = _obtener_build_mobalytics(context)
    if mobalytics.get("ok"):
        mobalytics["context"] = context
        return mobalytics

    notes = []
    if opponent:
        notes.append(f"Matchup detectado: {champion} {role or ''} vs {opponent}.")
    elif enemies:
        notes.append("Todavia no pude identificar rival de linea, pero ya veo enemigos: " + ", ".join(enemies) + ".")
    else:
        notes.append("Aun no hay enemigos visibles para adaptar la build.")

    if mobalytics.get("error"):
        notes.append(f"Mobalytics no respondio una build utilizable: {mobalytics['error']}.")
    notes.append("Regla base: prioriza botas defensivas contra mucho CC/AD/AP y anti-heal si ves curacion fuerte.")

    return {
        "ok": True,
        "champion": champion,
        "role": role,
        "opponent": opponent,
        "queue": context.get("queue"),
        "items": [],
        "runes": [],
        "notes": notes,
        "context": context,
    }

def _obtener_build_mobalytics(context):
    local_player = context.get("local_player") or {}
    champion = local_player.get("champion")
    role = local_player.get("position")
    opponent = (context.get("lane_opponent") or {}).get("champion")

    if not champion:
        return {"ok": False, "error": "sin campeon detectado"}

    role_mobalytics = _rol_mobalytics(role)
    game_mode = _modo_mobalytics(context)
    last_error = None

    for slug in _mobalytics_slug_candidates(champion):
        try:
            raw = _consultar_mobalytics(slug, role_mobalytics, game_mode)
            build = (((raw.get("data") or {}).get("lol") or {}).get("champion") or {}).get("build")
            stats = (((raw.get("data") or {}).get("lol") or {}).get("champion") or {}).get("stats") or {}
            if build:
                return _formatear_build_mobalytics(build, stats, champion, role, opponent, context)
            last_error = f"sin build para slug {slug}"
        except Exception as e:
            last_error = str(e)

    return {"ok": False, "error": last_error or "sin respuesta"}

def _consultar_mobalytics(champion_slug, role, game_mode):
    headers = {
        "Content-Type": "application/json",
        "Accept-Language": "en_us",
        "Authorization": f"Bearer {MOBALYTICS_WIDGET_TOKEN}",
    }
    variables = {
        "champion": champion_slug,
        "role": role,
        "patch": None,
        "region": "ALL",
        "buildID": None,
        "buildType": "RECOMMENDED",
        "gameMode": game_mode,
    }
    response = requests.post(
        MOBALYTICS_GQL_URL,
        headers=headers,
        json={"query": MOBALYTICS_DYNAMIC_BUILD_QUERY, "variables": variables},
        timeout=8,
    )
    response.raise_for_status()
    data = response.json()
    if data.get("errors"):
        raise RuntimeError(data["errors"][0].get("message", "error GraphQL"))
    return data

def _formatear_build_mobalytics(build, stats, champion, role, opponent, context):
    ddragon = _obtener_datadragon()
    items = _formatear_items_mobalytics(build.get("items") or [], ddragon["items"])
    spells = [_nombre_o_id(ddragon["summoners"], sid) for sid in build.get("spells") or []]
    runes = [_nombre_o_id(ddragon["runes"], rid) for rid in (build.get("perks") or {}).get("IDs") or []]
    skill_order = [_skill_key(idx) for idx in build.get("skillOrder") or []]
    skill_max_order = [_skill_key(idx) for idx in build.get("skillMaxOrder") or []]

    wins = (build.get("stats") or {}).get("wins")
    match_count = (build.get("stats") or {}).get("matchCount")
    winrate = round(wins * 100 / match_count, 1) if wins and match_count else None

    notes = [
        f"Fuente: Mobalytics {build.get('type') or 'build'} patch {build.get('patch') or 'actual'}.",
    ]
    if winrate:
        notes.append(f"Winrate de la build: {winrate}% en {match_count} partidas.")
    if opponent:
        notes.append(f"Matchup detectado: {champion} {role or ''} vs {opponent}.")
    else:
        notes.append("No detecte rival de linea; uso build recomendada general por campeon/rol.")
    notes.extend(_notas_situacionales(context))

    return {
        "ok": True,
        "source": "mobalytics",
        "champion": champion,
        "role": role or build.get("role"),
        "opponent": opponent,
        "queue": context.get("queue"),
        "patch": build.get("patch"),
        "tier": stats.get("tier"),
        "winrate": winrate,
        "games": match_count,
        "spells": spells,
        "items": items,
        "runes": runes,
        "skill_order": skill_order,
        "skill_max_order": skill_max_order,
        "notes": notes,
    }

def _formatear_items_mobalytics(item_groups, item_names):
    formatted = []
    for group in item_groups:
        item_ids = group.get("items") or []
        names = [_nombre_o_id(item_names, item_id) for item_id in item_ids]
        if names:
            formatted.append({"type": group.get("type") or "Items", "items": names})
    return formatted

def _obtener_datadragon():
    if _ddragon_cache["items"] and time.time() - _ddragon_cache["loaded_at"] < 86400:
        return _ddragon_cache

    try:
        version_response = requests.get("https://ddragon.leagueoflegends.com/api/versions.json", timeout=6)
        version_response.raise_for_status()
        version = version_response.json()[0]

        item_response = requests.get(
            f"https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/item.json",
            timeout=8,
        )
        summoner_response = requests.get(
            f"https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/summoner.json",
            timeout=8,
        )
        runes_response = requests.get(
            f"https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/runesReforged.json",
            timeout=8,
        )
        item_response.raise_for_status()
        summoner_response.raise_for_status()
        runes_response.raise_for_status()

        _ddragon_cache.update({
            "loaded_at": time.time(),
            "version": version,
            "items": {int(k): v.get("name", k) for k, v in item_response.json().get("data", {}).items()},
            "summoners": {
                int(v.get("key")): v.get("name", v.get("key"))
                for v in summoner_response.json().get("data", {}).values()
                if str(v.get("key", "")).isdigit()
            },
            "runes": _flatten_runes(runes_response.json()),
        })
    except Exception as e:
        logger.warning(f"[BestBuild] No se pudo cargar Data Dragon: {e}")

    return _ddragon_cache

def _flatten_runes(paths):
    runes = {}
    for path in paths:
        if path.get("id"):
            runes[int(path["id"])] = path.get("name", str(path["id"]))
        for slot in path.get("slots", []):
            for rune in slot.get("runes", []):
                if rune.get("id"):
                    runes[int(rune["id"])] = rune.get("name", str(rune["id"]))
    runes.update({
        5008: "Adaptive Force",
        5005: "Attack Speed",
        5007: "Ability Haste",
        5002: "Armor",
        5003: "Magic Resist",
        5001: "Health Scaling",
    })
    return runes

def _nombre_o_id(dictionary, value):
    if value is None:
        return None
    return dictionary.get(int(value), str(value)) if str(value).isdigit() else str(value)

def _skill_key(index):
    return {1: "Q", 2: "W", 3: "E", 4: "R"}.get(index, str(index))

def _rol_mobalytics(role):
    return {
        "TOP": "TOP",
        "JGL": "JUNGLE",
        "MID": "MID",
        "ADC": "ADC",
        "SUP": "SUPPORT",
    }.get(role or "", None)

def _modo_mobalytics(context):
    mode = (context.get("mode") or "").upper()
    queue = (context.get("queue") or "").upper()
    if "ARAM" in mode or "ARAM" in queue:
        return "ARAM"
    if "ARENA" in mode or "ARENA" in queue:
        return "ARENA"
    return "SUMMONER_RIFT"

def _mobalytics_slug_candidates(champion_name):
    base = _slugify_mobalytics(champion_name)
    aliases = {
        "aurelion-sol": ["aurelionsol"],
        "belveth": ["belveth"],
        "chogath": ["chogath"],
        "dr-mundo": ["drmundo"],
        "jarvan-iv": ["jarvaniv"],
        "kaisa": ["kaisa"],
        "khazix": ["khazix"],
        "ksante": ["ksante"],
        "kogmaw": ["kogmaw"],
        "leblanc": ["leblanc"],
        "lee-sin": ["leesin"],
        "master-yi": ["masteryi"],
        "miss-fortune": ["missfortune"],
        "nunu-willump": ["nunu"],
        "reksai": ["reksai"],
        "renata-glasc": ["renata"],
        "tahm-kench": ["tahmkench"],
        "twisted-fate": ["twistedfate"],
        "velkoz": ["velkoz"],
        "wukong": ["monkeyking"],
        "xin-zhao": ["xinzhao"],
    }
    candidates = [base] + aliases.get(base, [])
    compact = base.replace("-", "")
    if compact not in candidates:
        candidates.append(compact)
    return list(dict.fromkeys(candidates))

def _slugify_mobalytics(name):
    normalized = name.lower()
    normalized = normalized.replace("&", " ")
    normalized = normalized.replace(".", "")
    normalized = normalized.replace("'", "")
    normalized = normalized.replace("’", "")
    chars = []
    last_dash = False
    for ch in normalized:
        if ch.isalnum():
            chars.append(ch)
            last_dash = False
        elif not last_dash:
            chars.append("-")
            last_dash = True
    return "".join(chars).strip("-")

def _notas_situacionales(context):
    enemies = " ".join(p.get("champion") or "" for p in context.get("enemies", []))
    notes = []
    healing_names = ("Aatrox", "Soraka", "Yuumi", "Vladimir", "Warwick", "Dr. Mundo", "Briar", "Nami", "Sona")
    if any(name in enemies for name in healing_names):
        notes.append("Situacional: considera anti-heal si la curacion enemiga empieza a pesar.")
    if any(name in enemies for name in ("Zed", "Talon", "Qiyana", "Yasuo", "Yone", "Naafiri")):
        notes.append("Situacional: contra asesinos AD, valora armadura temprana o item defensivo antes de codiciar dano.")
    if any(name in enemies for name in ("Leona", "Nautilus", "Morgana", "Lissandra", "Sejuani", "Amumu")):
        notes.append("Situacional: si el CC enemigo manda, Mercurial/Mercs o tenacidad suben mucho de valor.")
    return notes

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 443))
        ip = s.getsockname()[0]
        s.close()
        return ip if ip and not ip.startswith("127.") else None
    except Exception:
        return None

def puerto_disponible(host, puerto):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, puerto))
        return True
    except OSError:
        return False

def iniciar_servidor_web():
    logging.getLogger('werkzeug').disabled = True
    ip_local = get_local_ip()

    print(f"\n[Web] IP detectada: {ip_local or 'FALLO'}\n")

    if ip_local:
        print(f"[Web] Intentando 0.0.0.0 en puertos 5000-5010...")
        for puerto in range(5000, 5011):
            try:
                if not puerto_disponible("0.0.0.0", puerto):
                    continue
                web_info["url"] = f"http://{ip_local}:{puerto}"
                print(f"[Web] ✓ Escuchando en 0.0.0.0:{puerto}")
                print(f"[Web] Accede desde celular: {web_info['url']}\n")
                app.run(host='0.0.0.0', port=puerto, debug=False, use_reloader=False, threaded=True)
                return
            except OSError:
                continue
            except Exception as e:
                print(f"[Web] Error: {e}")
                break

    print(f"[Web] Fallback a 127.0.0.1...")
    for puerto in range(5000, 5011):
        try:
            if not puerto_disponible("127.0.0.1", puerto):
                continue
            web_info["url"] = f"http://127.0.0.1:{puerto}"
            print(f"[Web] ✓ Escuchando en 127.0.0.1:{puerto} (solo localhost)\n")
            app.run(host='127.0.0.1', port=puerto, debug=False, use_reloader=False, threaded=True)
            return
        except OSError:
            continue

    print(f"[Web] ERROR: No se pudo iniciar el servidor")

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

        if msvcrt and msvcrt.kbhit():
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
    registrar_evento("ready_check_found", "Partida encontrada. Esperando decision o auto-accept.", {"attempt": intento})

def _on_aceptacion_ok():
    logger.info("Partida aceptada.")
    estado_partida["estado"] = "Partida aceptada - Esperando confirmacion"
    registrar_evento("accepted", "Partida aceptada.")

def _on_aceptacion_error(detalle):
    logger.warning(f"Error al aceptar ({detalle}). Reintentando...")
    estado_partida["intentos_partida_actual"] += 1

def _on_rechazo():
    logger.info("Partida rechazada.")
    estado_partida["estado"] = "Partida rechazada"
    registrar_evento("declined", "Partida rechazada.")
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
    fase_anterior = estado_partida.get("fase")
    estado_partida["fase"] = fase
    if fase == "Lobby":
        estado_partida["estado"] = "En lobby"
    elif fase == "Matchmaking":
        estado_partida["estado"] = "En cola"
    elif fase == "ChampSelect":
        estado_partida["estado"] = "Selección de campeones"
        if fase_anterior != "ChampSelect":
            registrar_evento("champ_select_started", "Champ select iniciado.")
    elif fase == "InProgress":
        if fase_anterior != "InProgress":
            registrar_evento("game_started", "Entraste a partida.")
    elif fase not in ("ReadyCheck", "InProgress"):
        _resetear_estado_lobby()

def _resetear_estado_lobby():
    estado_partida.update({"miembros_lobby": [], "posiciones": None, "modo_juego": None})
    champ_select_state.update({"local_cell_id": None, "action_id_ban": None, "action_id_pick": None})
    bestbuild_state.update({
        "updated_at": None,
        "phase": estado_partida.get("fase"),
        "queue": None,
        "mode": None,
        "local_player": None,
        "lane_opponent": None,
        "allies": [],
        "enemies": [],
    })

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

def _detectar_turno_pick(data):
    local_cell = data.get("localPlayerCellId")
    for grupo in data.get("actions", []):
        for action in grupo:
            if (action.get("type") == "pick"
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

def _elegir_campeon_pick(data):
    local_cell = data.get("localPlayerCellId")
    local_player = next((p for p in data.get("myTeam", []) if p.get("cellId") == local_cell), {})
    if local_player.get("championId"):
        return None
    role = _normalize_position(local_player.get("assignedPosition"))
    config = cargar_config()
    champion_id = config.get("picks", {}).get(role or "", 0)
    if not champion_id:
        return None
    bans = data.get("bans", {})
    banned = set(bans.get("myTeamBans", []) + bans.get("theirTeamBans", []))
    picked = {p.get("championId") for p in data.get("myTeam", []) + data.get("theirTeam", []) if p.get("championId")}
    if champion_id in banned or champion_id in picked:
        return None
    return champion_id

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
            registrar_evento("ban_done", f"Baneé {_champion_name(champion_id)}.", {"champion_id": champion_id})
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
            registrar_evento("ban_done", f"Baneé {_champion_name(champion_id)}.", {"champion_id": champion_id})
        else:
            logger.warning(f"[AutoBan] Error HTTP {r.status_code} al banear.")
    except Exception as e:
        logger.warning(f"[AutoBan] Error: {e}")
    finally:
        estado_partida["ban_en_progreso"] = False

async def _ejecutar_pick_ws(connection, action_id, champion_id):
    if estado_partida["pick_en_progreso"]:
        return
    estado_partida["pick_en_progreso"] = True
    try:
        r = await connection.request('patch',
            f'/lol-champ-select/v1/session/actions/{action_id}',
            data={"championId": champion_id, "completed": True})
        if r.status in (200, 204):
            logger.info(f"[AutoPick] Campeón ID {champion_id} pickeado.")
            registrar_evento("pick_done", f"Pickeé {_champion_name(champion_id)}.", {"champion_id": champion_id})
        else:
            logger.warning(f"[AutoPick] Error HTTP {r.status} al pickear.")
    except Exception as e:
        logger.warning(f"[AutoPick] Error: {e}")
    finally:
        estado_partida["pick_en_progreso"] = False

def _ejecutar_pick_polling(session, port, action_id, champion_id):
    if estado_partida["pick_en_progreso"]:
        return
    estado_partida["pick_en_progreso"] = True
    try:
        r = session.patch(
            f"https://127.0.0.1:{port}/lol-champ-select/v1/session/actions/{action_id}",
            json={"championId": champion_id, "completed": True}, timeout=3)
        if r.status_code in (200, 204):
            logger.info(f"[AutoPick] Campeón ID {champion_id} pickeado.")
            registrar_evento("pick_done", f"Pickeé {_champion_name(champion_id)}.", {"champion_id": champion_id})
        else:
            logger.warning(f"[AutoPick] Error HTTP {r.status_code} al pickear.")
    except Exception as e:
        logger.warning(f"[AutoPick] Error: {e}")
    finally:
        estado_partida["pick_en_progreso"] = False


# ==========================================
# Nivel 1: lcu-driver (WebSocket, deteccion automatica)
# ==========================================
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

connector = Connector()

@connector.ready
async def connect(connection):
    logger.info("✓ Cliente conectado (lcu-driver + WebSocket)")
    estado_partida["estado"] = "Buscando partida"
    estado_partida["nivel_conexion"] = "lcu-driver + WebSocket"
    registrar_evento("agent_online", "AutoQueue conectado al cliente de LoL.")
    try:
        if hasattr(connection, 'port'):
            lcu_conn["port"] = connection.port
        if hasattr(connection, 'auth_key'):
            lcu_conn["token"] = connection.auth_key

        r = await connection.request('get', '/lol-gameflow/v1/gameflow-phase')
        data = await r.json()
        _actualizar_fase(data)

        asyncio.create_task(_polling_lobby_loop(connection))
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

async def _polling_lobby_loop(connection):
    """Polling periódico de lobby y champ-select (fallback para WebSocket)"""
    while True:
        await asyncio.sleep(2.0)

        try:
            if estado_partida["fase"] == "Lobby" and time.time() - _last_lobby_update > 3.0:
                r = await connection.request('get', '/lol-lobby/v2/lobby')
                data = await r.json()
                _procesar_lobby(data)
        except Exception as e:
            logger.error(f"[Polling Lobby] Error: {e}")

        try:
            if estado_partida["fase"] == "ChampSelect" and time.time() - _last_champ_select_update > 2.0:
                r = await connection.request('get', '/lol-champ-select/v1/session')
                data = await r.json()
                champ_select_state["local_cell_id"] = data.get("localPlayerCellId")
                _procesar_bestbuild_context(data)
                action_id = _detectar_turno_ban(data)
                if action_id:
                    champion_id = _elegir_campeon_ban(data)
                    if champion_id:
                        await _ejecutar_ban_ws(connection, action_id, champion_id)
                pick_action_id = _detectar_turno_pick(data)
                if pick_action_id:
                    champion_id = _elegir_campeon_pick(data)
                    if champion_id:
                        await _ejecutar_pick_ws(connection, pick_action_id, champion_id)
        except Exception as e:
            logger.error(f"[Polling ChampSelect] Error: {e}")

@connector.close
async def disconnect(connection):
    logger.info("Cliente cerrado.")
    estado_partida["estado"] = "Cliente cerrado"
    estado_partida.update({"encontrada": False, "intentos_partida_actual": 0})
    registrar_evento("agent_offline", "Cliente de LoL cerrado.")

@connector.ws.register('/lol-gameflow/v1/gameflow-phase', event_types=('UPDATE',))
async def on_gameflow_phase(connection, event):
    try:
        fase = event.data
        logger.info(f"[Fase] {fase}")
        _actualizar_fase(fase)
    except Exception as e:
        logger.error(f"[Fase] Error: {e}")

_last_lobby_update = 0.0
_last_champ_select_update = 0.0

@connector.ws.register('/lol-lobby/v2/lobby', event_types=('UPDATE',))
async def on_lobby_update_ws(connection, event):
    global _last_lobby_update
    _last_lobby_update = time.time()
    try:
        _procesar_lobby(event.data)
    except Exception as e:
        logger.debug(f"[Lobby WS] Error: {e}")

@connector.ws.register('/lol-champ-select/v1/session', event_types=('UPDATE',))
async def on_champ_select(connection, event):
    global _last_champ_select_update
    _last_champ_select_update = time.time()
    try:
        data = event.data
        champ_select_state["local_cell_id"] = data.get("localPlayerCellId")
        _procesar_bestbuild_context(data)
        action_id = _detectar_turno_ban(data)
        if action_id:
            champion_id = _elegir_campeon_ban(data)
            if champion_id:
                logger.info(f"[Ban Auto] Baneando {champion_id}")
                await _ejecutar_ban_ws(connection, action_id, champion_id)
        pick_action_id = _detectar_turno_pick(data)
        if pick_action_id:
            champion_id = _elegir_campeon_pick(data)
            if champion_id:
                logger.info(f"[Pick Auto] Pickeando {champion_id}")
                await _ejecutar_pick_ws(connection, pick_action_id, champion_id)
    except Exception as e:
        logger.debug(f"[ChampSelect] Error: {e}")


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
            _procesar_bestbuild_context(data)
            action_id = _detectar_turno_ban(data)
            if action_id:
                champion_id = _elegir_campeon_ban(data)
                if champion_id:
                    _ejecutar_ban_polling(session, port, action_id, champion_id)
            pick_action_id = _detectar_turno_pick(data)
            if pick_action_id:
                champion_id = _elegir_campeon_pick(data)
                if champion_id:
                    _ejecutar_pick_polling(session, port, pick_action_id, champion_id)
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
            registrar_evento("agent_online", "AutoQueue conectado al cliente de LoL.")

            _t_fase = _t_lobby = _t_cs = 0.0

            while True:
                if not os.path.exists(lockfile_path):
                    logger.warning("Lockfile eliminado. Cliente cerrado.")
                    estado_partida["estado"] = "Cliente cerrado"
                    estado_partida.update({"encontrada": False, "intentos_partida_actual": 0})
                    registrar_evento("agent_offline", "Cliente de LoL cerrado.")
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
    print("=========================================")
    print("AutoQueue  ∞  dev by 8AM")
    print("=========================================")
    print()

    print("[1/3] Iniciando servidor web...")
    hilo_web = threading.Thread(target=iniciar_servidor_web, daemon=True, name="WebServer")
    hilo_web.start()

    print("[2/3] Esperando servidor web (máx 10s)...")
    for i in range(100):
        if web_info["url"]:
            print(f"✓ Servidor web listo: {web_info['url']}")
            mostrar_qr_web(web_info["url"])
            break
        time.sleep(0.1)
    else:
        print("✗ Servidor web no respondió")

    print()
    print("[3/3] Conectando con League Client...")
    logger.info("=========================================")
    logger.info("AutoQueue  ∞  dev by 8AM")
    logger.info("=========================================")

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
