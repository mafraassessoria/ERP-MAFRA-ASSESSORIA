"""Configuração do e-Contador e sincronização do cadastro de empresas."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..configuracoes import gravar_config
from ..db import get_db
from ..web import admin, auditar, flash, render, texto
from . import servico as es
from .cliente import ErroAPI

router = APIRouter()


def _redir(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


@router.post("/configuracoes/econtador")
async def econtador_config(request: Request, db: Session = Depends(get_db), u=Depends(admin)):
    from ..certidoes import servico as cs
    form = await request.form()
    es.gravar_token(db, texto(form, "econtador_token", 2000))
    gravar_config(db, "arquivador_entrada", texto(form, "arquivador_entrada", 300) or cs.ENTRADA_PADRAO)
    gravar_config(db, "certidoes_publicar", "1" if form.get("certidoes_publicar") == "1" else "")
    dia = form.get("certidoes_dia") or "4"
    hora = texto(form, "certidoes_hora", 5) or "17:00"
    try:
        dia_i = int(dia)
        assert -1 <= dia_i <= 6
        from datetime import datetime as _dt
        _dt.strptime(hora, "%H:%M")
    except (ValueError, AssertionError):
        flash(request, "Dia ou hora da consulta automática inválidos.", "erro")
        return _redir("/configuracoes#econtador")
    gravar_config(db, "certidoes_agenda", f"{dia_i} {hora}")
    auditar(db, request, u, "CONFIG_ECONTADOR", "configuracao", None,
            f"entrada={form.get('arquivador_entrada')} publicar={form.get('certidoes_publicar')} agenda={dia_i} {hora}")
    db.commit()
    flash(request, "Configuração do e-Contador e das certidões salva.")
    return _redir("/configuracoes#econtador")


@router.post("/configuracoes/econtador/testar")
async def econtador_testar(request: Request, db: Session = Depends(get_db), u=Depends(admin)):
    try:
        cli = es.cliente(db)
        emps = await run_in_threadpool(cli.empresas)
        flash(request, f"Conexão com a API do e-Contador OK: {len(emps)} empresa(s) ativa(s) lá.")
    except ErroAPI as e:
        flash(request, f"Falha no teste: {e}", "erro")
    return _redir("/configuracoes#econtador")


@router.post("/configuracoes/econtador/sincronizar")
async def econtador_sincronizar(request: Request, db: Session = Depends(get_db), u=Depends(admin)):
    try:
        res = await run_in_threadpool(es.sincronizar_empresas, db)
    except ErroAPI as e:
        db.rollback()
        flash(request, f"Não foi possível sincronizar: {e}", "erro")
        return _redir("/configuracoes#econtador")
    auditar(db, request, u, "SINCRONIZAR_ECONTADOR", "prestador", None,
            f"{res.resumo}; divergentes={res.divergentes[:30]}")
    db.commit()
    return render(request, "econtador_sincronizacao.html", res=res)
