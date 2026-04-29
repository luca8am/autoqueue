import makeWASocket, {
  DisconnectReason,
  fetchLatestBaileysVersion,
  useMultiFileAuthState,
} from '@whiskeysockets/baileys';
import P from 'pino';
import qrcode from 'qrcode-terminal';

const AUTOQUEUE_BASE_URL = process.env.AUTOQUEUE_BASE_URL || 'http://127.0.0.1:5000';
const ALLOWED_JIDS = new Set(
  (process.env.ALLOWED_JIDS || '')
    .split(',')
    .map((jid) => jid.trim())
    .filter(Boolean),
);

function getTextMessage(message) {
  const msg = message.message || {};
  return (
    msg.conversation ||
    msg.extendedTextMessage?.text ||
    msg.imageMessage?.caption ||
    msg.videoMessage?.caption ||
    ''
  ).trim();
}

function isAllowed(jid) {
  return ALLOWED_JIDS.size === 0 || ALLOWED_JIDS.has(jid);
}

function isBuildCommand(text) {
  const normalized = text.toLowerCase();
  return normalized === 'build' || normalized === '!build' || normalized === 'bestbuild';
}

async function fetchJson(path) {
  const response = await fetch(`${AUTOQUEUE_BASE_URL}${path}`);
  if (!response.ok) {
    throw new Error(`AutoQueue respondio HTTP ${response.status}`);
  }
  return response.json();
}

function formatRecommendation(rec) {
  if (!rec.ok) {
    return rec.message;
  }

  const title = [
    rec.champion,
    rec.role ? `(${rec.role})` : '',
    rec.opponent ? `vs ${rec.opponent}` : '',
  ].filter(Boolean).join(' ');

  const lines = [`BestBuild: ${title}`];

  if (rec.queue) {
    lines.push(`Queue: ${rec.queue}`);
  }

  if (rec.items?.length) {
    lines.push(`Items: ${rec.items.join(' > ')}`);
  }

  if (rec.runes?.length) {
    lines.push(`Runas: ${rec.runes.join(' / ')}`);
  }

  if (rec.notes?.length) {
    lines.push('', ...rec.notes.map((note) => `- ${note}`));
  }

  return lines.join('\n');
}

async function handleBuildCommand(sock, jid) {
  try {
    const recommendation = await fetchJson('/api/bestbuild/recommendation');
    await sock.sendMessage(jid, { text: formatRecommendation(recommendation) });
  } catch (error) {
    await sock.sendMessage(jid, {
      text: `No pude hablar con AutoQueue en ${AUTOQUEUE_BASE_URL}. Asegurate de tener main.py corriendo. Error: ${error.message}`,
    });
  }
}

async function startBot() {
  const { state, saveCreds } = await useMultiFileAuthState('auth');
  const { version } = await fetchLatestBaileysVersion();

  const sock = makeWASocket({
    version,
    auth: state,
    logger: P({ level: process.env.LOG_LEVEL || 'silent' }),
    browser: ['AutoQueue BestBuild', 'Chrome', '1.0.0'],
  });

  sock.ev.on('creds.update', saveCreds);

  sock.ev.on('connection.update', ({ connection, lastDisconnect, qr }) => {
    if (qr) {
      console.log('Escanea este QR con WhatsApp:');
      qrcode.generate(qr, { small: true });
    }

    if (connection === 'open') {
      console.log('WhatsApp conectado. Envia "build" para consultar AutoQueue.');
    }

    if (connection === 'close') {
      const statusCode = lastDisconnect?.error?.output?.statusCode;
      const shouldReconnect = statusCode !== DisconnectReason.loggedOut;
      console.log(`WhatsApp desconectado (${statusCode || 'sin codigo'}).`);
      if (shouldReconnect) {
        startBot().catch((error) => console.error('Error al reconectar:', error));
      } else {
        console.log('Sesion cerrada. Borra whatsapp-bot/auth si queres vincular de nuevo.');
      }
    }
  });

  sock.ev.on('messages.upsert', async ({ messages, type }) => {
    if (type !== 'notify') return;

    for (const message of messages) {
      const jid = message.key.remoteJid;
      if (!jid || message.key.fromMe || !isAllowed(jid)) continue;

      const text = getTextMessage(message);
      if (!isBuildCommand(text)) continue;

      await handleBuildCommand(sock, jid);
    }
  });
}

startBot().catch((error) => {
  console.error('No se pudo iniciar el bot:', error);
  process.exitCode = 1;
});
