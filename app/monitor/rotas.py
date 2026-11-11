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
    return render(request, "monitor_painel.html", pn=ms.painel(db), status_de=ms.status_de, rotinas=_rotinas_ctx(db), execucao=ms.situacao(),
                  STATUS=ms.STATUS, ROTULO=ms.ROTULO_ITEM, modo=sv.modo(db), DIAS=ms.DIAS)


@router.get("/monitor/item/{item}")
def monitor_item(item: str, request: Request, ref: str | None = None, status: str = "",
                 db: Session = Depends(get_db), u=Depends(usuario_atual)):
    if item not in ms.ROTULO_ITEM:
        return _redir("/monitor")
    ref = ms.referencia_padrao(item) if ref is None else ref.strip()
    if item == "SITFIS":
        return _tela_sitfis(request, db, u, status, request.query_params.get("tipo", ""), request.query_params.get("cert", ""))
    lista = ms.itens(db, item, ref)
    opcoes = ms.opcoes_status(item, {m.status for m in lista})
    ativos_parc = [m.prestador_id for m in lista if m.status == "COM"] if item == "PARCELAMENTO" else []
    if status:
        lista = [m for m in lista if m.status == status]
    refs = sorted({r for r in db.scalars(select(models.MonitorItem.referencia).where(models.MonitorItem.item == item))}, reverse=True)
    return render(request, "monitor_item.html", item=item, titulo_item=ms.ROTULO_ITEM[item], lista=lista, ref=ref, refs=refs[:24],
                  status=status, STATUS=ms.STATUS, detalhe=lambda m: json.loads(m.detalhe) if m.detalhe else {},
                  atrasos=ms.atrasos_sitfis(db) if item == "PARCELAMENTO" else {}, opcoes=opcoes,
                  n_ativos=len(ativos_parc), custo_ativos=_custo_parc_ativos(db, len(ativos_parc)),
                  e_admin=getattr(u, "perfil", "") == "ADMIN", execucao=ms.situacao(),
                  status_de=lambda st: ms.status_de(item, st))


def _custo_parc_ativos(db, n: int):
    """Pedidos + parcelas (uma consulta de cada) por empresa com parcelamento ativo."""
    from ..serpro.catalogo import POR_CODIGO
    if not n or sv.modo(db) != "producao":
        return 0
    return (sv.custo_previsto(db, POR_CODIGO["parcsn_pedidos"]) + sv.custo_previsto(db, POR_CODIGO["parcsn_parcelas"])) * n


@router.post("/monitor/parcelamentos/atualizar-ativos")
async def parcelamentos_atualizar_ativos(request: Request, db: Session = Depends(get_db), u=Depends(admin)):
    """Roda Parcelamentos só nas empresas com parcelamento ativo (traz as parcelas em aberto sem reconsultar todas)."""
    ids = [m.prestador_id for m in ms.itens(db, "PARCELAMENTO", "") if m.status == "COM"]
    if not ids:
        flash(request, "Nenhuma empresa com parcelamento ativo.")
        return _redir("/monitor/item/PARCELAMENTO")
    try:
        sv.cliente(db)
        ms.executar_em_segundo_plano("PARCELAMENTO", u.login, ids)
    except (sv.ErroConsulta, ValueError) as e:
        flash(request, str(e), "erro")
        return _redir("/monitor/item/PARCELAMENTO")
    flash(request, f"Parcelamentos: atualizando {len(ids)} empresa(s) com parcelamento ativo. Atualize a página em 1 ou 2 minutos.")
    return _redir("/monitor/item/PARCELAMENTO?status=COM")


# ---- Situação fiscal: tela própria (contadores, grupos, etiquetas, certidão) ----------------------

GRUPOS_SITFIS = [
    ("PENDENCIA", "Com pendência", "erro", ""),
    ("INDEFINIDO", "Conferir", "aviso", "A Receita não respondeu algumas bases (BASE INDISPONÍVEL); nas que responderam não há pendência. "
                                         "Consultar de novo em horário comercial."),
    ("ERRO", "Erro na consulta", "erro", "A consulta não trouxe o relatório (procuração, cadastro ou indisponibilidade)."),
    ("REGULAR", "Regular", "ok", "Sem pendências na Receita e na PGFN."),
]


def situacao_certidao(d: dict, hoje: date | None = None) -> tuple[str, str]:
    """(classe, texto) da certidão lida no relatório: vencida, vence em até 30 dias ou válida."""
    from datetime import date as _d, timedelta as _t
    hoje = hoje or _d.today()
    tipo, val = d.get("cert_tipo") or "", d.get("cert_validade") or ""
    if not tipo:
        return ("", "")
    try:
        v = _d.fromisoformat(val)
    except ValueError:
        return ("", tipo)
    if v < hoje:
        return ("erro", f"{tipo} venceu {v:%d/%m/%Y}")
    if v <= hoje + _t(days=30):
        return ("aviso", f"{tipo} vence {v:%d/%m/%Y}")
    return ("ok", f"{tipo} até {v:%d/%m/%Y}")


def _tela_sitfis(request, db, u, status: str, tipo: str, cert: str):
    todos = ms.itens(db, "SITFIS", "")
    linhas = []
    for m in todos:
        d = json.loads(m.detalhe) if m.detalhe else {}
        etiquetas = d.get("etiquetas") or [{"chave": x, "texto": x} for x in d.get("pendencias") or []]
        linhas.append(dict(m=m, d=d, etiquetas=etiquetas, cert=situacao_certidao(d)))
    cont = {g[0]: sum(1 for x in linhas if x["m"].status == g[0]) for g in GRUPOS_SITFIS}
    vencidas = sum(1 for x in linhas if x["cert"][0] == "erro")
    tipos = sorted({e["chave"] for x in linhas for e in x["etiquetas"]})
    filtradas = linhas
    if status:
        filtradas = [x for x in filtradas if x["m"].status == status]
    if tipo:
        filtradas = [x for x in filtradas if any(e["chave"] == tipo for e in x["etiquetas"])]
    if cert == "vencida":
        filtradas = [x for x in filtradas if x["cert"][0] == "erro"]
    grupos = [(g, [x for x in filtradas if x["m"].status == g[0]]) for g in GRUPOS_SITFIS]
    grupos = [(g, l) for g, l in grupos if l]
    conferir = [x["m"].prestador_id for x in linhas if x["m"].status == "INDEFINIDO"]
    return render(request, "monitor_sitfis.html", grupos_sitfis=grupos, cont=cont, vencidas=vencidas, total=len(linhas),
                  tipos=tipos, status=status, tipo=tipo, cert=cert, n_conferir=len(conferir),
                  custo_conferir=ms.custo_estimado(db, "SITFIS", len(conferir)), filtrado=bool(status or tipo or cert),
                  e_admin=getattr(u, "perfil", "") == "ADMIN", execucao=ms.situacao())


@router.post("/monitor/sitfis/repetir-conferir")
async def sitfis_repetir_conferir(request: Request, db: Session = Depends(get_db), u=Depends(admin)):
    ids = [m.prestador_id for m in ms.itens(db, "SITFIS", "") if m.status == "INDEFINIDO"]
    if not ids:
        flash(request, "Nenhuma empresa em 'Conferir'.")
        return _redir("/monitor/item/SITFIS")
    try:
        sv.cliente(db)
        ms.executar_em_segundo_plano("SITFIS", u.login, ids)
    except (sv.ErroConsulta, ValueError) as e:
        flash(request, str(e), "erro")
        return _redir("/monitor/item/SITFIS")
    flash(request, f"Situação fiscal: consultando de novo {len(ids)} empresa(s) em 'Conferir'. Atualize a página em alguns minutos.")
    return _redir("/monitor/item/SITFIS")


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
async def monitor_rodar(cod: str, request: Request, db: Session = Depends(get_db), u=Depends(admin)):
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
    return [dict(m=ult[k], nome=ms.ROTULO_ITEM[k], st=ms.status_de(k, ult[k].status)) for k in ms.ROTULO_ITEM if k in ult]
