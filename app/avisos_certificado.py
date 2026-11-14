"""Controle dos certificados digitais A1 das empresas e avisos de vencimento por e-mail.

Regra de uso (automática, em nfse.emissao.dono_certificado):
  certificado próprio válido → usa o da empresa; vencido ou inexistente → usa o do escritório (procuração).

Avisos: começam faltando 15 dias e seguem de 3 em 3 dias (15, 12, 9, 6, 3 e no dia do vencimento).
Rodam uma vez por dia, a partir das 8h, pelo agendador interno.
"""
from __future__ import annotations

import html as _html
import logging
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models

log = logging.getLogger(__name__)

MARCOS = (15, 12, 9, 6, 3, 0)
CHAVE_LIGADO = "certificados_aviso_email"
CHAVE_DIA = "certificados_aviso_ultimo_dia"
HORA_ENVIO = 8
CERTIFICADORA = dict(nome="InfoPUB Certificação Digital", site="https://www.infopubme.com.br/",
                     whatsapp="(33) 98876-2499", link_whatsapp="https://wa.me/message/BUQV6RWR2BEDD1")


def ligado(db: Session) -> bool:
    from .configuracoes import ler_config
    return ler_config(db, CHAVE_LIGADO, "1") == "1"


def escritorio(db: Session) -> models.Prestador | None:
    from .configuracoes import ler_config
    pid = ler_config(db, "prestador_procurador_id")
    return db.get(models.Prestador, int(pid)) if pid and pid.isdigit() else None


def situacao(p: models.Prestador) -> dict:
    """Situação do certificado da empresa, para telas."""
    c = p.certificado_ativo
    if c is None:
        return dict(cert=None, codigo="SEM", rotulo="Sem certificado", classe="", dias=None)
    d = c.dias_para_vencer
    if d < 0:
        return dict(cert=c, codigo="VENCIDO", rotulo=f"Vencido em {c.validade_fim:%d/%m/%Y}", classe="erro", dias=d)
    if d == 0:
        return dict(cert=c, codigo="VENCENDO", rotulo="Vence hoje", classe="erro", dias=d)
    if d <= 15:
        return dict(cert=c, codigo="VENCENDO", rotulo=f"Vence em {d} dia{'s' if d > 1 else ''}", classe="erro", dias=d)
    if d <= 30:
        return dict(cert=c, codigo="VENCENDO", rotulo=f"Vence em {d} dias", classe="aviso", dias=d)
    return dict(cert=c, codigo="VALIDO", rotulo="Válido", classe="ok", dias=d)


def marco_de(dias: int) -> int | None:
    """Qual aviso corresponde a quantos dias faltam (None = fora da janela de avisos)."""
    if dias < 0 or dias > MARCOS[0]:
        return None
    return min(m for m in MARCOS if m >= dias)


def _enviados(c: models.Certificado) -> set[str]:
    return {x for x in (c.avisos_enviados or "").split(",") if x}


def aviso_pendente(c: models.Certificado | None) -> int | None:
    if c is None:
        return None
    m = marco_de(c.dias_para_vencer)
    return m if m is not None and str(m) not in _enviados(c) else None


def _registrar(c: models.Certificado) -> None:
    dias = c.dias_para_vencer
    feitos = _enviados(c) | {str(m) for m in MARCOS if m >= dias}
    c.avisos_enviados = ",".join(sorted(feitos, key=int, reverse=True))
    c.ultimo_aviso_em, c.ultimo_aviso_dias = datetime.now(), dias


# ---------------------------------------------------------------- texto do e-mail

LOGO_CID = "logo-mafra"
COR = "#584C57"          # cor da marca MAFRA
NOME_ESC = "MAFRA ASSESSORIA CONTÁBIL"


def logo_arquivo():
    from pathlib import Path
    return Path(__file__).parent / "static" / "img" / "logo_mafra.png"


def _contato(db: Session) -> str:
    esc = escritorio(db)
    return " · ".join(x for x in ((esc.telefone if esc else ""), (esc.email if esc else "")) if x)


def _layout(destaque: str, cor_destaque: str, fundo_destaque: str, corpo: str, contato: str, logo_src: str) -> str:
    """Moldura comum dos e-mails de certificado: logo do escritório, um destaque suave e o rodapé."""
    e = _html.escape
    return f"""<!doctype html><html><body style="margin:0;background:#f4f3f4;font-family:Segoe UI,Arial,sans-serif;color:#2d2a2e">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f3f4;padding:24px 0"><tr><td align="center">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;background:#ffffff;border-radius:12px;overflow:hidden;border:1px solid #e4e0e4">
<tr><td align="center" style="padding:26px 28px 18px;border-bottom:3px solid {COR}">
  <img src="{logo_src}" width="200" alt="MAFRA Assessoria Contábil" style="display:block;width:200px;max-width:60%;height:auto;border:0"></td></tr>
<tr><td style="padding:24px 28px 8px">
  <div style="background:{fundo_destaque};border-left:4px solid {cor_destaque};border-radius:8px;padding:12px 16px;font-size:15px;color:#2d2a2e">{destaque}</div></td></tr>
<tr><td style="padding:12px 28px 22px;font-size:15px;line-height:1.6">{corpo}</td></tr>
<tr><td style="padding:14px 28px;background:#f8f7f8;border-top:1px solid #ece8ec;font-size:12.5px;color:#7a727a">
  🔒 Para sua segurança: nunca pedimos senhas por links, SMS ou números desconhecidos.
  Na dúvida, fale direto com a gente{(': ' + e(contato)) if contato else ''}.</td></tr>
<tr><td style="padding:16px 28px 22px;font-size:13px;color:#7a727a">Um abraço,<br><b style="color:{COR}">Equipe Mafra Assessoria Contábil</b>{('<br>' + e(contato)) if contato else ''}</td></tr>
</table></td></tr></table></body></html>"""


def _botoes() -> str:
    cd = CERTIFICADORA
    return f"""<table role="presentation" cellpadding="0" cellspacing="0" style="margin:10px 0 4px"><tr>
    <td style="padding-right:10px"><a href="{cd['link_whatsapp']}" style="display:inline-block;background:#1f8a4c;color:#ffffff;text-decoration:none;padding:10px 18px;border-radius:22px;font-weight:600;font-size:14px">Falar no WhatsApp</a></td>
    <td><a href="{cd['site']}" style="display:inline-block;background:#ffffff;color:{COR};text-decoration:none;padding:9px 18px;border-radius:22px;font-weight:600;font-size:14px;border:1px solid {COR}">Visitar o site</a></td>
  </tr></table>
  <p style="margin:2px 0 0;font-size:12.5px;color:#7a727a">InfoPUB · WhatsApp {_html.escape(cd['whatsapp'])}</p>"""


def _passos(e) -> str:
    return f"""<p style="margin:18px 0 6px;font-weight:600;color:{COR}">É simples:</p>
  <table role="presentation" cellpadding="0" cellspacing="0" style="font-size:15px;line-height:1.55">
   <tr><td valign="top" style="padding:3px 10px 3px 0;color:{COR};font-weight:700">1</td><td style="padding:3px 0">Emita o <b>e-CNPJ A1</b> com a certificadora de sua preferência. Se quiser uma indicação, a <b>InfoPUB Certificação Digital</b> atende rapidinho e online:</td></tr>
   <tr><td></td><td style="padding:0 0 6px">{_botoes()}</td></tr>
   <tr><td valign="top" style="padding:3px 10px 3px 0;color:{COR};font-weight:700">2</td><td style="padding:3px 0">Envie o novo certificado (<b>.pfx</b> ou <b>.p12</b>) e a senha para nós, pelo canal de sempre do escritório.</td></tr>
   <tr><td valign="top" style="padding:3px 10px 3px 0;color:{COR};font-weight:700">3</td><td style="padding:3px 0">Pronto! Atualizamos tudo aqui no mesmo dia.</td></tr>
  </table>"""


def _texto(saudacao: str, paragrafo: str, contato: str) -> str:
    cd = CERTIFICADORA
    return f"""{saudacao}

{paragrafo}

É simples:
1. Emita o e-CNPJ A1 com a certificadora de sua preferência. Se quiser uma indicação,
   a InfoPUB Certificação Digital atende online: WhatsApp {cd['whatsapp']}
   ({cd['link_whatsapp']}) – {cd['site']}
2. Envie o novo certificado (.pfx ou .p12) e a senha para nós, pelo canal de sempre do escritório.
3. Pronto! Atualizamos tudo aqui no mesmo dia.

Se você já renovou e nos enviou, pode desconsiderar este lembrete. Obrigado!

Para sua segurança: nunca pedimos senhas por links, SMS ou números desconhecidos.
Na dúvida, fale direto com a gente{(': ' + contato) if contato else ''}.

Um abraço,
Equipe Mafra Assessoria Contábil
{contato}
"""


def montar_email(db: Session, p: models.Prestador, c: models.Certificado, logo_src: str = f"cid:{LOGO_CID}") -> tuple[str, str, str]:
    """(assunto, texto, html) do lembrete de vencimento."""
    e = _html.escape
    contato = _contato(db)
    dias = c.dias_para_vencer
    venc = f"{c.validade_fim:%d/%m/%Y}"
    empresa = p.razao_social
    if dias <= 0:
        quando = "vence hoje"
    elif dias == 1:
        quando = "vence amanhã"
    else:
        quando = f"vence em {dias} dias"
    assunto = f"Lembrete: certificado digital {quando} – {empresa}"
    paragrafo = (f"Passando para lembrar que o certificado digital (e-CNPJ A1) da sua empresa {quando}, em {venc}. "
                 "Ele é usado para emitir notas fiscais e acessar e-CAC, eSocial e DCTFWeb — renovando antes, "
                 "tudo continua funcionando sem interrupção.")
    texto = _texto(f"Olá, {empresa}! Tudo bem?", paragrafo, contato)
    destaque = f"📅 O certificado digital da sua empresa <b>{e(quando)}</b> — validade até <b>{venc}</b>."
    corpo = f"""<p style="margin:0 0 12px">Olá, <b>{e(empresa)}</b>! Tudo bem?</p>
  <p style="margin:0 0 12px">{e(paragrafo)}</p>
  {_passos(e)}
  <p style="margin:18px 0 0;color:#7a727a;font-size:14px">Se você já renovou e nos enviou, pode desconsiderar este lembrete. Obrigado!</p>"""
    html = _layout(destaque, "#c9a227" if dias > 3 else "#d0703c", "#fbf7ea" if dias > 3 else "#fcf1ea", corpo, contato, logo_src)
    return assunto, texto, html


# ---------------------------------------------------------------- pedido de renovação (vencido)

def montar_email_renovacao(db: Session, p: models.Prestador, c: models.Certificado | None,
                           logo_src: str = f"cid:{LOGO_CID}") -> tuple[str, str, str]:
    """(assunto, texto, html) do pedido de renovação: certificado vencido (c) ou empresa sem certificado (c=None)."""
    e = _html.escape
    contato = _contato(db)
    empresa = p.razao_social
    if c:
        venc = f"{c.validade_fim:%d/%m/%Y}"
        assunto = f"Certificado digital vencido – vamos renovar? – {empresa}"
        destaque = f"O certificado digital da sua empresa <b>venceu em {venc}</b>. Vamos renovar?"
        inicio = f"Notamos que o certificado digital (e-CNPJ A1) da sua empresa venceu em {venc}."
    else:
        assunto = f"Certificado digital da sua empresa – {empresa}"
        destaque = "Ainda não temos o <b>certificado digital</b> da sua empresa cadastrado."
        inicio = "Notamos que ainda não temos cadastrado um certificado digital (e-CNPJ A1) válido da sua empresa."
    paragrafo = (f"{inicio} Por enquanto seguimos usando a procuração do escritório no que é possível, mas alguns serviços "
                 "— como a emissão de notas fiscais e o acesso a e-CAC, eSocial e DCTFWeb — dependem do certificado "
                 "da própria empresa. O e-CNPJ A1 vale por 1 ano e a emissão é rápida e online.")
    texto = _texto(f"Olá, {empresa}! Tudo bem?", paragrafo, contato)
    corpo = f"""<p style="margin:0 0 12px">Olá, <b>{e(empresa)}</b>! Tudo bem?</p>
  <p style="margin:0 0 12px">{e(paragrafo)}</p>
  {_passos(e)}
  <p style="margin:18px 0 0;color:#7a727a;font-size:14px">Se você já renovou e nos enviou, pode desconsiderar esta mensagem. Obrigado!</p>"""
    html = _layout("🔑 " + destaque, "#c0605a", "#fbefee", corpo, contato, logo_src)
    return assunto, texto, html


def pedir_renovacao(db: Session, p: models.Prestador, usuario: str) -> str:
    """Envia o pedido de renovação do certificado vencido. "" se enviou, senão o motivo."""
    from .configuracoes import ErroEmail, enviar_email
    c = p.certificado_ativo
    if c is not None and c.dias_para_vencer >= 0:
        return "o certificado não está vencido"
    para = [x.strip() for x in (p.email or "").replace(";", ",").split(",") if x.strip()]
    if not para:
        return "empresa sem e-mail no cadastro"
    assunto, texto, html = montar_email_renovacao(db, p, c)
    try:
        enviar_email(db, para, assunto, texto, html=html, imagens={LOGO_CID: logo_arquivo()})
    except ErroEmail as e:
        return str(e)
    p.certificado_pedido_em = datetime.now()
    db.add(models.Auditoria(usuario_login=usuario, acao="PEDIDO_RENOVACAO_CERTIFICADO", entidade="prestador",
                            entidade_id=p.id, detalhes=f"para={','.join(para)}", data_hora=datetime.now()))
    return ""


# ---------------------------------------------------------------- envio

def enviar_aviso(db: Session, p: models.Prestador, usuario: str = "agendador") -> str:
    """Envia o aviso do certificado ativo da empresa. Devolve "" se enviou ou o motivo de não ter enviado."""
    from .configuracoes import ErroEmail, enviar_email
    c = p.certificado_ativo
    if c is None:
        return "sem certificado"
    if c.dias_para_vencer < 0:
        return "certificado já vencido"
    para = [x.strip() for x in (p.email or "").replace(";", ",").split(",") if x.strip()]
    if not para:
        return "empresa sem e-mail no cadastro"
    assunto, texto, html = montar_email(db, p, c)
    try:
        enviar_email(db, para, assunto, texto, html=html, imagens={LOGO_CID: logo_arquivo()})
    except ErroEmail as e:
        return str(e)
    _registrar(c)
    db.add(models.Auditoria(usuario_login=usuario, acao="AVISO_CERTIFICADO", entidade="prestador", entidade_id=p.id,
                            detalhes=f"para={','.join(para)} dias={c.dias_para_vencer}", data_hora=datetime.now()))
    return ""


def enviar_pendentes(db: Session) -> list[tuple[models.Prestador, str]]:
    """Envia os avisos que estiverem no dia. Devolve [(empresa, ""=enviado ou motivo)]."""
    saida = []
    prest = db.scalars(select(models.Prestador).where(models.Prestador.ativo.is_(True))
                       .order_by(*models.ordem_prestador_sql())).all()
    for p in prest:
        if aviso_pendente(p.certificado_ativo) is None:
            continue
        saida.append((p, enviar_aviso(db, p)))
        db.commit()
    return saida


def verificar(agora: datetime | None = None) -> bool:
    """Chamado a cada minuto pelo agendador: roda uma vez por dia, a partir das 8h."""
    from .configuracoes import gravar_config, ler_config, smtp_configurado
    from .db import SessionLocal
    agora = agora or datetime.now()
    if agora.hour < HORA_ENVIO:
        return False
    with SessionLocal() as db:
        if not ligado(db) or ler_config(db, CHAVE_DIA) == agora.date().isoformat() or not smtp_configurado(db):
            return False
        gravar_config(db, CHAVE_DIA, agora.date().isoformat())
        db.commit()
        res = enviar_pendentes(db)
        for p, motivo in res:
            if motivo:
                log.warning("aviso de certificado não enviado para %s: %s", p.razao_social, motivo)
        return True


def linhas_tela(db: Session) -> list[dict]:
    prest = db.scalars(select(models.Prestador).where(models.Prestador.ativo.is_(True))
                       .order_by(*models.ordem_prestador_sql())).all()
    hoje = date.today()
    saida = []
    for p in prest:
        s = situacao(p)
        c = s["cert"]
        saida.append(dict(p=p, **s, em_uso="escritório (procuração)" if p.usa_procuracao else "da empresa",
                          avisos=(c.avisos_enviados if c else ""), pendente=aviso_pendente(c),
                          ultimo_aviso=(c.ultimo_aviso_em if c else None), ultimo_dias=(c.ultimo_aviso_dias if c else None),
                          renovacao=p.certificado_pedido_em,
                          sem_email=not (p.email or "").strip(), hoje=hoje))
    return saida
