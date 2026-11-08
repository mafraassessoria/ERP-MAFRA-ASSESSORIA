# Atualizacao automatica do ERP MAFRA ASSESSORIA a partir do GitHub.
# Executado pela tarefa "Emissor NFS-e - Atualizacao" (a cada 5 minutos, conta SYSTEM).
# Se houver versao nova: backup do banco -> aplica -> instala componentes (se mudaram) -> reinicia ->
# confere se o sistema voltou. Se nao voltar, retorna a versao anterior e reinicia de novo.
$ErrorActionPreference = 'Stop'
$pasta = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $pasta
$logs = Join-Path $pasta 'logs'
if (-not (Test-Path $logs)) { New-Item -ItemType Directory $logs | Out-Null }
$log = Join-Path $logs 'atualizacao.log'
$estado = Join-Path $logs 'atualizacao.json'
$python = Join-Path $pasta '.venv\Scripts\python.exe'
$tarefaServidor = 'Emissor NFS-e - Servidor'
$porta = 8800
$trava = Join-Path $logs 'atualizacao.lock'

function Log($m) { Add-Content -Path $log -Value ("[{0:dd/MM/yyyy HH:mm:ss}] {1}" -f (Get-Date), $m) -Encoding UTF8 }
function Git {
    $ErrorActionPreference = 'Continue'   # o git escreve avisos no stderr; o que vale e o codigo de saida
    $saida = & $script:git -c "safe.directory=*" @args 2>&1 | ForEach-Object { "$_" }
    if ($LASTEXITCODE -ne 0) { throw "git $($args -join ' ') falhou: $($saida -join ' ')" }
    return $saida
}
function Estado($resultado, $msg, $versao) {
    @{ resultado = $resultado; mensagem = $msg; versao = $versao; data = (Get-Date -Format 's') } |
        ConvertTo-Json | Set-Content -Path $estado -Encoding UTF8
}
function Parar {
    Stop-ScheduledTask -TaskName $tarefaServidor -ErrorAction SilentlyContinue
    Get-NetTCPConnection -LocalPort $porta -State Listen -ErrorAction SilentlyContinue |
        ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 2
}
function Subir {
    Start-ScheduledTask -TaskName $tarefaServidor
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Seconds 1
        try {
            $r = Invoke-WebRequest -Uri "http://127.0.0.1:$porta/login" -UseBasicParsing -TimeoutSec 3
            if ($r.StatusCode -eq 200) { return $true }
        } catch { }
    }
    return $false
}

$script:git = (Get-Command git.exe -ErrorAction SilentlyContinue).Source
if (-not $script:git) { $script:git = 'C:\Program Files\Git\cmd\git.exe' }
if (-not (Test-Path $script:git)) { Log 'Git nao encontrado; instale o Git for Windows.'; exit 1 }

# Uma execucao por vez
if (Test-Path $trava) {
    if ((Get-Item $trava).LastWriteTime -gt (Get-Date).AddMinutes(-30)) { exit 0 }
    Remove-Item $trava -Force
}
New-Item -ItemType File $trava -Force | Out-Null
try {
    # Janela de horario (scripts\atualizacao.json: {"janela": "sempre"} ou {"janela": "18:00-08:00"})
    $cfgArq = Join-Path $pasta 'scripts\atualizacao.json'
    $janela = 'sempre'
    if (Test-Path $cfgArq) { $janela = (Get-Content $cfgArq -Raw | ConvertFrom-Json).janela }
    if ($janela -and $janela -ne 'sempre') {
        $ini, $fim = $janela.Split('-') | ForEach-Object { [TimeSpan]::Parse($_) }
        $agora = (Get-Date).TimeOfDay
        $dentro = if ($ini -le $fim) { $agora -ge $ini -and $agora -lt $fim } else { $agora -ge $ini -or $agora -lt $fim }
        if (-not $dentro) { exit 0 }
    }

    Git fetch --quiet origin main | Out-Null
    $atual = (Git rev-parse HEAD).Trim()
    $nova = (Git rev-parse origin/main).Trim()
    if ($atual -eq $nova) { exit 0 }
    $bloq = Join-Path $logs 'atualizacao_bloqueada.txt'
    if ((Test-Path $bloq) -and ((Get-Content $bloq -Raw).Trim() -eq $nova)) { exit 0 }

    $resumo = (Git log -1 --format=%s $nova) -join ' '
    Log "Versao nova encontrada: $($nova.Substring(0,7)) - $resumo"
    $mudou = Git diff --name-only $atual $nova
    $componentes = $mudou -contains 'requirements.txt'

    # 1) backup do banco (o backup.bat ja guarda os ultimos backups na pasta backups)
    & cmd.exe /c "`"$pasta\backup.bat`"" | Out-Null
    Log 'Backup do banco feito.'

    # 2) aplica a versao
    Parar
    Git reset --hard --quiet $nova | Out-Null
    Log 'Arquivos atualizados.'
    if ($componentes) {
        & $python -m pip install --disable-pip-version-check -q -r (Join-Path $pasta 'requirements.txt') 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'Falha ao instalar componentes novos (internet do servidor?).' }
        Log 'Componentes instalados.'
    }

    # 3) sobe e confere
    if (Subir) {
        Log "OK: sistema no ar na versao $($nova.Substring(0,7))."
        Estado 'ok' "Atualizado: $resumo" $nova.Substring(0,7)
    } else {
        Log 'O sistema NAO respondeu; voltando para a versao anterior.'
        Parar
        Git reset --hard --quiet $atual | Out-Null
        if ($componentes) { & $python -m pip install --disable-pip-version-check -q -r (Join-Path $pasta 'requirements.txt') 2>&1 | Out-Null }
        $voltou = Subir
        Log ("Versao anterior {0} restaurada; sistema {1}." -f $atual.Substring(0,7), $(if ($voltou) { 'no ar' } else { 'AINDA FORA DO AR - verificar' }))
        Estado 'revertida' "A versao $($nova.Substring(0,7)) nao subiu e foi desfeita. Veja logs\servidor.log." $atual.Substring(0,7)
        # Evita tentar a mesma versao ruim a cada 5 minutos
        Set-Content -Path (Join-Path $logs 'atualizacao_bloqueada.txt') -Value $nova
    }
}
catch {
    Log "ERRO: $($_.Exception.Message)"
    Estado 'erro' $_.Exception.Message ''
    try { if (-not (Get-NetTCPConnection -LocalPort $porta -State Listen -ErrorAction SilentlyContinue)) { Subir | Out-Null } } catch { }
}
finally {
    Remove-Item $trava -Force -ErrorAction SilentlyContinue
}
