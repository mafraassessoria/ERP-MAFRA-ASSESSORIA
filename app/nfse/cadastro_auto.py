"""Cadastros automáticos a partir das NFS-e: tomador da nota, retenções e serviço padrão."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..documentos import so_digitos

# campo do tomador -> chave do dicionário extraído do XML
CAMPOS_TOMADOR = {
    "inscricao_municipal": ("toma_im", 20), "email": ("toma_email", 120), "telefone": ("toma_fone", 20),
    "codigo_municipio_ibge": ("toma_cmun", 7), "cep": ("toma_cep", 8), "logradouro": ("toma_lgr", 150),
    "numero": ("toma_nro", 20), "complemento": ("toma_cpl", 60), "bairro": ("toma_bairro", 60),
}


def _dec(v) -> Decimal:
    try:
        return Decimal(v or "0")
    except InvalidOperation:
        return Decimal("0")


def tomador_da_nota(db: Session, prestador_id: int, d: dict) -> models.Tomador | None:
    """Encontra ou cria o tomador da nota no cadastro do prestador; completa só campos vazios."""
    doc = d.get("toma_doc") or ""
    if not doc:
        return None
    t = db.scalar(select(models.Tomador).where(models.Tomador.prestador_id == prestador_id,
                                               models.Tomador.documento == doc))
    if t is None:
        t = models.Tomador(prestador_id=prestador_id, documento=doc,
                           nome=(d.get("toma_nome") or doc).upper()[:150], ativo=True)
        db.add(t)
    for campo, (chave, tam) in CAMPOS_TOMADOR.items():
        valor = d.get(chave) or ""
        if campo in ("cep", "codigo_municipio_ibge"):
            valor = so_digitos(valor)
        if valor and not getattr(t, campo):
            setattr(t, campo, valor[:tam])
    if t.codigo_municipio_ibge and not t.municipio:
        from .tabelas import municipios
        nome, uf = municipios().get(t.codigo_municipio_ibge, ("", ""))
        t.municipio, t.uf = nome[:80], t.uf or uf
    db.flush()
    return t


def aplicar_valores(nota: models.NotaFiscal, d: dict) -> None:
    """Retenções, alíquota, dedução e códigos lidos do XML da nota."""
    nota.tipo_retencao_iss = int(d["ret"]) if str(d.get("ret", "")).isdigit() else (nota.tipo_retencao_iss or 1)
    nota.tributacao_issqn = int(d["trib"]) if str(d.get("trib", "")).isdigit() else (nota.tributacao_issqn or 1)
    nota.aliquota_iss = _dec(d.get("aliq"))
    nota.ret_inss = _dec(d.get("ret_cp"))
    nota.ret_irrf = _dec(d.get("ret_irrf"))
    nota.ret_csll = _dec(d.get("ret_csll"))
    if d.get("ded_pdr"):
        nota.ded_tipo, nota.ded_valor = "PERCENTUAL", _dec(d["ded_pdr"])
    elif d.get("ded_vdr"):
        nota.ded_tipo, nota.ded_valor = "VALOR", _dec(d["ded_vdr"])
    if d.get("viss"):
        nota.valor_iss = _dec(d["viss"])
    nota.codigo_nbs = (d.get("nbs") or nota.codigo_nbs or "")[:9]
    nota.codigo_tributacao_municipal = (d.get("ctm") or nota.codigo_tributacao_municipal or "")[:3]
    nota.municipio_prestacao = (d.get("loc") or nota.municipio_prestacao or "")[:7]
    if d.get("info") and not nota.info_complementares:
        nota.info_complementares = d["info"][:2000]


def obra_da_nota(db: Session, prestador_id: int, tomador: models.Tomador | None, d: dict) -> models.Obra | None:
    """Encontra ou cria a obra informada na nota (vinculada ao tomador)."""
    o = d.get("obra")
    if not o or tomador is None:
        return None
    existentes = db.scalars(select(models.Obra).where(models.Obra.prestador_id == prestador_id,
                                                      models.Obra.tomador_id == tomador.id)).all()
    for x in existentes:
        if (o["cno"] and x.cno == o["cno"]) or (o["cib"] and x.cib == o["cib"]) or \
           (not o["cno"] and not o["cib"] and x.cep == so_digitos(o["cep"]) and x.numero == o["numero"]):
            return x
    nova = models.Obra(prestador_id=prestador_id, tomador_id=tomador.id, cno=o["cno"][:30], cib=o["cib"][:8],
                       inscricao_imobiliaria=o["inscricao_imobiliaria"][:30], cep=so_digitos(o["cep"])[:8],
                       logradouro=o["logradouro"][:150], numero=o["numero"][:20], complemento=o["complemento"][:60],
                       bairro=o["bairro"][:60], codigo_municipio_ibge=(o.get("codigo_municipio_ibge") or "")[:7], ativo=True)
    db.add(nova)
    db.flush()
    return nova
