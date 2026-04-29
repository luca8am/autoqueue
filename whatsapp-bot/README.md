# AutoQueue BestBuild WhatsApp Bot

Bot personal de WhatsApp usando Baileys. Responde al comando `build` consultando el AutoQueue local.

## Requisitos

- Node.js 20+
- AutoQueue corriendo en la misma PC
- WhatsApp en el celular para escanear el QR

## Instalacion

```bash
cd whatsapp-bot
npm install
npm start
```

La primera vez aparece un QR en consola. Escanealo desde WhatsApp > Dispositivos vinculados.

## Uso

Con AutoQueue abierto y, preferentemente, en champ select:

```text
build
```

El bot consulta:

```text
http://127.0.0.1:5000/api/bestbuild/recommendation
```

## Configuracion opcional

```bash
AUTOQUEUE_BASE_URL=http://127.0.0.1:5000 npm start
```

Para permitir solo chats especificos:

```bash
ALLOWED_JIDS=5491112345678@s.whatsapp.net npm start
```

Si `ALLOWED_JIDS` esta vacio, responde a cualquier chat que le escriba `build`.

## Notas

La sesion local de Baileys se guarda en `whatsapp-bot/auth/`. No la subas al repo.
El provider de builds meta todavia no esta conectado; por ahora el bot valida el flujo WhatsApp -> AutoQueue -> respuesta.
