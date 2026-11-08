"""Tabelas de referência: municípios do IBGE, códigos de tributação nacional (cTribNac),
NBS e a correlação subitem da LC 116 × NBS.

- Municípios: arquivo fixo em app/dados_ref/municipios.csv (código;nome;UF).
- cTribNac / NBS / correlação: importados das planilhas oficiais do Portal da NFS-e
  (Anexo B e Anexo VIII), pela tela de Configurações. Enquanto não houver planilha, o sistema
  aproveita os códigos que já aparecem nas notas e nos cadastros antigos.
"""
from __future__ import annotations

import csv
import io
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .. import models

REF = Path(__file__).parent / "dados_ref"


def sem_acento(txt: str) -> str:
    return unicodedata.normalize("NFKD", txt or "").encode("ascii", "ignore").decode().lower()


# ---- Municípios -----------------------------------------------------------------

@lru_cache(maxsize=1)
def municipios() -> dict[str, tuple[str, str]]:
    tabela = {}
    with open(REF / "municipios.csv", encoding="utf-8", newline="") as f:
        for codigo, nome, uf in csv.reader(f, delimiter=";"):
            tabela[codigo] = (nome, uf)
    return tabela


def nome_municipio(codigo: str) -> str:
    nome, uf = municipios().get((codigo or "").strip(), ("", ""))
    return f"{nome}/{uf}" if nome else (codigo or "")


def buscar_municipios(q: str, limite: int = 20) -> list[dict]:
    termo = sem_acento(q).strip()
    if not termo:
        return []
    uf_filtro = ""
    m = re.match(r"^(.*?)[\s/-]+([a-z]{2})$", termo)  # "betim mg" ou "betim/mg"
    if m:
        termo, uf_filtro = m.group(1).strip(), m.group(2)
    inicio, meio = [], []
    for cod, (nome, uf) in municipios().items():
        if uf_filtro and uf.lower() != uf_filtro:
            continue
        n = sem_acento(nome)
        if n.startswith(termo):
            inicio.append((n, cod, nome, uf))
        elif termo in n or termo == cod:
            meio.append((n, cod, nome, uf))
    achados = sorted(inicio) + sorted(meio)
    return [{"codigo": c, "nome": f"{nome}/{uf}"} for _n, c, nome, uf in achados[:limite]]


# ---- Formatação de códigos -------------------------------------------------------------

def fmt_ctribnac(c: str) -> str:
    c = re.sub(r"\D", "", c or "")
    return f"{c[:2]}.{c[2:4]}.{c[4:6]}" if len(c) == 6 else c


def fmt_nbs(c: str) -> str:
    c = re.sub(r"\D", "", c or "")
    return f"{c[0]}.{c[1:5]}.{c[5:7]}.{c[7:9]}" if len(c) == 9 else c


# ---- Obra ----------------------------------------------------------------------------

def subitens_obra(db: Session) -> set[str]:
    cfg = db.get(models.Configuracao, "obra_subitens")
    txt = cfg.valor if cfg and cfg.valor.strip() else models.OBRA_SUBITENS_PADRAO
    return {re.sub(r"\D", "", x)[:4] for x in txt.split(",") if re.sub(r"\D", "", x)}


def exige_obra(db: Session, ctribnac: str) -> bool:
    return bool(ctribnac) and re.sub(r"\D", "", ctribnac)[:4] in subitens_obra(db)


# ---- Consultas -------------------------------------------------------------------------

def descricao_ctribnac(db: Session, codigo: str) -> str:
    c = db.get(models.CodigoTributacao, codigo or "")
    return c.descricao if c else ""


def codigos_empresa(db: Session, prestador_id: int, tipo: str) -> list[str]:
    return list(db.scalars(select(models.CodigoEmpresa.codigo).where(
        models.CodigoEmpresa.prestador_id == prestador_id, models.CodigoEmpresa.tipo == tipo)
        .order_by(models.CodigoEmpresa.codigo)))


def lista_ctribnac(db: Session, codigos: list[str] | None = None) -> list[dict]:
    stmt = select(models.CodigoTributacao).order_by(models.CodigoTributacao.codigo)
    if codigos is not None:
        stmt = stmt.where(models.CodigoTributacao.codigo.in_(codigos))
    achados = {c.codigo: c.descricao for c in db.scalars(stmt)}
    if codigos is not None:  # código da empresa ainda sem descrição na tabela
        for c in codigos:
            achados.setdefault(c, "")
    return [{"codigo": c, "rotulo": f"{fmt_ctribnac(c)} - {d}".rstrip(" -")}
            for c, d in sorted(achados.items())]


def lista_nbs(db: Session, codigos: list[str] | None = None) -> list[dict]:
    stmt = select(models.CodigoNBS).order_by(models.CodigoNBS.codigo)
    if codigos is not None:
        stmt = stmt.where(models.CodigoNBS.codigo.in_(codigos))
    achados = {c.codigo: c.descricao for c in db.scalars(stmt)}
    if codigos is not None:
        for c in codigos:
            achados.setdefault(c, "")
    return [{"codigo": c, "rotulo": f"{c} - {d}".rstrip(" -")} for c, d in sorted(achados.items())]


def nbs_correlatas(db: Session, ctribnac: str) -> list[str]:
    sub = re.sub(r"\D", "", ctribnac or "")[:4]
    return list(db.scalars(select(models.CorrelacaoNBS.nbs).where(models.CorrelacaoNBS.subitem == sub)))


def buscar_ctribnac(db: Session, q: str, limite: int = 30) -> list[dict]:
    termo = sem_acento(q).strip()
    dig = re.sub(r"\D", "", q or "")
    out = []
    for c in db.scalars(select(models.CodigoTributacao).order_by(models.CodigoTributacao.codigo)):
        if (dig and c.codigo.startswith(dig)) or (termo and not dig and termo in sem_acento(c.descricao)):
            out.append({"codigo": c.codigo, "rotulo": f"{fmt_ctribnac(c.codigo)} - {c.descricao}"})
            if len(out) >= limite:
                break
    return out


def buscar_nbs(db: Session, q: str, limite: int = 30) -> list[dict]:
    termo = sem_acento(q).strip()
    dig = re.sub(r"\D", "", q or "")
    out = []
    for c in db.scalars(select(models.CodigoNBS).order_by(models.CodigoNBS.codigo)):
        if (dig and c.codigo.startswith(dig)) or (termo and not dig and termo in sem_acento(c.descricao)):
            out.append({"codigo": c.codigo, "rotulo": f"{c.codigo} - {c.descricao}"})
            if len(out) >= limite:
                break
    return out


# ---- Gravação ---------------------------------------------------------------------------

def gravar_ctribnac(db: Session, codigo: str, descricao: str, oficial: bool = False) -> None:
    codigo = re.sub(r"\D", "", codigo or "")
    if len(codigo) != 6:
        return
    c = db.get(models.CodigoTributacao, codigo)
    if c is None:
        db.add(models.CodigoTributacao(codigo=codigo, descricao=(descricao or "").strip(), oficial=oficial))
    elif oficial or (not c.oficial and descricao and not c.descricao):
        c.descricao = (descricao or c.descricao).strip()
        c.oficial = c.oficial or oficial


def gravar_nbs(db: Session, codigo: str, descricao: str, oficial: bool = False) -> None:
    codigo = re.sub(r"\D", "", codigo or "")
    if len(codigo) != 9:
        return
    c = db.get(models.CodigoNBS, codigo)
    if c is None:
        db.add(models.CodigoNBS(codigo=codigo, descricao=(descricao or "").strip(), oficial=oficial))
    elif oficial or (not c.oficial and descricao and not c.descricao):
        c.descricao = (descricao or c.descricao).strip()
        c.oficial = c.oficial or oficial


def vincular_codigo(db: Session, prestador_id: int, tipo: str, codigo: str) -> bool:
    codigo = re.sub(r"\D", "", codigo or "")
    if not codigo or (tipo == "CTRIBNAC" and len(codigo) != 6) or (tipo == "NBS" and len(codigo) != 9):
        return False
    existe = db.scalar(select(models.CodigoEmpresa).where(
        models.CodigoEmpresa.prestador_id == prestador_id, models.CodigoEmpresa.tipo == tipo,
        models.CodigoEmpresa.codigo == codigo))
    if existe is None:
        db.add(models.CodigoEmpresa(prestador_id=prestador_id, tipo=tipo, codigo=codigo))
        db.flush()
        return True
    return False


# ---- Planilhas oficiais ----------------------------------------------------------------

def _so_dig(v) -> str:
    return re.sub(r"\D", "", _texto_celula(v))


def _texto_celula(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


def ler_planilhas(conteudo: bytes) -> dict:
    """Lê o Anexo B (abas LISTA.SERV.NAC. e LISTA.NBS) e/ou o Anexo VIII (aba 'tabela geral').
    Devolve {"ctribnac": {cod: desc}, "nbs": {cod: desc}, "correlacao": {(subitem, nbs)}}."""
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(conteudo), read_only=True, data_only=True)
    out = {"ctribnac": {}, "nbs": {}, "correlacao": set()}
    for ws in wb.worksheets:
        linhas = ws.iter_rows(values_only=True)
        cab = next(linhas, None)
        if not cab:
            continue
        cab = [sem_acento(_texto_celula(c)) for c in cab]
        if cab and cab[0].startswith("codigo de tributacao nacional"):
            for r in linhas:
                r = list(r) + [None] * 5
                item, sub, desd = _so_dig(r[1]), _so_dig(r[2]), _so_dig(r[3])
                if item and sub and desd and int(desd) > 0:
                    out["ctribnac"][f"{int(item):02d}{int(sub):02d}{int(desd):02d}"] = _texto_celula(r[4])
        elif cab and cab[0].startswith("codigo nbs"):
            for r in linhas:
                cod = _so_dig(r[0] if r else "")
                if len(cod) == 9:
                    out["nbs"][cod] = _texto_celula(r[1] if len(r) > 1 else "")
        elif cab and cab[0].startswith("item lc 116") and "nbs" in cab:
            i_nbs = cab.index("nbs")
            i_desc = i_nbs + 1
            atual = ""
            for r in linhas:
                r = list(r) + [None] * 12
                m = re.match(r"^\s*(\d{1,2})\.(\d{2})", _texto_celula(r[0]))
                if m:
                    atual = f"{int(m.group(1)):02d}{m.group(2)}"
                cod = _so_dig(r[i_nbs])
                if atual and len(cod) == 9:
                    out["correlacao"].add((atual, cod))
                    out["nbs"].setdefault(cod, _texto_celula(r[i_desc]))
    return out


def gravar_tabelas(db: Session, dados: dict) -> dict:
    for c, d in dados["ctribnac"].items():
        gravar_ctribnac(db, c, d, oficial=True)
    for c, d in dados["nbs"].items():
        gravar_nbs(db, c, d, oficial=True)
    corr = dados["correlacao"]
    if corr:
        db.execute(delete(models.CorrelacaoNBS).where(models.CorrelacaoNBS.subitem.in_({s for s, _ in corr})))
        for s_, n in sorted(corr):
            db.add(models.CorrelacaoNBS(subitem=s_, nbs=n))
    db.flush()
    return {"ctribnac": len(dados["ctribnac"]), "nbs": len(dados["nbs"]), "correlacoes": len(corr)}


def importar_planilha(db: Session, conteudo: bytes, nome: str) -> dict:
    if not nome.lower().endswith(".xlsx"):
        raise ValueError("envie a planilha oficial em .xlsx")
    dados = ler_planilhas(conteudo)
    if not any(dados.values()):
        raise ValueError("não reconheci as abas do Anexo B nem do Anexo VIII")
    return gravar_tabelas(db, dados)


def carregar_tabelas_embutidas(db: Session) -> dict | None:
    """Na primeira subida (ou quando a versão embutida mudar), grava as tabelas oficiais
    que acompanham o sistema (app/dados_ref/*.csv)."""
    versao = (REF / "versao_tabelas.txt").read_text(encoding="utf-8").strip() if (REF / "versao_tabelas.txt").exists() else ""
    cfg = db.get(models.Configuracao, "tabelas_embutidas_versao")
    if not versao or (cfg and cfg.valor == versao):
        return None
    dados = {"ctribnac": {}, "nbs": {}, "correlacao": set()}
    with open(REF / "ctribnac.csv", encoding="utf-8", newline="") as f:
        dados["ctribnac"] = {c: d for c, d in csv.reader(f, delimiter=";")}
    with open(REF / "nbs.csv", encoding="utf-8", newline="") as f:
        dados["nbs"] = {c: d for c, d in csv.reader(f, delimiter=";")}
    with open(REF / "correlacao_nbs.csv", encoding="utf-8", newline="") as f:
        dados["correlacao"] = {(a, b) for a, b in csv.reader(f, delimiter=";")}
    q = gravar_tabelas(db, dados)
    if cfg is None:
        db.add(models.Configuracao(chave="tabelas_embutidas_versao", valor=versao))
    else:
        cfg.valor = versao
    db.flush()
    return q


def semear_do_historico(db: Session) -> dict:
    """Aproveita códigos já usados (serviços padrão antigos e notas) para preencher as tabelas
    e vincular cada código à empresa que o usa."""
    from ..config import XML_DIR
    from .nfse_xml import extrair

    n_vinc = 0
    for s in db.scalars(select(models.ServicoPadrao)):
        if s.codigo_tributacao_nacional:
            gravar_ctribnac(db, s.codigo_tributacao_nacional, "")
            n_vinc += vincular_codigo(db, s.prestador_id, "CTRIBNAC", s.codigo_tributacao_nacional)
        if s.codigo_nbs:
            gravar_nbs(db, s.codigo_nbs, "")
            n_vinc += vincular_codigo(db, s.prestador_id, "NBS", s.codigo_nbs)
    for n in db.scalars(select(models.NotaFiscal).where(models.NotaFiscal.papel == "PRESTADO")):
        desc = ""
        if n.caminho_xml and (XML_DIR / n.caminho_xml).exists():
            try:
                d = extrair((XML_DIR / n.caminho_xml).read_text(encoding="utf-8"))
                desc = d.get("xTribNac") or ""
                n.codigo_tributacao_nacional = n.codigo_tributacao_nacional or d.get("ctn") or ""
                n.codigo_nbs = n.codigo_nbs or d.get("nbs") or ""
            except Exception:  # noqa: BLE001
                pass
        if n.codigo_tributacao_nacional:
            gravar_ctribnac(db, n.codigo_tributacao_nacional, desc)
            n_vinc += vincular_codigo(db, n.prestador_id, "CTRIBNAC", n.codigo_tributacao_nacional)
        if n.codigo_nbs:
            gravar_nbs(db, n.codigo_nbs, "")
            n_vinc += vincular_codigo(db, n.prestador_id, "NBS", n.codigo_nbs)
    db.flush()
    return {"vinculos": n_vinc}
