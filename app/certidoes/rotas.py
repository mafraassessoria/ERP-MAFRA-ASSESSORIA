"""Telas do módulo Certidões."""
from __future__ import annotations

from collections import Counter

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy.orm import Session

from .. import models
from ..config import DATA_DIR
from ..configuracoes import ler_config
from ..db import get_db
from ..web import admin, auditar, flash, render, usuario_atual
from . import servico as cs
from . import tipos as T

router = APIRouter()


def _redir(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _ctx(db: Session) -> dict:
    from ..econtador.servico import tem_token
    return dict(execucao=cs.situacao_execucao(), tem_token=tem_token(db), publicar=cs.publicar_ligado(db),
                ultima=ler_config(db, "certidoes_ultima_execucao"), proxima=cs.proxima_execucao(db),
                ESTADOS=cs.ESTADOS, ROTULOS=T.ROTULOS)


@router.get("/certidoes")
def certidoes_geral(request: Request, estado: str = "", q: str = "", tipo: str = "", todas: int = 0,
                    db: Session = Depends(get_db), u=Depends(usuario_atual)):
    rows = cs.linhas(db)
    atuais = [r for r in rows if not r["substituida"] and r["c"].prestador.ativo]
    contagem = Counter(r["estado"] for r in atuais)
    lista = rows if todas else atuais
    if estado:
        lista = [r for r in lista if r["estado"] == estado]
    if tipo:
        lista = [r for r in lista if r["c"].tipo_nome == tipo]
    if q:
        t = q.strip().upper()
        lista = [r for r in lista if t in r["c"].prestador.razao_social.upper() or t == (r["c"].prestador.codigo_interno or "")
                 or t.replace(".", "").replace("/", "").replace("-", "") in r["c"].prestador.cnpj]
    tipos = sorted({r["c"].tipo_nome for r in rows})
    pendentes = len(cs.candidatas(db))
    return render(request, "certidoes_geral.html", lista=lista, contagem=contagem, total=len(atuais), estado=estado, q=q,
                  tipo=tipo, todas=todas, tipos=tipos, pendentes=pendentes, empresas=len(cs.empresas_alvo(db)), **_ctx(db))


@router.get("/prestadores/{pid}/certidoes")
def certidoes_empresa(pid: int, request: Request, db: Session = Depends(get_db), u=Depends(usuario_atual)):
    p = db.get(models.Prestador, pid)
    if not p:
        return _redir("/prestadores")
    if not p.usa_cnd:
        flash(request, "O módulo Certidões está desligado para esta empresa (Dados e configurações → Módulos).", "erro")
        return _redir(f"/prestadores/{pid}?aba=config")
    return render(request, "certidoes_empresa.html", p=p, aba="certidoes", lista=cs.linhas(db, pid), **_ctx(db))


@router.post("/certidoes/consultar")
async def certidoes_consultar(request: Request, db: Session = Depends(get_db), u=Depends(usuario_atual)):
    form = await request.form()
    pid = int(form.get("prestador_id") or 0) or None
    volta = f"/prestadores/{pid}/certidoes" if pid else "/certidoes"
    try:
        from ..econtador.servico import cliente
        cliente(db)  # confere a chave antes de abrir a execução
        cs.executar_em_segundo_plano(pid, u.login)
    except ValueError as e:
        flash(request, str(e), "erro")
        return _redir(volta)
    flash(request, "Consulta ao Pack CND iniciada. Esta página mostra o andamento; atualize em alguns instantes.")
    return _redir(volta)


@router.get("/certidoes/{cid}/arquivo")
def certidao_arquivo(cid: int, download: int = 0, db: Session = Depends(get_db), u=Depends(usuario_atual)):
    c = db.get(models.Certidao, cid)
    if not c or not c.arquivo:
        return _redir("/certidoes")
    arq = (DATA_DIR / c.arquivo).resolve()
    try:
        arq.relative_to(cs.PASTA.resolve())
    except ValueError:
        return _redir("/certidoes")
    if not arq.is_file():
        return _redir("/certidoes")
    nome = f"{c.prestador.codigo_interno or c.prestador.cnpj} - {T.ROTULOS.get(c.tipo, 'Certidao')} - " \
           f"val {c.vencimento:%d-%m-%Y}{arq.suffix}" if c.vencimento else arq.name
    return FileResponse(arq, filename=nome, content_disposition_type="attachment" if download else "inline")


@router.post("/certidoes/{cid}/entregar")
async def certidao_entregar(cid: int, request: Request, db: Session = Depends(get_db), u=Depends(usuario_atual)):
    c = db.get(models.Certidao, cid)
    if not c:
        return _redir("/certidoes")
    form = await request.form()
    volta = form.get("volta") or "/certidoes"
    if not volta.startswith("/"):
        volta = "/certidoes"
    try:
        msg = cs.publicar(db, c, cs.entrada(db))
        auditar(db, request, u, "CERTIDAO_ENTREGAR", "prestador", c.prestador_id, f"{c.pack_id} {c.tipo_nome}: {msg}")
        db.commit()
        flash(request, f"{c.tipo_nome}: {msg}.")
    except (ValueError, OSError) as e:
        db.rollback()
        flash(request, f"Não entregue: {e}", "erro")
    return _redir(volta)


@router.post("/certidoes/entregar-pendentes")
async def certidoes_entregar_pendentes(request: Request, db: Session = Depends(get_db), u=Depends(usuario_atual)):
    feitas, erros = cs.publicar_pendentes(db)
    auditar(db, request, u, "CERTIDOES_ENTREGAR", "prestador", None, f"{feitas} entregue(s); erros={erros[:20]}")
    db.commit()
    flash(request, f"{feitas} certidão(ões) processada(s): entregues ao Bot Arquivador ou já presentes no Drive (veja a coluna Arquivador).")
    for e in erros[:5]:
        flash(request, e, "erro")
    return _redir("/certidoes")


@router.post("/configuracoes/certidoes/marcar-arquivadas")
async def certidoes_marcar(request: Request, db: Session = Depends(get_db), u=Depends(admin)):
    n = cs.marcar_atuais_como_arquivadas(db)
    auditar(db, request, u, "CERTIDOES_MARCAR_ARQUIVADAS", "prestador", None, f"{n} certidão(ões)")
    db.commit()
    flash(request, f"{n} certidão(ões) atual(is) marcada(s) como já arquivada(s). Só as próximas novas serão entregues.")
    return _redir("/certidoes")


def contexto_config(db: Session) -> dict:
    from ..econtador.servico import tem_token
    dia, hora = cs.agenda(db)
    return {"ec": dict(tem_token=tem_token(db), entrada=cs.entrada(db), publicar=cs.publicar_ligado(db), dia=dia, hora=hora,
                       ultima=ler_config(db, "certidoes_ultima_execucao"), proxima=cs.proxima_execucao(db)),
            "DIAS_SEMANA": cs.DIAS_SEMANA}
