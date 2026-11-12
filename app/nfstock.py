"""Envio automático dos XML das NFS-e ao NF-Stock (Alterdata), pela API MS-Importação.

Fluxo da API (documentação: https://docs-nfstock.pack.alterdata.com.br/):
  1. POST {BASE}/storage  {"size": bytes, "extension": ".xml"}   → {"id", "url", "mensagem"}
  2. PUT  url (S3, vale 1 minuto) com o arquivo no corpo
  3. GET  {BASE}/storage/arquivo/{id}  → {"status": "Uploaded" | "Com Erro" | ..., "log": ...}
Autenticação: cabeçalho  Authorization: Bearer <token>  (token gerado no NF-Stock → Opções da Conta → Integração).

O agendador interno chama `ciclo()` a cada minuto; o envio roda a cada 10 minutos, em lotes.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from . import models

log = logging.getLogger(__name__)

BASE = "https://ms-importacao-service-nfstock.alterdatasoftware.com.br"
CHAVE_TOKEN = "nfstock_token_cripto"
CHAVE_LIGADO = "nfstock_ligado"
CHAVE_DESDE = "nfstock_desde"           # envia notas emitidas a partir desta data (AAAA-MM-DD)
CHAVE_IMPORTADAS = "nfstock_importadas"  # "1" = envia também as notas importadas do ADN
CHAVE_ULTIMO = "nfstock_ultimo_ciclo"
CHAVE_CRM = "nfstock_codigo_crm"         # código do cliente na Alterdata (6 dígitos), usado na consulta (MS-Exportação)
INTERVALO_MIN = 10
LOTE = 40
TIMEOUT = 60
ERROS_DE_REDE = ("timed out", "Connection", "conexão", "Max retries", "RemoteDisconnected", "SSLError")


class ErroNFStock(Exception):
    pass


# ---------------------------------------------------------------- configuração

def token(db: Session) -> str:
    from .configuracoes import ler_config
    from .seguranca import descriptografar
    v = ler_config(db, CHAVE_TOKEN)
    return descriptografar(v.encode()).decode() if v else ""


def gravar_token(db: Session, valor: str) -> None:
    from .configuracoes import gravar_config
    from .seguranca import criptografar
    gravar_config(db, CHAVE_TOKEN, criptografar(valor.strip().encode()).decode() if valor.strip() else "")


def ligado(db: Session) -> bool:
    from .configuracoes import ler_config
    return ler_config(db, CHAVE_LIGADO) == "1" and bool(token(db))


def desde(db: Session) -> date:
    from .configuracoes import ler_config
    try:
        return date.fromisoformat(ler_config(db, CHAVE_DESDE))
    except ValueError:
        return date.today().replace(day=1)


# ---------------------------------------------------------------- API

def _cab(tk: str) -> dict:
    return {"Authorization": f"Bearer {tk}", "Accept": "application/json"}


def enviar_arquivo(tk: str, conteudo: bytes, extensao: str = ".xml", sessao=None) -> str:
    """Faz os passos 1 e 2. Devolve o id do arquivo no NF-Stock."""
    s = sessao or requests
    r = s.post(f"{BASE}/storage", json={"size": len(conteudo), "extension": extensao}, headers=_cab(tk), timeout=TIMEOUT)
    if r.status_code in (401, 403):
        raise ErroNFStock("Token do NF-Stock recusado (confira em Configurações).")
    if r.status_code >= 300:
        raise ErroNFStock(f"NF-Stock respondeu {r.status_code}: {r.text[:200]}")
    dados = r.json()
    url, ident = dados.get("url"), dados.get("id")
    if not url or not ident:
        raise ErroNFStock(f"Resposta inesperada do NF-Stock: {str(dados)[:200]}")
    r2 = s.put(url, data=conteudo, timeout=TIMEOUT)
    if r2.status_code >= 300:
        raise ErroNFStock(f"Falha ao subir o arquivo ({r2.status_code}).")
    return str(ident)


def consultar(tk: str, ident: str, sessao=None) -> dict:
    s = sessao or requests
    r = s.get(f"{BASE}/storage/arquivo/{ident}", headers=_cab(tk), timeout=TIMEOUT)
    if r.status_code >= 300:
        raise ErroNFStock(f"NF-Stock respondeu {r.status_code} na consulta.")
    return r.json()


def testar(db: Session) -> str:
    """Confere o token pedindo um endereço de envio (não envia arquivo). Devolve "" se ok."""
    tk = token(db)
    if not tk:
        return "token não informado"
    try:
        r = requests.post(f"{BASE}/storage", json={"size": 100, "extension": ".xml"}, headers=_cab(tk), timeout=TIMEOUT)
    except requests.RequestException as e:
        return f"sem conexão com o NF-Stock ({e.__class__.__name__})"
    if r.status_code in (401, 403):
        return "token recusado pelo NF-Stock"
    if r.status_code >= 300:
        return f"NF-Stock respondeu {r.status_code}"
    return ""


# ---------------------------------------------------------------- notas

def _base_notas(db: Session):
    from .configuracoes import ler_config
    origens = ["EMITIDA", "IMPORTADA"] if ler_config(db, CHAVE_IMPORTADAS) == "1" else ["EMITIDA"]
    inicio = datetime.combine(desde(db), datetime.min.time())
    return (select(models.NotaFiscal)
            .where(models.NotaFiscal.ambiente == "PRODUCAO",
                   models.NotaFiscal.situacao.in_(("AUTORIZADA", "CANCELADA")),
                   models.NotaFiscal.origem.in_(origens),
                   models.NotaFiscal.caminho_xml != "",
                   models.NotaFiscal.data_emissao >= inicio))


def pendentes(db: Session, limite: int = LOTE) -> list[models.NotaFiscal]:
    q = _base_notas(db).where(or_(models.NotaFiscal.nfstock_status == "",
                                  models.NotaFiscal.nfstock_status.is_(None)))
    return list(db.scalars(q.order_by(models.NotaFiscal.data_emissao).limit(limite)))


def eventos_pendentes(db: Session, limite: int = LOTE) -> list[models.NotaFiscal]:
    q = _base_notas(db).where(models.NotaFiscal.situacao == "CANCELADA",
                              models.NotaFiscal.caminho_evento_xml != "",
                              models.NotaFiscal.nfstock_status == "ENVIADO",
                              or_(models.NotaFiscal.nfstock_evento_status == "",
                                  models.NotaFiscal.nfstock_evento_status.is_(None)))
    return list(db.scalars(q.limit(limite)))


def resumo(db: Session) -> dict:
    from sqlalchemy import func
    q = _base_notas(db).subquery()
    linhas = db.execute(select(q.c.nfstock_status, func.count()).group_by(q.c.nfstock_status)).all()
    cont = {(k or ""): n for k, n in linhas}
    erros = list(db.scalars(_base_notas(db).where(models.NotaFiscal.nfstock_status == "ERRO")
                            .order_by(models.NotaFiscal.data_emissao.desc()).limit(30)))
    return dict(enviadas=cont.get("ENVIADO", 0), processando=cont.get("PROCESSANDO", 0),
                pendentes=cont.get("", 0), erros=cont.get("ERRO", 0), lista_erros=erros)


def _arquivo(rel: str) -> Path:
    from .config import XML_DIR
    return XML_DIR / rel


def enviar_nota(db: Session, nota: models.NotaFiscal, tk: str, evento: bool = False, sessao=None) -> None:
    rel = nota.caminho_evento_xml if evento else nota.caminho_xml
    caminho = _arquivo(rel)
    agora = datetime.now()
    try:
        if not caminho.exists():
            raise ErroNFStock(f"arquivo não encontrado: {rel}")
        ident = enviar_arquivo(tk, caminho.read_bytes(), ".xml", sessao)
    except requests.RequestException as e:
        # falha de rede (lentidão/queda): fica na fila e tenta de novo no próximo ciclo
        nota.nfstock_log = f"tentativa falhou ({datetime.now():%d/%m %H:%M}): {e.__class__.__name__}"[:500]
        raise
    except ErroNFStock as e:
        if evento:
            nota.nfstock_evento_status = "ERRO"
        else:
            nota.nfstock_status, nota.nfstock_em = "ERRO", agora
        nota.nfstock_log = str(e)[:500]
        if isinstance(e, ErroNFStock) and "Token" in str(e):
            raise
        return
    if evento:
        nota.nfstock_evento_id, nota.nfstock_evento_status = ident, "PROCESSANDO"
    else:
        nota.nfstock_id, nota.nfstock_status, nota.nfstock_em, nota.nfstock_log = ident, "PROCESSANDO", agora, ""


def conferir_processando(db: Session, tk: str, sessao=None) -> None:
    limite = datetime.now() - timedelta(minutes=1)
    for nota in db.scalars(select(models.NotaFiscal).where(
            or_(models.NotaFiscal.nfstock_status == "PROCESSANDO",
                models.NotaFiscal.nfstock_evento_status == "PROCESSANDO")).limit(LOTE * 2)):
        for evento in (False, True):
            st = nota.nfstock_evento_status if evento else nota.nfstock_status
            ident = nota.nfstock_evento_id if evento else nota.nfstock_id
            if st != "PROCESSANDO" or not ident or (nota.nfstock_em and nota.nfstock_em > limite and not evento):
                continue
            try:
                r = consultar(tk, ident, sessao)
            except (ErroNFStock, requests.RequestException) as e:
                log.warning("NF-Stock: consulta %s falhou: %s", ident, e)
                continue
            status = (r.get("status") or "").strip().lower()
            if status == "uploaded":
                novo = "ENVIADO"
            elif "erro" in status:
                novo = "ERRO"
                nota.nfstock_log = (r.get("log") or "Com erro no NF-Stock")[:500]
            else:
                continue  # ainda processando
            if evento:
                nota.nfstock_evento_status = novo
            else:
                nota.nfstock_status = novo


def reenviar_erros(db: Session) -> int:
    n = 0
    for nota in db.scalars(_base_notas(db).where(models.NotaFiscal.nfstock_status == "ERRO")):
        nota.nfstock_status, nota.nfstock_log = "", ""
        n += 1
    return n


def rodar(db: Session, sessao=None) -> dict:
    """Um ciclo completo: confere os em processamento, envia notas novas e eventos de cancelamento."""
    tk = token(db)
    if not tk:
        raise ErroNFStock("Token do NF-Stock não informado.")
    # erros antigos de rede (de versões anteriores) voltam para a fila
    for nota in db.scalars(_base_notas(db).where(models.NotaFiscal.nfstock_status == "ERRO")):
        if any(x in (nota.nfstock_log or "") for x in ERROS_DE_REDE):
            nota.nfstock_status = ""
    conferir_processando(db, tk, sessao)
    db.commit()
    enviadas, falhas_rede = 0, 0
    for evento, lista in ((False, pendentes(db)), (True, eventos_pendentes(db))):
        for nota in lista:
            try:
                enviar_nota(db, nota, tk, evento=evento, sessao=sessao)
                enviadas += 0 if evento else 1
                falhas_rede = 0
            except requests.RequestException:
                falhas_rede += 1
            db.commit()
            if falhas_rede >= 3:   # NF-Stock fora do ar/lento: para e tenta no próximo ciclo
                return dict(enviadas=enviadas, rede=True)
    return dict(enviadas=enviadas, rede=False)


def ciclo(agora: datetime | None = None) -> bool:
    """Chamado pelo agendador a cada minuto; roda de 10 em 10 minutos quando ligado."""
    from .configuracoes import gravar_config, ler_config
    from .db import SessionLocal
    agora = agora or datetime.now()
    with SessionLocal() as db:
        if not ligado(db):
            return False
        ult = ler_config(db, CHAVE_ULTIMO)
        try:
            if ult and agora - datetime.fromisoformat(ult) < timedelta(minutes=INTERVALO_MIN):
                return False
        except ValueError:
            pass
        gravar_config(db, CHAVE_ULTIMO, agora.isoformat(timespec="seconds"))
        db.commit()
        try:
            rodar(db)
        except ErroNFStock as e:
            log.warning("NF-Stock: %s", e)
            db.rollback()
        return True
