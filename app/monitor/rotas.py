"""Telas do Monitor e-CAC."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

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
    rot = _rotinas_ctx(db)
    pn = ms.painel(db)
    atrasos = ms.atrasos_sitfis(db)
    parc_atraso = sum(1 for v in atrasos.values() if any(n for _t, n in v["itens"]))
    prio = []
    if pn["SITFIS"]["cont"].get("PENDENCIA"):
        prio.append(("/monitor/item/SITFIS?status=PENDENCIA", f"{pn['SITFIS']['cont']['PENDENCIA']} com pendência fiscal"))
    for cod, nome in (("PGDASD", "PGDAS-D"), ("DCTFWEB", "DCTFWeb"), ("MIT", "MIT")):
        d = pn[cod]
        n = d["cont"].get("NAO_ENTREGUE")
        if not n:
            continue
        comp = f"{d['ref'][5:]}/{d['ref'][:4]}"
        if d["vencido"]:
            txt = f"{n} {nome} {comp} atrasada(s)"
        elif d["prazo"]:
            falta = "hoje" if not d["dias"] else f"faltam {d['dias']} dia{'s' if d['dias'] != 1 else ''}"
            txt = f"{n} {nome} {comp} a entregar · vence {d['prazo']:%d/%m} ({falta})"
        else:
            txt = f"{n} {nome} {comp} a entregar"
        prio.append((f"/monitor/item/{cod}?status=NAO_ENTREGUE", txt))
    if parc_atraso:
        prio.append(("/monitor/item/PARCELAMENTO", f"{parc_atraso} com parcelas em atraso"))
    for cod, nome in (("CAIXA", "Caixa postal"), ("PARCELAMENTO", "Parcelamentos"), ("SITFIS", "Situação fiscal")):
        if pn[cod]["so_erro"]:
            prio.append((f"#q-{cod}", f"{nome}: consultar de novo"))
    return render(request, "monitor_painel.html", pn=pn, status_de=ms.status_de, rotinas=rot, execucao=ms.situacao(),
                  STATUS=ms.STATUS, ROTULO=ms.ROTULO_ITEM, modo=sv.modo(db), DIAS=ms.DIAS,
                  rot={x["r"].codigo: x for x in rot}, gasto=sv.gasto_mes(db), limite=sv.limite_mensal(db), prio=prio)


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
        lista = [x for x in lista if t in x.prestador.razao_social.upper() or t.lstrip("0") == (x.prestador.codigo_interno or "")]
    lista = sorted(lista, key=lambda x: x.prestador.ordem)
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


# ---- Conferência faturamento x PGDAS-D ----------------------------------------------------------

@router.get("/conferencia-pgdas")
def conferencia_pgdas(request: Request, ref: str = "", db: Session = Depends(get_db), u=Depends(usuario_atual)):
    from .. import conferencia as cf
    from ..serpro.catalogo import POR_CODIGO
    from .rotinas import mes_anterior
    ref = ref if len(ref) == 7 and ref[4] == "-" else mes_anterior(date.today())
    lista = cf.linhas(db, ref)
    sem_decl = [x for x in lista if x["entregue"] != "NAO_ENTREGUE" and (not x["r"] or x["r"].declarado is None)]
    custo_um = sv.custo_previsto(db, POR_CODIGO["pgdasd_ultima"]) if sv.modo(db) == "producao" else 0
    refs = []
    d = date.today().replace(day=1)
    for _ in range(12):
        d = (d - timedelta(days=1)).replace(day=1)
        refs.append(f"{d:%Y-%m}")
    cont = {}
    for x in lista:
        cont[x["situacao"]] = cont.get(x["situacao"], 0) + 1
    return render(request, "conferencia_pgdas.html", lista=lista, ref=ref, refs=refs, cont=cont,
                  n_sem_decl=len(sem_decl), custo_um=custo_um, custo_todos=custo_um * len(sem_decl),
                  e_admin=getattr(u, "perfil", "") == "ADMIN", modo=sv.modo(db), pasta=str(cf.pasta_extratos(db)))


@router.post("/conferencia-pgdas/faturamento")
async def conferencia_faturamento(request: Request, db: Session = Depends(get_db), u=Depends(usuario_atual)):
    from .. import conferencia as cf
    form = await request.form()
    ref = form.get("ref", "")
    msgs, erro = [], False
    try:
        r = cf.atualizar_faturamento(db, ref)
        msgs.append(f"Faturamento atualizado em {r['feitas']} empresa(s).")
        if r["erros"]:
            erro = True
            msgs.append(f"NF-Stock com problema em {len(r['erros'])}: " + "; ".join(r["erros"][:3]) + ".")
    except cf.ErroConferencia as e:
        erro = True
        msgs.append(str(e))
    try:
        x = cf.ler_extratos(db, ref)
        msgs.append(f"Declarado lido dos extratos em {x['lidos']} empresa(s)"
                    + (f"; sem extrato na pasta: {len(x['sem'])}." if x["sem"] else "."))
    except cf.ErroConferencia as e:
        erro = True
        msgs.append(str(e))
    flash(request, " ".join(msgs), "erro" if erro else "ok")
    return _redir(f"/conferencia-pgdas?ref={ref}")


@router.post("/conferencia-pgdas/declarado")
async def conferencia_declarado(request: Request, db: Session = Depends(get_db), u=Depends(admin)):
    """Lê o valor declarado (RPA e RBT12) — de uma empresa (prestador_id) ou de todas sem valor lido."""
    from .. import conferencia as cf
    form = await request.form()
    ref = form.get("ref", "")
    pid = form.get("prestador_id", "")
    if pid.isdigit():
        alvos = [db.get(models.Prestador, int(pid))]
    else:
        alvos = [x["p"] for x in cf.linhas(db, ref)
                 if x["entregue"] != "NAO_ENTREGUE" and (not x["r"] or x["r"].declarado is None)]
    lidas, falhas = 0, []
    for p in alvos:
        if p is None:
            continue
        motivo = cf.ler_declarado(db, p, ref, u.login)
        if motivo:
            falhas.append(f"{p.codigo_fmt}: {motivo[:80]}")
        else:
            lidas += 1
    auditar(db, request, u, "CONFERENCIA_PGDAS_DECLARADO", "", None, f"ref={ref} lidas={lidas} falhas={len(falhas)}")
    db.commit()
    msg = f"Valor declarado lido em {lidas} empresa(s)."
    if falhas:
        msg += " Sem valor: " + "; ".join(falhas[:5]) + ("…" if len(falhas) > 5 else "")
    flash(request, msg, "erro" if falhas and not lidas else "ok")
    return _redir(f"/conferencia-pgdas?ref={ref}" + (f"#emp-{pid}" if pid.isdigit() else ""))


@router.get("/conferencia-pgdas/extrato/{rid}")
def conferencia_extrato(rid: int, db: Session = Depends(get_db), u=Depends(usuario_atual)):
    from pathlib import Path
    from fastapi.responses import FileResponse
    from .. import conferencia as cf
    r = db.get(models.ConferenciaPGDAS, rid)
    if not r or not r.arquivo_extrato:
        return _redir("/conferencia-pgdas")
    arq = Path(r.arquivo_extrato)
    try:
        arq.resolve().relative_to(cf.pasta_extratos(db).resolve())
    except ValueError:
        return _redir("/conferencia-pgdas")
    if not arq.exists():
        return _redir(f"/conferencia-pgdas?ref={r.referencia}")
    return FileResponse(arq, media_type="application/pdf", filename=arq.name, content_disposition_type="inline")


@router.get("/conferencia-pgdas/diagnostico-nfstock")
def conferencia_diag(cnpj: str, ref: str = "", db: Session = Depends(get_db), u=Depends(admin)):
    """Mostra as respostas brutas do NF-Stock para cada formato de consulta (ajuste da integração)."""
    import re as _re
    from fastapi.responses import JSONResponse
    from .. import conferencia as cf
    from .rotinas import mes_anterior
    ref = ref or mes_anterior(date.today())
    return JSONResponse(cf.diagnostico(db, _re.sub(r"\D", "", cnpj), ref))


@router.get("/conferencia-pgdas/nfstock-acesso")
def conferencia_nfstock_acesso(db: Session = Depends(get_db), u=Depends(admin)):
    """Lista as empresas ativas do ERP que o NF-Stock não reconhece (403) — para cadastrar lá."""
    import requests
    from fastapi.responses import JSONResponse
    from .. import conferencia as cf, nfstock
    tk = nfstock.token(db)
    crm = (ler_config(db, nfstock.CHAVE_CRM) or "592534").zfill(6)
    out = []
    for p in db.scalars(select(models.Prestador).where(models.Prestador.ativo.is_(True)).order_by(*models.ordem_prestador_sql())):
        try:
            r = requests.get(f"{cf.EXPORTACAO}/api/v1/{crm}/{p.cnpj}/documentos/0/novos", params={"Tamanho": 1, "Pagina": 1},
                             headers={"Authorization": f"Bearer {tk}"}, timeout=30)
            st = r.status_code
        except requests.RequestException as e:
            st = str(e)[:60]
        if st != 200:
            out.append(dict(codigo=p.codigo_fmt, nome=p.razao_social, cnpj=p.cnpj, regime=p.regime,
                            municipio=getattr(p, "municipio_nome", "") or "", status=st))
    return JSONResponse(out)


@router.get("/conferencia-pgdas/diagnostico-tipos")
def conferencia_diag_tipos(cnpj: str, ref: str = "", db: Session = Depends(get_db), u=Depends(admin)):
    """Testa os endpoints de cada tipo de documento no NF-Stock (MS-Exportação)."""
    import re as _re
    import requests
    from fastapi.responses import JSONResponse
    from .. import conferencia as cf, nfstock
    from .rotinas import mes_anterior
    ref = ref or mes_anterior(date.today())
    ini, fim = cf.periodo(ref)
    tk = nfstock.token(db)
    crm = (ler_config(db, nfstock.CHAVE_CRM) or "592534").zfill(6)
    cnpj = _re.sub(r"\D", "", cnpj)
    out = []
    for doc in ("nfe", "nfce", "nfse", "cte", "cfe", "sat", "nfcom", "nf3e", "nfag", "documentos"):
        for params in ({"Tipo": "Emitidas", "Status": "Autorizada"}, {}):
            p = {"DataInicial": ini.isoformat(), "DataFinal": fim.isoformat(), "Tamanho": 3, "Pagina": 1, **params}
            try:
                r = requests.get(f"{cf.EXPORTACAO}/api/v1/{crm}/{cnpj}/{doc}", params=p, headers={"Authorization": f"Bearer {tk}"}, timeout=40)
                out.append(dict(doc=doc, params=params, status=r.status_code, corpo=(r.text or "")[:900]))
            except requests.RequestException as e:
                out.append(dict(doc=doc, params=params, status=0, corpo=str(e)[:200]))
    return JSONResponse(out)


@router.get("/conferencia-pgdas/empresa/{pid}")
def conferencia_empresa(pid: int, request: Request, ref: str = "", db: Session = Depends(get_db), u=Depends(usuario_atual)):
    from .. import conferencia as cf
    from .rotinas import mes_anterior
    p = db.get(models.Prestador, pid)
    if not p:
        return _redir("/conferencia-pgdas")
    ref = ref if len(ref) == 7 and ref[4] == "-" else mes_anterior(date.today())
    lista = [x["p"].id for x in cf.linhas(db, ref)]
    i = lista.index(pid) if pid in lista else -1
    ant = lista[i - 1] if i > 0 else None
    prox = lista[i + 1] if 0 <= i < len(lista) - 1 else None
    return render(request, "conferencia_empresa.html", p=p, ref=ref, d=cf.detalhe_empresa(db, p, ref),
                  ROTULO=cf.ROTULO_MODELO, ant=ant, prox=prox, e_admin=getattr(u, "perfil", "") == "ADMIN")
