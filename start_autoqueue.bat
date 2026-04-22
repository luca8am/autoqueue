@echo off
setlocal enabledelayedexpansion
title LoL AutoQueue Bot - Edicion Profesional

echo ==============================================
echo    LoL AutoQueue - Verificando Entorno
echo ==============================================

:: Verificar si existe el comando 'py' o 'python'
where py >nul 2>nul
if %errorlevel% equ 0 (
    set PY_CMD=py
) else (
    where python >nul 2>nul
    if %errorlevel% equ 0 (
        set PY_CMD=python
    ) else (
        echo [ERROR] No se encontro Python instalado. 
        echo Por favor, instala Python desde https://www.python.org/
        pause
        exit /b
    )
)

echo [+] Usando comando: !PY_CMD!
echo [+] Instalando dependencias necesarias (esto puede tardar la primera vez)...
!PY_CMD! -m pip install -r requirements.txt --quiet

echo [+] Iniciando AutoQueue...
echo ----------------------------------------------
!PY_CMD! main.py
if %errorlevel% neq 0 (
    echo.
    echo [ERROR] El script se detuvo inesperadamente.
    pause
)
pause
