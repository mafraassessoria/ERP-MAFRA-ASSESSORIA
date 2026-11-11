"""Aplicação web do ERP MAFRA ASSESSORIA (M & R Assessoria Contábil) — NFS-e, Receita Federal, e-Contador e certidões."""
import logging
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from . import models
from .config import BACKUP_DIR, DIAS_AVISO_CERTIFICADO, HTTPS_ONLY, SESSAO_SECRET
from .db import criar_tabelas, get_db
from .rotas_cadastros import router as cadastros_router
from .nfse.rotas_notas import router as notas_router
from .rotas_gerais import router as gerais_router
from .serpro.rotas import router as receita_router
from .econtador.rotas import router as econtador_router
from .certidoes.rotas import router as certidoes_router
from .monitor.rotas import router as monitor_router
from .guias.rotas import router as guias_router
from .honorarios.rotas import router as honorarios_router
from .seguranca import (
    conferir_senha, criptografar, descriptografar, falhas_recentes, gerar_hash_senha, login_bloqueado, qr_svg,
    senha_forte, totp_conferir, totp_novo_segredo, totp_uri,
    EMAIL_CODIGO_INTERVALO, EMAIL_CODIGO_MINUTOS, EMAIL_CODIGO_TENTATIVAS, email_codigo_novo, email_mascarado,
)
from .web import (
    PrecisaLogin, SemPermissao, admin, auditar, flash, ip_de, render, texto, usuario_atual,
)

app = FastAPI(title="ERP MAFRA ASSESSORIA", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(
    SessionMiddleware, secret_key=SESSAO_SECRET, session_cookie="nfse_sessao",
    max_age=10 * 60 * 60, same_site="strict", https_only=HTTPS_ONLY,
)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
app.include_router(cadastros_router)
app.include_router(notas_router)
app.include_router(gerais_router)
app.include_router(receita_router)
app.include_router(econtador_router)
app.include_router(certidoes_router)
app.include_router(monitor_router)
app.include_router(guias_router)
app.include_router(honorarios_router)


@app.on_event("startup")
def _inicio():
    criar_tabelas()
    _ajustes_unicos()
    _limpar_demonstracao()
    _reler_sitfis()
    _ajuste_texto_parcelamentos()
    from .agendador import iniciar as iniciar_agendador
    iniciar_agendador()


def _limpar_demonstracao():
    """Com o Integra Contador em produção, nada do ambiente de demonstração (dados fictícios) fica no sistema."""
    from .db import SessionLocal
    from .serpro import servico as sv
    with SessionLocal() as db:
        try:
            if sv.modo(db) == "producao":
                sv.limpar_demonstracao_e_registrar(db)
        except Exception:  # noqa: BLE001
            db.rollback()
            logging.getLogger("emissor").exception("limpeza da demonstração")


def _reler_sitfis():
    """Uma vez: relê os relatórios de situação fiscal já baixados com o leitor por seções (sem nova consulta)."""
    from .db import SessionLocal
    from .monitor.rotinas import gravar_sitfis
    chave = "ajuste_2026_11_releitura_sitfis_v5"
    with SessionLocal() as db:
        if db.get(models.Configuracao, chave) is not None:
            return
        try:
            n = 0
            for it in db.query(models.MonitorItem).filter(models.MonitorItem.item == "SITFIS",
                                                          models.MonitorItem.consulta_id.is_not(None)).all():
                c = db.get(models.ConsultaReceita, it.consulta_id)
                if c is None or not c.ok or not c.lista_arquivos:
                    continue
                quando = it.atualizado_em
                gravar_sitfis(db, it.prestador, c).atualizado_em = quando
                n += 1
            db.add(models.Configuracao(chave=chave, valor=datetime.now().isoformat()))
            db.add(models.Auditoria(usuario_login="sistema", acao="AJUSTE_RELEITURA_SITFIS", entidade="monitor",
                                    detalhes=f"{n} relatório(s) relidos", data_hora=datetime.now()))
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
            logging.getLogger("emissor").exception("releitura SITFIS")


def _ajuste_texto_parcelamentos():
    """Uma vez: resumos antigos dos parcelamentos ("em andamento") passam a dizer "ativo(s)"."""
    from .db import SessionLocal
    chave = "ajuste_2026_11_parcelamentos_ativos"
    with SessionLocal() as db:
        if db.get(models.Configuracao, chave) is not None:
            return
        for m in db.query(models.MonitorItem).filter(models.MonitorItem.item == "PARCELAMENTO").all():
            m.resumo = (m.resumo or "").replace(" em andamento de ", " ativo(s) de ")
        db.add(models.Configuracao(chave=chave, valor=datetime.now().isoformat()))
        db.commit()


def _ajustes_unicos():
    """Ajustes de dados que rodam uma única vez, na primeira subida de uma versão."""
    from .db import SessionLocal
    db = SessionLocal()
    try:
        chave = "ajuste_2026_11_municipios_aderentes"
        if db.get(models.Configuracao, chave) is None:
            qtd = db.query(models.Prestador).filter(models.Prestador.municipio_aderente.is_not(True)).update(
                {models.Prestador.municipio_aderente: True}, synchronize_session=False)
            db.add(models.Configuracao(chave=chave, valor=datetime.now().isoformat()))
            db.add(models.Auditoria(usuario_login="sistema", acao="AJUSTE_MUNICIPIOS_ADERENTES", entidade="prestador",
                                    detalhes=f"{qtd} prestador(es) marcados como município aderente ao Emissor Nacional",
                                    data_hora=datetime.now()))
            db.commit()
        chave = "ajuste_2026_11_tomadores_e_retencoes"
        if db.get(models.Configuracao, chave) is None:
            from .config import XML_DIR
            from .nfse.importacao import completar_nota_importada
            qtd = 0
            for n in db.scalars(select(models.NotaFiscal).where(models.NotaFiscal.origem == "IMPORTADA")):
                if n.caminho_xml and (XML_DIR / n.caminho_xml).exists():
                    try:
                        completar_nota_importada(db, n, (XML_DIR / n.caminho_xml).read_text(encoding="utf-8"))
                        qtd += 1
                    except Exception:  # noqa: BLE001 — uma nota ruim não impede as demais
                        pass
            tomadores = db.scalar(select(func.count(models.Tomador.id)))
            db.add(models.Configuracao(chave=chave, valor=datetime.now().isoformat()))
            db.add(models.Auditoria(usuario_login="sistema", acao="AJUSTE_TOMADORES_RETENCOES", entidade="nota",
                                    detalhes=f"{qtd} nota(s) importada(s) atualizadas com retenções; tomadores cadastrados agora: {tomadores}",
                                    data_hora=datetime.now()))
            db.commit()
        chave = "ajuste_2026_11_danfse_canhoto"
        if db.get(models.Configuracao, chave) is None:
            # PDFs feitos antes desta versão saíram sem o canhoto. As notas emitidas aqui são refeitas
            # agora; as importadas são refeitas quando o PDF for aberto, exportado ou enviado.
            from .config import XML_DIR
            from .nfse.emissao import gerar_danfse
            refeitas = limpas = 0
            for n in db.scalars(select(models.NotaFiscal).where(models.NotaFiscal.caminho_pdf != "")):
                if n.origem == "EMITIDA" and n.caminho_xml and (XML_DIR / n.caminho_xml).exists():
                    try:
                        gerar_danfse(n)
                        refeitas += 1
                        continue
                    except Exception:  # noqa: BLE001
                        pass
                n.caminho_pdf = ""
                limpas += 1
            db.add(models.Configuracao(chave=chave, valor=datetime.now().isoformat()))
            db.add(models.Auditoria(usuario_login="sistema", acao="AJUSTE_DANFSE_CANHOTO", entidade="nota",
                                    detalhes=f"DANFSe refeito com canhoto: {refeitas} emitida(s) agora, {limpas} sob demanda",
                                    data_hora=datetime.now()))
            db.commit()
        # Tabelas oficiais de serviços (cTribNac, NBS e correlação) que acompanham o sistema
        from .nfse import tabelas
        try:
            q = tabelas.carregar_tabelas_embutidas(db)
            if q:
                db.add(models.Auditoria(usuario_login="sistema", acao="TABELAS_SERVICOS", entidade="configuracao",
                                        detalhes=f"Tabelas oficiais carregadas: {q}", data_hora=datetime.now()))
                db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
            import logging
            logging.getLogger("emissor").exception("falha ao carregar as tabelas de serviços")
        chave = "ajuste_2026_11_somente_producao"
        if db.get(models.Configuracao, chave) is None:
            # Todos os municípios já estão no ambiente nacional de produção: homologação sai do sistema.
            qtd = db.query(models.Prestador).filter(models.Prestador.ambiente != "PRODUCAO").update(
                {models.Prestador.ambiente: "PRODUCAO"}, synchronize_session=False)
            rasc = db.query(models.NotaFiscal).filter(models.NotaFiscal.ambiente != "PRODUCAO",
                                                      models.NotaFiscal.situacao.in_(("RASCUNHO", "REJEITADA"))).update(
                {models.NotaFiscal.ambiente: "PRODUCAO"}, synchronize_session=False)
            db.add(models.Configuracao(chave=chave, valor=datetime.now().isoformat()))
            db.add(models.Auditoria(usuario_login="sistema", acao="AJUSTE_SOMENTE_PRODUCAO", entidade="prestador",
                                    detalhes=f"{qtd} empresa(s) passadas para produção; {rasc} rascunho(s) de homologação "
                                             "passam a ser de produção. Notas de teste já emitidas ficam guardadas, fora das listas.",
                                    data_hora=datetime.now()))
            db.commit()
        chave = "ajuste_2026_11_codigos_empresa"
        if db.get(models.Configuracao, chave) is None:
            # Os serviços padrão deixam de existir: os códigos deles (e os das notas) viram a lista de cada empresa.
            q = tabelas.semear_do_historico(db)
            db.add(models.Configuracao(chave=chave, valor=datetime.now().isoformat()))
            db.add(models.Auditoria(usuario_login="sistema", acao="AJUSTE_CODIGOS_EMPRESA", entidade="prestador",
                                    detalhes=f"Códigos de serviço vinculados às empresas a partir dos serviços padrão e das notas: {q['vinculos']}",
                                    data_hora=datetime.now()))
            db.commit()
    finally:
        db.close()


@app.middleware("http")
async def cabecalhos_seguranca(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "same-origin"
    return resp


@app.exception_handler(Exception)
async def _erro_inesperado(request: Request, exc: Exception):
    """Em vez de "Internal Server Error", mostra o erro e grava o detalhe no log."""
    import html
    import logging
    import traceback
    logging.getLogger("emissor").error("Erro em %s %s\n%s", request.method, request.url.path,
                                       "".join(traceback.format_exception(exc)))
    dica = ""
    if isinstance(exc, ModuleNotFoundError):
        dica = "<p><b>Componente não instalado.</b> Execute o <code>ATUALIZAR_SISTEMA.bat</code> no servidor.</p>"
    corpo = (f"<!doctype html><meta charset='utf-8'><link rel='stylesheet' href='/static/estilo.css'>"
             f"<main><div class='cartao'><h1>Ocorreu um erro</h1>{dica}"
             f"<div class='msg erro'>{html.escape(type(exc).__name__)}: {html.escape(str(exc))[:800]}</div>"
             f"<p class='sutil'>Detalhes técnicos em <code>logs\\servidor.log</code>. Página: {html.escape(request.url.path)}</p>"
             f"<p><a class='botao' href='javascript:history.back()'>Voltar</a></p></div></main>")
    from fastapi.responses import HTMLResponse
    return HTMLResponse(corpo, status_code=500)


@app.exception_handler(PrecisaLogin)
async def _precisa_login(request: Request, _):
    return RedirectResponse("/login", status_code=303)


@app.exception_handler(SemPermissao)
async def _sem_permissao(request: Request, _):
    flash(request, "Apenas administradores podem acessar essa área.", "erro")
    return RedirectResponse("/", status_code=303)


# ---- Login ------------------------------------------------------------------

def _existe_usuario(db: Session) -> bool:
    return db.scalar(select(func.count(models.Usuario.id))) > 0


@app.get("/login")
def login_form(request: Request, db: Session = Depends(get_db)):
    if not _existe_usuario(db):
        return RedirectResponse("/primeiro-acesso", status_code=303)
    return render(request, "login.html")


def _entrar(request: Request, db: Session, u: models.Usuario) -> RedirectResponse:
    request.session.clear()
    request.session["uid"] = u.id
    auditar(db, request, u, "LOGIN", "usuario", u.id)
    db.commit()
    return RedirectResponse("/", status_code=303)


def _falhou(request: Request, db: Session, login_: str, codigo: bool = False) -> None:
    auditar(db, request, None, "LOGIN_FALHOU", "usuario", None, f"login={login_}" + (" codigo" if codigo else ""))
    db.commit()


@app.post("/login")
async def login(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    login_ = texto(form, "login").lower()
    if login_bloqueado(db, login_):
        flash(request, "Muitas tentativas erradas. Aguarde 15 minutos.", "erro")
        return RedirectResponse("/login", status_code=303)
    u = db.scalar(select(models.Usuario).where(models.Usuario.login == login_))
    if not u or not u.ativo or not conferir_senha(form.get("senha", ""), u.senha_hash):
        _falhou(request, db, login_)
        flash(request, "Usuário ou senha incorretos.", "erro")
        return RedirectResponse("/login", status_code=303)
    if u.tem_2fa:   # senha certa: falta o código do aplicativo
        request.session.clear()
        request.session["pre_uid"] = u.id
        return RedirectResponse("/login/codigo", status_code=303)
    return _entrar(request, db, u)


@app.get("/login/codigo")
def login_codigo_form(request: Request, db: Session = Depends(get_db)):
    uid = request.session.get("pre_uid")
    u = db.get(models.Usuario, uid) if uid else None
    if not u:
        return RedirectResponse("/login", status_code=303)
    from .configuracoes import smtp_configurado
    pode_email = bool(u.email_2fa) and smtp_configurado(db)
    enviado = bool(u.email_codigo_hash and u.email_codigo_expira and u.email_codigo_expira > datetime.now())
    return render(request, "login_codigo.html", pode_email=pode_email, email_mask=email_mascarado(u.email_2fa),
                  email_enviado=enviado, minutos=EMAIL_CODIGO_MINUTOS)


@app.post("/login/codigo/email")
def login_codigo_email(request: Request, db: Session = Depends(get_db)):
    """Sem o celular: manda um código de uso único para o e-mail cadastrado do usuário (vale 10 minutos)."""
    from .configuracoes import ErroEmail, enviar_email, smtp_configurado
    uid = request.session.get("pre_uid")
    u = db.get(models.Usuario, uid) if uid else None
    if not u or not u.ativo or not u.tem_2fa:
        request.session.clear()
        return RedirectResponse("/login", status_code=303)
    if login_bloqueado(db, u.login):
        request.session.clear()
        flash(request, "Muitas tentativas erradas. Aguarde 15 minutos.", "erro")
        return RedirectResponse("/login", status_code=303)
    if not u.email_2fa or not smtp_configurado(db):
        flash(request, "Código por e-mail não disponível para este usuário.", "erro")
        return RedirectResponse("/login/codigo", status_code=303)
    agora = datetime.now()
    if u.email_codigo_enviado and (agora - u.email_codigo_enviado).total_seconds() < EMAIL_CODIGO_INTERVALO:
        flash(request, "Um código acabou de ser enviado. Confira o e-mail (e a pasta de spam) ou aguarde 1 minuto para pedir outro.", "aviso")
        return RedirectResponse("/login/codigo", status_code=303)
    codigo = email_codigo_novo()
    try:
        enviar_email(db, [u.email_2fa], "Código de acesso ao ERP MAFRA ASSESSORIA",
                     f"Seu código de acesso ao ERP MAFRA ASSESSORIA é: {codigo}\n\n"
                     f"Ele vale por {EMAIL_CODIGO_MINUTOS} minutos e só pode ser usado uma vez.\n"
                     f"Pedido em {agora:%d/%m/%Y %H:%M} para o usuário {u.login}, de {ip_de(request)}.\n\n"
                     "Se não foi você, troque sua senha assim que possível.", sem_copia=True)
    except ErroEmail as e:
        flash(request, f"Não foi possível enviar o e-mail: {e}", "erro")
        return RedirectResponse("/login/codigo", status_code=303)
    u.email_codigo_hash = gerar_hash_senha(codigo)
    u.email_codigo_expira = agora + timedelta(minutes=EMAIL_CODIGO_MINUTOS)
    u.email_codigo_tentativas = 0
    u.email_codigo_enviado = agora
    auditar(db, request, u, "2FA_CODIGO_EMAIL", "usuario", u.id, email_mascarado(u.email_2fa))
    db.commit()
    flash(request, f"Código enviado para {email_mascarado(u.email_2fa)}. Vale por {EMAIL_CODIGO_MINUTOS} minutos.")
    return RedirectResponse("/login/codigo", status_code=303)


def _conferir_codigo_email(db: Session, u: models.Usuario, codigo: str) -> bool:
    codigo = "".join(ch for ch in (codigo or "") if ch.isdigit())
    if not (u.email_codigo_hash and u.email_codigo_expira) or len(codigo) != 6:
        return False
    if u.email_codigo_expira < datetime.now() or u.email_codigo_tentativas >= EMAIL_CODIGO_TENTATIVAS:
        u.email_codigo_hash, u.email_codigo_expira = "", None
        return False
    if conferir_senha(codigo, u.email_codigo_hash):
        u.email_codigo_hash, u.email_codigo_expira, u.email_codigo_tentativas = "", None, 0   # uso único
        return True
    u.email_codigo_tentativas += 1
    return False


@app.post("/login/codigo")
async def login_codigo(request: Request, db: Session = Depends(get_db)):
    uid = request.session.get("pre_uid")
    u = db.get(models.Usuario, uid) if uid else None
    if not u or not u.ativo or not u.tem_2fa:
        request.session.clear()
        return RedirectResponse("/login", status_code=303)
    if login_bloqueado(db, u.login):
        request.session.clear()
        flash(request, "Muitas tentativas erradas. Aguarde 15 minutos.", "erro")
        return RedirectResponse("/login", status_code=303)
    form = await request.form()
    passo = totp_conferir(descriptografar(u.totp_segredo.encode()).decode(), form.get("codigo", ""), u.totp_ultimo)
    if passo:
        u.totp_ultimo = passo
        return _entrar(request, db, u)
    if _conferir_codigo_email(db, u, form.get("codigo", "")):
        auditar(db, request, u, "LOGIN_CODIGO_EMAIL", "usuario", u.id)
        return _entrar(request, db, u)
    _falhou(request, db, u.login, codigo=True)
    flash(request, "Código incorreto, vencido ou já usado. Use o código atual do aplicativo ou o último enviado por e-mail.", "erro")
    return RedirectResponse("/login/codigo", status_code=303)


# ---- Verificação em duas etapas (cada usuário ativa a sua) ------------------------------------

@app.get("/minha-conta/2fa")
def dois_fatores_form(request: Request, db: Session = Depends(get_db), u=Depends(usuario_atual)):
    segredo = ""
    if not u.tem_2fa:
        segredo = request.session.get("totp_novo") or totp_novo_segredo()
        request.session["totp_novo"] = segredo
    uri = totp_uri(segredo, u.login) if segredo else ""
    from .configuracoes import smtp_configurado
    sugestao = u.email_2fa or (EMAIL_2FA_PADRAO_ADMIN if u.perfil == "ADMIN" else "")
    return render(request, "dois_fatores.html", segredo=segredo, qr=qr_svg(uri) if uri else "",
                  segredo_formatado=" ".join(segredo[i:i + 4] for i in range(0, len(segredo), 4)),
                  email_sugerido=sugestao, smtp_ok=smtp_configurado(db))


EMAIL_2FA_PADRAO_ADMIN = "mafraassessoria1@gmail.com"


@app.post("/minha-conta/2fa/email")
async def dois_fatores_email(request: Request, db: Session = Depends(get_db), u=Depends(usuario_atual)):
    """Grava (ou remove) o e-mail do código alternativo. Exige o código atual do aplicativo e manda um e-mail de teste."""
    from .configuracoes import ErroEmail, enviar_email
    form = await request.form()
    email = texto(form, "email_2fa").lower()
    passo = u.tem_2fa and totp_conferir(descriptografar(u.totp_segredo.encode()).decode(), form.get("codigo", ""), u.totp_ultimo)
    if not passo:
        flash(request, "Para alterar o e-mail alternativo, informe o código atual do aplicativo.", "erro")
        return RedirectResponse("/minha-conta/2fa", status_code=303)
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        flash(request, "E-mail inválido.", "erro")
        return RedirectResponse("/minha-conta/2fa", status_code=303)
    u.totp_ultimo = passo
    if email:
        try:
            enviar_email(db, [email], "ERP MAFRA ASSESSORIA: e-mail alternativo de acesso",
                         f"Este e-mail foi cadastrado para receber o código de acesso do usuário {u.login} "
                         "quando o celular não estiver à mão.\n\nSe não foi você, avise o administrador do sistema.", sem_copia=True)
        except ErroEmail as e:
            db.commit()
            flash(request, f"E-mail não gravado: o teste de envio falhou ({e}).", "erro")
            return RedirectResponse("/minha-conta/2fa", status_code=303)
    u.email_2fa = email
    u.email_codigo_hash, u.email_codigo_expira = "", None
    auditar(db, request, u, "2FA_EMAIL", "usuario", u.id, email_mascarado(email) or "removido")
    db.commit()
    flash(request, f"E-mail alternativo gravado: {email_mascarado(email)}. Um e-mail de teste foi enviado." if email
          else "E-mail alternativo removido.")
    return RedirectResponse("/minha-conta/2fa", status_code=303)


@app.post("/minha-conta/2fa/ativar")
async def dois_fatores_ativar(request: Request, db: Session = Depends(get_db), u=Depends(usuario_atual)):
    form = await request.form()
    segredo = request.session.get("totp_novo", "")
    passo = totp_conferir(segredo, form.get("codigo", ""))
    if u.tem_2fa or not segredo or not passo:
        flash(request, "O código não confere. Confira se o aplicativo leu o QR code desta tela e digite o código atual.", "erro")
        return RedirectResponse("/minha-conta/2fa", status_code=303)
    u.totp_segredo, u.totp_ultimo = criptografar(segredo.encode()).decode(), passo
    db.merge(u)
    request.session.pop("totp_novo", None)
    auditar(db, request, u, "ATIVAR_2FA", "usuario", u.id)
    db.commit()
    flash(request, "Verificação em duas etapas ativada. A partir do próximo login o sistema pedirá o código do aplicativo.")
    return RedirectResponse("/", status_code=303)


@app.post("/minha-conta/2fa/desativar")
async def dois_fatores_desativar(request: Request, db: Session = Depends(get_db), u=Depends(usuario_atual)):
    form = await request.form()
    ok = u.tem_2fa and conferir_senha(form.get("senha", ""), u.senha_hash) and totp_conferir(
        descriptografar(u.totp_segredo.encode()).decode(), form.get("codigo", ""), u.totp_ultimo)
    if not ok:
        flash(request, "Para desativar, informe a senha e o código atual do aplicativo.", "erro")
        return RedirectResponse("/minha-conta/2fa", status_code=303)
    u.totp_segredo, u.totp_ultimo = "", 0
    db.merge(u)
    auditar(db, request, u, "DESATIVAR_2FA", "usuario", u.id)
    db.commit()
    flash(request, "Verificação em duas etapas desativada.")
    return RedirectResponse("/minha-conta/2fa", status_code=303)


@app.post("/usuarios/{uid}/zerar-2fa")
def usuario_zerar_2fa(uid: int, request: Request, db: Session = Depends(get_db), u=Depends(admin)):
    """Para quem perdeu o celular: o administrador remove a verificação de OUTRO usuário."""
    alvo = db.get(models.Usuario, uid)
    if alvo and alvo.id != u.id and alvo.tem_2fa:
        alvo.totp_segredo, alvo.totp_ultimo = "", 0
        auditar(db, request, u, "ZERAR_2FA", "usuario", uid, f"login={alvo.login}")
        db.commit()
        flash(request, f"Verificação em duas etapas de {alvo.login} removida; ele pode ativar de novo.")
    return RedirectResponse("/usuarios", status_code=303)


@app.get("/sair")
def sair(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/primeiro-acesso")
def primeiro_acesso_form(request: Request, db: Session = Depends(get_db)):
    if _existe_usuario(db):
        return RedirectResponse("/login", status_code=303)
    return render(request, "primeiro_acesso.html")


@app.post("/primeiro-acesso")
async def primeiro_acesso(request: Request, db: Session = Depends(get_db)):
    if _existe_usuario(db):
        return RedirectResponse("/login", status_code=303)
    form = await request.form()
    senha = form.get("senha", "")
    erro = senha_forte(senha) or (None if senha == form.get("senha2") else "As senhas não conferem.")
    if not texto(form, "nome") or not texto(form, "login"):
        erro = "Preencha nome e login."
    if erro:
        flash(request, erro, "erro")
        return RedirectResponse("/primeiro-acesso", status_code=303)
    u = models.Usuario(nome=texto(form, "nome", 120), login=texto(form, "login", 60).lower(),
                       senha_hash=gerar_hash_senha(senha), perfil="ADMIN")
    db.add(u)
    db.flush()
    auditar(db, request, u, "CRIAR_ADMIN_INICIAL", "usuario", u.id)
    db.commit()
    flash(request, "Administrador criado. Faça o login.")
    return RedirectResponse("/login", status_code=303)


# ---- Painel -----------------------------------------------------------------

@app.get("/")
def painel(request: Request, db: Session = Depends(get_db), u=Depends(usuario_atual)):
    from .painel import montar
    return render(request, "painel.html", **montar(db, u))


# ---- Usuários (somente admin) -----------------------------------------------

@app.get("/usuarios")
def usuarios(request: Request, db: Session = Depends(get_db), u=Depends(admin)):
    lista = db.scalars(select(models.Usuario).order_by(models.Usuario.nome)).all()
    return render(request, "usuarios.html", lista=lista)


@app.post("/usuarios")
async def usuario_criar(request: Request, db: Session = Depends(get_db), u=Depends(admin)):
    form = await request.form()
    login_ = texto(form, "login", 60).lower()
    senha = form.get("senha", "")
    erro = senha_forte(senha)
    if not texto(form, "nome") or not login_:
        erro = "Preencha nome e login."
    elif db.scalar(select(models.Usuario).where(models.Usuario.login == login_)):
        erro = "Já existe um usuário com esse login."
    if erro:
        flash(request, erro, "erro")
        return RedirectResponse("/usuarios", status_code=303)
    novo = models.Usuario(nome=texto(form, "nome", 120), login=login_, senha_hash=gerar_hash_senha(senha),
                          perfil=form.get("perfil") if form.get("perfil") in models.PERFIS else "OPERADOR")
    db.add(novo)
    db.flush()
    auditar(db, request, u, "CRIAR_USUARIO", "usuario", novo.id, f"login={login_} perfil={novo.perfil}")
    db.commit()
    flash(request, f"Usuário {login_} criado.")
    return RedirectResponse("/usuarios", status_code=303)


@app.post("/usuarios/{uid}/ativo")
def usuario_ativo(uid: int, request: Request, db: Session = Depends(get_db), u=Depends(admin)):
    alvo = db.get(models.Usuario, uid)
    if alvo and alvo.id != u.id:
        alvo.ativo = not alvo.ativo
        auditar(db, request, u, "ATIVAR_USUARIO" if alvo.ativo else "DESATIVAR_USUARIO", "usuario", uid)
        db.commit()
    return RedirectResponse("/usuarios", status_code=303)


@app.get("/minha-senha")
def minha_senha_form(request: Request, u=Depends(usuario_atual)):
    return render(request, "minha_senha.html")


@app.post("/minha-senha")
async def minha_senha(request: Request, db: Session = Depends(get_db), u=Depends(usuario_atual)):
    form = await request.form()
    if not conferir_senha(form.get("atual", ""), u.senha_hash):
        flash(request, "Senha atual incorreta.", "erro")
        return RedirectResponse("/minha-senha", status_code=303)
    nova = form.get("nova", "")
    erro = senha_forte(nova) or (None if nova == form.get("nova2") else "As senhas não conferem.")
    if erro:
        flash(request, erro, "erro")
        return RedirectResponse("/minha-senha", status_code=303)
    u.senha_hash = gerar_hash_senha(nova)
    db.merge(u)
    auditar(db, request, u, "TROCAR_SENHA", "usuario", u.id)
    db.commit()
    flash(request, "Senha alterada.")
    return RedirectResponse("/", status_code=303)


# ---- Auditoria --------------------------------------------------------------

@app.get("/auditoria")
def auditoria(request: Request, acao: str = "", db: Session = Depends(get_db), u=Depends(admin)):
    stmt = select(models.Auditoria)
    if acao:
        stmt = stmt.where(models.Auditoria.acao == acao)
    lista = db.scalars(stmt.order_by(models.Auditoria.id.desc()).limit(500)).all()
    return render(request, "auditoria.html", lista=lista, acao=acao)
