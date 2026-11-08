# Gerenciador do Emissor NFS-e no Windows (executar como Administrador).
#   -Acao instalar   cria as tarefas: servidor ao ligar o Windows + backup diario + importacao ADN
#   -Acao reiniciar  reinicia o sistema
#   -Acao atualizar  para o sistema, instala componentes novos e sobe de novo (apos copiar versao nova)
#   -Acao remover    remove as tarefas e para o sistema
param(
    [ValidateSet('instalar', 'remover', 'reiniciar', 'atualizar')]
    [string]$Acao = 'instalar',
    [string]$HoraBackup = '23:00'
)

$ErrorActionPreference = 'Stop'
$pasta = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$python = Join-Path $pasta '.venv\Scripts\python.exe'
$tarefaServidor = 'Emissor NFS-e - Servidor'
$tarefaBackup = 'Emissor NFS-e - Backup diario'
$tarefaImportacao = 'Emissor NFS-e - Importacao ADN'
$porta = 8800

function Pausar { Write-Host ''; Read-Host 'Pressione ENTER para fechar' | Out-Null }

function Parar-Sistema {
    if (Get-ScheduledTask -TaskName $tarefaServidor -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $tarefaServidor -ErrorAction SilentlyContinue
    }
    # Encerra qualquer processo usando a porta (inclusive a janela aberta pelo iniciar.bat)
    $conexoes = Get-NetTCPConnection -LocalPort $porta -State Listen -ErrorAction SilentlyContinue
    foreach ($c in $conexoes) {
        Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 2
}

function Aguardar-Sistema {
    Write-Host 'Aguardando o sistema subir...'
    for ($i = 0; $i -lt 40; $i++) {
        Start-Sleep -Seconds 1
        if (Get-NetTCPConnection -LocalPort $porta -State Listen -ErrorAction SilentlyContinue) {
            Write-Host ''
            Write-Host "OK! Sistema no ar em http://localhost:$porta" -ForegroundColor Green
            return $true
        }
    }
    Write-Host ''
    Write-Host 'O sistema nao respondeu em 40 segundos.' -ForegroundColor Red
    Write-Host "Veja o arquivo de log: $pasta\logs\servidor.log" -ForegroundColor Red
    return $false
}

try {
    Write-Host "Emissor NFS-e - pasta: $pasta" -ForegroundColor Cyan
    Write-Host ''

    switch ($Acao) {
        'instalar' {
            if (-not (Test-Path $python)) {
                throw 'Ambiente Python nao encontrado. Execute primeiro o iniciar.bat uma vez (ele instala tudo) e depois tente de novo.'
            }
            New-Item -ItemType Directory -Force -Path (Join-Path $pasta 'logs') | Out-Null
            $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest

            # 1) Servidor: inicia junto com o Windows, sem janela, sem limite de tempo
            $acaoSrv = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument "/c `"$pasta\servidor.bat`"" -WorkingDirectory $pasta
            $gatSrv = New-ScheduledTaskTrigger -AtStartup
            $cfgSrv = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
                -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
                -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
            Register-ScheduledTask -TaskName $tarefaServidor -Action $acaoSrv -Trigger $gatSrv `
                -Principal $principal -Settings $cfgSrv -Description 'Mantem o Emissor NFS-e no ar' -Force | Out-Null
            Write-Host "[1/3] Tarefa criada: $tarefaServidor (inicia junto com o Windows)" -ForegroundColor Green

            # 2) Backup diario
            $acaoBkp = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument "/c `"$pasta\backup.bat`"" -WorkingDirectory $pasta
            $gatBkp = New-ScheduledTaskTrigger -Daily -At $HoraBackup
            $cfgBkp = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
                -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 2)
            Register-ScheduledTask -TaskName $tarefaBackup -Action $acaoBkp -Trigger $gatBkp `
                -Principal $principal -Settings $cfgBkp -Description 'Backup diario do Emissor NFS-e' -Force | Out-Null
            Write-Host "[2/3] Tarefa criada: $tarefaBackup (todo dia as $HoraBackup; se o servidor estiver desligado, roda ao ligar)" -ForegroundColor Green

            # 3) Importacao ADN: 2x por dia
            $acaoImp = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument "/c `"$pasta\importar.bat`"" -WorkingDirectory $pasta
            $gatImp = @((New-ScheduledTaskTrigger -Daily -At '07:00'), (New-ScheduledTaskTrigger -Daily -At '13:00'))
            $cfgImp = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
                -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 3) -MultipleInstances IgnoreNew
            Register-ScheduledTask -TaskName $tarefaImportacao -Action $acaoImp -Trigger $gatImp `
                -Principal $principal -Settings $cfgImp -Description 'Importa notas do ADN (NFS-e Nacional)' -Force | Out-Null
            Write-Host "[3/3] Tarefa criada: $tarefaImportacao (todo dia as 07:00 e 13:00)" -ForegroundColor Green
            Write-Host ''

            Parar-Sistema
            Start-ScheduledTask -TaskName $tarefaServidor
            Aguardar-Sistema | Out-Null

            Write-Host ''
            Write-Host 'Fazendo um primeiro backup de teste...'
            Start-ScheduledTask -TaskName $tarefaBackup
            Start-Sleep -Seconds 8
            $ultimo = Get-ChildItem (Join-Path $pasta 'backups') -Filter 'emissor_nfse_*.zip' -ErrorAction SilentlyContinue |
                Sort-Object LastWriteTime | Select-Object -Last 1
            if ($ultimo) {
                Write-Host "Backup de teste OK: $($ultimo.FullName)" -ForegroundColor Green
            } else {
                Write-Host "Backup de teste ainda nao apareceu. Confira em alguns minutos a pasta backups e o arquivo logs\backup.log" -ForegroundColor Yellow
            }
        }
        'reiniciar' {
            if (-not (Get-ScheduledTask -TaskName $tarefaServidor -ErrorAction SilentlyContinue)) {
                throw 'Inicio automatico nao instalado. Execute INSTALAR_INICIO_AUTOMATICO.bat.'
            }
            Parar-Sistema
            Start-ScheduledTask -TaskName $tarefaServidor
            Aguardar-Sistema | Out-Null
        }
        'atualizar' {
            if (-not (Test-Path $python)) {
                throw 'Ambiente Python nao encontrado. Execute primeiro o iniciar.bat uma vez.'
            }
            Write-Host 'Parando o sistema...'
            Parar-Sistema
            Write-Host 'Instalando/atualizando componentes (pode levar alguns minutos)...'
            & $python -m pip install --disable-pip-version-check -r (Join-Path $pasta 'requirements.txt')
            if ($LASTEXITCODE -ne 0) { throw 'Falha ao instalar componentes. Verifique a internet do servidor.' }
            Write-Host 'Componentes OK.' -ForegroundColor Green
            if (-not (Get-ScheduledTask -TaskName $tarefaImportacao -ErrorAction SilentlyContinue)) {
                Write-Host 'Nova tarefa disponivel (importacao ADN): execute tambem INSTALAR_INICIO_AUTOMATICO.bat' -ForegroundColor Yellow
            }
            if (Get-ScheduledTask -TaskName $tarefaServidor -ErrorAction SilentlyContinue) {
                Start-ScheduledTask -TaskName $tarefaServidor
                Aguardar-Sistema | Out-Null
            } else {
                Write-Host 'Inicio automatico nao instalado: execute INSTALAR_INICIO_AUTOMATICO.bat' -ForegroundColor Yellow
            }
        }
        'remover' {
            Parar-Sistema
            foreach ($t in @($tarefaServidor, $tarefaBackup, $tarefaImportacao)) {
                if (Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue) {
                    Unregister-ScheduledTask -TaskName $t -Confirm:$false
                    Write-Host "Tarefa removida: $t" -ForegroundColor Yellow
                }
            }
            Write-Host 'Sistema parado. Os dados e backups NAO foram apagados.'
        }
    }
}
catch {
    Write-Host ''
    Write-Host "ERRO: $($_.Exception.Message)" -ForegroundColor Red
}
Pausar
