"""Configuração do e-Contador e sincronização do cadastro de empresas."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..configuracoes import gravar_config
from ..db import get_db
from ..web import admin, auditar, flash, render, texto, usuario_atual
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


# ---- sessão de tela (baixas, envio e exclusão de documentos) -------------------------------------

@router.post("/configuracoes/econtador/web/testar")
async def econtador_web_testar(request: Request, db: Session = Depends(get_db), u=Depends(admin)):
    from fastapi.concurrency import run_in_threadpool
    from . import sessao
    from .web import ErroSessao, ErroWeb
    try:
        n = await run_in_threadpool(lambda: sessao.cliente(db).testar())
        msg, ok = f"sessão ativa, {n} empresa(s) no e-Contador", True
    except (ErroSessao, ErroWeb) as e:
        msg, ok = str(e), False
    sessao.registrar_teste(db, ok, msg)
    db.commit()
    flash(request, ("e-Contador: " + msg) if ok else msg, "ok" if ok else "erro")
    return _redir("/configuracoes#econtador-web")


@router.post("/configuracoes/econtador/web/esquecer")
async def econtador_web_esquecer(request: Request, db: Session = Depends(get_db), u=Depends(admin)):
    from . import sessao
    sessao.esquecer(db)
    auditar(db, request, u, "EC_SESSAO_ESQUECER", "configuracao", None, "")
    db.commit()
    flash(request, "Sessão do e-Contador apagada do ERP.")
    return _redir("/configuracoes#econtador-web")


# ---- conectar pelo próprio ERP -----------------------------------------------------------------

@router.get("/econtador/conectar")
def econtador_conectar_form(request: Request, volta: str = "", db: Session = Depends(get_db), u=Depends(usuario_atual)):
    from . import sessao
    etapa = "codigo" if sessao.login_pendente(u.id) else "senha"
    return render(request, "econtador_conectar.html", etapa=etapa, st=sessao.estado(db), volta=volta or request.headers.get("referer", ""),
                  opcoes=[])


async def _resultado(request, db, u, r, volta):
    from . import sessao
    if r.get("ok"):
        auditar(db, request, u, "EC_CONECTAR", "configuracao", None, f"{r['empresas']} empresa(s)")
        db.commit()
        flash(request, f"e-Contador conectado ({r['empresas']} empresas). A sessão fica guardada no ERP até o e-Contador encerrá-la.")
        return _redir(volta if volta.startswith("/") else "/configuracoes#econtador-web")
    if r.get("canais") is not None:
        return render(request, "econtador_conectar.html", etapa="canal", st=sessao.estado(db), volta=volta, opcoes=r["canais"])
    if r.get("codigo"):
        if r.get("aviso"):
            flash(request, r["aviso"], "erro")
        return render(request, "econtador_conectar.html", etapa="codigo", st=sessao.estado(db), volta=volta, opcoes=[])
    return render(request, "econtador_conectar.html", etapa="escolher", st=sessao.estado(db), volta=volta, opcoes=r.get("escolher", []))


@router.post("/econtador/conectar")
async def econtador_conectar(request: Request, db: Session = Depends(get_db), u=Depends(usuario_atual)):
    from . import sessao
    form = await request.form()
    volta = form.get("volta") or ""
    try:
        r = await run_in_threadpool(sessao.iniciar_login, db, u.id, form.get("usuario", ""), form.get("senha", ""))
    except sessao.ErroLogin as e:
        flash(request, str(e), "erro")
        return render(request, "econtador_conectar.html", etapa="senha", st=sessao.estado(db), volta=volta, opcoes=[])
    except Exception as e:  # noqa: BLE001 — teste da sessão recém-criada
        flash(request, f"Login feito, mas o ERP não conseguiu usar a sessão: {e}", "erro")
        return render(request, "econtador_conectar.html", etapa="senha", st=sessao.estado(db), volta=volta, opcoes=[])
    return await _resultado(request, db, u, r, volta)


@router.post("/econtador/conectar/codigo")
async def econtador_conectar_codigo(request: Request, db: Session = Depends(get_db), u=Depends(usuario_atual)):
    from . import sessao
    form = await request.form()
    volta = form.get("volta") or ""
    try:
        r = await run_in_threadpool(sessao.enviar_codigo, db, u.id, form.get("codigo", ""))
    except sessao.ErroLogin as e:
        flash(request, str(e), "erro")
        etapa = "codigo" if sessao.login_pendente(u.id) else "senha"
        return render(request, "econtador_conectar.html", etapa=etapa, st=sessao.estado(db), volta=volta, opcoes=[])
    except Exception as e:  # noqa: BLE001
        flash(request, f"Login feito, mas o ERP não conseguiu usar a sessão: {e}", "erro")
        return render(request, "econtador_conectar.html", etapa="senha", st=sessao.estado(db), volta=volta, opcoes=[])
    return await _resultado(request, db, u, r, volta)


@router.post("/econtador/conectar/acesso")
async def econtador_conectar_acesso(request: Request, db: Session = Depends(get_db), u=Depends(usuario_atual)):
    from . import sessao
    form = await request.form()
    volta = form.get("volta") or ""
    try:
        r = await run_in_threadpool(sessao.escolher_acesso, db, u.id, form.get("conexao", ""))
    except sessao.ErroLogin as e:
        flash(request, str(e), "erro")
        return render(request, "econtador_conectar.html", etapa="senha", st=sessao.estado(db), volta=volta, opcoes=[])
    return await _resultado(request, db, u, r, volta)


@router.post("/econtador/conectar/cancelar")
async def econtador_conectar_cancelar(request: Request, u=Depends(usuario_atual)):
    from . import sessao
    sessao.cancelar_login(u.id)
    return _redir("/econtador/conectar")


@router.post("/econtador/conectar/canal")
async def econtador_conectar_canal(request: Request, db: Session = Depends(get_db), u=Depends(usuario_atual)):
    from . import sessao
    form = await request.form()
    volta = form.get("volta") or ""
    try:
        if form.get("novo") == "1":
            opcoes = await run_in_threadpool(sessao.negociar_canal, u.id)
            return render(request, "econtador_conectar.html", etapa="canal", st=sessao.estado(db), volta=volta, opcoes=opcoes)
        await run_in_threadpool(sessao.enviar_canal, u.id, form.get("canal", ""))
    except sessao.ErroLogin as e:
        flash(request, str(e), "erro")
        etapa = "codigo" if sessao.login_pendente(u.id) else "senha"
        return render(request, "econtador_conectar.html", etapa=etapa, st=sessao.estado(db), volta=volta, opcoes=[])
    flash(request, "Código enviado. Confira e digite abaixo.")
    return render(request, "econtador_conectar.html", etapa="codigo", st=sessao.estado(db), volta=volta, opcoes=[])
