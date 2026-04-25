# 📚 GEMINI - Guía de Desarrollo AutoQueue

## Descripción General

AutoQueue es un bot para auto-aceptar partidas de League of Legends. Detecta cuando se encontró una partida y automáticamente la acepta, con opción de control manual vía web desde un celular.

## Arquitectura de Niveles

### Conexión LCU (League Client Update API)
- **Nivel 1**: `lcu-driver` con WebSocket (ideal, automático)
- **Nivel 2**: Lockfile en rutas conocidas + polling REST
- **Nivel 3**: Escaneo de todas las unidades + polling REST
- **Nivel 4**: Solicitud de permisos de Admin vía UAC

### Servidor Web
- **Nivel A**: 0.0.0.0 (accesible desde red local)
- **Nivel B**: 127.0.0.1 (solo localhost)
- **Nivel C**: Sin servidor (modo consola)

## Notas Importantes para Desarrollo

### ⚡ Performance
- El polling en modo REST ocurre cada 500ms
- Las actualizaciones en web suceden cada 1000ms desde el cliente
- Los endpoints de API cachean el `ultimo_acceso` para ajustar tiempos de decisión

### 🔐 Seguridad
- No modifica archivos del cliente LoL
- No lee memoria de procesos
- Usa solo la API oficial LCU (segura)
- El servidor web escucha en 0.0.0.0 solo si está en red local

### 🎯 Estados Principales
- **Lobby**: Usuario en sala, puede ver miembros y roles
- **Matchmaking**: En cola buscando partida
- **ReadyCheck**: Partida encontrada, esperando aceptación
- **ChampSelect**: En selección de campeones (auto-ban)
- **InProgress**: Partida en juego

### 🏗️ Estructura de Código
```
main.py
├── HTML_PAGE: Interfaz web (todo en uno)
├── Flask routes: /api/*
├── Connector (lcu-driver): Nivel 1
├── Polling threads: Niveles 2-3
└── Main: Detección y ejecución
```

### 💾 Persistencia
- `config.json`: Campeones/bans por rol (se carga al iniciar web)
- Se guarda automáticamente cuando se cambia desde la web

### 🚨 Problemas Comunes

**"No detecta LoL"**
- Asegurate que LoL esté abierto
- Ejecuta como Admin si LoL está en ruta protegida
- El lockfile se crea cuando LoL carga

**"Tarda mucho en cargar desde celular"**
- La carga inicial de campeones es lenta (>1MB HTML)
- Usa búsqueda para filtrar campeones (reduce DOM)
- El polling cada segundo puede saturar conexiones lentas

**"No auto-batea"**
- El ban debe estar configurado antes de entrar a champ select
- Algunos modos de juego (ARAM) no tienen ban

## Checklist Antes de Commitear

- [ ] Probé desde el cliente Windows (conectado)
- [ ] Probé desde celular en red local (si modifiqué web)
- [ ] No hay cambios en .gitignore aparte de necesario
- [ ] Ejecuté tests si existen cambios en lógica crítica
- [ ] Los logs son claros y útiles
- [ ] No hay hardcodes de rutas personales
