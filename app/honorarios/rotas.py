"""Tela de honorários: importar recebidos, selecionar e gerar rascunhos de NFS-e do escritório."""
from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..configuracoes import gravar_config, ler_config
from ..db import get_db
from ..web import admin, auditar, flash, render, texto, usuario_atual
from . import servico as hs

router = APIRouter()


def _redir(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


@router.get("/honorarios")
def honorarios(request: Request, mes: str = "", situacao: str = "", q: str = "",
               db: Session = Depends(get_db), u=Depends(usuario_atual)):
    if not mes:
        ult = db.scalar(select(models.Recebimento.pagamento).order_by(models.Recebimento.pagamento.desc()))
        mes = f"{ult:%Y-%m}" if ult else f"{date.today():%Y-%m}"
    try:
        ini = date.fromisoformat(mes + "-01")
    except ValueError:
        ini = date.today().replace(day=1)
    fim = (ini.replace(day=28) + timedelta(days=4)).replace(day=1)
    lista = db.scalars(select(models.Recebimento).where(models.Recebimento.pagamento >= ini, models.Recebimento.pagamento < fim)
                       .order_by(models.Recebimento.codigo_empresa)).all()
    lista = sorted(lista, key=lambda r: (int(r.codigo_empresa) if r.codigo_empresa.isdigit() else 99999, r.titulo))
    if situacao == "sem_nota":
        lista = [r for r in lista if not r.nota_id]
    elif situacao == "com_nota":
        lista = [r for r in lista if r.nota_id]
    if q:
        t = q.strip().upper()
        lista = [r for r in lista if t in r.nome.upper() or t == r.codigo_empresa]
    esc = hs.escritorio(db)
    codigos = db.scalars(select(models.CodigoEmpresa).where(models.CodigoEmpresa.prestador_id == esc.id)).all() if esc else []
    emissoras = db.scalars(select(models.Prestador).where(models.Prestador.ativo.is_(True), models.Prestador.usa_nfse.is_(True),
                                                          models.Prestador.modo == "EMISSAO").order_by(models.Prestador.razao_social)).all()
    return render(request, "honorarios.html", lista=lista, mes=mes, situacao=situacao, q=q, esc=esc, emissoras=emissoras,
                  codigos=codigos, cfg={k: ler_config(db, k) for k in ("honorarios_ctribnac", "honorarios_nbs", "honorarios_descricao")},
                  descricao_padrao=hs.DESCRICAO_PADRAO, exemplo=hs.descricao(ini, ler_config(db, "honorarios_descricao")),
                  total=sum(r.recebido for r in lista), sem_nota=sum(1 for r in lista if not r.nota_id))


@router.post("/honorarios/importar")
async def honorarios_importar(request: Request, db: Session = Depends(get_db), u=Depends(usuario_atual)):
    form = await request.form()
    arq = form.get("arquivo")
    if not getattr(arq, "filename", ""):
        flash(request, "Selecione o relatório XLS de títulos baixados.", "erro")
        return _redir("/honorarios")
    try:
        conteudo = await arq.read(8 * 1024 * 1024 + 1)
        if len(conteudo) > 8 * 1024 * 1024:
            raise ValueError("Planilha maior que 8 MB.")
        linhas = hs.ler_xls(conteudo)
        res = hs.importar(db, linhas, arq.filename)
    except ValueError as e:
        db.rollback()
        flash(request, str(e), "erro")
        return _redir("/honorarios")
    auditar(db, request, u, "HONORARIOS_IMPORTAR", "recebimento", None,
            f"{arq.filename}: {res.novos} novo(s), {res.alterados} alterado(s), {res.ja_existiam} já existiam; sem empresa={res.sem_empresa[:20]}")
    db.commit()
    flash(request, f"{arq.filename}: {res.novos} recebimento(s) novo(s), {res.alterados} atualizado(s), {res.ja_existiam} já importado(s).")
    if res.sem_empresa:
        flash(request, f"{len(res.sem_empresa)} sem empresa com esse código no cadastro: " + "; ".join(res.sem_empresa[:6]), "erro")
    meses = sorted({l["pagamento"] for l in linhas if l["pagamento"]})
    return _redir(f"/honorarios?mes={meses[-1]:%Y-%m}" if meses else "/honorarios")


@router.post("/honorarios/rascunhos")
async def honorarios_rascunhos(request: Request, db: Session = Depends(get_db), u=Depends(usuario_atual)):
    form = await request.form()
    ids = [int(x) for x in form.getlist("ids") if str(x).isdigit()]
    volta = form.get("volta") or "/honorarios"
    if not ids:
        flash(request, "Marque os recebimentos.", "erro")
        return _redir(volta)
    try:
        notas, avisos = hs.gerar_rascunhos(db, ids, u.login)
    except ValueError as e:
        db.rollback()
        flash(request, str(e), "erro")
        return _redir(volta)
    auditar(db, request, u, "HONORARIOS_RASCUNHOS", "nota", None, f"{len(notas)} rascunho(s): {[n.id for n in notas]}; {avisos[:10]}")
    db.commit()
    if notas:
        flash(request, f"{len(notas)} rascunho(s) de NFS-e criado(s) na empresa do escritório. Confira e transmita na aba Notas fiscais.")
    for a in avisos[:8]:
        flash(request, a, "erro")
    return _redir(volta)


@router.post("/honorarios/config")
async def honorarios_config(request: Request, db: Session = Depends(get_db), u=Depends(admin)):
    form = await request.form()
    gravar_config(db, "honorarios_prestador_id", texto(form, "honorarios_prestador_id", 10))
    gravar_config(db, "honorarios_ctribnac", texto(form, "honorarios_ctribnac", 9))
    gravar_config(db, "honorarios_nbs", texto(form, "honorarios_nbs", 9))
    d = texto(form, "honorarios_descricao", 500)
    gravar_config(db, "honorarios_descricao", "" if d == hs.DESCRICAO_PADRAO else d)
    auditar(db, request, u, "HONORARIOS_CONFIG", "configuracao", None, "notas de honorários")
    db.commit()
    flash(request, "Configuração das notas de honorários salva.")
    return _redir("/honorarios")
