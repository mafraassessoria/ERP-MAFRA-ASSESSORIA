"""Configurações gerais (SMTP, pasta extra de XML, procurador) e envio de e-mail."""
from __future__ import annotations

import logging
import shutil
import smtplib
import ssl
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path

from sqlalchemy.orm import Session

from . import models
from .config import XML_DIR
from .seguranca import criptografar, descriptografar

log = logging.getLogger("emissor.config")

CAMPOS_SMTP = ("smtp_host", "smtp_porta", "smtp_usuario", "smtp_remetente_nome", "smtp_remetente_email",
               "smtp_seguranca", "email_copia_oculta")


def ler_config(db: Session, chave: str, padrao: str = "") -> str:
    c = db.get(models.Configuracao, chave)
    return c.valor if c and c.valor is not None else padrao


def gravar_config(db: Session, chave: str, valor: str) -> None:
    c = db.get(models.Configuracao, chave)
    if c is None:
        db.add(models.Configuracao(chave=chave, valor=valor))
    else:
        c.valor = valor


def gravar_senha_smtp(db: Session, senha: str) -> None:
    gravar_config(db, "smtp_senha_cripto", criptografar(senha.encode()).decode())


def _senha_smtp(db: Session) -> str:
    v = ler_config(db, "smtp_senha_cripto")
    return descriptografar(v.encode()).decode() if v else ""


# ---- Cópia de XML para pasta extra (ex.: C:\NF-Stock) ----------------------

def copiar_para_pasta_extra(db: Session, cnpj: str, arquivo: Path) -> None:
    destino = ler_config(db, "pasta_copia_xml").strip()
    if not destino or not arquivo.exists():
        return
    try:
        pasta = Path(destino) / cnpj
        pasta.mkdir(parents=True, exist_ok=True)
        alvo = pasta / arquivo.name
        if not alvo.exists():  # não duplica
            shutil.copy2(arquivo, alvo)
    except OSError as e:
        log.warning("cópia para pasta extra falhou (%s): %s", destino, e)


# ---- E-mail ---------------------------------------------------------------

class ErroEmail(Exception):
    pass


def smtp_configurado(db: Session) -> bool:
    return bool(ler_config(db, "smtp_host") and ler_config(db, "smtp_remetente_email"))


def enviar_email(db: Session, para: list[str], assunto: str, corpo: str,
                 anexos: list[Path] | None = None, sem_copia: bool = False) -> None:
    if not smtp_configurado(db):
        raise ErroEmail("E-mail não configurado. Um administrador deve preencher em Configurações.")
    host = ler_config(db, "smtp_host")
    porta = int(ler_config(db, "smtp_porta", "587") or 587)
    seguranca = ler_config(db, "smtp_seguranca", "STARTTLS")
    usuario = ler_config(db, "smtp_usuario")
    remetente = ler_config(db, "smtp_remetente_email")
    nome = ler_config(db, "smtp_remetente_nome", "")
    copia = [] if sem_copia else [e.strip() for e in ler_config(db, "email_copia_oculta").replace(";", ",").split(",") if e.strip()]

    msg = EmailMessage()
    msg["From"] = formataddr((nome, remetente)) if nome else remetente
    msg["To"] = ", ".join(para)
    msg["Subject"] = assunto
    msg.set_content(corpo)
    for a in anexos or []:
        tipo = ("application", "pdf") if a.suffix.lower() == ".pdf" else ("application", "xml")
        msg.add_attachment(a.read_bytes(), maintype=tipo[0], subtype=tipo[1], filename=a.name)
    destinatarios = para + copia
    contexto = ssl.create_default_context()
    etapa = "conectar"
    try:
        if seguranca == "SSL":
            s = smtplib.SMTP_SSL(host, porta, context=contexto, timeout=30)
        else:
            s = smtplib.SMTP(host, porta, timeout=30)
        with s:
            etapa = "cumprimentar o servidor"
            s.ehlo()
            if seguranca == "STARTTLS":
                etapa = "iniciar a criptografia (STARTTLS)"
                s.starttls(context=contexto)
                s.ehlo()
            if usuario:
                etapa = "fazer login"
                s.login(usuario, _senha_smtp(db))
            etapa = "enviar a mensagem"
            s.send_message(msg, to_addrs=destinatarios)
    except smtplib.SMTPAuthenticationError as e:
        raise ErroEmail("Usuário ou senha do e-mail recusados. No Gmail, use uma 'senha de app' (16 letras), não a senha normal.") from e
    except (smtplib.SMTPServerDisconnected, ConnectionResetError, ssl.SSLError) as e:
        dica = {"conectar": " Algo no servidor (antivírus com proteção de e-mail ou firewall) está bloqueando a conexão.",
                "cumprimentar o servidor": " Algo no servidor (antivírus com proteção de e-mail ou firewall) está interceptando a conexão.",
                "iniciar a criptografia (STARTTLS)": " Tente porta 465 com SSL.",
                "fazer login": " O Gmail encerrou no login: confira o usuário e a senha de app.",
                "enviar a mensagem": " O servidor recusou a mensagem: confira o e-mail do remetente (deve ser o mesmo do usuário) e os destinatários."}.get(etapa, "")
        raise ErroEmail(f"Falha ao enviar e-mail na etapa \"{etapa}\": {host}:{porta} fechou a conexão ({type(e).__name__}: {e}).{dica}") from e
    except (smtplib.SMTPException, OSError) as e:
        raise ErroEmail(f"Falha ao enviar e-mail na etapa \"{etapa}\": {type(e).__name__}: {e}") from e


def enviar_nota_por_email(db: Session, nota: models.NotaFiscal, para: list[str]) -> None:
    from .nfse.emissao import gerar_danfse

    if nota.situacao not in ("AUTORIZADA", "CANCELADA") or not nota.caminho_xml:
        raise ErroEmail("Só é possível enviar notas autorizadas.")
    para = [e.strip() for e in para if e and "@" in e]
    if not para:
        raise ErroEmail("Informe pelo menos um e-mail de destino.")
    if not nota.caminho_pdf:
        gerar_danfse(nota)
    anexos = [XML_DIR / nota.caminho_pdf, XML_DIR / nota.caminho_xml]
    p = nota.prestador
    cancelada = nota.situacao == "CANCELADA"
    assunto = f"{'CANCELAMENTO - ' if cancelada else ''}NFS-e nº {nota.numero_nfse} - {p.razao_social}"
    if nota.ambiente == "HOMOLOGACAO":
        assunto = "[TESTE - SEM VALIDADE] " + assunto
    corpo = (
        f"Prezado(a),\n\n"
        f"{'Informamos o CANCELAMENTO da' if cancelada else 'Segue a'} Nota Fiscal de Serviço Eletrônica "
        f"nº {nota.numero_nfse}, emitida por {p.razao_social} (CNPJ {p.cnpj}).\n\n"
        f"Valor: R$ {nota.valor_servicos:,.2f}\n".replace(",", "X").replace(".", ",").replace("X", ".")
        + f"Competência: {nota.competencia:%m/%Y}\n"
        f"Chave de acesso: {nota.chave_acesso}\n\n"
        f"A autenticidade pode ser consultada no portal da NFS-e Nacional (www.nfse.gov.br).\n\n"
        f"Em anexo: DANFSe (PDF) e arquivo XML.\n\n"
        f"Atenciosamente,\n{p.razao_social}\n"
    )
    enviar_email(db, para, assunto, corpo, [a for a in anexos if a.exists()])
    nota.email_enviado_em = datetime.now()
    nota.email_enviado_para = ", ".join(para)[:300]
