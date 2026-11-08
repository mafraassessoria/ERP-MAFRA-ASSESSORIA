"""Rotinas automáticas internas do servidor (sem tarefa do Windows): hoje, a coleta semanal de certidões.

Verifica a cada minuto. Se o horário marcado passou e a rotina ainda não rodou desde então (servidor
desligado, por exemplo), roda assim que puder, desde que no mesmo dia.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timedelta

log = logging.getLogger(__name__)
_iniciado = False


def _momento_anterior(dia: int, hora: str, agora: datetime) -> datetime:
    hh, mm = map(int, hora.split(":"))
    m = agora.replace(hour=hh, minute=mm, second=0, microsecond=0) - timedelta(days=(agora.weekday() - dia) % 7)
    return m if m <= agora else m - timedelta(days=7)


def verificar(agora: datetime | None = None) -> bool:
    """Dispara as rotinas do Monitor e-CAC e a coleta de certidões que estiverem na hora."""
    from .monitor.servico import executar_agendadas
    try:
        executar_agendadas(agora)
    except Exception:  # noqa: BLE001
        log.exception("monitor agendado")
    return verificar_certidoes(agora)


def verificar_certidoes(agora: datetime | None = None) -> bool:
    """Dispara a coleta de certidões se estiver na hora. Devolve True se disparou."""
    from .certidoes import servico as cs
    from .configuracoes import ler_config
    from .db import SessionLocal
    from .econtador.servico import tem_token
    agora = agora or datetime.now()
    with SessionLocal() as db:
        dia, hora = cs.agenda(db)
        if dia < 0 or not tem_token(db):
            return False
        alvo = _momento_anterior(dia, hora, agora)
        ultima = ler_config(db, "certidoes_ultima_execucao")
    try:
        ultima_dt = datetime.fromisoformat(ultima) if ultima else None
    except ValueError:
        ultima_dt = None
    if (ultima_dt and ultima_dt >= alvo) or agora.date() != alvo.date() or cs.situacao_execucao()["rodando"]:
        return False
    cs.executar_em_segundo_plano(None, "agendador")
    return True


def iniciar() -> None:
    global _iniciado
    if _iniciado or os.getenv("NFSE_AGENDADOR", "1") == "0":
        return
    _iniciado = True

    def laco():
        time.sleep(30)
        while True:
            try:
                verificar()
            except Exception:  # noqa: BLE001
                log.exception("agendador")
            time.sleep(60)
    threading.Thread(target=laco, daemon=True, name="agendador").start()
