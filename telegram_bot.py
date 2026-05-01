"""
Bot personal de Telegram para consultar la build recomendada de AutoQueue.

Usa long polling contra Telegram Bot API y consulta el endpoint local:
http://127.0.0.1:5000/api/bestbuild/recommendation
"""

import os
import time
import logging
import json
import threading
import requests


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def safe_error(error):
    message = str(error)
    if TELEGRAM_BOT_TOKEN:
        message = message.replace(TELEGRAM_BOT_TOKEN, "***")
    return message


def load_local_config():
    path = os.getenv("TELEGRAM_CONFIG_PATH", "telegram_config.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning("No se pudo leer %s: %s", path, e)
        return {}


_local_config = load_local_config()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or _local_config.get("bot_token")
_configured_autoqueue_base_url = (
    os.getenv("AUTOQUEUE_BASE_URL")
    or _local_config.get("autoqueue_base_url")
)
AUTOQUEUE_BASE_URL = (_configured_autoqueue_base_url or "http://127.0.0.1:5000").rstrip("/")
AUTOQUEUE_BASE_URLS = list(dict.fromkeys(
    [AUTOQUEUE_BASE_URL] + [f"http://127.0.0.1:{port}" for port in range(5000, 5011)]
))
_allowed_chat_ids = os.getenv("TELEGRAM_ALLOWED_CHAT_IDS") or ",".join(
    str(chat_id) for chat_id in _local_config.get("allowed_chat_ids", [])
)
ALLOWED_CHAT_IDS = {chat_id.strip() for chat_id in _allowed_chat_ids.split(",") if chat_id.strip()}
SUBSCRIBED_CHAT_IDS = set(ALLOWED_CHAT_IDS)
SUBSCRIBED_LOCK = threading.Lock()


def telegram_api(method):
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("Falta TELEGRAM_BOT_TOKEN en el entorno.")
    return f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"


def is_allowed(chat_id):
    return not ALLOWED_CHAT_IDS or str(chat_id) in ALLOWED_CHAT_IDS


def autoqueue_request(method, path, **kwargs):
    global AUTOQUEUE_BASE_URL
    last_error = None
    bases = [AUTOQUEUE_BASE_URL] + [base for base in AUTOQUEUE_BASE_URLS if base != AUTOQUEUE_BASE_URL]
    for base_url in bases:
        response = None
        try:
            response = requests.request(method, f"{base_url}{path}", timeout=12, **kwargs)
            data = response.json()
            if response.status_code >= 500:
                response.raise_for_status()
            AUTOQUEUE_BASE_URL = base_url
            return data
        except ValueError as e:
            last_error = RuntimeError(f"{base_url} no parece ser AutoQueue.")
            if response is not None:
                try:
                    response.raise_for_status()
                except Exception as status_error:
                    last_error = status_error
        except Exception as e:
            last_error = e
    if last_error:
        raise last_error
    raise RuntimeError("AutoQueue no esta disponible.")


def get_recommendation(params=None):
    if params:
        return autoqueue_request("get", "/api/bestbuild/manual", params=params)
    return autoqueue_request("get", "/api/bestbuild/recommendation")


def agent_get(path, **params):
    return autoqueue_request("get", path, params=params)


def agent_post(path, payload):
    return autoqueue_request("post", path, json=payload)
    try:
        data = response.json()
    except ValueError:
        response.raise_for_status()
        return {}
    if response.status_code >= 500:
        response.raise_for_status()
    return data


def format_recommendation(rec):
    if not rec.get("ok"):
        return rec.get("message") or "Todavia no hay build disponible."

    title_parts = [
        rec.get("champion"),
        f"({rec['role']})" if rec.get("role") else "",
        f"vs {rec['opponent']}" if rec.get("opponent") else "",
    ]
    lines = ["BestBuild: " + " ".join(part for part in title_parts if part)]

    if rec.get("queue"):
        lines.append(f"Queue: {rec['queue']}")

    meta = [
        f"Patch {rec['patch']}" if rec.get("patch") else "",
        f"Tier {rec['tier']}" if rec.get("tier") else "",
        f"{rec['winrate']}% WR" if rec.get("winrate") else "",
        f"{rec['games']} games" if rec.get("games") else "",
    ]
    meta = [part for part in meta if part]
    if meta:
        lines.append(" | ".join(meta))

    if rec.get("spells"):
        lines.append(f"Spells: {' + '.join(rec['spells'])}")

    if rec.get("items"):
        lines.append("")
        lines.append("Items:")
        for group in rec["items"]:
            items = group.get("items") or []
            if items:
                lines.append(f"{group.get('type', 'Items')}: {' > '.join(items)}")

    if rec.get("runes"):
        lines.append("")
        lines.append("Runas: " + " / ".join(rec["runes"]))

    if rec.get("skill_max_order"):
        lines.append("Max: " + " > ".join(rec["skill_max_order"]))

    if rec.get("notes"):
        lines.append("")
        lines.extend(f"- {note}" for note in rec["notes"])

    return "\n".join(lines)


def send_message(chat_id, text):
    response = requests.post(
        telegram_api("sendMessage"),
        json={
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        },
        timeout=12,
    )
    response.raise_for_status()


def subscribe_chat(chat_id):
    with SUBSCRIBED_LOCK:
        SUBSCRIBED_CHAT_IDS.add(str(chat_id))


def broadcast(text):
    with SUBSCRIBED_LOCK:
        chat_ids = list(SUBSCRIBED_CHAT_IDS)
    for chat_id in chat_ids:
        try:
            send_message(chat_id, text)
        except Exception as e:
            logger.warning("No se pudo notificar a %s: %s", chat_id, safe_error(e))


def help_text():
    return "\n".join([
        "AutoQueue BestBuild",
        "",
        "/status - ver si el agente esta prendido",
        "/build - build del champ select actual",
        "/accept - aceptar ready check",
        "/reject - rechazar ready check",
        "/insta_on - activar insta-accept",
        "/insta_off - desactivar insta-accept",
        "/setban zed - configurar ban 1",
        "/setban2 yasuo - configurar ban 2",
        "/setpick ahri mid - configurar pick por rol",
        "/bans - ver bans configurados",
        "/picks - ver picks configurados",
    ])


def format_status(status):
    lines = [
        "Agente conectado.",
        f"Estado: {status.get('estado')}",
        f"Fase: {status.get('fase') or '-'}",
        f"Queue: {status.get('tipo_queue') or '-'}",
        f"Posiciones: {status.get('posiciones') or '-'}",
        f"Insta-accept: {'ON' if status.get('velocidad_maxima') else 'OFF'}",
    ]
    return "\n".join(lines)


def format_config_summary(summary, section):
    data = summary.get(section) or {}
    if not data:
        return "No hay configuracion cargada."
    lines = []
    for key, value in sorted(data.items()):
        lines.append(f"{key.upper()}: {value.get('name') or value.get('id')}")
    return "\n".join(lines)


def parse_pick_args(args):
    roles = {"top", "jgl", "jg", "jungle", "mid", "middle", "adc", "bot", "bottom", "sup", "supp", "support"}
    if len(args) < 2:
        return None, None
    if args[-1].lower() in roles:
        return " ".join(args[:-1]), args[-1]
    if args[0].lower() in roles:
        return " ".join(args[1:]), args[0]
    return None, None


def parse_build_args(args):
    roles = {"top", "jgl", "jg", "jungle", "mid", "middle", "adc", "bot", "bottom", "sup", "supp", "support"}
    if not args:
        return None

    role = None
    cleaned = []
    for arg in args:
        if arg in roles and role is None:
            role = arg
        else:
            cleaned.append(arg)

    separators = {"vs", "contra", "versus"}
    separator_index = next((i for i, arg in enumerate(cleaned) if arg in separators), None)
    if separator_index is None:
        champion = " ".join(cleaned).strip()
        opponent = None
    else:
        champion = " ".join(cleaned[:separator_index]).strip()
        opponent = " ".join(cleaned[separator_index + 1:]).strip() or None

    if not champion:
        return None
    if opponent and role is None:
        role = "mid"
    return {"champion": champion, "opponent": opponent, "role": role}


def handle_message(message):
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    text = (message.get("text") or "").strip().lower()

    if not chat_id or not is_allowed(chat_id):
        return
    subscribe_chat(chat_id)

    parts = text.split()
    command = parts[0].split("@", 1)[0] if parts else ""
    args = parts[1:]

    if command in ("/start", "start", "/help", "help"):
        send_message(chat_id, help_text())
        return

    if command in ("/status", "status"):
        try:
            send_message(chat_id, format_status(agent_get("/api/status")))
        except Exception:
            send_message(chat_id, f"Tu agente no esta conectado. Abri AutoQueue-Agent.exe en la PC donde jugas.")
        return

    if command in ("/insta_on", "insta_on"):
        result = agent_post("/api/agent/insta", {"enabled": True})
        send_message(chat_id, "Insta-accept activado." if result.get("ok") else "No pude activar insta-accept.")
        return

    if command in ("/insta_off", "insta_off"):
        result = agent_post("/api/agent/insta", {"enabled": False})
        send_message(chat_id, "Insta-accept desactivado." if result.get("ok") else "No pude desactivar insta-accept.")
        return

    if command in ("/accept", "accept", "/aceptar", "aceptar"):
        agent_post("/api/agent/action", {"action": "aceptar"})
        send_message(chat_id, "Orden enviada: aceptar.")
        return

    if command in ("/reject", "reject", "/rechazar", "rechazar"):
        agent_post("/api/agent/action", {"action": "rechazar"})
        send_message(chat_id, "Orden enviada: rechazar.")
        return

    if command in ("/setban", "setban", "/ban", "ban", "/setban1", "setban1"):
        if not args:
            send_message(chat_id, "Uso: /setban zed")
            return
        result = agent_post("/api/agent/ban", {"slot": 1, "champion": " ".join(args)})
        send_message(chat_id, f"Ban 1 configurado: {result.get('champion')}" if result.get("ok") else result.get("error", "Error"))
        return

    if command in ("/setban2", "setban2", "/ban2", "ban2"):
        if not args:
            send_message(chat_id, "Uso: /setban2 yasuo")
            return
        result = agent_post("/api/agent/ban", {"slot": 2, "champion": " ".join(args)})
        send_message(chat_id, f"Ban 2 configurado: {result.get('champion')}" if result.get("ok") else result.get("error", "Error"))
        return

    if command in ("/setpick", "setpick", "/pick", "pick"):
        champion, role = parse_pick_args(args)
        if not champion or not role:
            send_message(chat_id, "Uso: /setpick ahri mid")
            return
        result = agent_post("/api/agent/pick", {"role": role, "champion": champion})
        if result.get("ok"):
            send_message(chat_id, f"Pick {result.get('role')} configurado: {result.get('champion')}")
        else:
            send_message(chat_id, result.get("error", "No pude configurar el pick."))
        return

    if command in ("/bans", "bans"):
        send_message(chat_id, format_config_summary(agent_get("/api/agent/config-summary"), "bans"))
        return

    if command in ("/picks", "picks"):
        send_message(chat_id, format_config_summary(agent_get("/api/agent/config-summary"), "picks"))
        return

    if command not in ("/build", "build", "/bestbuild", "bestbuild"):
        return

    try:
        send_message(chat_id, format_recommendation(get_recommendation(parse_build_args(args))))
    except Exception as e:
        logger.warning("No se pudo responder build: %s", safe_error(e))
        send_message(
            chat_id,
            f"No pude consultar AutoQueue en {AUTOQUEUE_BASE_URL}. Asegurate de tener main.py corriendo.",
        )


def poll_agent_events():
    last_event_id = 0
    initialized = False
    while True:
        try:
            result = agent_get("/api/agent/events", since=last_event_id)
            for event in result.get("events", []):
                last_event_id = max(last_event_id, int(event.get("id", 0)))
                if initialized:
                    broadcast(event.get("message") or event.get("type") or "Evento de AutoQueue")
            initialized = True
        except Exception:
            time.sleep(3)
            continue
        time.sleep(1.2)


def poll_updates():
    offset = None
    logger.info("Bot de Telegram iniciado. Comandos: /build, build")
    threading.Thread(target=poll_agent_events, daemon=True).start()

    while True:
        try:
            response = requests.get(
                telegram_api("getUpdates"),
                params={"timeout": 25, "offset": offset},
                timeout=35,
            )
            response.raise_for_status()

            for update in response.json().get("result", []):
                offset = update["update_id"] + 1
                message = update.get("message") or update.get("edited_message")
                if message:
                    try:
                        handle_message(message)
                    except Exception as e:
                        logger.warning("Error manejando mensaje: %s", safe_error(e))
                        chat_id = (message.get("chat") or {}).get("id")
                        if chat_id and is_allowed(chat_id):
                            send_message(
                                chat_id,
                                f"No pude comunicarme con AutoQueue en {AUTOQUEUE_BASE_URL}. Revisa que el .exe este abierto.",
                            )
        except KeyboardInterrupt:
            logger.info("Apagando bot de Telegram...")
            return
        except Exception as e:
            logger.warning("Error en polling de Telegram: %s", safe_error(e))
            time.sleep(3)


if __name__ == "__main__":
    poll_updates()
