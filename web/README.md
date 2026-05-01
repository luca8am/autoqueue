# AutoQueue BestBuild Web

Sitio estatico para Vercel.

## Configuracion

Edita `site-config.js` antes de deployar:

```js
window.AUTOQUEUE_SITE_CONFIG = {
  botUrl: "https://t.me/bestbuildlol_bot",
  downloadUrl: "https://github.com/luca8am/autoqueue/releases/latest/download/AutoQueue-Agent.exe",
  releaseUrl: "https://github.com/luca8am/autoqueue/releases/latest"
};
```

## Deploy en Vercel

Configura `web` como Root Directory del proyecto en Vercel.

No requiere build step: Vercel sirve `index.html` como sitio estatico.
