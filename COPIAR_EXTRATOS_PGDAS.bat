@echo off
rem Copia os extratos do PGDAS-D do Google Drive (G:) para o ERP e agenda a copia a cada 30 minutos.
rem O ERP roda como SYSTEM e nao enxerga o G: do Google Drive; esta tarefa roda com o SEU usuario.
rem Rode este arquivo uma vez, no servidor, com o usuario que tem o Google Drive aberto.
setlocal
set "DESTINO=%~dp0dados\extratos_pgdas"
set "ORIGEM="
for /d %%D in ("G:\Meu Drive\Rotinas Autom*ticas Fiscal") do set "ORIGEM=%%~fD"
if not defined ORIGEM (
  echo Pasta "G:\Meu Drive\Rotinas Automaticas Fiscal" nao encontrada. O Google Drive esta aberto?
  if /i not "%1"=="/auto" pause
  exit /b 1
)
if not exist "%DESTINO%" mkdir "%DESTINO%"
if not exist "%~dp0logs" mkdir "%~dp0logs"
robocopy "%ORIGEM%" "%DESTINO%" "extrato_pgdas*.pdf" /S /XO /R:1 /W:2 /NP /NJH /NJS /NDL > "%~dp0logs\copia_extratos.log" 2>&1
if /i "%1"=="/auto" exit /b 0
schtasks /Create /TN "Emissor NFS-e - Copia extratos PGDAS" /TR "\"%~f0\" /auto" /SC MINUTE /MO 30 /RU "%USERDOMAIN%\%USERNAME%" /IT /F
echo.
echo Pronto: extratos copiados para "%DESTINO%".
echo A copia vai se repetir a cada 30 minutos enquanto este usuario estiver conectado.
pause
