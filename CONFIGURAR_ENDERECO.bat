@echo off
rem Configura o endereco http://erp.mafra para o ERP MAFRA ASSESSORIA neste servidor.
rem Execute UMA vez: clique com o botao direito e escolha "Executar como administrador".
rem O que faz:
rem   1) acrescenta "127.0.0.1  erp.mafra" no arquivo hosts do Windows (se ainda nao existir);
rem   2) se a porta 80 estiver livre, redireciona a porta 80 para a 8800 (endereco sem ":8800");
rem   3) cria o atalho "ERP MAFRA ASSESSORIA" na Area de Trabalho de TODOS os usuarios com o novo endereco.
rem Pode ser executado de novo sem problema (nao duplica nada).
setlocal
set "PASTA=%~dp0"
set "NOME=erp.mafra"

net session >nul 2>&1
if errorlevel 1 (
  echo.
  echo  Este arquivo precisa ser executado como ADMINISTRADOR.
  echo  Clique com o botao direito nele e escolha "Executar como administrador".
  echo.
  pause
  exit /b 1
)

echo.
echo [1/3] Arquivo hosts...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$h = Join-Path $env:SystemRoot 'System32\drivers\etc\hosts';" ^
  "$t = Get-Content -Path $h -ErrorAction SilentlyContinue;" ^
  "if ($t -match '^\s*127\.0\.0\.1\s+erp\.mafra(\s|$)') { Write-Host '      ja configurado.' }" ^
  "else { Add-Content -Path $h -Value ([Environment]::NewLine + '127.0.0.1    erp.mafra    # ERP MAFRA ASSESSORIA') -Encoding ASCII; Write-Host '      erp.mafra adicionado.' }"
ipconfig /flushdns >nul 2>&1

echo [2/3] Porta 80...
set "URL=http://%NOME%:8800/"
netsh interface portproxy show v4tov4 | findstr /C:"127.0.0.1       80" >nul 2>&1
if not errorlevel 1 (
  echo       redirecionamento 80 para 8800 ja existe.
  set "URL=http://%NOME%/"
  goto atalho
)
netstat -ano | findstr /R /C:"^ *TCP *[0-9.]*:80 .*LISTENING" /C:"^ *TCP *\[::\]:80 .*LISTENING" >nul 2>&1
if not errorlevel 1 (
  echo       a porta 80 ja esta em uso por outro programa: o endereco fica %URL%
  goto atalho
)
sc config iphlpsvc start= auto >nul 2>&1
net start iphlpsvc >nul 2>&1
netsh interface portproxy add v4tov4 listenaddress=127.0.0.1 listenport=80 connectaddress=127.0.0.1 connectport=8800 >nul
if errorlevel 1 (
  echo       nao foi possivel redirecionar a porta 80: o endereco fica %URL%
) else (
  echo       porta 80 redirecionada para 8800.
  set "URL=http://%NOME%/"
)

:atalho
echo [3/3] Atalho na Area de Trabalho de todos os usuarios...
if not exist "%PASTA%dados" mkdir "%PASTA%dados"
> "%PASTA%dados\endereco.txt" echo %URL%
for /f "usebackq delims=" %%D in (`powershell -NoProfile -Command "[Environment]::GetFolderPath('CommonDesktopDirectory')"`) do set "DESKTOP=%%D"
(
  echo [InternetShortcut]
  echo URL=%URL%
  echo IconFile=%PASTA%app\static\emissor.ico
  echo IconIndex=0
) > "%DESKTOP%\ERP MAFRA ASSESSORIA.url"
echo       atalho criado em %DESKTOP%

echo.
echo Testando o endereco...
powershell -NoProfile -Command "try { $r = Invoke-WebRequest -Uri '%URL%login' -UseBasicParsing -TimeoutSec 15; if ($r.StatusCode -eq 200) { Write-Host '  OK: o ERP abriu em %URL%' } } catch { Write-Host '  Atencao: o endereco nao respondeu agora. Confira se o ERP esta rodando e tente abrir no navegador.' }"
echo.
echo Pronto. Endereco do ERP: %URL%
echo Na primeira vez, digite o endereco com http:// na frente (ou use o atalho).
echo Cada usuario entra de novo uma vez (o login do endereco antigo nao passa para o novo).
echo Se algum usuario ja tinha um atalho pessoal antigo (localhost:8800), pode apaga-lo.
echo.
pause
