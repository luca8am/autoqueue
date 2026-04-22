"""
Resumen: Script para auto-aceptar partidas de League of Legends utilizando la LCU API.
Se utiliza 'lcu-driver' para escuchar eventos del juego de forma asíncrona.
Se añade la base de un servidor web Flask en un hilo separado para una interfaz local, donde
podremos ver el estado en tiempo real.
"""

from lcu_driver import Connector
import threading
from flask import Flask, jsonify
import logging
import asyncio
import sys
import socket
import time

# Forzar codificación UTF-8 en Windows para que soporte los emojis en consola sin tirar error
if sys.stdout.encoding.lower() != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

# ==========================================
# Configuración del Logging y Debugs
# ==========================================
# Añadimos logs robustos para verificar todo el flujo y hacer debugs fácilmente
logging.basicConfig(
    level=logging.INFO, # Si quieres ver absolutamente todos los mensajes internos, cambia a logging.DEBUG
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%H:%M:%S',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# ==========================================
# Configuración del servidor web local
# ==========================================
app = Flask(__name__)

# Estado global para saber si encontró partida y exponerlo en la web
estado_partida = {
    "estado": "Buscando o Esperando a iniciar el LoL",
    "encontrada": False,
    "intentos_partida_actual": 0,
    "bloqueo_aceptacion": False,
    "ultimo_estado_lcu": None
}

@app.route('/')
def index():
    # Ruta básica expuesta en la red local (para consultar desde el móvil)
    return jsonify(estado_partida)

def get_local_ip():
    """Detecta la IP local de la PC para mostrarla en los logs."""
    try:
        # No se envía data real, solo se abre un socket para ver qué interfaz usa el SO
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        return local_ip
    except Exception:
        return "127.0.0.1"

def iniciar_servidor_web():
    """Inicia Flask intentando varios puertos si el 5000 está ocupado."""
    log_flask = logging.getLogger('werkzeug')
    log_flask.disabled = True
    
    puertos_a_probar = range(5000, 5011)
    ip_local = get_local_ip()
    
    for puerto in puertos_a_probar:
        try:
            logger.info(f"🛰️ Intentando hostear monitor web en {ip_local}:{puerto}...")
            app.run(host='0.0.0.0', port=puerto, debug=False, use_reloader=False)
            return # Si logra iniciar, salimos del bucle
        except OSError as e:
            if puerto == puertos_a_probar[-1]:
                 logger.error("❌ No se pudo encontrar ningun puerto libre para el monitor web.")
            else:
                 continue # Probar el siguiente puerto
        except Exception as e:
            logger.error(f"⚠️ Error inesperado en el servidor web: {e}")
            break

# ==========================================
# Lógica del cliente de LoL (LCU)
# ==========================================
connector = Connector()

@connector.ready
async def connect(connection):
    logger.info("✅ Conectado al cliente de League of Legends.")
    estado_partida["estado"] = "Conectado al LoL - Buscando partida o en el Lobby"

@connector.ws.register('/lol-matchmaking/v1/ready-check', event_types=('UPDATE',))
async def match_found(connection, event):
    # Analizamos la respuesta para saber en qué estado se encuentra el lobby
    estado = event.data.get('state')
    respuesta_jugador = event.data.get('playerResponse')
    
    # Log de cambio de estado para diagnostico
    if estado and estado != estado_partida["ultimo_estado_lcu"]:
        logger.info(f"🔎 [Matchmaking Update] Estado: '{estado}' | Respuesta: '{respuesta_jugador}'")
        estado_partida["ultimo_estado_lcu"] = estado

    if estado == 'InProgress':
        # ESCUDO 1: Si ya aceptamos segun LCU, no hacemos nada
        if respuesta_jugador == 'Accepted':
            return
            
        # ESCUDO 2: Si ya hay una peticion volando, bloqueamos esta
        if estado_partida["bloqueo_aceptacion"]:
            return
            
        # ESCUDO 3: Limite de reintentos por si el LCU se queda "tildado"
        if estado_partida["intentos_partida_actual"] >= 3:
            if estado_partida["estado"] != "Error - Limite de intentos alcanzado":
                logger.error("🛑 Limite de reintentos alcanzado (3). Deteniendo spam para esta partida.")
                estado_partida["estado"] = "Error - Limite de intentos alcanzado"
            return

        logger.info(f"🔔 ¡Partida encontrada! Intento {estado_partida['intentos_partida_actual'] + 1}/3...")
        estado_partida["estado"] = f"Aceptando partida (Intento {estado_partida['intentos_partida_actual'] + 1})..."
        estado_partida["encontrada"] = True
        estado_partida["bloqueo_aceptacion"] = True # Activamos el bloqueo
        
        try:
            # Hacemos la petición POST a la API del LoL
            response = await connection.request('post', '/lol-matchmaking/v1/ready-check/accept')
            
            if response.status in [200, 204]:
                logger.info("✅ Partida aceptada con éxito.")
                estado_partida["estado"] = "Partida aceptada - Esperando confirmacion"
            else:
                logger.warning(f"⚠️ Error {response.status} al aceptar. Reintentando en breve...")
                estado_partida["intentos_partida_actual"] += 1
                
        except Exception as e:
            logger.error(f"❌ Error de red/conexion al aceptar: {e}")
            estado_partida["intentos_partida_actual"] += 1
        finally:
            # Importante: Soltamos el bloqueo siempre, pero con un pequeño delay para dejar al LCU respirar
            await asyncio.sleep(0.5)
            estado_partida["bloqueo_aceptacion"] = False
            
    elif estado == 'EveryoneReady':
        if estado_partida["estado"] != "Todos aceptaron":
            logger.info("🎮 ¡Todos aceptaron! Entrando a seleccion de campeones.")
            estado_partida["estado"] = "Todos aceptaron"
            # No reseteamos intentos aqui todavia por si acaso, se hara en el else
            
    elif estado in [None, 'None', 'Error', 'Declined']:
        # Si el estado es nulo o declinado por alguien, reseteamos contadores para la proxima cola
        if estado_partida["intentos_partida_actual"] > 0 or estado_partida["encontrada"]:
            logger.info("🔄 Matchmaking finalizado o cancelado. Reseteando bot para la siguiente cola.")
            estado_partida["intentos_partida_actual"] = 0
            estado_partida["encontrada"] = False
            estado_partida["bloqueo_aceptacion"] = False

@connector.close
async def disconnect(connection):
    logger.info("❌ Se ha cerrado el cliente de LoL.")
    estado_partida["estado"] = "Cliente de LoL cerrado"
    estado_partida["encontrada"] = False
    estado_partida["intentos_partida_actual"] = 0

if __name__ == '__main__':
    logger.info("=========================================")
    logger.info("🚀 AutoQueue LoL - Edición Reforzada")
    logger.info("=========================================")
    
    # Lanzamos Flask
    hilo_web = threading.Thread(target=iniciar_servidor_web, daemon=True)
    hilo_web.start()
    
    # Damos un pequeño respiro para que el hilo de Flask imprima su puerto antes que el bot
    time.sleep(0.5)
    
    try:
        connector.start()
    except KeyboardInterrupt:
        logger.info("\nApagando AutoQueue...")
    except Exception as e:
        logger.error(f"Error critico: {e}")

