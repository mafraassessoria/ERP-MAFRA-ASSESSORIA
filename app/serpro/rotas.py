"""Telas do módulo Receita Federal (Integra Contador)."""
from __future__ import annotations

import json
import re
import secrets
import threading
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..configuracoes import gravar_config, ler_config
from ..db import SessionLocal, get_db
from ..web import admin, auditar, flash, render, templates, texto, usuario_atual
from . import servico as sv
from .catalogo import GRUPOS, POR_CODIGO, PRECOS_PADRAO, SERVICOS, servicos_para

router = APIRouter()


def _rotulo_campo(nome) -> str:
    """'dataArrecadacao' -> 'Data arrecadacao' (para mostrar as chaves do JSON da Receita)."""
    t = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", str(nome)).replace("_", " ").strip()
    return t[:1].upper() + t[1:].lower() if t else ""


templates.env.filters["rotulo_campo"] = _rotulo_campo
def _de_json(v):
    try:
        x = json.loads(v or "{}")
    except ValueError:
        return {}
    return x if isinstance(x, dict) else {}


templates.env.filters["from_json"] = _de_json
templates.env.tests["dicionario"] = lambda v: isinstance(v, dict)
templates.env.tests["lista"] = lambda v: isinstance(v, list)
_LOTES: dict[str, dict] = {}  # progresso dos lotes em andamento (na memória do servidor)


def _redir(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _situacao_modulo(db: Session) -> dict:
    m = sv.modo(db)
    return {"modo": m, "modo_rotulo": sv.MODOS[m], "gasto": sv.gasto_mes(db), "limite": sv.limite_mensal(db),
            "chamadas": sv.chamadas_mes(db), "precos": sv.precos(db)}


# ---- Aba "Receita Federal" da empresa -------------------------------------------------

@router.get("/prestadores/{pid}/receita")
def receita_empresa(pid: int, request: Request, ver: int = 0, db: Session = Depends(get_db), u=Depends(usuario_atual)):
    p = db.get(models.Prestador, pid)
    if not p:
        return _redir("/prestadores")
    if not p.usa_receita:
        flash(request, "O módulo Receita Federal está desligado para esta empresa (Dados e configurações → Módulos).", "erro")
        return _redir(f"/prestadores/{pid}?aba=config")
    servs = servicos_para(p.regime)
    grupos = [(g, [s for s in servs if s.grupo == g]) for g in GRUPOS]
    grupos = [(g, l) for g, l in grupos if l]
    historico = db.scalars(select(models.ConsultaReceita).where(models.ConsultaReceita.prestador_id == pid)
                           .order_by(models.ConsultaReceita.id.desc()).limit(60)).all()
    aberta = db.get(models.ConsultaReceita, ver) if ver else None
    if aberta is not None and aberta.prestador_id != pid:
        aberta = None
    precos = sv.precos(db)
    custos = {s.codigo: sum((precos.get(t, Decimal(0)) for t in sv.chamadas_previstas(s)), Decimal(0)) for s in servs}
    from ..monitor.rotas import resumo_empresa
    return render(request, "receita_empresa.html", p=p, aba="receita", monitor=resumo_empresa(db, pid), grupos=grupos, historico=historico,
                  aberta=aberta, dados_abertos=_carregar(aberta), custos=custos, POR_CODIGO=POR_CODIGO,
                  sit=_situacao_modulo(db))


def _carregar(c: models.ConsultaReceita | None):
    if c is None or not c.dados:
        return None
    try:
        return json.loads(c.dados)
    except ValueError:
        return c.dados


@router.post("/prestadores/{pid}/receita/{codigo}")
async def receita_executar(pid: int, codigo: str, request: Request, db: Session = Depends(get_db), u=Depends(usuario_atual)):
    p = db.get(models.Prestador, pid)
    s = POR_CODIGO.get(codigo)
    if not p or not s or not p.usa_receita:
        return _redir("/prestadores")
    form = await request.form()
    valores = {c.nome: str(form.get(c.nome) or "") for c in s.campos}
    if form.get("repetir") != "1":
        igual = sv.ultima_igual(db, p, s, valores)
        if igual is not None:
            flash(request, f"Esta consulta já foi feita hoje às {igual.data_hora:%H:%M} — mostrando o resultado guardado "
                           "(sem custo). Para consultar de novo, use 'Consultar de novo'.")
            return _redir(f"/prestadores/{pid}/receita?ver={igual.id}#resultado")
    try:
        c = await run_in_threadpool(sv.executar, db, p, codigo, valores, u.login)
    except sv.ErroConsulta as e:
        flash(request, str(e), "erro")
        return _redir(f"/prestadores/{pid}/receita")
    auditar(db, request, u, "RECEITA_" + s.id_servico, "prestador", pid,
            f"{s.nome} {valores} modo={c.modo} ok={c.ok} custo={c.custo} {c.mensagens[:200]}")
    db.commit()
    if c.ok:
        flash(request, f"{s.nome}: consulta concluída." + (f" {len(c.lista_arquivos)} PDF(s) guardado(s)." if c.lista_arquivos else ""))
    else:
        flash(request, f"{s.nome}: {c.mensagens or 'a Receita não devolveu resultado.'}", "erro")
    return _redir(f"/prestadores/{pid}/receita?ver={c.id}#resultado")


@router.get("/receita/consultas/{cid}/arquivo/{n}")
def receita_arquivo(cid: int, n: int, download: int = 0, db: Session = Depends(get_db), u=Depends(usuario_atual)):
    c = db.get(models.ConsultaReceita, cid)
    if not c or not (0 <= n < len(c.lista_arquivos)):
        return _redir("/receita")
    arq = sv.arquivo_absoluto(c.lista_arquivos[n])
    if arq is None:
        return _redir(f"/prestadores/{c.prestador_id}/receita")
    return FileResponse(arq, media_type="application/pdf", filename=arq.name,
                        content_disposition_type="attachment" if download else "inline")


# ---- Tela geral: uso, histórico e lote --------------------------------------------------

@router.get("/receita")
def receita_geral(request: Request, db: Session = Depends(get_db), u=Depends(usuario_atual)):
    recentes = db.scalars(select(models.ConsultaReceita).order_by(models.ConsultaReceita.id.desc()).limit(40)).all()
    prestadores = db.scalars(select(models.Prestador).where(models.Prestador.ativo.is_(True),
                                                            models.Prestador.usa_receita.is_(True))
                             .order_by(models.Prestador.razao_social)).all()
    return render(request, "receita_geral.html", recentes=recentes, prestadores=prestadores,
                  servicos_lote=[s for s in SERVICOS if s.lote], sit=_situacao_modulo(db), POR_CODIGO=POR_CODIGO,
                  lotes=sorted(_LOTES.values(), key=lambda x: x["inicio"], reverse=True)[:5])


def _alvos(db: Session, s, form) -> list[models.Prestador]:
    escolha = form.get("empresas") or "todas"
    ids = [int(x) for x in form.getlist("ids") if str(x).isdigit()]
    stmt = select(models.Prestador).where(models.Prestador.ativo.is_(True), models.Prestador.usa_receita.is_(True)) \
        .order_by(models.Prestador.razao_social)
    lista = db.scalars(stmt).all()
    if escolha == "marcadas":
        lista = [p for p in lista if p.id in ids]
    out = []
    for p in lista:
        if s.codigo in {x.codigo for x in servicos_para(p.regime)}:
            out.append(p)
    return out


@router.post("/receita/lote/previa")
async def receita_lote_previa(request: Request, db: Session = Depends(get_db), u=Depends(usuario_atual)):
    form = await request.form()
    s = POR_CODIGO.get(form.get("servico") or "")
    if not s or not s.lote:
        flash(request, "Escolha o serviço.", "erro")
        return _redir("/receita")
    valores = {c.nome: str(form.get(c.nome) or c.valor_padrao()) for c in s.campos}
    try:
        s.montar(valores)
    except (ValueError, TypeError) as e:
        flash(request, str(e), "erro")
        return _redir("/receita")
    alvos = _alvos(db, s, form)
    unit = sv.custo_previsto(db, s)
    m = sv.modo(db)
    total = unit * len(alvos) if m == "producao" else Decimal(0)
    return render(request, "receita_lote_previa.html", s=s, valores=valores, alvos=alvos, unit=unit, total=total,
                  sit=_situacao_modulo(db), estoura=(m == "producao" and sv.gasto_mes(db) + total > sv.limite_mensal(db)))


@router.post("/receita/lote/executar")
async def receita_lote_executar(request: Request, db: Session = Depends(get_db), u=Depends(usuario_atual)):
    form = await request.form()
    s = POR_CODIGO.get(form.get("servico") or "")
    ids = [int(x) for x in form.getlist("ids") if str(x).isdigit()]
    if not s or not ids:
        flash(request, "Nada para consultar.", "erro")
        return _redir("/receita")
    if any(t["rodando"] for t in _LOTES.values()):
        flash(request, "Já há um lote em andamento. Aguarde terminar.", "erro")
        return _redir("/receita")
    valores = {c.nome: str(form.get(c.nome) or "") for c in s.campos}
    lote = f"RF-{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"
    info = {"id": lote, "servico": s.nome, "total": len(ids), "feitos": 0, "ok": 0, "erros": [], "rodando": True,
            "inicio": datetime.now(), "usuario": u.login, "consultas": []}
    _LOTES[lote] = info
    auditar(db, request, u, "RECEITA_LOTE", "receita", None, f"{lote} {s.nome} {valores} {len(ids)} empresa(s)")
    db.commit()
    threading.Thread(target=_rodar_lote, args=(lote, s.codigo, ids, valores, u.login), daemon=True).start()
    return _redir(f"/receita/lote/{lote}")


def _rodar_lote(lote: str, codigo: str, ids: list[int], valores: dict, usuario: str) -> None:
    info = _LOTES[lote]
    with SessionLocal() as db:
        try:
            cli = sv.cliente(db)
        except sv.ErroConsulta as e:
            info["erros"].append(str(e))
            info["rodando"] = False
            return
        for pid in ids:
            p = db.get(models.Prestador, pid)
            if p is None:
                continue
            try:
                c = sv.executar(db, p, codigo, valores, usuario, lote=lote, cli=cli)
                db.commit()
                info["consultas"].append(c.id)
                if c.ok:
                    info["ok"] += 1
                else:
                    info["erros"].append(f"{p.razao_social}: {c.mensagens[:200]}")
            except sv.ErroConsulta as e:
                db.rollback()
                info["erros"].append(f"{p.razao_social}: {e}")
                if "Limite de gasto" in str(e):
                    break
            except Exception as e:  # noqa: BLE001
                db.rollback()
                info["erros"].append(f"{p.razao_social}: falha inesperada ({type(e).__name__})")
            info["feitos"] += 1
    info["rodando"] = False
    info["fim"] = datetime.now()


@router.get("/receita/lote/{lote}")
def receita_lote_ver(lote: str, request: Request, db: Session = Depends(get_db), u=Depends(usuario_atual)):
    info = _LOTES.get(lote)
    consultas = db.scalars(select(models.ConsultaReceita).where(models.ConsultaReceita.lote == lote)
                           .order_by(models.ConsultaReceita.id)).all()
    if info is None and not consultas:
        flash(request, "Lote não encontrado.", "erro")
        return _redir("/receita")
    return render(request, "receita_lote.html", lote=lote, info=info, consultas=consultas, POR_CODIGO=POR_CODIGO)


# ---- Configuração -------------------------------------------------------------------------

@router.post("/configuracoes/receita")
async def receita_config(request: Request, db: Session = Depends(get_db), u=Depends(admin)):
    form = await request.form()
    m = form.get("serpro_modo") or "desligado"
    if m not in sv.MODOS:
        m = "desligado"
    gravar_config(db, "serpro_modo", m)
    sv.gravar_chaves(db, texto(form, "serpro_ck", 200), texto(form, "serpro_cs", 200))
    for tipo in PRECOS_PADRAO:
        v = texto(form, f"serpro_preco_{tipo}", 12)
        if v:
            gravar_config(db, f"serpro_preco_{tipo}", v)
    if texto(form, "serpro_limite_mensal", 15):
        gravar_config(db, "serpro_limite_mensal", texto(form, "serpro_limite_mensal", 15))
    auditar(db, request, u, "CONFIG_RECEITA", "configuracao", None, f"modo={m}")
    db.commit()
    if m == "producao" and not sv.tem_chaves(db):
        flash(request, "Modo produção ativado, mas faltam a Consumer Key e a Consumer Secret do Serpro.", "erro")
    else:
        flash(request, "Configuração do Integra Contador salva.")
    return _redir("/configuracoes#receita")


@router.post("/configuracoes/receita/testar")
async def receita_testar(request: Request, db: Session = Depends(get_db), u=Depends(admin)):
    """Testa a conexão consultando a procuração do próprio escritório (Consultar = 1 chamada)."""
    try:
        cli = sv.cliente(db)
        if cli.modo == "producao":
            await run_in_threadpool(cli._autenticar)
            msg = "Autenticação no Serpro OK (certificado do escritório e chaves aceitos). Nenhuma chamada cobrada."
        else:
            r = await run_in_threadpool(cli.chamar, "Monitorar", "00000000000000", "CAIXAPOSTAL", "INNOVAMSG63", "1.0", "")
            msg = f"Ambiente de demonstração respondeu (HTTP {r.http}). {r.texto_mensagens}"
        flash(request, msg)
    except (sv.ErroConsulta, sv.ErroSerpro) as e:
        flash(request, f"Falha no teste: {e}", "erro")
    return _redir("/configuracoes#receita")


# ---- APIs avulsas: Consulta CPF e Consulta CND ------------------------------------------------

@router.post("/configuracoes/apis-serpro")
async def apis_config(request: Request, db: Session = Depends(get_db), u=Depends(admin)):
    from . import apis
    form = await request.form()
    det = []
    for prod in apis.PRODUTOS:
        k = prod.lower()
        m = form.get(f"{k}_modo") or "desligado"
        m = m if m in apis.MODOS else "desligado"
        gravar_config(db, f"serpro_{k}_modo", m)
        apis.gravar_chaves(db, prod, texto(form, f"{k}_ck", 200), texto(form, f"{k}_cs", 200))
        if texto(form, f"{k}_preco", 12):
            gravar_config(db, f"serpro_preco_{prod}", texto(form, f"{k}_preco", 12))
        det.append(f"{prod}={m}")
        if m == "producao" and not apis.chaves(db, prod)[0]:
            flash(request, f"{apis.PRODUTOS[prod]} em produção sem chaves: informe a Consumer Key/Secret do contrato.", "erro")
    if texto(form, "cnd_codigo", 10):
        gravar_config(db, "serpro_cnd_codigo", texto(form, "cnd_codigo", 10))
    gravar_config(db, "certidoes_federal_auto", "1" if form.get("federal_auto") == "1" else "0")
    auditar(db, request, u, "CONFIG_APIS_SERPRO", "configuracao", None, " ".join(det))
    db.commit()
    flash(request, "Configuração das APIs do Serpro salva.")
    return _redir("/configuracoes#apis-serpro")


@router.post("/configuracoes/apis-serpro/testar")
async def apis_testar(request: Request, produto: str = "CPF", db: Session = Depends(get_db), u=Depends(admin)):
    """Produção: só autentica (não cobra). Demonstração: faz uma consulta com dado fictício do Serpro."""
    from . import apis
    from .gateway import ErroGateway
    produto = produto if produto in apis.PRODUTOS else "CPF"
    try:
        cli = apis.cliente(db, produto)
        if cli.modo == "producao":
            await run_in_threadpool(cli.token)
            msg = f"{apis.PRODUTOS[produto]}: chaves aceitas pelo Serpro. Nenhuma consulta cobrada."
        elif produto == "CPF":
            r = await run_in_threadpool(cli.cpf, "40442820135", "14111970")
            d = r.json() if r.status_code == 200 else {}
            msg = f"Consulta CPF (demonstração) respondeu HTTP {r.status_code}" + (f": {d.get('nome')} — {(d.get('situacao') or {}).get('descricao', '')}" if d else "")
        else:
            r = await run_in_threadpool(cli.cnd, {"TipoContribuinte": 1, "ContribuinteConsulta": "00000000000191",
                                                  "CodigoIdentificacao": apis.codigo_cnd(db), "GerarCertidaoPdf": False})
            msg = f"Consulta CND (demonstração) respondeu HTTP {r.status_code}: {r.text[:200]}"
        flash(request, msg)
    except (apis.ErroAPI, ErroGateway) as e:
        flash(request, f"Falha no teste: {e}", "erro")
    except ValueError:
        flash(request, "O Serpro respondeu num formato inesperado.", "erro")
    return _redir("/configuracoes#apis-serpro")


def contexto_apis(db: Session) -> dict:
    from . import apis
    out = {}
    for prod, nome in apis.PRODUTOS.items():
        ck, cs, origem = apis.chaves(db, prod)
        n, v = apis.uso_mes(db, prod)
        out[prod] = dict(nome=nome, modo=apis.modo(db, prod), origem_chaves=origem,
                         proprias=bool(ler_config(db, f"serpro_{prod.lower()}_ck_cripto")),
                         preco=ler_config(db, f"serpro_preco_{prod}") or apis.PRECOS_PADRAO[prod], chamadas=n, gasto=v)
    return {"apis_serpro": out, "APIS_MODOS": apis.MODOS, "cnd_codigo": apis.codigo_cnd(db),
            "federal_auto": (ler_config(db, "certidoes_federal_auto") or "1") == "1"}


def contexto_config(db: Session) -> dict:
    return {**contexto_apis(db), "serpro": {"modo": sv.modo(db), "tem_chaves": sv.tem_chaves(db), "precos": {
        t: ler_config(db, f"serpro_preco_{t}") or p for t, p in PRECOS_PADRAO.items()},
        "limite": ler_config(db, "serpro_limite_mensal") or "200,00", "gasto": sv.gasto_mes(db),
        "chamadas": sv.chamadas_mes(db), "escritorio": sv.escritorio(db)}, "SERPRO_MODOS": sv.MODOS}
