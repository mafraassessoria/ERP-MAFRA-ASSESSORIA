"""Configurações, importação pelo ADN e relatórios por competência."""
import csv
import io
import zipfile
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .nfse import importacao
from . import models
from .config import XML_DIR
from .configuracoes import (
    CAMPOS_SMTP, ErroEmail, enviar_email, gravar_config, gravar_senha_smtp, ler_config, smtp_configurado,
)
from .db import get_db
from .documentos import formatar_documento
from .web import admin, auditar, flash, render, texto, usuario_atual

router = APIRouter()


def _redir(url: str):
    return RedirectResponse(url, status_code=303)


# ---- Configurações (admin) --------------------------------------------------

@router.get("/configuracoes")
def configuracoes(request: Request, db: Session = Depends(get_db), u=Depends(admin)):
    cfg = {c: ler_config(db, c) for c in CAMPOS_SMTP + ("pasta_copia_xml", "prestador_procurador_id")}
    cfg["tem_senha"] = bool(ler_config(db, "smtp_senha_cripto"))
    cfg["obra_subitens"] = ler_config(db, "obra_subitens") or models.OBRA_SUBITENS_PADRAO
    prestadores = db.scalars(select(models.Prestador).order_by(models.Prestador.razao_social)).all()
    tab = {
        "ctribnac": db.scalar(select(func.count()).select_from(models.CodigoTributacao)),
        "ctribnac_oficial": db.scalar(select(func.count()).select_from(models.CodigoTributacao).where(models.CodigoTributacao.oficial.is_(True))),
        "nbs": db.scalar(select(func.count()).select_from(models.CodigoNBS)),
        "nbs_oficial": db.scalar(select(func.count()).select_from(models.CodigoNBS).where(models.CodigoNBS.oficial.is_(True))),
        "correlacoes": db.scalar(select(func.count()).select_from(models.CorrelacaoNBS)),
        "importada_em": ler_config(db, "tabelas_importadas_em"),
    }
    import json as _json
    from .config import BASE_DIR
    try:
        atualizacao = _json.loads((BASE_DIR / "logs" / "atualizacao.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        atualizacao = None
    from .certidoes.rotas import contexto_config as ctx_cert
    from .serpro.rotas import contexto_config
    from .econtador import sessao as ec_sessao
    return render(request, "configuracoes.html", cfg=cfg, prestadores=prestadores, tab=tab, atualizacao=atualizacao, **contexto_config(db), **ctx_cert(db),
                  ec_web=ec_sessao.estado(db))


@router.post("/configuracoes")
async def configuracoes_salvar(request: Request, db: Session = Depends(get_db), u=Depends(admin)):
    form = await request.form()
    for c in CAMPOS_SMTP:
        gravar_config(db, c, texto(form, c, 300))
    if form.get("smtp_senha"):
        gravar_senha_smtp(db, form.get("smtp_senha"))
    gravar_config(db, "pasta_copia_xml", texto(form, "pasta_copia_xml", 300))
    gravar_config(db, "prestador_procurador_id", texto(form, "prestador_procurador_id", 10))
    import re as _re
    subs = sorted({_re.sub(r"\D", "", x)[:4] for x in (form.get("obra_subitens") or "").split(",") if len(_re.sub(r"\D", "", x)) >= 4})
    gravar_config(db, "obra_subitens", ",".join(subs) or models.OBRA_SUBITENS_PADRAO)
    auditar(db, request, u, "SALVAR_CONFIGURACOES", "configuracao", None,
            "smtp/pasta/procurador" + (" + nova senha SMTP" if form.get("smtp_senha") else ""))
    db.commit()
    flash(request, "Configurações salvas.")
    return _redir("/configuracoes")


@router.post("/configuracoes/empresas/importar")
async def configuracoes_importar_empresas(request: Request, db: Session = Depends(get_db), u=Depends(admin)):
    """Importa o empresas.json do Bot Arquivador para o cadastro geral (sem apagar nem sobrescrever dados fiscais)."""
    from . import importar_empresas as ie
    form = await request.form()
    arq = form.get("arquivo")
    if not getattr(arq, "filename", ""):
        flash(request, "Selecione o empresas.json do Bot Arquivador.", "erro")
        return _redir("/configuracoes#importar-empresas")
    try:
        registros = ie.ler_arquivo(await arq.read(5 * 1024 * 1024))
        res = ie.importar(db, registros)
    except ValueError as e:
        db.rollback()
        flash(request, str(e), "erro")
        return _redir("/configuracoes#importar-empresas")
    auditar(db, request, u, "IMPORTAR_EMPRESAS", "prestador", None,
            f"{arq.filename}: {res.resumo}; criadas={res.criadas[:50]}; ignoradas={res.ignoradas[:50]}")
    db.commit()
    return render(request, "importar_empresas.html", res=res, arquivo=arq.filename)


@router.post("/configuracoes/tabelas")
async def configuracoes_tabelas(request: Request, db: Session = Depends(get_db), u=Depends(admin)):
    """Importa as planilhas oficiais (Anexo B e Anexo VIII) de códigos de serviço e NBS."""
    from datetime import datetime as _dt

    from .nfse import tabelas
    form = await request.form()
    arquivos = [a for a in form.getlist("arquivos") if getattr(a, "filename", "")]
    if not arquivos:
        flash(request, "Selecione a(s) planilha(s) oficial(is).", "erro")
        return _redir("/configuracoes#tabelas")
    msgs = []
    for a in arquivos:
        conteudo = await a.read(30 * 1024 * 1024)
        try:
            q = await run_in_threadpool(tabelas.importar_planilha, db, conteudo, a.filename)
            msgs.append(f"{a.filename}: {q['ctribnac']} código(s) de tributação, {q['nbs']} NBS, {q['correlacoes']} correlação(ões)")
        except Exception as e:  # noqa: BLE001
            db.rollback()
            msgs.append(f"{a.filename}: não foi possível ler ({e})")
    gravar_config(db, "tabelas_importadas_em", _dt.now().strftime("%d/%m/%Y %H:%M"))
    auditar(db, request, u, "IMPORTAR_TABELAS", "configuracao", None, " | ".join(msgs)[:500])
    db.commit()
    flash(request, "Tabelas importadas. " + " | ".join(msgs))
    return _redir("/configuracoes#tabelas")


@router.post("/configuracoes/teste-email")
async def configuracoes_teste_email(request: Request, db: Session = Depends(get_db), u=Depends(admin)):
    form = await request.form()
    para = texto(form, "para", 200)
    try:
        await run_in_threadpool(enviar_email, db, [para], "Teste - Emissor NFS-e",
                                "Este é um e-mail de teste do Emissor NFS-e. Se chegou, a configuração está correta.")
        flash(request, f"E-mail de teste enviado para {para}.")
    except ErroEmail as e:
        flash(request, str(e), "erro")
    return _redir("/configuracoes")


# ---- Importação ADN --------------------------------------------------------

@router.post("/prestadores/{pid}/importar")
async def prestador_importar(pid: int, request: Request, db: Session = Depends(get_db), u=Depends(usuario_atual)):
    p = db.get(models.Prestador, pid)
    if not p:
        return _redir("/prestadores")
    res = await run_in_threadpool(importacao.importar, db, p, u.login)
    auditar(db, request, u, "IMPORTAR_ADN", "prestador", pid, res.mensagem[:500])
    db.commit()
    flash(request, res.mensagem, "ok" if res.ok else "erro")
    return _redir(f"/prestadores/{pid}?aba=config#importacao")


@router.post("/importar-todos")
async def importar_todos(request: Request, db: Session = Depends(get_db), u=Depends(usuario_atual)):
    resultados = await run_in_threadpool(importacao.importar_todos, db, u.login)
    falhas = [p.razao_social for p, r in resultados if not r.ok]
    novas = sum(r.novas_prestadas + r.novas_tomadas for _, r in resultados)
    auditar(db, request, u, "IMPORTAR_ADN_TODOS", "", None, f"{len(resultados)} empresas, {novas} notas novas, falhas: {falhas}")
    db.commit()
    flash(request, f"Importação concluída: {len(resultados)} empresa(s), {novas} nota(s) nova(s)."
          + (f" Com problema: {', '.join(falhas)}." if falhas else ""), "erro" if falhas else "ok")
    return _redir("/relatorios")


# ---- Relatórios ------------------------------------------------------------

def _periodo(competencia: str) -> tuple[date, date, str]:
    try:
        ini = datetime.strptime(competencia, "%Y-%m").date()
    except ValueError:
        hoje = date.today()
        ini = date(hoje.year - (hoje.month == 1), (hoje.month - 2) % 12 + 1, 1)  # mês anterior
    fim = date(ini.year + (ini.month == 12), ini.month % 12 + 1, 1)
    return ini, fim, ini.strftime("%Y-%m")


def _notas_periodo(db: Session, ini: date, fim: date, prestador_id: int, homologacao: bool):
    stmt = select(models.NotaFiscal).where(
        models.NotaFiscal.competencia >= ini, models.NotaFiscal.competencia < fim,
        models.NotaFiscal.situacao.in_(("AUTORIZADA", "CANCELADA")))
    stmt = stmt.where(models.NotaFiscal.ambiente == "PRODUCAO")  # notas de teste não entram
    if prestador_id:
        stmt = stmt.where(models.NotaFiscal.prestador_id == prestador_id)
    return db.scalars(stmt.order_by(models.NotaFiscal.prestador_id, models.NotaFiscal.papel,
                                    models.NotaFiscal.data_emissao)).all()


@router.get("/relatorios")
def relatorios(request: Request, competencia: str = "", prestador_id: int = 0, homologacao: int = 0,
               db: Session = Depends(get_db), u=Depends(usuario_atual)):
    ini, fim, comp = _periodo(competencia)
    notas = _notas_periodo(db, ini, fim, prestador_id, bool(homologacao))
    zero = Decimal("0")
    resumo = defaultdict(lambda: {"emitidas": 0, "valor_emitido": zero, "iss_emitido": zero, "iss_retido_prest": zero,
                                  "canceladas": 0, "valor_cancelado": zero,
                                  "tomadas": 0, "valor_tomado": zero, "iss_retido_tomado": zero})
    for n in notas:
        r = resumo[n.prestador]
        if n.papel == "PRESTADO":
            if n.situacao == "AUTORIZADA":
                r["emitidas"] += 1
                r["valor_emitido"] += n.valor_servicos
                r["iss_emitido"] += n.valor_iss
                if n.tipo_retencao_iss == 2:
                    r["iss_retido_prest"] += n.valor_iss
            else:
                r["canceladas"] += 1
                r["valor_cancelado"] += n.valor_servicos
        elif n.situacao == "AUTORIZADA":
            r["tomadas"] += 1
            r["valor_tomado"] += n.valor_servicos
            if n.tipo_retencao_iss == 2:
                r["iss_retido_tomado"] += n.valor_iss
    linhas = sorted(resumo.items(), key=lambda kv: kv[0].razao_social)
    total = {k: sum((v[k] for _, v in linhas), zero if not isinstance(v0, int) else 0)
             for k, v0 in (linhas[0][1].items() if linhas else [])}
    prestadores = db.scalars(select(models.Prestador).order_by(models.Prestador.razao_social)).all()
    controles = {c.prestador_id: c for c in db.scalars(select(models.ControleNSU)).all()}
    return render(request, "relatorios.html", linhas=linhas, total=total, comp=comp, prestadores=prestadores,
                  f=dict(prestador_id=prestador_id, homologacao=homologacao), controles=controles)


def _br(v) -> str:
    return f"{Decimal(v or 0):.2f}".replace(".", ",")


@router.get("/relatorios/notas.csv")
def relatorio_csv(competencia: str = "", prestador_id: int = 0, homologacao: int = 0,
                  db: Session = Depends(get_db), u=Depends(usuario_atual)):
    ini, fim, comp = _periodo(competencia)
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Empresa CNPJ", "Empresa", "Papel", "Situação", "Ambiente", "Nº NFS-e", "Data emissão",
                "Competência", "Prestador CPF/CNPJ", "Prestador", "Tomador CPF/CNPJ", "Tomador",
                "Cód. tributação", "Valor serviço", "ISS", "ISS retido", "Valor ISS retido", "INSS retido", "Chave de acesso", "Origem"])
    for n in _notas_periodo(db, ini, fim, prestador_id, bool(homologacao)):
        prest_doc = n.emitente_documento or n.prestador.cnpj
        prest_nome = n.emitente_nome or n.prestador.razao_social
        w.writerow([formatar_documento(n.prestador.cnpj), n.prestador.razao_social,
                    "Prestado" if n.papel == "PRESTADO" else "Tomado", models.SITUACOES_NOTA[n.situacao],
                    "Produção" if n.ambiente == "PRODUCAO" else "Homologação", n.numero_nfse or "",
                    n.data_emissao.strftime("%d/%m/%Y %H:%M") if n.data_emissao else "",
                    n.competencia.strftime("%d/%m/%Y") if n.competencia else "",
                    formatar_documento(prest_doc), prest_nome, formatar_documento(n.tomador_documento), n.tomador_nome,
                    n.codigo_tributacao_nacional, _br(n.valor_servicos), _br(n.valor_iss),
                    "Sim" if (n.tipo_retencao_iss or 1) in (2, 3) else "Não",
                    _br(n.iss_retido), _br(n.ret_inss or 0), n.chave_acesso or "",
                    "Emitida no sistema" if n.origem == "EMITIDA" else "Importada ADN"])
    dados = "﻿" + buf.getvalue()  # BOM: o Excel abre com acentos corretos
    nome = f"notas_{comp}{'_empresa%d' % prestador_id if prestador_id else ''}.csv"
    return Response(dados.encode("utf-8"), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{nome}"'})


@router.get("/relatorios/xml.zip")
def relatorio_zip(competencia: str = "", prestador_id: int = 0, homologacao: int = 0,
                  db: Session = Depends(get_db), u=Depends(usuario_atual)):
    ini, fim, comp = _periodo(competencia)
    buf = io.BytesIO()
    qtd = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for n in _notas_periodo(db, ini, fim, prestador_id, bool(homologacao)):
            pasta = f"{n.prestador.cnpj}/{'prestadas' if n.papel == 'PRESTADO' else 'tomadas'}"
            for campo in ("caminho_xml", "caminho_evento_xml"):
                rel = getattr(n, campo)
                if rel and (XML_DIR / rel).exists():
                    z.write(XML_DIR / rel, f"{pasta}/{(XML_DIR / rel).name}")
                    qtd += 1
        if not qtd:
            z.writestr("LEIA-ME.txt", "Nenhum XML encontrado para os filtros escolhidos.")
    nome = f"xml_nfse_{comp}{'_empresa%d' % prestador_id if prestador_id else ''}.zip"
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{nome}"'})
