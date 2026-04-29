@echo off
setlocal enabledelayedexpansion
title AutoQueue - Generador de .exe

echo =========================================
echo   AutoQueue LoL - Generador de .exe
echo            dev by 8AM
echo =========================================
echo.

:: Detectar Python
where py >nul 2>nul
if %errorlevel% equ 0 (
    set PY=py
) else (
    where python >nul 2>nul
    if %errorlevel% equ 0 (
        set PY=python
    ) else (
        echo [ERROR] No se encontro Python instalado.
        echo Instala Python desde https://www.python.org/
        pause
        exit /b 1
    )
)

echo [1/5] Python detectado: !PY!
echo.

echo [1.5/5] Preparando entorno de compilacion...
taskkill /F /IM "AutoQueue-LoL.exe" 2>nul
if exist "dist" rmdir /S /Q "dist" 2>nul
if exist "build" rmdir /S /Q "build" 2>nul
echo      ✓ Procesos previos cerrados, directorios limpios
echo.

echo [2/5] Instalando dependencias...
!PY! -m pip install -r requirements.txt --quiet
!PY! -m pip install pyinstaller Pillow --quiet
echo      Listo.
echo.

echo [3/5] Generando icono (icon.ico)...
!PY! create_icon.py
echo.

:: Usar --icon solo si el .ico se generó correctamente
set ICON_FLAG=
if exist icon.ico (
    set ICON_FLAG=--icon icon.ico
    echo      Icono encontrado: icon.ico
) else (
    echo      Icono no disponible, se compilara sin icono personalizado.
)
echo.

echo [4/5] Compilando AutoQueue-LoL.exe...
echo      (esto puede tardar 1-2 minutos la primera vez)
echo.

!PY! -m PyInstaller ^
    --onefile ^
    --console ^
    --name "AutoQueue-LoL" ^
    %ICON_FLAG% ^
    --hidden-import=psutil ^
    --hidden-import=lcu_driver ^
    --hidden-import=requests ^
    --hidden-import=urllib3 ^
    --hidden-import=qrcode ^
    --hidden-import=qrcode.image.base ^
    --hidden-import=qrcode.image.svg ^
    --clean ^
    main.py

set BUILD_RESULT=%errorlevel%

echo.
echo [5/5] Resultado:
echo.

if %BUILD_RESULT% equ 0 (
    if exist dist\AutoQueue-LoL.exe (
        echo  [OK] dist\AutoQueue-LoL.exe generado con exito!
        echo.
        echo  Podes copiar ese .exe a cualquier carpeta y ejecutarlo
        echo  directamente. No necesita Python ni dependencias instaladas.
        echo.
        echo  NOTA: si Windows Defender lo bloquea al ejecutar, agrega
        echo  una excepcion para la carpeta dist\. Es un falso positivo
        echo  muy comun con ejecutables generados por PyInstaller.
    ) else (
        echo  [ERROR] No se pudo generar el .exe.
        echo  Revisa los errores que aparecieron arriba.
    )
) else (
    echo  [ERROR] PyInstaller fallo con codigo %BUILD_RESULT%.
    echo  Revisa los errores que aparecieron arriba.
)

echo.
pause
endlocal
