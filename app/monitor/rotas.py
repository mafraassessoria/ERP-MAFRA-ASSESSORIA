"""Telas do Monitor e-CAC."""
from __future__ import annotations

import json
from datetime import date, datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..configuracoes import gravar_config, ler_config
from ..db import get_db
from ..serpro import servico as sv
from ..web import admin, auditar, flash, render, usuario_atual
from . import servico as ms
from .rotinas import POR_ROTINA, ROTINAS

router = APIRouter()


def _redir(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _rotinas_ctx(db: Session) -> list[dict]:
    agora = datetime.now()
    out = []
    for r in ROTINAS:
        ligada, regra = ms.agenda(db, r.codigo)
        n = len(ms.empresas(db, r.codigo))
        ult = ler_config(db, f"monitor_ultima_{r.codigo}")
        out.append(dict(r=r, ligada=ligada, regra=regra, quando=ms.descrever(regra), proxima=ms.proxima(regra, agora) if ligada else None,
                        ultima=ult[:16].replace("T", " ") if ult else "", empresas=n, custo=ms.custo_estimado(db, r.codigo, n)))
    return out


@router.get("/monitor")
def monitor_painel(request: Request, db: Session = Depends(get_db), u=Depends(usuario_atual)):
    return render(request, "monitor_painel.html", pn=ms.painel(db), rotinas=_rotinas_ctx(db), execucao=ms.situacao(),
                  STATUS=ms.STATUS, ROTULO=ms.ROTULO_ITEM, modo=sv.modo(db), DIAS=ms.DIAS)


@router.get("/monitor/item/{item}")
def monitor_item(item: str, request: Request, ref: str | None = None, status: str = "",
                 db: Session = Depends(get_db), u=Depends(usuario_atual)):
    if item not in ms.ROTULO_ITEM:
        return _redir("/monitor")
    ref = ms.referencia_padrao(item) if ref is None else ref.strip()
    lista = ms.itens(db, item, ref)
    if status:
        lista = [m for m in lista if m.status == status]
    refs = sorted({r for r in db.scalars(select(models.MonitorItem.referencia).where(models.MonitorItem.item == item))}, reverse=True)
    return render(request, "monitor_item.html", item=item, titulo_item=ms.ROTULO_ITEM[item], lista=lista, ref=ref, refs=refs[:24],
                  status=status, STATUS=ms.STATUS, detalhe=lambda m: json.loads(m.detalhe) if m.detalhe else {})


@router.get("/monitor/pagamentos")
def monitor_pagamentos(request: Request, mes: str = "", atraso: int = 0, q: str = "",
                       db: Session = Depends(get_db), u=Depends(usuario_atual)):
    mes = mes or f"{date.today():%Y-%m}"
    try:
        ini = date.fromisoformat(mes + "-01")
    except ValueError:
        ini = date.today().replace(day=1)
    fim = (ini.replace(day=28) + __import__("datetime").timedelta(days=4)).replace(day=1)
    stmt = select(models.PagamentoRF).where(models.PagamentoRF.data_arrecadacao >= ini, models.PagamentoRF.data_arrecadacao < fim)
    lista = db.scalars(stmt.order_by(models.PagamentoRF.data_arrecadacao.desc())).all()
    if atraso:
        lista = [x for x in lista if x.em_atraso]
    if q:
        t = q.strip().upper()
        lista = [x for x in lista if t in x.prestador.razao_social.upper() or t == (x.prestador.codigo_interno or "")]
    return render(request, "monitor_pagamentos.html", lista=lista, mes=mes, atraso=atraso, q=q,
                  total=sum(x.valor for x in lista), n_atraso=sum(1 for x in lista if x.em_atraso))


@router.post("/monitor/rotinas/{cod}/rodar")
async def monitor_rodar(cod: str, request: Request, db: Session = Depends(get_db), u=Depends(usuario_atual)):
    if cod not in POR_ROTINA:
        return _redir("/monitor")
    form = await request.form()
    pid = int(form.get("prestador_id") or 0)
    volta = f"/prestadores/{pid}/receita" if pid else "/monitor#rotinas"
    try:
        sv.cliente(db)
        ms.executar_em_segundo_plano(cod, u.login, [pid] if pid else None)
    except (sv.ErroConsulta, ValueError) as e:
        flash(request, str(e), "erro")
        return _redir(volta)
    flash(request, f"{POR_ROTINA[cod].nome}: rotina iniciada. Acompanhe o andamento no Monitor e-CAC.")
    return _redir(volta)


@router.post("/monitor/rotinas/{cod}/agenda")
async def monitor_agenda(cod: str, request: Request, db: Session = Depends(get_db), u=Depends(admin)):
    if cod not in POR_ROTINA:
        return _redir("/monitor")
    form = await request.form()
    tipo = form.get("tipo") or "M"
    valor = (form.get("valor") or "").strip()
    hora = (form.get("hora") or "07:30").strip()
    try:
        regra = ms.validar_regra(f"{tipo}{valor} {hora}")
    except ValueError:
        flash(request, "Agenda inválida: confira dia e horário.", "erro")
        return _redir("/monitor#rotinas")
    gravar_config(db, f"monitor_agenda_{cod}", regra)
    gravar_config(db, f"monitor_ativo_{cod}", "1" if form.get("ligada") == "1" else "")
    auditar(db, request, u, "MONITOR_AGENDA", "configuracao", None, f"{cod}: {regra} ligada={form.get('ligada') == '1'}")
    db.commit()
    flash(request, f"{POR_ROTINA[cod].nome}: {'ligada, ' + ms.descrever(regra) if form.get('ligada') == '1' else 'agenda desligada'}.")
    return _redir("/monitor#rotinas")


def resumo_empresa(db: Session, pid: int) -> list[dict]:
    """Último resultado de cada acompanhamento da empresa (para a aba Receita Federal)."""
    itens = db.scalars(select(models.MonitorItem).where(models.MonitorItem.prestador_id == pid)).all()
    ult = {}
    for m in itens:
        if m.item not in ult or (m.referencia, m.atualizado_em) > (ult[m.item].referencia, ult[m.item].atualizado_em):
            ult[m.item] = m
    return [dict(m=ult[k], nome=ms.ROTULO_ITEM[k], st=ms.STATUS.get(ult[k].status, (ult[k].status, ""))) for k in ms.ROTULO_ITEM if k in ult]
