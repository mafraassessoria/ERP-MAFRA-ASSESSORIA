"""Telas de notas fiscais: rascunho, transmissão, consulta, cancelamento e download."""
import json
import os
import tempfile
import zipfile
from datetime import date, datetime
from urllib.parse import urlencode
from decimal import Decimal

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import func, or_, select
from starlette.background import BackgroundTask
from sqlalchemy.orm import Session

from . import emissao, tabelas
from .. import models
from ..configuracoes import ErroEmail, enviar_nota_por_email, smtp_configurado
from ..config import XML_DIR
from ..db import get_db
from ..documentos import limpar, so_digitos
from ..web import auditar, decimal_br, flash, render, texto, usuario_atual

router = APIRouter()
EDITAVEIS = ("RASCUNHO", "REJEITADA")


def _redir(url: str):
    return RedirectResponse(url, status_code=303)


def _filtrar(prestador_id=0, situacao="", competencia="", q="", papel="", de="", ate=""):
    """Consulta das notas conforme os filtros da tela (usada na lista e na exportação)."""
    stmt = select(models.NotaFiscal).where(models.NotaFiscal.ambiente == "PRODUCAO") \
        .order_by(models.NotaFiscal.competencia.desc(), models.NotaFiscal.id.desc())
    if papel in models.PAPEIS_NOTA:
        stmt = stmt.where(models.NotaFiscal.papel == papel)
    if prestador_id:
        stmt = stmt.where(models.NotaFiscal.prestador_id == prestador_id)
    if situacao in models.SITUACOES_NOTA:
        stmt = stmt.where(models.NotaFiscal.situacao == situacao)
    if competencia:
        try:
            ini = datetime.strptime(competencia, "%Y-%m").date()
            fim = date(ini.year + (ini.month == 12), ini.month % 12 + 1, 1)
            stmt = stmt.where(models.NotaFiscal.competencia >= ini, models.NotaFiscal.competencia < fim)
        except ValueError:
            pass
    for valor, operador in ((de, ">="), (ate, "<=")):
        if valor:
            try:
                d = datetime.strptime(valor, "%Y-%m-%d").date()
                stmt = stmt.where(models.NotaFiscal.competencia >= d if operador == ">=" else models.NotaFiscal.competencia <= d)
            except ValueError:
                pass
    if q:
        termo = f"%{q.strip()}%"
        stmt = stmt.where(or_(models.NotaFiscal.tomador_nome.ilike(termo),
                              models.NotaFiscal.numero_nfse == q.strip(),
                              models.NotaFiscal.emitente_nome.ilike(termo),
                              models.NotaFiscal.tomador_documento == limpar(q),
                              models.NotaFiscal.emitente_documento == limpar(q)))
    return stmt


@router.get("/notas")
def notas(request: Request, prestador_id: int = 0, situacao: str = "", competencia: str = "",
          q: str = "", papel: str = "", de: str = "", ate: str = "",
          db: Session = Depends(get_db), u=Depends(usuario_atual)):
    f = dict(prestador_id=prestador_id, situacao=situacao, competencia=competencia, q=q, papel=papel, de=de, ate=ate)
    stmt = _filtrar(**f)
    lista = db.scalars(stmt.limit(500)).all()
    qtd_total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
    prestadores = db.scalars(select(models.Prestador).order_by(*models.ordem_prestador_sql())).all()
    total = sum((n.valor_servicos for n in lista if n.situacao == "AUTORIZADA" and n.papel == "PRESTADO"), Decimal("0"))
    qs = urlencode({k: v for k, v in f.items() if v})
    return render(request, "notas.html", lista=lista, prestadores=prestadores, total=total, f=f,
                  qtd_total=qtd_total, qs=qs)


def _tipos_export(pdf, xml) -> tuple[bool, bool]:
    """Sem nenhuma marcação = os dois (compatível com os links antigos)."""
    com_pdf, com_xml = bool(pdf), bool(xml)
    return (True, True) if not (com_pdf or com_xml) else (com_pdf, com_xml)


def _zip_notas(db, notas_, u, descricao: str, nome_arquivo: str, incluir_pdf: bool = True, incluir_xml: bool = True):
    """ZIP com PDF (DANFSe) e/ou XML (+ XML de cancelamento) das notas."""
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False, dir=str(XML_DIR.parent))
    tmp.close()
    qtd_xml = qtd_pdf = 0
    sem_arquivo, pdf_falhou = [], []
    with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED) as z:
        for n in notas_:
            nome = n.numero_nfse or f"rascunho{n.id}"
            pasta = f"{n.prestador.cnpj}_{_sem_acento(n.prestador.razao_social)[:40]}/{'prestadas' if n.papel == 'PRESTADO' else 'tomadas'}"
            if not n.caminho_xml or not (XML_DIR / n.caminho_xml).exists():
                if n.situacao in ("AUTORIZADA", "CANCELADA"):
                    sem_arquivo.append(f"{n.prestador.razao_social} nº {nome}")
                continue
            xml = XML_DIR / n.caminho_xml
            if incluir_xml:
                z.write(xml, f"{pasta}/{xml.name}")
                qtd_xml += 1
            if incluir_pdf and (not n.caminho_pdf or not (XML_DIR / n.caminho_pdf).exists()):
                try:
                    emissao.gerar_danfse(n)  # notas importadas: gera o PDF a partir do XML
                    db.commit()
                except Exception as e:  # noqa: BLE001
                    pdf_falhou.append(f"{n.prestador.razao_social} nº {nome}: {e}")
            if incluir_pdf and n.caminho_pdf and (XML_DIR / n.caminho_pdf).exists():
                z.write(XML_DIR / n.caminho_pdf, f"{pasta}/{(XML_DIR / n.caminho_pdf).name}")
                qtd_pdf += 1
            if incluir_xml and n.caminho_evento_xml and (XML_DIR / n.caminho_evento_xml).exists():
                ev = XML_DIR / n.caminho_evento_xml
                z.write(ev, f"{pasta}/{ev.name}")
        resumo = [f"Exportação gerada em {datetime.now():%d/%m/%Y %H:%M} por {u.login}",
                  descricao, f"Notas: {len(notas_)} | XML: {qtd_xml} | PDF: {qtd_pdf}"]
        if sem_arquivo:
            resumo += ["", "Notas sem XML guardado:"] + sem_arquivo
        if pdf_falhou:
            resumo += ["", "PDF não gerado:"] + pdf_falhou
        z.writestr("LEIA-ME.txt", "\r\n".join(resumo))
    return FileResponse(tmp.name, media_type="application/zip", filename=nome_arquivo,
                        background=BackgroundTask(os.remove, tmp.name))


@router.get("/notas/exportar.zip")
def notas_exportar(prestador_id: int = 0, situacao: str = "", competencia: str = "",
                   q: str = "", papel: str = "", de: str = "", ate: str = "", pdf: str = "", xml: str = "",
                   db: Session = Depends(get_db), u=Depends(usuario_atual)):
    """ZIP de todas as notas do filtro."""
    f = dict(prestador_id=prestador_id, situacao=situacao, competencia=competencia, q=q, papel=papel, de=de, ate=ate)
    notas_ = db.scalars(_filtrar(**f)).all()
    partes = ["notas"]
    if competencia:
        partes.append(competencia)
    if de or ate:
        partes.append(f"{de or 'inicio'}_a_{ate or 'hoje'}")
    if prestador_id:
        p = db.get(models.Prestador, prestador_id)
        partes.append(p.cnpj if p else str(prestador_id))
    com_pdf, com_xml = _tipos_export(pdf, xml)
    if not (com_pdf and com_xml):
        partes.append("pdf" if com_pdf else "xml")
    return _zip_notas(db, notas_, u, f"Filtros: {', '.join(f'{k}={v}' for k, v in f.items() if v) or 'nenhum'}",
                      "_".join(partes) + ".zip", com_pdf, com_xml)


@router.post("/notas/selecionadas")
async def notas_selecionadas(request: Request, db: Session = Depends(get_db), u=Depends(usuario_atual)):
    """Ações nas notas marcadas: exportar (ZIP), clonar ou transmitir."""
    form = await request.form()
    volta = form.get("volta") or "/notas"
    if not (volta.startswith("/notas") or volta.startswith("/prestadores/")):
        volta = "/notas"
    ids = [int(x) for x in form.getlist("ids") if str(x).isdigit()]
    notas_ = [n for n in (db.get(models.NotaFiscal, i) for i in ids) if n is not None]
    acao = form.get("acao")
    if not notas_:
        flash(request, "Marque pelo menos uma nota.", "erro")
        return _redir(volta)
    if acao == "exportar":
        auditar(db, request, u, "EXPORTAR_NOTAS", "nota", None, f"{len(notas_)} nota(s) selecionada(s)")
        db.commit()
        com_pdf, com_xml = _tipos_export(form.get("pdf"), form.get("xml"))
        sufixo = "" if com_pdf and com_xml else ("_pdf" if com_pdf else "_xml")
        return _zip_notas(db, notas_, u, f"Notas selecionadas: {len(notas_)}",
                          f"notas_selecionadas_{datetime.now():%Y%m%d_%H%M}{sufixo}.zip", com_pdf, com_xml)
    if acao == "clonar":
        criadas, puladas = 0, []
        for o in notas_:
            erro = _pode_clonar(o)
            if erro:
                puladas.append(o.numero_nfse or str(o.id))
                continue
            n = clonar_nota(db, o, u.login)
            auditar(db, request, u, "CLONAR_NOTA", "nota", n.id, f"origem={o.id} nº {o.numero_nfse} (em lote)")
            criadas += 1
        db.commit()
        flash(request, f"{criadas} rascunho(s) criado(s) com competência de hoje. Confira e transmita."
                       + (f" Não clonadas (só notas prestadas de empresa em emissão): {', '.join(puladas)}." if puladas else ""))
        pid = notas_[0].prestador_id
        return _redir(f"/prestadores/{pid}?aba=notas&situacao=RASCUNHO")
    if acao == "transmitir":
        alvo = [n for n in notas_ if n.situacao in EDITAVEIS and n.origem == "EMITIDA"]
        ok, falhas = 0, []
        for n in alvo:
            res = await run_in_threadpool(emissao.emitir, db, n, u.login)
            auditar(db, request, u, "TRANSMITIR_NOTA", "nota", n.id,
                    f"(em lote) ambiente={n.ambiente} dps={n.id_dps} resultado={'OK' if res.ok else 'FALHA'} {res.mensagem[:300]}")
            db.commit()
            if res.ok:
                ok += 1
                await _email_automatico(request, db, u, n, True)
            else:
                falhas.append(f"{n.tomador_nome[:40]}: {res.mensagem[:160]}")
        ignoradas = len(notas_) - len(alvo)
        flash(request, f"{ok} nota(s) autorizada(s)." + (f" {ignoradas} ignorada(s) (já transmitidas ou importadas)." if ignoradas else ""),
              "ok" if not falhas else "erro")
        for f_ in falhas[:10]:
            flash(request, f_, "erro")
        return _redir(volta)
    flash(request, "Ação inválida.", "erro")
    return _redir(volta)


def _sem_acento(txt: str) -> str:
    import re
    import unicodedata
    t = unicodedata.normalize("NFKD", txt or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9]+", "_", t).strip("_")


def _form_nota(request, db, p, n, mensagens=None, tomador_id: int = 0):
    from . import tabelas
    if tomador_id and not n.id:
        n.tomador_id = tomador_id
    tomadores = db.scalars(select(models.Tomador).where(
        models.Tomador.prestador_id == p.id, models.Tomador.ativo.is_(True)).order_by(models.Tomador.nome)).all()
    intermediarios = db.scalars(select(models.Intermediario).where(
        models.Intermediario.prestador_id == p.id, models.Intermediario.ativo.is_(True)).order_by(models.Intermediario.nome)).all()
    obras = db.scalars(select(models.Obra).where(models.Obra.prestador_id == p.id, models.Obra.ativo.is_(True))
                       .order_by(models.Obra.descricao)).all()
    cod_ct = tabelas.codigos_empresa(db, p.id, "CTRIBNAC")
    if n.codigo_tributacao_nacional and n.codigo_tributacao_nacional not in cod_ct:
        cod_ct.append(n.codigo_tributacao_nacional)
    cod_nbs = tabelas.codigos_empresa(db, p.id, "NBS")
    if n.codigo_nbs and n.codigo_nbs not in cod_nbs:
        cod_nbs.append(n.codigo_nbs)
    correl = {c: tabelas.nbs_correlatas(db, c) for c in cod_ct}
    local = n.municipio_prestacao or p.codigo_municipio_ibge
    dados_js = {
        "obraSubitens": sorted(tabelas.subitens_obra(db)),
        "correlacao": correl,
        "obras": [{"id": o.id, "tomador": o.tomador_id, "rotulo": o.rotulo, "mun": o.codigo_municipio_ibge or ""} for o in obras],
        "pTotTribSN": float(p.p_tot_trib_sn or 0),
        "mei": p.regime == "MEI",
    }
    from datetime import date as _date
    aliq_mes = list(db.scalars(select(models.AliquotaSimples).where(
        models.AliquotaSimples.prestador_id == p.id, models.AliquotaSimples.referencia == _date.today().strftime("%Y-%m"))))
    return render(request, "nota_form.html", p=p, n=n, tomadores=tomadores, intermediarios=intermediarios, obras=obras,
                  aliq_mes=[a for a in aliq_mes if a.anexo in ("III", "IV", "V")],
                  ctribnac=tabelas.lista_ctribnac(db, cod_ct), nbs=tabelas.lista_nbs(db, cod_nbs),
                  local_rotulo=tabelas.nome_municipio(local), local=local,
                  municipio_sede=tabelas.nome_municipio(p.codigo_municipio_ibge),
                  dados_js=json.dumps(dados_js), mensagens=mensagens or [],
                  TIPOS_IMUNIDADE=models.TIPOS_IMUNIDADE, TIPOS_RET_PISCOFINS=models.TIPOS_RET_PISCOFINS,
                  REG_ESP=REG_ESP_TRIB, hoje=date.today().isoformat())


REG_ESP_TRIB = {0: "Nenhum", 1: "Ato cooperado (cooperativa)", 2: "Estimativa", 3: "Microempresa municipal",
                4: "Notário ou registrador", 5: "Profissional autônomo", 6: "Sociedade de profissionais"}


@router.get("/prestadores/{pid}/notas/nova")
def nota_nova(pid: int, request: Request, tomador_id: int = 0, db: Session = Depends(get_db), u=Depends(usuario_atual)):
    p = db.get(models.Prestador, pid)
    if not p:
        return _redir("/prestadores")
    if not p.emite_nfse:
        flash(request, "Esta empresa não emite NFS-e pelo sistema (módulo NFS-e desligado ou modo somente importação).", "erro")
        return _redir(f"/prestadores/{pid}")
    n = models.NotaFiscal(prestador_id=pid, competencia=date.today(), tributacao_issqn=1, tipo_retencao_iss=1,
                          tot_trib_tipo="SN", municipio_prestacao=p.codigo_municipio_ibge)
    return _form_nota(request, db, p, n, tomador_id=tomador_id)


@router.get("/notas/{nid}/editar")
def nota_editar(nid: int, request: Request, tomador_id: int = 0, db: Session = Depends(get_db), u=Depends(usuario_atual)):
    n = db.get(models.NotaFiscal, nid)
    if not n or n.situacao not in EDITAVEIS:
        return _redir(f"/notas/{nid}")
    if tomador_id:
        t = db.get(models.Tomador, tomador_id)
        if t and t.prestador_id == n.prestador_id:
            n.tomador_id = t.id
            db.flush()
    resp = _form_nota(request, db, n.prestador, n)
    db.rollback()  # a troca de tomador só vale quando o usuário salvar
    return resp


def _dec_form(form, campo: str, erros: list[str]) -> Decimal:
    try:
        v = decimal_br(form.get(campo))
    except ValueError as e:
        erros.append(str(e))
        return Decimal("0")
    if v < 0:
        erros.append("Valores não podem ser negativos.")
        return Decimal("0")
    return v


def _obra_nova(db, form, pid: int, tomador, erros: list[str]):
    """Obra digitada na própria nota (CNO, CIB ou endereço) — vira cadastro do tomador."""
    tipo = form.get("obra_tipo")
    if tomador is None:
        erros.append("Para informar a obra, escolha antes o tomador.")
        return None
    o = models.Obra(prestador_id=pid, tomador_id=tomador.id, ativo=True,
                    descricao=texto(form, "obra_descricao", 120),
                    inscricao_imobiliaria=texto(form, "obra_insc_imob", 30))
    if tipo == "CNO":
        o.cno = so_digitos(form.get("obra_cno"))[:30]
        if not o.cno:
            erros.append("Informe o código da obra (CNO).")
    elif tipo == "CIB":
        o.cib = texto(form, "obra_cib", 8).upper()
        if len(o.cib) != 8:
            erros.append("O código CIB tem 8 caracteres.")
    else:
        o.cep = so_digitos(form.get("obra_cep"))[:8]
        o.logradouro = texto(form, "obra_logradouro", 150)
        o.numero = texto(form, "obra_numero", 20)
        o.complemento = texto(form, "obra_complemento", 60)
        o.bairro = texto(form, "obra_bairro", 60)
        o.codigo_municipio_ibge = so_digitos(form.get("obra_municipio"))[:7]
        if len(o.cep) != 8 or not o.logradouro or not o.bairro:
            erros.append("Endereço da obra: informe CEP, logradouro e bairro.")
    for x in db.scalars(select(models.Obra).where(models.Obra.prestador_id == pid, models.Obra.tomador_id == tomador.id)):
        if (o.cno and x.cno == o.cno) or (o.cib and x.cib == o.cib):
            return x
    if erros:
        return None
    db.add(o)
    db.flush()
    return o


@router.post("/prestadores/{pid}/notas/salvar")
async def nota_salvar(pid: int, request: Request, db: Session = Depends(get_db), u=Depends(usuario_atual)):
    from . import tabelas
    p = db.get(models.Prestador, pid)
    if not p or not p.emite_nfse:
        return _redir("/prestadores")
    form = await request.form()
    nid = int(form.get("id") or 0)
    n = db.get(models.NotaFiscal, nid) if nid else models.NotaFiscal(prestador_id=pid, origem="EMITIDA",
                                                                       situacao="RASCUNHO", criada_por=u.login,
                                                                       ambiente=p.ambiente)
    if n is None or n.prestador_id != pid or n.situacao not in EDITAVEIS:
        return _redir("/notas")

    erros: list[str] = []
    # --- Pessoas
    tid = int(form.get("tomador_id") or 0)
    tomador = db.get(models.Tomador, tid) if tid else None
    if tid and (tomador is None or tomador.prestador_id != pid):
        erros.append("Tomador inválido.")
        tomador = None
    iid = int(form.get("intermediario_id") or 0)
    interm = db.get(models.Intermediario, iid) if iid else None
    if iid and (interm is None or interm.prestador_id != pid):
        erros.append("Intermediário inválido.")
        interm = None
    try:
        competencia = datetime.strptime(form.get("competencia") or "", "%Y-%m-%d").date()
        if competencia > date.today():
            erros.append("A competência não pode ser futura.")
    except ValueError:
        erros.append("Data de competência inválida.")
        competencia = date.today()

    # --- Serviço
    local = so_digitos(form.get("municipio_prestacao"))
    if local not in tabelas.municipios():
        erros.append("Escolha o município do local da prestação.")
        local = p.codigo_municipio_ibge
    ctn = so_digitos(form.get("codigo_tributacao_nacional_outro") or form.get("codigo_tributacao_nacional"))
    if len(ctn) != 6:
        erros.append("Escolha o código de tributação nacional.")
    nbs = so_digitos(form.get("codigo_nbs_outro") or form.get("codigo_nbs"))
    if nbs and len(nbs) != 9:
        erros.append("O código NBS tem 9 dígitos.")
    from .dps_extra import limpar_texto
    descricao = limpar_texto(texto(form, "descricao", 2000))
    if not descricao:
        erros.append("Informe a descrição do serviço.")
    trib = 1
    tipo_imun = None
    if form.get("caso_especial") == "sim":
        trib = int(form.get("tributacao_issqn") or 0)
        if trib not in (2, 3, 4):
            erros.append("Informe se é imunidade, exportação ou não incidência.")
            trib = 1
        if trib == 2:
            try:
                tipo_imun = int(form.get("tipo_imunidade"))
                if tipo_imun not in models.TIPOS_IMUNIDADE:
                    raise ValueError
            except (TypeError, ValueError):
                erros.append("Informe o tipo de imunidade.")
                tipo_imun = None

    # --- Obra
    obra = None
    if tabelas.exige_obra(db, ctn):
        if form.get("obra_modo") == "nova":
            obra = _obra_nova(db, form, pid, tomador, erros)
        else:
            oid = int(form.get("obra_id") or 0)
            obra = db.get(models.Obra, oid) if oid else None
            if obra is not None and (obra.prestador_id != pid or (tomador and obra.tomador_id and obra.tomador_id != tomador.id)):
                erros.append("A obra escolhida não pertence a este tomador.")
                obra = None
            if obra is None:
                erros.append("Serviço de obra: escolha a obra ou informe os dados dela.")

    # --- Valores
    valor = _dec_form(form, "valor", erros)
    if valor <= 0:
        erros.append("Informe o valor do serviço.")
    n.valor_recebido_intermediario = _dec_form(form, "valor_recebido_intermediario", erros) if interm else Decimal("0")
    n.desconto_incondicionado = _dec_form(form, "desconto_incondicionado", erros)
    n.desconto_condicionado = _dec_form(form, "desconto_condicionado", erros)
    ret = 1
    aliq = Decimal("0")
    if form.get("iss_retido") == "sim":
        ret = 3 if form.get("retido_por") == "INTERMEDIARIO" else 2
        if ret == 3 and interm is None:
            erros.append("ISS retido pelo intermediário: escolha o intermediário na etapa Pessoas.")
        if ret == 2 and tomador is None:
            erros.append("ISS retido pelo tomador: escolha o tomador.")
        aliq = _dec_form(form, "aliquota_iss", erros)
        if not (emissao.ALIQ_ISS_MIN <= aliq <= emissao.ALIQ_ISS_MAX):
            erros.append("Alíquota do ISS retido: informe entre 1,80% e 5,00%.")
    n.ded_tipo, n.ded_valor = "", Decimal("0")
    if obra is not None and form.get("tem_deducao") == "sim":
        n.ded_tipo = form.get("ded_tipo") if form.get("ded_tipo") in ("PERCENTUAL", "VALOR") else ""
        n.ded_valor = _dec_form(form, "ded_valor", erros)
        if not n.ded_tipo or n.ded_valor <= 0:
            erros.append("Redução da base de cálculo: escolha o tipo e informe o valor.")
    n.tipo_ret_piscofins = form.get("tipo_ret_piscofins") if form.get("tipo_ret_piscofins") in models.TIPOS_RET_PISCOFINS else ""
    for campo in ("ret_inss", "ret_irrf", "ret_csll"):
        setattr(n, campo, Decimal("0") if p.regime == "MEI" else _dec_form(form, campo, erros))
    n.tot_trib_tipo = form.get("tot_trib_tipo") if form.get("tot_trib_tipo") in models.TOT_TRIB_TIPOS else "SN"
    for campo in ("tot_trib_fed", "tot_trib_est", "tot_trib_mun"):
        setattr(n, campo, _dec_form(form, campo, erros) if n.tot_trib_tipo != "SN" else Decimal("0"))
    if n.tot_trib_tipo == "PERCENTUAL" and any(getattr(n, c) >= 100 for c in ("tot_trib_fed", "tot_trib_est", "tot_trib_mun")):
        erros.append("Percentuais aproximados dos tributos devem ser menores que 100%.")

    n.tomador_id = tomador.id if tomador else None
    n.intermediario_id = interm.id if interm else None
    n.competencia = competencia
    n.municipio_prestacao = local
    n.codigo_tributacao_nacional = ctn
    n.codigo_tributacao_municipal = so_digitos(form.get("codigo_tributacao_municipal"))[:3]
    n.codigo_nbs = nbs
    n.descricao_servico = descricao
    n.info_complementares = limpar_texto(texto(form, "info_complementares", 2000))
    n.tributacao_issqn = trib
    n.tipo_imunidade = tipo_imun
    n.obra_id = obra.id if obra else None
    n.valor_servicos = valor
    n.tipo_retencao_iss = ret
    n.aliquota_iss = aliq
    n.servico_id = None
    n.tomador_documento = tomador.documento if tomador else ""
    n.tomador_nome = tomador.nome if tomador else "CONSUMIDOR NÃO IDENTIFICADO"
    if erros:
        db.rollback() if not nid else None
        return _form_nota(request, db, p, n, [["erro", e] for e in dict.fromkeys(erros)])
    # Códigos novos usados na nota entram na lista da empresa
    if len(ctn) == 6:
        tabelas.gravar_ctribnac(db, ctn, "")
        tabelas.vincular_codigo(db, pid, "CTRIBNAC", ctn)
    if nbs:
        tabelas.gravar_nbs(db, nbs, "")
        tabelas.vincular_codigo(db, pid, "NBS", nbs)
    if n.situacao == "REJEITADA":
        n.situacao = "RASCUNHO"
    if not nid:
        db.add(n)
    db.flush()
    auditar(db, request, u, "SALVAR_NOTA", "nota", n.id, f"prestador={pid} valor={valor} tomador={n.tomador_documento}")
    db.commit()
    if form.get("acao") == "transmitir":
        return await _transmitir(request, db, u, n)
    flash(request, "Rascunho salvo. Confira os dados e clique em Transmitir.")
    return _redir(f"/notas/{n.id}")


@router.get("/notas/{nid}")
def nota_detalhe(nid: int, request: Request, db: Session = Depends(get_db), u=Depends(usuario_atual)):
    n = db.get(models.NotaFiscal, nid)
    if not n:
        return _redir("/notas")
    pendencias = emissao.conferir(db, n) if n.situacao in ("RASCUNHO", "REJEITADA", "PROCESSANDO") else []
    email_sugerido = n.tomador.email if n.tomador and n.tomador.email else ""
    return render(request, "nota_detalhe.html", n=n, p=n.prestador, pendencias=pendencias,
                  email_sugerido=email_sugerido, smtp_ok=smtp_configurado(db),
                  MOTIVOS=models.MOTIVOS_CANCELAMENTO, TRIB=models.TRIBUTACAO_ISSQN, RET=models.RETENCAO_ISS,
                  TIPOS_IMUNIDADE=models.TIPOS_IMUNIDADE, TIPOS_RET_PISCOFINS=models.TIPOS_RET_PISCOFINS,
                  local_rotulo=tabelas.nome_municipio(n.municipio_prestacao),
                  desc_servico=(tabelas.fmt_ctribnac(n.codigo_tributacao_nacional) + " - " +
                                tabelas.descricao_ctribnac(db, n.codigo_tributacao_nacional)).strip(" -"))


async def _transmitir(request, db, u, n: models.NotaFiscal):
    nid = n.id
    res = await run_in_threadpool(emissao.emitir, db, n, u.login)
    auditar(db, request, u, "TRANSMITIR_NOTA", "nota", n.id,
            f"ambiente={n.ambiente} dps={n.id_dps} resultado={'OK' if res.ok else 'FALHA'} {res.mensagem[:300]}")
    db.commit()
    flash(request, res.mensagem, "ok" if res.ok else "erro")
    await _email_automatico(request, db, u, n, res.ok)
    return _redir(f"/notas/{nid}")


async def _email_automatico(request, db, u, n: models.NotaFiscal, autorizada: bool) -> None:
    if autorizada and n.prestador.enviar_email_automatico and n.tomador and n.tomador.email and not n.email_enviado_em:
        try:
            await run_in_threadpool(enviar_nota_por_email, db, n, [n.tomador.email])
            auditar(db, request, u, "EMAIL_NOTA", "nota", n.id, f"automático para {n.tomador.email}")
            db.commit()
            flash(request, f"Nota enviada automaticamente para {n.tomador.email}.")
        except ErroEmail as e:
            db.rollback()
            flash(request, f"Nota autorizada, mas o e-mail automático falhou: {e}", "erro")


@router.post("/notas/{nid}/transmitir")
async def nota_transmitir(nid: int, request: Request, db: Session = Depends(get_db), u=Depends(usuario_atual)):
    n = db.get(models.NotaFiscal, nid)
    if not n:
        return _redir("/notas")
    form = await request.form()
    return await _transmitir(request, db, u, n)


@router.post("/notas/{nid}/cancelar")
async def nota_cancelar(nid: int, request: Request, db: Session = Depends(get_db), u=Depends(usuario_atual)):
    n = db.get(models.NotaFiscal, nid)
    if not n:
        return _redir("/notas")
    form = await request.form()
    try:
        motivo = int(form.get("motivo") or 0)
    except ValueError:
        motivo = 0
    res = await run_in_threadpool(emissao.cancelar, db, n, u.login, motivo, form.get("justificativa", ""))
    auditar(db, request, u, "CANCELAR_NOTA", "nota", n.id,
            f"chave={n.chave_acesso} resultado={'OK' if res.ok else 'FALHA'} {res.mensagem[:300]}")
    db.commit()
    flash(request, res.mensagem, "ok" if res.ok else "erro")
    return _redir(f"/notas/{nid}")


@router.post("/notas/{nid}/email")
async def nota_email(nid: int, request: Request, db: Session = Depends(get_db), u=Depends(usuario_atual)):
    n = db.get(models.NotaFiscal, nid)
    if not n:
        return _redir("/notas")
    form = await request.form()
    para = (form.get("para") or "").replace(";", ",").split(",")
    try:
        await run_in_threadpool(enviar_nota_por_email, db, n, para)
        auditar(db, request, u, "EMAIL_NOTA", "nota", n.id, f"para {n.email_enviado_para}")
        db.commit()
        flash(request, f"E-mail enviado para {n.email_enviado_para}.")
    except ErroEmail as e:
        db.rollback()
        flash(request, str(e), "erro")
    except Exception as e:  # noqa: BLE001
        db.rollback()
        flash(request, f"Falha ao preparar o e-mail: {e}", "erro")
    return _redir(f"/notas/{nid}")


def clonar_nota(db: Session, origem: models.NotaFiscal, usuario: str) -> models.NotaFiscal:
    """Rascunho igual à nota (emitida aqui ou importada do ADN), com competência de hoje."""
    from .cadastro_auto import obra_da_nota, tomador_da_nota
    from .nfse_xml import extrair
    d = None
    if origem.caminho_xml and (XML_DIR / origem.caminho_xml).exists():
        d = extrair((XML_DIR / origem.caminho_xml).read_text(encoding="utf-8"))
    tomador = origem.tomador
    if tomador is None and d:
        tomador = tomador_da_nota(db, origem.prestador_id, d)
    obra = origem.obra
    if obra is None and d:
        obra = obra_da_nota(db, origem.prestador_id, tomador, d)
    p = origem.prestador
    n = models.NotaFiscal(
        prestador_id=p.id, origem="EMITIDA", papel="PRESTADO", situacao="RASCUNHO", ambiente=p.ambiente,
        criada_por=usuario, competencia=date.today(),
        tomador_id=tomador.id if tomador else None, tomador_documento=tomador.documento if tomador else "",
        tomador_nome=tomador.nome if tomador else (origem.tomador_nome or "CONSUMIDOR NÃO IDENTIFICADO"),
        obra_id=obra.id if obra else None,
        valor_servicos=origem.valor_servicos, descricao_servico=origem.descricao_servico,
        info_complementares=origem.info_complementares or "",
        codigo_tributacao_nacional=origem.codigo_tributacao_nacional, codigo_tributacao_municipal=origem.codigo_tributacao_municipal or "",
        codigo_nbs=origem.codigo_nbs or "", municipio_prestacao=origem.municipio_prestacao or p.codigo_municipio_ibge,
        tributacao_issqn=origem.tributacao_issqn or 1, tipo_retencao_iss=origem.tipo_retencao_iss or 1,
        tipo_imunidade=origem.tipo_imunidade, intermediario_id=origem.intermediario_id,
        aliquota_iss=origem.aliquota_iss or 0, ded_tipo=origem.ded_tipo or "", ded_valor=origem.ded_valor or 0,
        ret_inss=origem.ret_inss or 0, ret_irrf=origem.ret_irrf or 0, ret_csll=origem.ret_csll or 0,
        tipo_ret_piscofins=origem.tipo_ret_piscofins or "",
        desconto_incondicionado=origem.desconto_incondicionado or 0, desconto_condicionado=origem.desconto_condicionado or 0,
        valor_recebido_intermediario=origem.valor_recebido_intermediario or 0,
        tot_trib_tipo=origem.tot_trib_tipo or "SN", tot_trib_fed=origem.tot_trib_fed or 0,
        tot_trib_est=origem.tot_trib_est or 0, tot_trib_mun=origem.tot_trib_mun or 0,
    )
    db.add(n)
    db.flush()
    return n


def _pode_clonar(origem: models.NotaFiscal | None) -> str:
    if not origem or origem.papel != "PRESTADO":
        return "Só é possível clonar notas prestadas pela empresa."
    if not origem.prestador.emite_nfse:
        return ("Esta empresa está no modo 'somente importação'. Mude para 'Emissão pelo escritório' "
                "(Configurações da empresa) para emitir por aqui.")
    return ""


@router.post("/notas/{nid}/clonar")
def nota_clonar(nid: int, request: Request, db: Session = Depends(get_db), u=Depends(usuario_atual)):
    origem = db.get(models.NotaFiscal, nid)
    erro = _pode_clonar(origem)
    if erro:
        flash(request, erro, "erro")
        return _redir(f"/notas/{nid}")
    n = clonar_nota(db, origem, u.login)
    auditar(db, request, u, "CLONAR_NOTA", "nota", n.id, f"origem={nid} nº {origem.numero_nfse}")
    db.commit()
    flash(request, f"Rascunho criado a partir da nota nº {origem.numero_nfse or origem.id}. Ajuste o que for preciso "
                   "(valor, competência, descrição) e salve.")
    return _redir(f"/notas/{n.id}/editar")


@router.post("/notas/{nid}/excluir")
def nota_excluir(nid: int, request: Request, db: Session = Depends(get_db), u=Depends(usuario_atual)):
    n = db.get(models.NotaFiscal, nid)
    if not n or n.situacao not in EDITAVEIS:
        flash(request, "Só rascunhos e notas rejeitadas podem ser excluídos.", "erro")
        return _redir(f"/notas/{nid}")
    pid = n.prestador_id
    auditar(db, request, u, "EXCLUIR_NOTA", "nota", nid, f"prestador={pid} dps={n.id_dps}")
    db.delete(n)
    db.commit()
    flash(request, "Nota excluída.")
    return _redir(f"/notas?prestador_id={pid}")


def _arquivo(n: models.NotaFiscal, campo: str):
    rel = getattr(n, campo) or ""
    if not rel:
        return None
    caminho = (XML_DIR / rel).resolve()
    if XML_DIR.resolve() not in caminho.parents or not caminho.exists():
        return None
    return caminho


@router.get("/notas/{nid}/arquivo/{tipo}")
def nota_arquivo(nid: int, tipo: str, request: Request, db: Session = Depends(get_db), u=Depends(usuario_atual)):
    n = db.get(models.NotaFiscal, nid)
    campo = {"xml": "caminho_xml", "pdf": "caminho_pdf", "cancelamento": "caminho_evento_xml"}.get(tipo)
    caminho = _arquivo(n, campo) if n and campo else None
    if not caminho and tipo == "pdf" and n and _arquivo(n, "caminho_xml"):
        try:  # PDF ainda não gerado (nota importada ou refeito após atualização)
            emissao.gerar_danfse(n)
            db.commit()
            caminho = _arquivo(n, campo)
        except Exception as e:  # noqa: BLE001
            flash(request, f"Não foi possível gerar o DANFSe: {e}", "erro")
            return _redir(f"/notas/{nid}")
    if not caminho:
        flash(request, "Arquivo não encontrado.", "erro")
        return _redir(f"/notas/{nid}")
    tipo_mime = "application/pdf" if tipo == "pdf" else "application/xml"
    return FileResponse(caminho, media_type=tipo_mime, filename=caminho.name,
                        content_disposition_type="inline" if tipo == "pdf" else "attachment")


@router.post("/notas/{nid}/gerar-pdf")
def nota_gerar_pdf(nid: int, request: Request, db: Session = Depends(get_db), u=Depends(usuario_atual)):
    n = db.get(models.NotaFiscal, nid)
    if n and n.caminho_xml:
        try:
            emissao.gerar_danfse(n)
            db.commit()
            flash(request, "DANFSe gerado.")
        except Exception as e:  # noqa: BLE001
            flash(request, f"Não foi possível gerar o DANFSe: {e}", "erro")
    return _redir(f"/notas/{nid}")


# ---- Emissão em lote por planilha --------------------------------------------------

def _form_lote(request, db, p, previa=None, token="", params=None, mensagens=None):
    from . import tabelas
    params = params or {}
    cod_ct = tabelas.codigos_empresa(db, p.id, "CTRIBNAC")
    cod_nbs = tabelas.codigos_empresa(db, p.id, "NBS")
    local = params.get("municipio_prestacao") or p.codigo_municipio_ibge
    return render(request, "nota_lote.html", p=p, previa=previa, token=token, params=params,
                  ctribnac=tabelas.lista_ctribnac(db, cod_ct), nbs=tabelas.lista_nbs(db, cod_nbs),
                  local=local, local_rotulo=tabelas.nome_municipio(local), hoje=date.today().isoformat(),
                  mensagens=mensagens or [])


@router.get("/notas/lote/modelo.xlsx")
def lote_modelo(u=Depends(usuario_atual)):
    from fastapi.responses import Response
    from .lote import modelo_xlsx
    return Response(modelo_xlsx(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": 'attachment; filename="modelo_notas_em_lote.xlsx"'})


@router.get("/prestadores/{pid}/notas/lote")
def lote_form(pid: int, request: Request, db: Session = Depends(get_db), u=Depends(usuario_atual)):
    p = db.get(models.Prestador, pid)
    if not p or not p.emite_nfse:
        flash(request, "Só empresas no modo 'emissão pelo escritório' emitem em lote.", "erro")
        return _redir(f"/prestadores/{pid}" if p else "/prestadores")
    if not p.usa_lote:
        flash(request, "Emissão em lote desligada para esta empresa. Para ligar: Dados → Editar → Emissão em lote.", "erro")
        return _redir(f"/prestadores/{pid}?aba=notas")
    return _form_lote(request, db, p)


@router.post("/prestadores/{pid}/notas/lote/previa")
async def lote_previa(pid: int, request: Request, db: Session = Depends(get_db), u=Depends(usuario_atual)):
    from . import lote, tabelas
    p = db.get(models.Prestador, pid)
    if not p or not p.emite_nfse or not p.usa_lote:
        return _redir(f"/prestadores/{pid}" if p else "/prestadores")
    form = await request.form()
    params = {
        "codigo_tributacao_nacional": so_digitos(form.get("codigo_tributacao_nacional")),
        "codigo_nbs": so_digitos(form.get("codigo_nbs")),
        "municipio_prestacao": so_digitos(form.get("municipio_prestacao")),
        "competencia": form.get("competencia") or "",
        "info_complementares": texto(form, "info_complementares", 2000),
    }
    erros = []
    if len(params["codigo_tributacao_nacional"]) != 6:
        erros.append("Escolha o código de tributação nacional do lote.")
    if tabelas.exige_obra(db, params["codigo_tributacao_nacional"]):
        erros.append("Serviço de obra não pode ser emitido em lote (cada nota exige a obra).")
    if params["municipio_prestacao"] not in tabelas.municipios():
        erros.append("Escolha o município do local da prestação.")
    try:
        comp_padrao = datetime.strptime(params["competencia"], "%Y-%m-%d").date() if params["competencia"] else None
        if comp_padrao and comp_padrao > date.today():
            erros.append("A competência padrão não pode ser futura.")
    except ValueError:
        comp_padrao = None
        erros.append("Competência padrão inválida.")
    arquivo = form.get("arquivo")
    if not arquivo or not getattr(arquivo, "filename", ""):
        erros.append("Selecione a planilha (.xlsx ou .csv).")
    elif not arquivo.filename.lower().endswith((".xlsx", ".csv")):
        erros.append("A planilha deve ser .xlsx ou .csv.")
    if erros:
        return _form_lote(request, db, p, params=params, mensagens=[["erro", e] for e in erros])
    linhas, problemas = lote.ler(await arquivo.read(10 * 1024 * 1024), arquivo.filename)
    if problemas:
        return _form_lote(request, db, p, params=params, mensagens=[["erro", e] for e in problemas])
    for it in linhas:
        if it["data"] is None:
            if comp_padrao:
                it["data"] = comp_padrao
            else:
                it["erros"].append("sem data (informe a competência padrão)")
    validas = [it for it in linhas if not it["erros"]]
    token = lote.salvar_previa({"pid": pid, "params": params, "linhas": [dict(it, data=it["data"].isoformat() if it["data"] else None,
                                                                                   valor=str(it["valor"]) if it["valor"] is not None else None)
                                                                           for it in validas]})
    previa = {"linhas": linhas, "validas": len(validas), "com_erro": len(linhas) - len(validas),
              "total": sum((it["valor"] for it in validas), Decimal("0")), "arquivo": arquivo.filename}
    return _form_lote(request, db, p, previa=previa, token=token, params=params)


@router.post("/prestadores/{pid}/notas/lote/confirmar")
async def lote_confirmar(pid: int, request: Request, db: Session = Depends(get_db), u=Depends(usuario_atual)):
    from . import lote, tabelas
    p = db.get(models.Prestador, pid)
    form = await request.form()
    dados = lote.abrir_previa(form.get("token"))
    if p is not None and not p.usa_lote:
        return _redir(f"/prestadores/{pid}")
    if not p or not dados or dados["pid"] != pid:
        flash(request, "A prévia expirou. Envie a planilha de novo.", "erro")
        return _redir(f"/prestadores/{pid}/notas/lote")
    transmitir = form.get("acao") == "transmitir"
    prm = dados["params"]
    nome_lote = f"LOTE-{datetime.now():%Y%m%d-%H%M%S}"
    tabelas.vincular_codigo(db, pid, "CTRIBNAC", prm["codigo_tributacao_nacional"])
    if prm.get("codigo_nbs"):
        tabelas.vincular_codigo(db, pid, "NBS", prm["codigo_nbs"])
    criadas = []
    for it in dados["linhas"]:
        t = db.scalar(select(models.Tomador).where(models.Tomador.prestador_id == pid, models.Tomador.documento == it["cpf"]))
        if t is None:
            t = models.Tomador(prestador_id=pid, documento=it["cpf"], nome=it["nome"], email=it["email"] or "", ativo=True)
            db.add(t)
            db.flush()
        elif it["email"] and t.email != it["email"]:
            t.email = it["email"]
        n = models.NotaFiscal(
            prestador_id=pid, origem="EMITIDA", papel="PRESTADO", situacao="RASCUNHO", ambiente=p.ambiente,
            criada_por=u.login, lote=nome_lote, competencia=date.fromisoformat(it["data"]),
            tomador_id=t.id, tomador_documento=t.documento, tomador_nome=t.nome,
            valor_servicos=Decimal(it["valor"]), descricao_servico=it["historico"],
            info_complementares=prm.get("info_complementares") or "",
            codigo_tributacao_nacional=prm["codigo_tributacao_nacional"], codigo_nbs=prm.get("codigo_nbs") or "",
            municipio_prestacao=prm["municipio_prestacao"], tributacao_issqn=1, tipo_retencao_iss=1, tot_trib_tipo="SN")
        db.add(n)
        db.flush()
        criadas.append(n)
    auditar(db, request, u, "LOTE_NOTAS", "prestador", pid, f"{nome_lote}: {len(criadas)} rascunho(s)")
    db.commit()
    lote.apagar_previa(form.get("token"))
    if not transmitir:
        flash(request, f"{len(criadas)} rascunho(s) criado(s) ({nome_lote}). Marque-os e use 'Transmitir selecionadas'.")
        return _redir(f"/prestadores/{pid}?aba=notas&situacao=RASCUNHO")
    ok, falhas, emails = 0, [], 0
    enviar = form.get("enviar_email") == "1"
    for n in criadas:
        res = await run_in_threadpool(emissao.emitir, db, n, u.login)
        auditar(db, request, u, "TRANSMITIR_NOTA", "nota", n.id,
                f"({nome_lote}) dps={n.id_dps} resultado={'OK' if res.ok else 'FALHA'} {res.mensagem[:300]}")
        db.commit()
        if not res.ok:
            falhas.append(f"{n.tomador_nome[:40]}: {res.mensagem[:160]}")
            continue
        ok += 1
        if enviar and n.tomador and n.tomador.email:
            try:
                await run_in_threadpool(enviar_nota_por_email, db, n, [n.tomador.email])
                auditar(db, request, u, "EMAIL_NOTA", "nota", n.id, f"({nome_lote}) para {n.tomador.email}")
                db.commit()
                emails += 1
            except ErroEmail as e:
                db.rollback()
                falhas.append(f"{n.tomador_nome[:40]}: autorizada, mas o e-mail falhou ({e})")
    flash(request, f"{nome_lote}: {ok} de {len(criadas)} nota(s) autorizada(s)" + (f", {emails} e-mail(s) enviado(s)." if enviar else "."),
          "ok" if not falhas else "erro")
    for f_ in falhas[:15]:
        flash(request, f_, "erro")
    return _redir(f"/prestadores/{pid}?aba=notas")
