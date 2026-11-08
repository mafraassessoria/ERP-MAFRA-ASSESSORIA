# Instala a atualizacao automatica pelo GitHub (executar UMA vez, como Administrador).
# - cria uma chave SSH de leitura (deploy key) so para este servidor
# - liga a pasta do sistema ao repositorio, sem mexer em dados, backups, logs e .venv
# - cria a tarefa "Emissor NFS-e - Atualizacao" (a cada 5 minutos, conta SYSTEM)
param([string]$Repositorio = 'mafraassessoria/ERP-MAFRA-ASSESSORIA')
$ErrorActionPreference = 'Stop'
$pasta = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $pasta
function Pausar { Write-Host ''; Read-Host 'Pressione ENTER para continuar' | Out-Null }
try {
    $git = (Get-Command git.exe -ErrorAction SilentlyContinue).Source
    if (-not $git) { $git = 'C:\Program Files\Git\cmd\git.exe' }
    if (-not (Test-Path $git)) { throw 'Git nao encontrado. Instale o "Git for Windows" (git-scm.com) e rode de novo.' }
    $sshKeygen = Join-Path (Split-Path (Split-Path $git)) 'usr\bin\ssh-keygen.exe'
    $ssh = Join-Path (Split-Path (Split-Path $git)) 'usr\bin\ssh.exe'

    # 1) chave de leitura
    $dirChave = Join-Path $pasta '.deploy'
    $chave = Join-Path $dirChave 'id_ed25519'
    if (-not (Test-Path $chave)) {
        New-Item -ItemType Directory $dirChave -Force | Out-Null
        # cmd.exe passa a senha vazia ("") corretamente em qualquer versao do PowerShell
        cmd.exe /c "`"$sshKeygen`" -q -t ed25519 -N `"`" -C servidor-mafra-erp -f `"$chave`""
        if (-not (Test-Path $chave)) { throw 'Nao foi possivel criar a chave SSH.' }
    }
    # So Administradores e SYSTEM leem a chave
    icacls $dirChave /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' | Out-Null
    $pub = Get-Content ($chave + '.pub') -Raw
    Write-Host ''
    Write-Host '=== PASSO NO GITHUB ===' -ForegroundColor Cyan
    Write-Host "Abra https://github.com/$Repositorio/settings/keys/new"
    Write-Host 'Title: servidor M&R   |   Key: cole o texto abaixo   |   NAO marque "Allow write access"   |   Add key'
    Write-Host ''
    Write-Host $pub -ForegroundColor Yellow
    Set-Clipboard -Value $pub.Trim() -ErrorAction SilentlyContinue
    Write-Host '(o texto ja foi copiado para a area de transferencia)'
    Pausar

    # 2) liga a pasta ao repositorio
    # O git (bash do Git for Windows) acha o "ssh" sozinho; os caminhos vao sem aspas e com "/".
    # A pasta do sistema nao pode ter espacos no caminho (C:\emissor_nfse).
    $c = $chave -replace '\\', '/'
    $k = (Join-Path $dirChave 'known_hosts') -replace '\\', '/'
    if ($c -match ' ') { throw 'O caminho da pasta do sistema tem espacos; mova para C:\emissor_nfse.' }
    $sshCmd = "ssh -i $c -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=$k"
    if (-not (Test-Path (Join-Path $pasta '.git'))) { & $git init -q -b main }
    & $git -c safe.directory=* config core.sshCommand $sshCmd
    $remotos = @(& $git -c safe.directory=* remote)
    if ($remotos -contains 'origin') { & $git -c safe.directory=* remote set-url origin "git@github.com:$Repositorio.git" }
    else { & $git -c safe.directory=* remote add origin "git@github.com:$Repositorio.git" }
    Write-Host 'Buscando a versao no GitHub...'
    & $git -c safe.directory=* fetch origin main
    if ($LASTEXITCODE -ne 0) { throw 'Nao foi possivel acessar o repositorio. Confira se a chave foi adicionada em Deploy keys.' }
    & $git -c safe.directory=* reset -q --hard origin/main
    if ($LASTEXITCODE -ne 0) { throw 'Falha ao aplicar a versao do repositorio.' }
    Write-Host 'Pasta ligada ao repositorio. Dados, backups, logs e .venv nao foram alterados.' -ForegroundColor Green

    # 3) tarefa a cada 5 minutos
    $tarefa = 'Emissor NFS-e - Atualizacao'
    $acao = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$pasta\scripts\atualizar_git.ps1`"" -WorkingDirectory $pasta
    $gat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
    $cfg = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -MultipleInstances IgnoreNew
    $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    Register-ScheduledTask -TaskName $tarefa -Action $acao -Trigger $gat -Settings $cfg -Principal $principal `
        -Description 'Aplica as versoes novas do ERP MAFRA ASSESSORIA publicadas no GitHub' -Force | Out-Null
    Write-Host "Tarefa criada: $tarefa (a cada 5 minutos)." -ForegroundColor Green

    # Aplica a versao do repositorio agora (instala componentes e reinicia)
    Write-Host 'Instalando componentes e reiniciando o sistema...'
    & (Join-Path $pasta '.venv\Scripts\python.exe') -m pip install --disable-pip-version-check -q -r (Join-Path $pasta 'requirements.txt')
    Stop-ScheduledTask -TaskName 'Emissor NFS-e - Servidor' -ErrorAction SilentlyContinue
    Get-NetTCPConnection -LocalPort 8800 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
    Start-Sleep 2
    Start-ScheduledTask -TaskName 'Emissor NFS-e - Servidor'
    Write-Host ''
    Write-Host 'PRONTO. A partir de agora as versoes novas entram sozinhas (veja logs\atualizacao.log).' -ForegroundColor Green
}
catch {
    Write-Host ''
    Write-Host "ERRO: $($_.Exception.Message)" -ForegroundColor Red
}
Write-Host ''; Read-Host 'Pressione ENTER para fechar' | Out-Null
