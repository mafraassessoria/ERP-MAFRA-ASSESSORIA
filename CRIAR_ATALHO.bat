@echo off
rem Cria o atalho "ERP MAFRA ASSESSORIA" na Area de Trabalho do usuario do Windows que executar este arquivo.
rem Use em cada conta do servidor que precisar acessar o emissor (o sistema ja roda para todas as contas).
setlocal
set "PASTA=%~dp0"
for /f "usebackq delims=" %%D in (`powershell -NoProfile -Command "[Environment]::GetFolderPath('Desktop')"`) do set "DESKTOP=%%D"
set "ATALHO=%DESKTOP%\ERP MAFRA ASSESSORIA.url"
(
  echo [InternetShortcut]
  echo URL=http://localhost:8800/
  echo IconFile=%PASTA%app\static\emissor.ico
  echo IconIndex=0
) > "%ATALHO%"
echo.
echo Atalho criado em: %ATALHO%
echo Abra o atalho e entre com o usuario e a senha do emissor.
echo.
pause
