"""Cadastros: prestadores, certificados A1, serviços padrão e tomadores."""
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import models
from .certificados import CertificadoInvalido, ler_pfx
from .db import get_db
from .documentos import formatar_documento, limpar, so_digitos, validar_cnpj, validar_documento
from .seguranca import criptografar
from .web import auditar, decimal_br, flash, render, texto, usuario_atual

router = APIRouter()
TAMANHO_MAX_PFX = 50 * 1024

CAMPOS_ENDERECO = {
    "logradouro": 150, "numero": 20, "complemento": 60, "bairro": 60, "municipio": 80,
}


def _redir(url: str):
    return RedirectResponse(url, status_code=303)


def _prestador_ou_404(db: Session, pid: int) -> models.Prestador | None:
    return db.get(models.Prestador, pid)


# ---- Consulta de CNPJ / CEP (preenchimento automático) -----------------------

@router.get("/api/cnpj/{cnpj}")
async def api_cnpj(cnpj: str, u=Depends(usuario_atual)):
    from .consulta_cnpj import ConsultaErro, consultar_cnpj
    try:
        return JSONResponse(await run_in_threadpool(consultar_cnpj, cnpj))
    except ConsultaErro as e:
        return JSONResponse({"erro": str(e)}, status_code=422)


@router.get("/api/cpf/{cpf}")
async def api_cpf(cpf: str, request: Request, nasc: str = "", forcar: int = 0, db: Session = Depends(get_db), u=Depends(usuario_atual)):
    """Consulta CPF v3 do Serpro (nome e situação na Receita). A Receita exige a data de nascimento.
    O endereço não vem da Receita: se o CPF já estiver cadastrado em alguma empresa do escritório,
    os contatos/endereço de lá completam os campos vazios."""
    from .serpro import apis
    doc = so_digitos(cpf)
    if len(doc) != 11 or not validar_documento(doc):
        return JSONResponse({"erro": "CPF inválido (confira os dígitos)."}, status_code=422)
    if not apis.data_nascimento(nasc):
        return JSONResponse({"erro": "Informe a data de nascimento para consultar o CPF na Receita.",
                             "precisa_nascimento": True}, status_code=422)
    try:
        r = await run_in_threadpool(apis.consultar_cpf, db, doc, nasc, u.login, bool(forcar))
    except apis.ErroAPI as e:
        db.commit()   # guarda o registro da tentativa (custo)
        return JSONResponse({"erro": str(e)}, status_code=422)
    if not r.do_cache:
        auditar(db, request, u, "CONSULTA_CPF", "cpf", None, f"{doc[:3]}***{doc[-2:]} {r.situacao}")
    db.commit()
    dados = {"nome": r.nome, "nome_social": r.nome_social, "situacao": r.situacao, "regular": r.regular,
             "data_nascimento": r.nascimento.isoformat() if r.nascimento else "",
             "fonte": "Receita Federal (Consulta CPF – Serpro" + (", resultado guardado" if r.do_cache else "") + ")"}
    campos = ("email", "telefone", "cep", "logradouro", "numero", "complemento", "bairro",
              "municipio", "uf", "codigo_municipio_ibge", "inscricao_municipal")
    achados = list(db.scalars(select(models.Tomador).where(models.Tomador.documento == doc)
                              .order_by(models.Tomador.atualizado_em.desc())))
    achados += list(db.scalars(select(models.Intermediario).where(models.Intermediario.documento == doc)))
    for a in achados:
        for c in campos:
            v = getattr(a, c, "") or ""
            if v and not dados.get(c):
                dados[c] = v
    if achados:
        dados["complemento_fonte"] = "endereço e contatos do cadastro já existente no escritório"
    return JSONResponse(dados)


@router.get("/api/cep/{cep}")
async def api_cep(cep: str, u=Depends(usuario_atual)):
    from .consulta_cnpj import ConsultaErro, consultar_cep
    try:
        return JSONResponse(await run_in_threadpool(consultar_cep, cep))
    except ConsultaErro as e:
        return JSONResponse({"erro": str(e)}, status_code=422)


# ---- Prestadores ------------------------------------------------------------

MODULOS_EMPRESA = {"usa_nfse": "NFS-e", "usa_receita": "Receita Federal", "usa_econtador": "e-Contador", "usa_cnd": "Certidões"}


@router.get("/prestadores/sair")
def prestador_sair(request: Request, u=Depends(usuario_atual)):
    request.session.pop("prestador_atual", None)
    return _redir("/prestadores")


@router.get("/prestadores")
def prestadores(request: Request, q: str = "", modo: str = "", situacao: str = "", modulo: str = "",
                db: Session = Depends(get_db), u=Depends(usuario_atual)):
    stmt = select(models.Prestador).order_by(models.Prestador.razao_social)
    if modo in models.MODOS:
        stmt = stmt.where(models.Prestador.modo == modo, models.Prestador.usa_nfse.is_(True))
    if modulo in MODULOS_EMPRESA:
        stmt = stmt.where(getattr(models.Prestador, modulo).is_(True))
    if situacao == "ativos":
        stmt = stmt.where(models.Prestador.ativo.is_(True))
    elif situacao == "inativos":
        stmt = stmt.where(models.Prestador.ativo.is_(False))
    if q:
        termo = f"%{q.strip()}%"
        stmt = stmt.where(or_(models.Prestador.razao_social.ilike(termo),
                              models.Prestador.nome_fantasia.ilike(termo),
                              models.Prestador.cnpj.ilike(f"%{limpar(q)}%" if limpar(q) else termo),
                              models.Prestador.codigo_interno == q.strip()))
    def chave(p):  # código interno numérico primeiro, em ordem numérica
        c = (p.codigo_interno or "").strip()
        return (0, int(c), "") if c.isdigit() else (1 if c else 2, 0, c or p.razao_social)
    lista = sorted(db.scalars(stmt).all(), key=chave)
    return render(request, "prestadores.html", lista=lista, q=q, modo=modo, situacao=situacao, modulo=modulo,
                  MODULOS_EMPRESA=MODULOS_EMPRESA)


@router.post("/prestadores/{pid}/procuracao")
async def prestador_procuracao(pid: int, request: Request, db: Session = Depends(get_db), u=Depends(usuario_atual)):
    p = db.get(models.Prestador, pid)
    if not p:
        return _redir("/prestadores")
    form = await request.form()
    p.importar_por_procuracao = form.get("valor") == "1"
    auditar(db, request, u, "PROCURACAO_PRESTADOR", "prestador", pid, f"procuracao={p.importar_por_procuracao}")
    db.commit()
    flash(request, "Emissão, cancelamento e importação agora usam o certificado do escritório (procuração)."
          if p.importar_por_procuracao else "Emissão, cancelamento e importação agora usam o certificado da própria empresa.")
    return _redir(f"/prestadores/{pid}?aba=config#importacao")


@router.post("/prestadores/lote")
async def prestadores_lote(request: Request, db: Session = Depends(get_db), u=Depends(usuario_atual)):
    """Ações em várias empresas de uma vez (selecionadas na lista)."""
    form = await request.form()
    ids = [int(x) for x in form.getlist("ids") if str(x).isdigit()]
    acao = form.get("acao", "")
    acoes = {
        "procuracao_sim": ("importar_por_procuracao", True, "certificado do escritório (procuração) ativado"),
        "procuracao_nao": ("importar_por_procuracao", False, "certificado da própria empresa"),
        "aderente_sim": ("municipio_aderente", True, "município aderente ao Emissor Nacional: sim"),
        "aderente_nao": ("municipio_aderente", False, "município aderente ao Emissor Nacional: não"),
        "auto_sim": ("importar_automaticamente", True, "importação automática ativada"),
        "auto_nao": ("importar_automaticamente", False, "importação automática desativada"),
        "email_sim": ("enviar_email_automatico", True, "envio automático da nota por e-mail ligado"),
        "email_nao": ("enviar_email_automatico", False, "envio automático da nota por e-mail desligado"),
        "modo_importacao": ("modo", "IMPORTACAO", "modo: somente importação"),
        "modo_emissao": ("modo", "EMISSAO", "modo: emissão pelo escritório"),
        "nfse_sim": ("usa_nfse", True, "módulo NFS-e ligado"), "nfse_nao": ("usa_nfse", False, "módulo NFS-e desligado"),
        "receita_sim": ("usa_receita", True, "módulo Receita Federal ligado"),
        "receita_nao": ("usa_receita", False, "módulo Receita Federal desligado"),
        "econtador_sim": ("usa_econtador", True, "integração e-Contador ligada"),
        "econtador_nao": ("usa_econtador", False, "integração e-Contador desligada"),
        "cnd_sim": ("usa_cnd", True, "certidões ligadas"), "cnd_nao": ("usa_cnd", False, "certidões desligadas"),
        "ativar": ("ativo", True, "empresa ativada"), "inativar": ("ativo", False, "empresa inativada"),
    }
    if ids and acao == "excluir":
        if u.perfil != "ADMIN":
            flash(request, "Somente o administrador pode excluir empresas.", "erro")
            return _redir("/prestadores")
        return _excluir_empresas(request, db, u, ids)
    if not ids or acao not in acoes:
        flash(request, "Selecione as empresas e a ação.", "erro")
        return _redir("/prestadores")
    campo, valor, texto_ = acoes[acao]
    alterados = 0
    for p in db.scalars(select(models.Prestador).where(models.Prestador.id.in_(ids))):
        setattr(p, campo, valor)
        alterados += 1
    auditar(db, request, u, "LOTE_PRESTADORES", "prestador", None, f"{acao} em {alterados} empresas: {ids[:200]}")
    db.commit()
    flash(request, f"{alterados} empresa(s) atualizada(s): {texto_}.")
    return _redir("/prestadores")


def _uso_empresa(db: Session, pid: int) -> list[str]:
    """Tabelas que têm registros desta empresa (notas, certificados, consultas…). Vazia = pode excluir."""
    from sqlalchemy import func as _f
    usos = []
    for tabela in models.Base.metadata.sorted_tables:
        if tabela.name == "prestadores" or "prestador_id" not in tabela.c:
            continue
        n = db.scalar(select(_f.count()).select_from(tabela).where(tabela.c.prestador_id == pid))
        if n:
            usos.append(f"{n} em {tabela.name}")
    return usos


def _excluir_empresas(request: Request, db: Session, u, ids: list[int]):
    from .configuracoes import gravar_config, ler_config
    excluidas, mantidas = [], []
    ignorar = set(filter(None, (ler_config(db, "empresas_excluidas") or "").split(",")))
    for p in db.scalars(select(models.Prestador).where(models.Prestador.id.in_(ids))).all():
        usos = _uso_empresa(db, p.id)
        if usos:
            mantidas.append(p.razao_social)
            continue
        ignorar.update([p.cnpj] + p.lista_cnpjs_adicionais)
        excluidas.append(f"{p.codigo_interno} {p.razao_social} ({p.cnpj})".strip())
        db.delete(p)
    gravar_config(db, "empresas_excluidas", ",".join(sorted(ignorar)))
    auditar(db, request, u, "EXCLUIR_EMPRESAS", "prestador", None, f"excluídas={excluidas}; mantidas={mantidas}")
    db.commit()
    if excluidas:
        flash(request, f"{len(excluidas)} empresa(s) excluída(s). Elas não voltam numa nova importação do Bot Arquivador.")
    if mantidas:
        flash(request, f"{len(mantidas)} empresa(s) não excluída(s) porque já têm notas, certificados ou consultas "
                       f"(use Inativar): {', '.join(mantidas[:10])}{'…' if len(mantidas) > 10 else ''}", "erro")
    return _redir("/prestadores")


@router.get("/prestadores/novo")
def prestador_novo(request: Request, u=Depends(usuario_atual)):
    return render(request, "prestador_form.html", p=models.Prestador(
        regime="SIMPLES", modo="EMISSAO", ambiente="PRODUCAO", serie_dps="00001", proximo_numero_dps=1,
        proximo_numero_dps_homologacao=1, reg_ap_trib_sn=1, reg_esp_trib=0, cst_piscofins="08", ativo=True,
        importar_automaticamente=True, importar_por_procuracao=False, enviar_email_automatico=False,
        municipio_aderente=True, usa_nfse=True, usa_receita=True, usa_econtador=True, usa_cnd=True))


@router.get("/prestadores/{pid}/editar")
def prestador_editar(pid: int, request: Request, db: Session = Depends(get_db), u=Depends(usuario_atual)):
    p = _prestador_ou_404(db, pid)
    if not p:
        return _redir("/prestadores")
    return render(request, "prestador_form.html", p=p)


def _preencher_prestador(p: models.Prestador, form) -> str | None:
    cnpj = limpar(form.get("cnpj"))
    if not validar_cnpj(cnpj):
        return "CNPJ inválido."
    if not texto(form, "razao_social"):
        return "Informe a razão social."
    ibge = so_digitos(form.get("codigo_municipio_ibge"))
    if ibge and len(ibge) != 7:
        return "O código IBGE do município tem 7 dígitos."
    serie = so_digitos(form.get("serie_dps")) or "1"
    if len(serie) > 5:
        return "A série da DPS tem no máximo 5 dígitos."
    try:
        proximo = int(form.get("proximo_numero_dps") or 1)
        if proximo < 1:
            raise ValueError
    except ValueError:
        return "Próximo número da DPS inválido."
    try:
        p_tot = decimal_br(form.get("p_tot_trib_sn"))
        if not (0 <= p_tot <= 100):
            raise ValueError("% de tributos do Simples deve ficar entre 0 e 100.")
    except ValueError as e:
        return str(e)
    cst = so_digitos(form.get("cst_piscofins")) or "08"
    if len(cst) != 2:
        return "CST do PIS/COFINS tem 2 dígitos (ex.: 08)."

    p.cnpj = cnpj
    p.codigo_interno = texto(form, "codigo_interno", 20)
    p.razao_social = texto(form, "razao_social", 150).upper()
    p.nome_fantasia = texto(form, "nome_fantasia", 150)
    p.inscricao_municipal = texto(form, "inscricao_municipal", 20)
    p.regime = form.get("regime") if form.get("regime") in models.REGIMES else "SIMPLES"
    p.modo = form.get("modo") if form.get("modo") in models.MODOS else "EMISSAO"
    p.ambiente = "PRODUCAO"  # todos os municípios já estão no ambiente nacional de produção
    p.cep = so_digitos(form.get("cep"))[:8]
    for campo, tam in CAMPOS_ENDERECO.items():
        setattr(p, campo, texto(form, campo, tam))
    p.codigo_municipio_ibge = ibge
    p.uf = texto(form, "uf", 2).upper()
    p.email = texto(form, "email", 120)
    p.telefone = texto(form, "telefone", 20)
    p.serie_dps = serie.zfill(5)
    p.proximo_numero_dps = proximo
    p.p_tot_trib_sn = p_tot
    p.cst_piscofins = cst
    ap = int(form.get("reg_ap_trib_sn") or 1)
    p.reg_ap_trib_sn = ap if ap in models.REG_AP_TRIB_SN else 1
    p.reg_esp_trib = int(so_digitos(form.get("reg_esp_trib")) or 0)
    p.municipio_aderente = form.get("municipio_aderente") == "1"
    p.autorizacao_uso_certificado = form.get("autorizacao_uso_certificado") == "1"
    p.enviar_email_automatico = form.get("enviar_email_automatico") == "1"
    p.importar_automaticamente = form.get("importar_automaticamente") == "1"
    p.importar_por_procuracao = form.get("importar_por_procuracao") == "1"
    p.observacoes = texto(form, "observacoes")
    p.ativo = form.get("ativo") == "1"
    for m in ("usa_nfse", "usa_receita", "usa_econtador", "usa_cnd"):
        if form.get("form_modulos") == "1":   # o formulário da empresa sempre envia os módulos
            setattr(p, m, form.get(m) == "1")
        elif getattr(p, m) is None:
            setattr(p, m, True)
    p.pasta_drive = texto(form, "pasta_drive", 300)
    p.id_econtador = texto(form, "id_econtador", 30)
    extras = [so_digitos(x) for x in (form.get("cnpjs_adicionais") or "").split(",")]
    extras = [x for x in extras if x and x != cnpj]
    if any(len(x) not in (11, 14) for x in extras):
        return "Outros CNPJs/CPFs: informe números com 11 (CPF) ou 14 (CNPJ) dígitos, separados por vírgula."
    p.cnpjs_adicionais = ", ".join(dict.fromkeys(extras))[:300]
    p.apelidos = "\n".join(dict.fromkeys(l.strip().upper() for l in (form.get("apelidos") or "").splitlines() if l.strip()))
    return None


@router.post("/prestadores/salvar")
async def prestador_salvar(request: Request, db: Session = Depends(get_db), u=Depends(usuario_atual)):
    form = await request.form()
    pid = int(form.get("id") or 0)
    p = _prestador_ou_404(db, pid) if pid else models.Prestador()
    if p is None:
        return _redir("/prestadores")
    antes = {"serie": p.serie_dps, "numero": p.proximo_numero_dps,
             "numero_homolog": p.proximo_numero_dps_homologacao, "ambiente": p.ambiente} if pid else None
    erro = _preencher_prestador(p, form)
    if erro:
        return render(request, "prestador_form.html", p=p, mensagens=[["erro", erro]])
    if not pid:
        db.add(p)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        flash(request, "Já existe uma empresa com esse CNPJ.", "erro")
        return _redir("/prestadores")
    det = f"cnpj={p.cnpj}"
    depois = {"serie": p.serie_dps, "numero": p.proximo_numero_dps,
              "numero_homolog": p.proximo_numero_dps_homologacao, "ambiente": p.ambiente}
    if antes and antes != depois:
        det += f" | numeração/ambiente alterados: {antes} -> {depois}"
    auditar(db, request, u, "EDITAR_PRESTADOR" if pid else "CRIAR_PRESTADOR", "prestador", p.id, det)
    db.commit()
    flash(request, "Empresa salva.")
    return _redir(f"/prestadores/{p.id}?aba=config")


@router.get("/prestadores/{pid}")
def prestador_detalhe(pid: int, request: Request, aba: str = "notas", competencia: str = "", papel: str = "",
                      situacao: str = "", q: str = "", de: str = "", ate: str = "",
                      db: Session = Depends(get_db), u=Depends(usuario_atual)):
    p = _prestador_ou_404(db, pid)
    if not p:
        return _redir("/prestadores")
    aba = aba if aba in ("notas", "cadastros", "config") else "notas"
    if aba in ("notas", "cadastros") and not p.usa_nfse:
        if p.usa_receita:
            return _redir(f"/prestadores/{pid}/receita")
        aba = "config"
    ctx = dict(p=p, aba=aba)
    if aba == "notas":
        from .nfse.rotas_notas import _filtrar
        from urllib.parse import urlencode
        f = dict(competencia=competencia, papel=papel if papel in models.PAPEIS_NOTA else "", situacao=situacao, q=q, de=de, ate=ate)
        stmt = _filtrar(prestador_id=pid, **f)
        notas = db.scalars(stmt.limit(300)).all()
        ctx.update(lista=notas, f=f, qs=urlencode({k: v for k, v in f.items() if v}),
                   total=sum((n.valor_servicos for n in notas if n.situacao == "AUTORIZADA" and n.papel == "PRESTADO"), Decimal("0")))
    elif aba == "cadastros":
        ctx["tomadores"] = db.scalars(select(models.Tomador).where(models.Tomador.prestador_id == pid)
                                      .order_by(models.Tomador.nome)).all()
        obras = db.scalars(select(models.Obra).where(models.Obra.prestador_id == pid, models.Obra.ativo.is_(True))).all()
        ctx["obras_por_tomador"] = {t: sum(1 for o in obras if o.tomador_id == t) for t in {o.tomador_id for o in obras}}
        from .nfse import tabelas
        cods = db.scalars(select(models.CodigoEmpresa).where(models.CodigoEmpresa.prestador_id == pid)
                          .order_by(models.CodigoEmpresa.codigo)).all()
        desc_ct = {c.codigo: c.descricao for c in db.scalars(select(models.CodigoTributacao).where(
            models.CodigoTributacao.codigo.in_([c.codigo for c in cods if c.tipo == "CTRIBNAC"])))}
        desc_nbs = {c.codigo: c.descricao for c in db.scalars(select(models.CodigoNBS).where(
            models.CodigoNBS.codigo.in_([c.codigo for c in cods if c.tipo == "NBS"])))}
        obra_sub = tabelas.subitens_obra(db)
        ctx["cod_ctribnac"] = [dict(id=c.id, codigo=tabelas.fmt_ctribnac(c.codigo), descricao=desc_ct.get(c.codigo, ""),
                                    obra=c.codigo[:4] in obra_sub) for c in cods if c.tipo == "CTRIBNAC"]
        ctx["cod_nbs"] = [dict(id=c.id, codigo=tabelas.fmt_nbs(c.codigo), descricao=desc_nbs.get(c.codigo, ""))
                          for c in cods if c.tipo == "NBS"]
        ctx["intermediarios"] = db.scalars(select(models.Intermediario).where(models.Intermediario.prestador_id == pid)
                                           .order_by(models.Intermediario.nome)).all()
        ctx["tabela_oficial"] = db.scalar(select(models.CodigoTributacao).where(models.CodigoTributacao.oficial.is_(True)).limit(1)) is not None
    else:
        from .nfse.emissao import descricao_certificado
        ctx["ctrl"] = db.scalar(select(models.ControleNSU).where(models.ControleNSU.prestador_id == pid))
        ctx["dono_cert"], ctx["problema_cert"] = descricao_certificado(db, p) if p.importar_por_procuracao else (None, "")
    return render(request, "prestador_detalhe.html", **ctx)


# ---- Certificados A1 --------------------------------------------------------

@router.post("/prestadores/{pid}/certificado")
async def certificado_enviar(pid: int, request: Request, db: Session = Depends(get_db), u=Depends(usuario_atual)):
    p = _prestador_ou_404(db, pid)
    if not p:
        return _redir("/prestadores")
    form = await request.form()
    arquivo = form.get("arquivo")
    senha = form.get("senha", "")
    if not arquivo or not getattr(arquivo, "filename", ""):
        flash(request, "Selecione o arquivo .pfx do certificado.", "erro")
        return _redir(f"/prestadores/{pid}?aba=config")
    conteudo = await arquivo.read(TAMANHO_MAX_PFX + 1)
    if len(conteudo) > TAMANHO_MAX_PFX:
        flash(request, "Arquivo grande demais para um certificado A1.", "erro")
        return _redir(f"/prestadores/{pid}?aba=config")
    try:
        dados = ler_pfx(conteudo, senha)
    except CertificadoInvalido as e:
        flash(request, str(e), "erro")
        return _redir(f"/prestadores/{pid}?aba=config")

    # O CNPJ do certificado precisa ser o do prestador (ou da matriz, mesma raiz de 8 dígitos).
    if dados.cnpj and dados.cnpj[:8] != p.cnpj[:8]:
        flash(request, f"Certificado pertence a {formatar_documento(dados.cnpj)}, não a este prestador. Não foi salvo.", "erro")
        return _redir(f"/prestadores/{pid}?aba=config")
    if dados.validade_fim < datetime.now():
        flash(request, f"Certificado vencido em {dados.validade_fim:%d/%m/%Y}. Não foi salvo.", "erro")
        return _redir(f"/prestadores/{pid}?aba=config")

    for c in p.certificados:
        c.ativo = False
    novo = models.Certificado(
        prestador_id=p.id, arquivo_cripto=criptografar(conteudo), senha_cripto=criptografar(senha.encode()),
        nome_arquivo=arquivo.filename[:200], titular=dados.titular[:200], cnpj_certificado=dados.cnpj,
        numero_serie=dados.numero_serie[:64], emissora=dados.emissora[:200],
        validade_inicio=dados.validade_inicio, validade_fim=dados.validade_fim, ativo=True, enviado_por=u.login,
    )
    db.add(novo)
    db.flush()
    auditar(db, request, u, "ENVIAR_CERTIFICADO", "prestador", p.id,
            f"serie={dados.numero_serie} validade={dados.validade_fim:%d/%m/%Y}")
    db.commit()
    aviso = "" if dados.cnpj else " Atenção: não foi possível ler o CNPJ dentro do certificado; confira o titular."
    flash(request, f"Certificado salvo (válido até {dados.validade_fim:%d/%m/%Y}).{aviso}")
    return _redir(f"/prestadores/{pid}?aba=config")


@router.post("/certificados/{cid}/excluir")
def certificado_excluir(cid: int, request: Request, db: Session = Depends(get_db), u=Depends(usuario_atual)):
    c = db.get(models.Certificado, cid)
    if not c:
        return _redir("/prestadores")
    pid = c.prestador_id
    auditar(db, request, u, "EXCLUIR_CERTIFICADO", "prestador", pid, f"serie={c.numero_serie}")
    db.delete(c)
    db.commit()
    flash(request, "Certificado excluído.")
    return _redir(f"/prestadores/{pid}?aba=config")


# ---- Serviços padrão --------------------------------------------------------

# ---- Códigos de serviço da empresa (cTribNac e NBS) -------------------------

@router.post("/prestadores/{pid}/codigos")
async def codigos_adicionar(pid: int, request: Request, db: Session = Depends(get_db), u=Depends(usuario_atual)):
    from .nfse import tabelas
    p = _prestador_ou_404(db, pid)
    if not p:
        return _redir("/prestadores")
    form = await request.form()
    tipo = "NBS" if form.get("tipo") == "NBS" else "CTRIBNAC"
    codigo = so_digitos(form.get("codigo"))
    tam = 9 if tipo == "NBS" else 6
    if len(codigo) != tam:
        flash(request, f"Código {'NBS' if tipo == 'NBS' else 'de tributação nacional'} deve ter {tam} dígitos.", "erro")
        return _redir(f"/prestadores/{pid}?aba=cadastros#codigos")
    if tipo == "NBS":
        tabelas.gravar_nbs(db, codigo, texto(form, "descricao", 500))
    else:
        tabelas.gravar_ctribnac(db, codigo, texto(form, "descricao", 500))
    novo = tabelas.vincular_codigo(db, pid, tipo, codigo)
    auditar(db, request, u, "CODIGO_EMPRESA", "prestador", pid, f"+{tipo} {codigo}")
    db.commit()
    flash(request, "Código incluído." if novo else "A empresa já tinha esse código.")
    return _redir(f"/prestadores/{pid}?aba=cadastros#codigos")


@router.post("/codigos-empresa/{cid}/excluir")
def codigos_excluir(cid: int, request: Request, db: Session = Depends(get_db), u=Depends(usuario_atual)):
    c = db.get(models.CodigoEmpresa, cid)
    if not c:
        return _redir("/prestadores")
    pid = c.prestador_id
    auditar(db, request, u, "CODIGO_EMPRESA", "prestador", pid, f"-{c.tipo} {c.codigo}")
    db.delete(c)
    db.commit()
    return _redir(f"/prestadores/{pid}?aba=cadastros#codigos")


@router.get("/api/municipios")
def api_municipios(q: str = "", u=Depends(usuario_atual)):
    from .nfse import tabelas
    return JSONResponse(tabelas.buscar_municipios(q))


@router.get("/api/ctribnac")
def api_ctribnac(q: str = "", db: Session = Depends(get_db), u=Depends(usuario_atual)):
    from .nfse import tabelas
    return JSONResponse(tabelas.buscar_ctribnac(db, q))


@router.get("/api/nbs")
def api_nbs(q: str = "", ctribnac: str = "", db: Session = Depends(get_db), u=Depends(usuario_atual)):
    from .nfse import tabelas
    if ctribnac and not q:
        return JSONResponse(tabelas.lista_nbs(db, tabelas.nbs_correlatas(db, ctribnac)))
    return JSONResponse(tabelas.buscar_nbs(db, q))


# ---- Intermediários ---------------------------------------------------------------

@router.get("/prestadores/{pid}/intermediarios/novo")
def intermediario_novo(pid: int, request: Request, volta: str = "", db: Session = Depends(get_db), u=Depends(usuario_atual)):
    p = _prestador_ou_404(db, pid)
    if not p:
        return _redir("/prestadores")
    return render(request, "intermediario_form.html", p=p, i=models.Intermediario(prestador_id=pid, ativo=True), volta=volta)


@router.get("/intermediarios/{iid}/editar")
def intermediario_editar(iid: int, request: Request, db: Session = Depends(get_db), u=Depends(usuario_atual)):
    i = db.get(models.Intermediario, iid)
    if not i:
        return _redir("/prestadores")
    return render(request, "intermediario_form.html", p=db.get(models.Prestador, i.prestador_id), i=i, volta="")


@router.post("/prestadores/{pid}/intermediarios/salvar")
async def intermediario_salvar(pid: int, request: Request, db: Session = Depends(get_db), u=Depends(usuario_atual)):
    p = _prestador_ou_404(db, pid)
    if not p:
        return _redir("/prestadores")
    form = await request.form()
    iid = int(form.get("id") or 0)
    i = db.get(models.Intermediario, iid) if iid else models.Intermediario(prestador_id=pid)
    if i is None or i.prestador_id != pid:
        return _redir(f"/prestadores/{pid}")
    doc = so_digitos(form.get("documento"))
    erro = ""
    if not validar_documento(doc):
        erro = "CPF/CNPJ do intermediário inválido."
    elif not texto(form, "nome"):
        erro = "Informe o nome do intermediário."
    else:
        dup = db.scalar(select(models.Intermediario).where(models.Intermediario.prestador_id == pid,
                                                           models.Intermediario.documento == doc))
        if dup is not None and dup.id != i.id:
            erro = "Este prestador já tem um intermediário com esse CPF/CNPJ."
    i.documento, i.nome = doc, texto(form, "nome", 150)
    for campo, tam in (("inscricao_municipal", 20), ("email", 120), ("telefone", 20), ("logradouro", 150),
                       ("numero", 20), ("complemento", 60), ("bairro", 60), ("municipio", 80), ("uf", 2)):
        setattr(i, campo, texto(form, campo, tam))
    i.cep = so_digitos(form.get("cep"))[:8]
    i.codigo_municipio_ibge = so_digitos(form.get("codigo_municipio_ibge"))[:7]
    i.ativo = form.get("ativo") == "1"
    volta = form.get("volta") or ""
    if erro:
        return render(request, "intermediario_form.html", p=p, i=i, volta=volta, mensagens=[["erro", erro]])
    if not iid:
        db.add(i)
    db.flush()
    auditar(db, request, u, "SALVAR_INTERMEDIARIO", "prestador", pid, f"intermediario={i.id} doc={doc}")
    db.commit()
    flash(request, "Intermediário salvo.")
    if volta.startswith("/notas/") or volta.startswith("/prestadores/"):
        return _redir(volta)
    return _redir(f"/prestadores/{pid}?aba=cadastros#intermediarios")


# ---- Tomadores --------------------------------------------------------------

@router.get("/tomadores")
def tomadores_todos(request: Request, q: str = "", db: Session = Depends(get_db), u=Depends(usuario_atual)):
    stmt = select(models.Tomador).join(models.Prestador).order_by(models.Tomador.nome)
    if q:
        termo = f"%{q.strip()}%"
        stmt = stmt.where(or_(models.Tomador.nome.ilike(termo), models.Tomador.documento == limpar(q),
                              models.Prestador.razao_social.ilike(termo)))
    return render(request, "tomadores.html", lista=db.scalars(stmt.limit(2000)).all(), q=q)


@router.get("/prestadores/{pid}/tomadores/novo")
def tomador_novo(pid: int, request: Request, volta: str = "", doc: str = "", db: Session = Depends(get_db), u=Depends(usuario_atual)):
    p = _prestador_ou_404(db, pid)
    if not p:
        return _redir("/prestadores")
    return render(request, "tomador_form.html", p=p, t=models.Tomador(prestador_id=pid, ativo=True, documento=so_digitos(doc)[:14]),
                  volta=volta)


@router.get("/tomadores/{tid}/editar")
def tomador_editar(tid: int, request: Request, db: Session = Depends(get_db), u=Depends(usuario_atual)):
    t = db.get(models.Tomador, tid)
    if not t:
        return _redir("/prestadores")
    obras = db.scalars(select(models.Obra).where(models.Obra.tomador_id == tid).order_by(models.Obra.id)).all()
    return render(request, "tomador_form.html", p=t.prestador, t=t, obras=obras)


@router.post("/tomadores/{tid}/obras")
async def obra_salvar(tid: int, request: Request, db: Session = Depends(get_db), u=Depends(usuario_atual)):
    t = db.get(models.Tomador, tid)
    if not t:
        return _redir("/prestadores")
    form = await request.form()
    oid = int(form.get("id") or 0)
    o = db.get(models.Obra, oid) if oid else models.Obra(prestador_id=t.prestador_id, tomador_id=tid)
    if o is None or o.tomador_id != tid:
        return _redir(f"/tomadores/{tid}/editar")
    o.descricao = texto(form, "descricao", 120)
    o.cno = limpar(form.get("cno"))[:30]
    o.cib = limpar(form.get("cib"))[:8]
    o.inscricao_imobiliaria = texto(form, "inscricao_imobiliaria", 30)
    o.cep = so_digitos(form.get("cep"))[:8]
    for campo, tam in CAMPOS_ENDERECO.items():
        setattr(o, campo, texto(form, campo, tam))
    o.codigo_municipio_ibge = so_digitos(form.get("codigo_municipio_ibge"))[:7]
    o.uf = texto(form, "uf", 2).upper()
    o.ativo = True
    from .nfse.dps_extra import problemas_obra
    erros = problemas_obra(o)
    if not o.codigo_municipio_ibge or len(o.codigo_municipio_ibge) != 7:
        erros.append("Informe o código IBGE do município da obra (7 dígitos) — ele define o local da prestação.")
    if erros:
        flash(request, " ".join(erros), "erro")
        return _redir(f"/tomadores/{tid}/editar#obras")
    if not oid:
        db.add(o)
    db.flush()
    auditar(db, request, u, "SALVAR_OBRA", "tomador", tid, f"obra={o.id} {o.identificacao}")
    db.commit()
    flash(request, "Obra salva.")
    return _redir(f"/tomadores/{tid}/editar#obras")


@router.post("/obras/{oid}/desativar")
def obra_desativar(oid: int, request: Request, db: Session = Depends(get_db), u=Depends(usuario_atual)):
    o = db.get(models.Obra, oid)
    if not o:
        return _redir("/prestadores")
    o.ativo = not o.ativo
    auditar(db, request, u, "ATIVAR_OBRA" if o.ativo else "DESATIVAR_OBRA", "tomador", o.tomador_id, f"obra={oid}")
    db.commit()
    return _redir(f"/tomadores/{o.tomador_id}/editar#obras")


@router.post("/prestadores/{pid}/tomadores/salvar")
async def tomador_salvar(pid: int, request: Request, db: Session = Depends(get_db), u=Depends(usuario_atual)):
    p = _prestador_ou_404(db, pid)
    if not p:
        return _redir("/prestadores")
    form = await request.form()
    tid = int(form.get("id") or 0)
    t = db.get(models.Tomador, tid) if tid else models.Tomador(prestador_id=pid)
    if t is None or t.prestador_id != pid:
        return _redir(f"/prestadores/{pid}")
    doc = limpar(form.get("documento"))
    erro = None
    if not validar_documento(doc):
        erro = "CPF/CNPJ do tomador inválido."
    elif not texto(form, "nome"):
        erro = "Informe o nome do tomador."
    ibge = so_digitos(form.get("codigo_municipio_ibge"))
    if not erro and ibge and len(ibge) != 7:
        erro = "O código IBGE do município tem 7 dígitos."
    t.documento = doc
    t.nome = texto(form, "nome", 150).upper()
    t.inscricao_municipal = texto(form, "inscricao_municipal", 20)
    t.email = texto(form, "email", 120)
    t.telefone = texto(form, "telefone", 20)
    t.cep = so_digitos(form.get("cep"))[:8]
    for campo, tam in CAMPOS_ENDERECO.items():
        setattr(t, campo, texto(form, campo, tam))
    t.codigo_municipio_ibge = ibge
    t.uf = texto(form, "uf", 2).upper()
    t.ativo = form.get("ativo") == "1"
    from .serpro.apis import data_nascimento
    t.data_nascimento = data_nascimento(form.get("data_nascimento")) if len(doc) == 11 else None
    if erro:
        return render(request, "tomador_form.html", p=p, t=t, volta=form.get("volta") or "", mensagens=[["erro", erro]])
    if not tid:
        db.add(t)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return render(request, "tomador_form.html", p=p, t=t,
                      mensagens=[["erro", "Este prestador já tem um tomador com esse CPF/CNPJ."]])
    auditar(db, request, u, "EDITAR_TOMADOR" if tid else "CRIAR_TOMADOR", "tomador", t.id, f"prestador={pid} doc={doc}")
    db.commit()
    flash(request, "Tomador salvo.")
    volta = form.get("volta") or ""
    if volta.startswith("/notas/") or volta.startswith("/prestadores/"):
        return _redir(volta + ("&" if "?" in volta else "?") + f"tomador_id={t.id}")
    return _redir(f"/prestadores/{pid}?aba=cadastros#tomadores")
