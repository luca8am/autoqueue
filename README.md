# 🚀 AutoQueue LoL - Edición Profesional

[![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Una herramienta ligera y robusta diseñada para auto-aceptar partidas de League of Legends utilizando la **LCU API (League Client Update)**. Olvídate de perder partidas por estar lejos de la PC o distraído.

---

## ✨ Características Principales

*   **⚡ Auto-Aceptación Instantánea**: Detecta y acepta partidas en milisegundos mediante la API oficial del cliente.
*   **📱 Monitor Web Remoto**: Escanea un código o entra a una URL desde tu celular para ver si ya entró en partida.
*   **🛡️ Triple Escudo Anti-Bucle**: Lógica inteligente de bloqueos y reintentos para evitar errores del cliente de Riot.
*   **🌐 Portabilidad Total**: Detección automática de IP local y búsqueda dinámica de puertos.
*   **📂 Logging Detallado**: Historial claro con emojis y marcas de tiempo para depuración.

---

## 🛠️ Instalación Rápida

### 1. Requisitos Previos
*   Tener instalado [Python 3.8+](https://www.python.org/downloads/).
*   Tener el cliente de League of Legends abierto.

### 2. Clonar y Ejecutar
1. Descarga este repositorio o clónalo:
   ```bash
   git clone https://github.com/tu-usuario/autoqueue-lol.git
   ```
2. Entra a la carpeta y ejecuta el archivo:
   - **En Windows**: Doble clic en `start_autoqueue.bat`.
   - **Manual**: Ejecuta `py main.py` (instalar dependencias con `pip install -r requirements.txt` primero).

---

## 📱 Cómo usar el Monitor Web

Al iniciar el script, verás un mensaje como este en la consola:
`🛰️ Intentando hostear monitor web en 192.168.1.15:5000...`

Solo tienes que entrar a esa dirección desde cualquier dispositivo conectado a tu misma red Wi-Fi (celular, tablet, otra PC) para ver el estado de tu cola en tiempo real.

---

## ⚠️ Notas Importantes

*   **Seguridad**: Esta herramienta utiliza la API LCU oficial que el propio cliente de LoL usa internamente. **No** modifica archivos del juego ni lee la memoria de procesos externos, por lo que su uso es generalmente seguro.
*   **Compatibilidad**: Diseñado específicamente para Windows.
*   **Fallback**: Si el servidor web no puede iniciarse por falta de permisos, el bot de auto-aceptar seguirá funcionando normalmente por consola.

---

## 📝 Licencia

Distribuido bajo la Licencia MIT. Consulta `LICENSE` para más información.

---

> [!TIP]
> Si el script no detecta tu LoL, asegúrate de estar ejecutando ambos (LoL y el script) con los mismos permisos (preferiblemente como Administrador si el LoL está en una ruta protegida).
