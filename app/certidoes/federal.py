"""Certidão federal (Conjunta RFB/PGFN) emitida pela API Consulta CND do Serpro.

O Pack CND não traz a federal; aqui ela é emitida direto na Receita e gravada como uma Certidao igual às
do Pack (tipo FEDERAL, pack_id "RFB-<código de controle>"), entrando na mesma lista e na mesma entrega ao
Bot Arquivador. A renovação automática só emite quando não há federal válida por mais de DIAS_AVISO dias
e não houve tentativa cobrada sem sucesso nos últimos DIAS_ESPERA_FALHA dias (evita pagar todo dia por
uma empresa com pendência).
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..config import DATA_DIR
from ..configuracoes import ler_config
from ..serpro import apis
from . import tipos as T

NOME_FEDERAL = next(n for n, t in T.TIPOS.items() if t == "FEDERAL")
DIAS_AVISO = 15
DIAS_ESPERA_FALHA = 7


def automatico(db: Session) -> bool:
    return apis.modo(db, "CND") != "desligado" and (ler_config(db, "certidoes_federal_auto") or "1") == "1"


def _situacao(tipo_certidao: str) -> str:
    t = (tipo_certidao or "").lower()
    if "efeito" in t:
        return "Positiva com Efeitos de Negativa"
    if "negativa" in t:
        return "Negativa"
    return (tipo_certidao or "Positiva")[:60]


def federal_atual(db: Session, p: models.Prestador) -> models.Certidao | None:
    certs = db.scalars(select(models.Certidao).where(models.Certidao.prestador_id == p.id, models.Certidao.tipo == "FEDERAL",
                                                     models.Certidao.no_pack.is_(True))).all()
    return max(certs, key=lambda c: (c.vencimento or date.min, c.id), default=None)


def ultima_tentativa(db: Session, p: models.Prestador) -> models.ConsultaAPI | None:
    return db.scalar(select(models.ConsultaAPI).where(models.ConsultaAPI.produto == "CND", models.ConsultaAPI.documento == p.cnpj)
                     .order_by(models.ConsultaAPI.id.desc()).limit(1))


def precisa_renovar(db: Session, p: models.Prestador, hoje: date | None = None) -> bool:
    hoje = hoje or date.today()
    c = federal_atual(db, p)
    if c and c.vencimento and c.vencimento > hoje + timedelta(days=DIAS_AVISO) and c.situacao in T.SITUACOES_OK:
        return False
    falha = db.scalar(select(models.ConsultaAPI).where(
        models.ConsultaAPI.produto == "CND", models.ConsultaAPI.documento == p.cnpj, models.ConsultaAPI.ok.is_(False),
        models.ConsultaAPI.custo > 0, models.ConsultaAPI.data_hora >= datetime.combine(hoje - timedelta(days=DIAS_ESPERA_FALHA), datetime.min.time()))
        .limit(1))
    return falha is None


def emitir(db: Session, p: models.Prestador, usuario: str = "", agora: datetime | None = None) -> dict:
    """Emite/recupera a federal da empresa. Devolve {ok, mensagem, certidao}."""
    agora = agora or datetime.now()
    r = apis.emitir_cnd(db, p.cnpj, usuario)
    if not (r.tem_certidao and r.pdf):
        return dict(ok=False, mensagem=r.mensagem, certidao=None, status=r.status)
    pack_id = ("RFB-" + r.codigo_controle)[:20]
    c = db.scalar(select(models.Certidao).where(models.Certidao.pack_id == pack_id))
    if c is None:
        c = models.Certidao(prestador_id=p.id, pack_id=pack_id, alterado_em=agora)
        db.add(c)
    elif c.prestador_id != p.id:
        return dict(ok=False, mensagem="Código de controle já registrado para outra empresa; conferir cadastro.", certidao=None, status=r.status)
    destino = DATA_DIR / "certidoes" / p.cnpj / f"{(p.codigo_interno or 'x').zfill(2)}-{pack_id}.pdf"
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_bytes(r.pdf)
    c.tipo_id, c.tipo_nome, c.tipo = "RFB", NOME_FEDERAL, "FEDERAL"
    c.emissao, c.vencimento = r.emissao or agora.date(), r.validade
    c.situacao, c.disponivel, c.no_pack, c.consultado_em = _situacao(r.tipo_certidao), True, True, agora
    c.avisos = "Emitida pela API Consulta CND (Serpro)"
    c.arquivo, c.sha256, c.erro = str(destino.relative_to(DATA_DIR)), hashlib.sha256(r.pdf).hexdigest(), ""
    db.flush()
    return dict(ok=True, mensagem=r.mensagem, certidao=c, status=r.status)


def renovar(db: Session, prestadores: list[models.Prestador], usuario: str = "agendador") -> dict:
    """Renova a federal de quem precisa. Para no primeiro erro de acesso (chaves/limite)."""
    feitas, emitidas, problemas = 0, 0, []
    for p in prestadores:
        if not precisa_renovar(db, p):
            continue
        try:
            r = emitir(db, p, usuario)
        except apis.ErroAPI as e:
            problemas.append(f"Federal: {e}")
            break
        feitas += 1
        if r["ok"]:
            emitidas += 1
        else:
            problemas.append(f"{p.codigo_interno} {p.razao_social} · federal: {r['mensagem']}")
        db.commit()
    return dict(consultadas=feitas, emitidas=emitidas, problemas=problemas)


def empresas(db: Session, prestador_id: int | None = None) -> list[models.Prestador]:
    stmt = select(models.Prestador).where(models.Prestador.ativo.is_(True), models.Prestador.usa_cnd.is_(True))
    if prestador_id:
        stmt = stmt.where(models.Prestador.id == prestador_id)
    return [p for p in db.scalars(stmt).all() if len(p.cnpj or "") == 14]
