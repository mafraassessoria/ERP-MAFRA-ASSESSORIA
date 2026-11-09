"""Tela de guias (vencidas e parcelamentos) e baixa."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..configuracoes import gravar_config, ler_config
from ..db import get_db
from ..serpro import servico as sv
from ..web import admin, auditar, flash, render, usuario_atual
from . import servico as gs

router = APIRouter()


def _redir(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _entregar_auto(db) -> bool:
    return ler_config(db, "guias_entregar") == "1"


@router.get("/guias")
def guias(request: Request, situacao: str = "ABERTA", empresa: int = 0, tipo: str = "",
          db: Session = Depends(get_db), u=Depends(usuario_atual)):
    stmt = select(models.GuiaRF).order_by(models.GuiaRF.gerada_em.desc())
    if situacao:
        stmt = stmt.where(models.GuiaRF.situacao == situacao)
    if empresa:
        stmt = stmt.where(models.GuiaRF.prestador_id == empresa)
    if tipo:
        stmt = stmt.where(models.GuiaRF.tipo == tipo)
    lista = db.scalars(stmt.limit(500)).all()
    empresas = db.scalars(select(models.Prestador).where(models.Prestador.ativo.is_(True), models.Prestador.usa_receita.is_(True))
                          .order_by(models.Prestador.razao_social)).all()
    com_parc = db.scalars(select(models.MonitorItem).where(models.MonitorItem.item == "PARCELAMENTO",
                                                           models.MonitorItem.status == "COM")).all()
    return render(request, "guias.html", lista=lista, empresas=empresas, situacao=situacao, empresa=empresa, tipo=tipo,
                  TIPOS=gs.TIPOS, SITUACOES=gs.SITUACOES, entregar_auto=_entregar_auto(db), n_parc=len(com_parc),
                  hoje=date.today(), modo=sv.modo(db))


@router.post("/guias/gerar")
async def guias_gerar(request: Request, db: Session = Depends(get_db), u=Depends(usuario_atual)):
    form = await request.form()
    p = db.get(models.Prestador, int(form.get("prestador_id") or 0))
    tipo, comp, cons = form.get("tipo") or "", (form.get("competencia") or "").strip(), (form.get("consolidacao") or "").strip()
    if not p or not p.usa_receita:
        flash(request, "Escolha uma empresa com o módulo Receita Federal ligado.", "erro")
        return _redir("/guias")
    try:
        g = await run_in_threadpool(gs.gerar, db, p, tipo, comp, u.login, cons)
        msg = f"{gs.TIPOS[tipo][0]} {comp} gerada: R$ {g.valor:.2f}".replace(".", ",")
        if _entregar_auto(db):
            try:
                gs.entregar(db, g)
                msg += " · entregue ao arquivador"
            except (ValueError, OSError) as e:
                g.erro = (g.erro + " " if g.erro else "") + f"Entrega: {e}"
        auditar(db, request, u, "GUIA_GERAR", "prestador", p.id, f"{tipo} {comp} {g.numero_documento} {g.valor}")
        db.commit()
        flash(request, msg + ".")
        if g.erro:
            flash(request, g.erro, "erro")
    except sv.ErroConsulta as e:
        db.rollback()
        flash(request, str(e), "erro")
    return _redir(f"/guias?empresa={p.id}&situacao=")


@router.post("/guias/parcelas-do-mes")
async def guias_parcelas(request: Request, db: Session = Depends(get_db), u=Depends(usuario_atual)):
    """Gera a parcela do mês para todas as empresas que o Monitor e-CAC indicou com parcelamento em andamento."""
    comp = f"{date.today():%Y-%m}"
    itens = db.scalars(select(models.MonitorItem).where(models.MonitorItem.item == "PARCELAMENTO",
                                                        models.MonitorItem.status == "COM")).all()
    feitas, erros = 0, []
    for m in itens:
        p = m.prestador
        if not p.ativo or not p.usa_receita:
            continue
        tipo = "PARC_MEI" if p.regime == "MEI" else "PARC_SN"
        ja = db.scalar(select(models.GuiaRF).where(models.GuiaRF.prestador_id == p.id, models.GuiaRF.tipo == tipo,
                                                   models.GuiaRF.competencia == comp, models.GuiaRF.situacao != "SUBSTITUIDA"))
        if ja:
            continue
        try:
            g = await run_in_threadpool(gs.gerar, db, p, tipo, comp, u.login)
            if _entregar_auto(db):
                try:
                    gs.entregar(db, g)
                except (ValueError, OSError) as e:
                    g.erro = f"Entrega: {e}"
            db.commit()
            feitas += 1
        except sv.ErroConsulta as e:
            db.rollback()
            erros.append(f"{p.razao_social}: {e}")
            if "Limite de gasto" in str(e):
                break
    auditar(db, request, u, "GUIA_PARCELAS_MES", "prestador", None, f"{comp}: {feitas} gerada(s); {erros[:10]}")
    db.commit()
    flash(request, f"Parcelas de {comp}: {feitas} guia(s) gerada(s).")
    for e in erros[:8]:
        flash(request, e, "erro")
    return _redir("/guias")


@router.post("/guias/conciliar")
async def guias_conciliar(request: Request, db: Session = Depends(get_db), u=Depends(usuario_atual)):
    r = gs.conciliar(db)
    auditar(db, request, u, "GUIA_CONCILIAR", "prestador", None, f"{r['pagas']} paga(s); conferir={r['conferir'][:10]}")
    db.commit()
    flash(request, f"{r['pagas']} guia(s) baixada(s) pelos pagamentos encontrados na Receita.")
    for x in r["conferir"][:8]:
        flash(request, "Conferir: " + x, "erro")
    return _redir("/guias")


@router.post("/guias/{gid}/{acao}")
async def guias_acao(gid: int, acao: str, request: Request, db: Session = Depends(get_db), u=Depends(usuario_atual)):
    g = db.get(models.GuiaRF, gid)
    if not g:
        return _redir("/guias")
    form = await request.form()
    try:
        if acao == "entregar":
            gs.entregar(db, g)
            flash(request, "Guia entregue ao Bot Arquivador.")
        elif acao == "paga":
            d = form.get("data") or date.today().isoformat()
            g.situacao, g.pago_em, g.criterio_baixa = "PAGA", date.fromisoformat(d), f"manual ({u.login})"
            flash(request, "Guia marcada como paga.")
        elif acao == "reabrir":
            g.situacao, g.pago_em, g.pagamento_id, g.criterio_baixa = "ABERTA", None, None, ""
            flash(request, "Guia reaberta.")
        else:
            return _redir("/guias")
        auditar(db, request, u, "GUIA_" + acao.upper(), "prestador", g.prestador_id, f"guia {g.id} {g.tipo} {g.competencia}")
        db.commit()
    except (ValueError, OSError) as e:
        db.rollback()
        flash(request, str(e), "erro")
    return _redir(form.get("volta") or "/guias")


@router.post("/configuracoes/guias")
async def guias_config(request: Request, db: Session = Depends(get_db), u=Depends(admin)):
    form = await request.form()
    gravar_config(db, "guias_entregar", "1" if form.get("guias_entregar") == "1" else "")
    db.commit()
    flash(request, "Preferência salva.")
    return _redir("/guias")
