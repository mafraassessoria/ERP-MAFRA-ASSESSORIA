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


# ---------------------------------------------------------------- texto do e-mail

def montar_email(db: Session, p: models.Prestador, c: models.Certificado) -> tuple[str, str, str]:
    """(assunto, texto, html) do aviso de vencimento."""
    esc = escritorio(db)
    nome_esc = "M & R Assessoria Contábil"
    tel = (esc.telefone if esc else "") or ""
    mail = (esc.email if esc else "") or ""
    dias = c.dias_para_vencer
    venc = f"{c.validade_fim:%d/%m/%Y}"
    empresa = p.razao_social
    if dias <= 0:
        quando, quando_curto = f"vence HOJE ({venc})", "vence HOJE"
    elif dias == 1:
        quando, quando_curto = f"vence AMANHÃ ({venc})", "vence AMANHÃ"
    else:
        quando, quando_curto = f"vence em {dias} dias ({venc})", f"vence em {dias} dias"
    assunto = f"⚠️ Certificado digital {quando_curto} – {empresa}"
    contato = " · ".join(x for x in (tel, mail) if x)

    texto = f"""Olá, {empresa}!

O CERTIFICADO DIGITAL A1 da sua empresa {quando}.

Por que isso é importante?
Sem certificado válido, a emissão de notas fiscais e serviços como e-CAC, eSocial e
DCTFWeb podem ficar bloqueados, gerando atrasos, multas e transtornos.

O que fazer:
1. Renove o certificado A1 com a certificadora de sua preferência
   (se preferir, a {nome_esc} indica uma).
2. Envie o novo certificado (.pfx ou .p12) e a senha para o seu contador
   (MAFRA ASSESSORIA CONTÁBIL) pelo canal habitual do escritório.
3. Pronto! Atualizamos o cadastro no mesmo dia e você não precisa fazer mais nada.

Já renovou e nos enviou? Desconsidere este aviso.

Segurança: a {nome_esc} nunca pede senhas por links, SMS ou mensagens de números
desconhecidos. Na dúvida, fale direto com o escritório{(': ' + contato) if contato else ''}.

Atenciosamente,
{nome_esc}
{contato}
"""
    e = _html.escape
    html = f"""<!doctype html><html><body style="margin:0;background:#f2f4f7;font-family:Segoe UI,Arial,sans-serif;color:#1d2733">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f2f4f7;padding:24px 0"><tr><td align="center">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;background:#ffffff;border-radius:10px;overflow:hidden;border:1px solid #d6dce3">
<tr><td style="background:#1f4e79;color:#ffffff;padding:16px 28px;font-size:15px"><b>{e(nome_esc)}</b></td></tr>
<tr><td style="background:{'#b42318' if dias <= 3 else '#d97706'};color:#ffffff;padding:22px 28px;text-align:center">
  <div style="font-size:14px;letter-spacing:.5px">⚠️ AVISO IMPORTANTE</div>
  <div style="font-size:24px;font-weight:700;margin-top:6px">Seu certificado digital {e(quando_curto)}</div>
  <div style="font-size:15px;margin-top:4px">Validade: <b>{venc}</b></div></td></tr>
<tr><td style="padding:24px 28px;font-size:15px;line-height:1.55">
  <p style="margin:0 0 14px">Olá, <b>{e(empresa)}</b>!</p>
  <p style="margin:0 0 14px">Sem o certificado digital A1 válido, a <b>emissão de notas fiscais</b> e serviços como
    <b>e-CAC, eSocial e DCTFWeb</b> podem ficar bloqueados, gerando atrasos, multas e transtornos.</p>
  <p style="margin:18px 0 8px;font-weight:700;color:#1f4e79">O que fazer</p>
  <table role="presentation" cellpadding="0" cellspacing="0" style="font-size:15px">
   <tr><td valign="top" style="padding:4px 10px 4px 0;font-weight:700;color:#1f4e79">1.</td><td style="padding:4px 0">Renove o certificado A1 com a certificadora de sua preferência (se preferir, nós indicamos uma).</td></tr>
   <tr><td valign="top" style="padding:4px 10px 4px 0;font-weight:700;color:#1f4e79">2.</td><td style="padding:4px 0">Envie o novo certificado (<b>.pfx</b> ou <b>.p12</b>) e a senha para o seu contador (<b>MAFRA ASSESSORIA CONTÁBIL</b>) pelo canal habitual do escritório.</td></tr>
   <tr><td valign="top" style="padding:4px 10px 4px 0;font-weight:700;color:#1f4e79">3.</td><td style="padding:4px 0">Pronto! Atualizamos o cadastro no mesmo dia.</td></tr>
  </table>
  <p style="margin:18px 0 0;color:#66727f;font-size:14px">Já renovou e nos enviou? Desconsidere este aviso.</p>
</td></tr>
<tr><td style="padding:14px 28px;background:#f7f9fb;border-top:1px solid #e3e8ee;font-size:13px;color:#66727f">
  🔒 <b>Segurança:</b> a {e(nome_esc)} nunca pede senhas por links, SMS ou mensagens de números desconhecidos.
  Na dúvida, fale direto com o escritório{(': ' + e(contato)) if contato else ''}.</td></tr>
<tr><td style="padding:14px 28px;font-size:13px;color:#66727f">Atenciosamente,<br><b>{e(nome_esc)}</b>{('<br>' + e(contato)) if contato else ''}</td></tr>
</table></td></tr></table></body></html>"""
    return assunto, texto, html


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
        enviar_email(db, para, assunto, texto, html=html)
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
                          sem_email=not (p.email or "").strip(), hoje=hoje))
    return saida
