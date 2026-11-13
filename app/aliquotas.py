"""Alíquota nominal do Simples Nacional para o "% aproximado dos tributos" (pTotTribSN) das notas.

Fonte: o extrato do PGDAS-D (o mesmo lido na Conferência PGDAS-D).
- Notas do mês T usam o RBT12 do PA T: soma das receitas dos 12 meses anteriores a T.
  Definitiva: extrato de T-1 (RBT12 de T-1 + RPA de T-1 − receita de T-13).
  Provisória: extrato de T-2 + faturamento conferido de T-1 (NFS-e do ERP + NF-Stock).
- Início de atividade (< 12 meses): RBT12 proporcionalizado (média mensal × 12).
- Anexo: as atividades declaradas no extrato (o fator r diz III ou V). Empresa com mais de um anexo:
  cada nota usa o anexo do serviço (cadastro do serviço → código de tributação → anexo principal).
"""
from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models

LIMITES = (Decimal("180000"), Decimal("360000"), Decimal("720000"), Decimal("1800000"), Decimal("3600000"), Decimal("4800000"))
TABELAS = {   # LC 123/2006, Anexos I a V (redação da LC 155/2016) — alíquotas nominais por faixa
    "I": ("4.00", "7.30", "9.50", "10.70", "14.30", "19.00"),
    "II": ("4.50", "7.80", "10.00", "11.20", "14.70", "30.00"),
    "III": ("6.00", "11.20", "13.50", "16.00", "21.00", "33.00"),
    "IV": ("4.50", "9.00", "10.20", "14.00", "22.00", "33.00"),
    "V": ("15.50", "18.00", "19.50", "20.50", "23.00", "30.50"),
}
SERVICOS = ("III", "IV", "V")
ROTULO_ANEXO = {"I": "I · Comércio", "II": "II · Indústria", "III": "III · Serviços", "IV": "IV · Serviços", "V": "V · Serviços"}
# Serviços do Anexo IV (LC 123, art. 18, § 5º-C): obras e construção, vigilância, limpeza e conservação, advocacia.
ANEXO_IV_ITENS = ("0702", "0704", "0705", "0710", "0711", "1102", "1714")
SUBLIMITE = Decimal("3600000")
CHAVE_AUTO = "aliquotas_simples_auto"

_DIN = re.compile(r"\d{1,3}(?:\.\d{3})*,\d{2}")


def _v(txt: str) -> Decimal:
    return Decimal(txt.replace(".", "").replace(",", "."))


def ref_mais(ref: str, n: int) -> str:
    a, m = int(ref[:4]), int(ref[5:7]) - 1 + n
    return f"{a + m // 12:04d}-{m % 12 + 1:02d}"


def faixa(rbt12: Decimal) -> int:
    for i, lim in enumerate(LIMITES, 1):
        if rbt12 <= lim:
            return i
    return 6


def nominal(anexo: str, rbt12: Decimal) -> Decimal:
    return Decimal(TABELAS[anexo][faixa(rbt12) - 1])


# ---------------------------------------------------------------- leitura do extrato

def _anexo_atividade(desc: str, anexo_r: str | None) -> tuple[str, str]:
    """(anexo, observação) a partir da descrição da atividade no extrato."""
    d = re.sub(r"\s+", " ", desc)
    m = re.search(r"Anexo (V|IV|III|II|I)\b", d)
    if m:
        return m.group(1), ""
    if re.search(r"n[ãa]o sujeit", d, re.I):
        return "III", "anexo deduzido (não sujeito ao fator r)"
    if re.search(r"fator\s*[“\"']?r", d, re.I):
        return (anexo_r or "V"), "" if anexo_r else "fator r não lido; considerado Anexo V"
    if re.search(r"revenda de mercadorias", d, re.I):
        return "I", ""
    if re.search(r"industrializ", d, re.I):
        return "II", ""
    if re.search(r"loca[çc][ãa]o de bens m[óo]veis|transporte|comunica[çc][ãa]o|servi[çc]os cont[áa]beis", d, re.I):
        return "III", ""
    return "III", "anexo deduzido (atividade não reconhecida)"


def ler_extrato(texto: str) -> dict:
    """PA, RPA, RBT12, receitas anteriores {AAAA-MM: valor}, data de abertura, fator r e atividades por anexo."""
    t = texto or ""
    out = {"pa": None, "rpa": None, "rbt12": None, "anteriores": {}, "abertura": None, "anexo_r": None,
           "anexos": {}, "obs": []}
    m = re.search(r"Per[íi]odo de Apura[çc][ãa]o \(PA\):\s*(\d{2})/(\d{4})", t)
    if m:
        out["pa"] = f"{m.group(2)}-{m.group(1)}"
    from .conferencia import ler_declaracao
    out.update({k: v for k, v in ler_declaracao(t).items()})
    m = re.search(r"Data de Abertura:\s*(\d{2})/(\d{2})/(\d{4})", t)
    if m:
        out["abertura"] = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    # 2.2) receitas anteriores: mercado interno + externo, mês a mês
    sec = re.search(r"2\.2\)\s*Receitas Brutas Anteriores(.*?)(?:2\.3\)|2\.4\)|3\) Informa)", t, re.S)
    if sec:
        for mm, aaaa, val in re.findall(r"(\d{2})/(\d{4})\s+(\d{1,3}(?:\.\d{3})*,\d{2})", sec.group(1)):
            k = f"{aaaa}-{mm}"
            out["anteriores"][k] = out["anteriores"].get(k, Decimal("0")) + _v(val)
    m = re.search(r"Fator r\s*=\s*[\d,]+\s*-\s*Anexo\s*(V|III)\b", t)
    if m:
        out["anexo_r"] = m.group(1)
    # atividades: cada bloco "Valor do Débito por Tributo para a Atividade" traz a descrição e a receita
    for bloco in re.split(r"Valor do D[ée]bito por Tributo para a Atividade \(R\$\):", t)[1:]:
        desc, _, resto = bloco.partition("Receita Bruta Informada")
        mv = _DIN.search(resto)
        anexo, obs = _anexo_atividade(desc, out["anexo_r"])
        out["anexos"][anexo] = out["anexos"].get(anexo, Decimal("0")) + (_v(mv.group(0)) if mv else Decimal("0"))
        if obs and obs not in out["obs"]:
            out["obs"].append(obs)
    return out


def rbt12_do_pa(ext: dict, alvo: str, extras: dict[str, Decimal] | None = None) -> tuple[Decimal, bool]:
    """RBT12 do PA `alvo` a partir do extrato de um PA anterior (+ meses conhecidos depois dele).
    Devolve (valor usado para a faixa, proporcionalizado?)."""
    meses = dict(ext["anteriores"])
    if ext.get("pa") and ext.get("rpa") is not None:
        meses[ext["pa"]] = ext["rpa"]
    meses.update(extras or {})
    janela = [ref_mais(alvo, -i) for i in range(12, 0, -1)]
    total = sum((meses.get(k, Decimal("0")) for k in janela), Decimal("0"))
    ab = ext.get("abertura")
    if ab:
        ini = f"{ab.year:04d}-{ab.month:02d}"
        n = sum(1 for k in janela if k >= ini)   # meses de atividade antes do PA
        if 0 < n < 12:
            return (total / n * 12).quantize(Decimal("0.01")), True
        if n == 0:   # primeiro mês de atividade: receita do próprio mês × 12 (desconhecida aqui)
            return total, True
    return total, False


# ---------------------------------------------------------------- cálculo por empresa

def _extrato(db: Session, p: models.Prestador, ref: str, pastas: dict):
    from . import conferencia as cf
    try:
        cod = int(re.sub(r"\D", "", p.codigo_interno or "") or 0)
    except ValueError:
        cod = 0
    arq = cf.achar_extrato(pastas.get(cod), ref)
    if arq is None:
        return None, None
    texto = cf.texto_pdf(arq)
    basico = re.search(r"CNPJ B[áa]sico:\s*([\d.]+)", texto)
    if basico and re.sub(r"\D", "", basico.group(1)) != p.cnpj[:8]:
        return None, None
    ext = ler_extrato(texto)
    if ext["pa"] and ext["pa"] != ref:
        return None, None
    return ext, arq


def _faturamento(db: Session, p: models.Prestador, ref: str) -> Decimal:
    from . import conferencia as cf
    r = db.scalar(select(models.ConferenciaPGDAS).where(models.ConferenciaPGDAS.prestador_id == p.id,
                                                         models.ConferenciaPGDAS.referencia == ref))
    if r is not None and r.declarado is not None:
        return r.declarado
    if r is not None and r.faturamento_em:
        return r.faturamento
    v, _n = cf.nfse_do_mes(db, p, ref)
    outros, _d = cf.outros_do_mes(db, p, ref)
    return v + (outros or 0)


def calcular_empresa(db: Session, p: models.Prestador, alvo: str, pastas: dict) -> list[models.AliquotaSimples] | str:
    """Grava as alíquotas da empresa para as notas do mês `alvo`. Devolve as linhas ou o motivo de não calcular."""
    base = ref_mais(alvo, -1)
    ext, arq = _extrato(db, p, base, pastas)
    origem, extras = "DEFINITIVA", {}
    if ext is None:
        base = ref_mais(alvo, -2)
        ext, arq = _extrato(db, p, base, pastas)
        if ext is None:
            return f"sem extrato do PGDAS-D de {ref_mais(alvo, -1)[5:]}/{ref_mais(alvo, -1)[:4]} nem do mês anterior"
        origem, extras = "PROVISORIA", {ref_mais(alvo, -1): _faturamento(db, p, ref_mais(alvo, -1))}
    if not ext["anexos"]:
        return "extrato sem atividades declaradas (sem receita no mês)"
    rbt, prop = rbt12_do_pa(ext, alvo, extras)
    anteriores = {a.anexo: a for a in db.scalars(select(models.AliquotaSimples).where(
        models.AliquotaSimples.prestador_id == p.id, models.AliquotaSimples.referencia == ref_mais(alvo, -1)))}
    atuais = {a.anexo: a for a in db.scalars(select(models.AliquotaSimples).where(
        models.AliquotaSimples.prestador_id == p.id, models.AliquotaSimples.referencia == alvo))}
    if origem == "PROVISORIA" and any(a.origem == "DEFINITIVA" for a in atuais.values()):
        return list(atuais.values())   # já há definitiva: não volta para provisória
    obs = list(ext["obs"])
    if rbt > SUBLIMITE:
        obs.append("acima do sublimite: ISS/ICMS fora do DAS")
    for anexo in list(atuais):
        if anexo not in ext["anexos"]:
            db.delete(atuais.pop(anexo))
    out = []
    for anexo, receita in ext["anexos"].items():
        a = atuais.get(anexo) or models.AliquotaSimples(prestador_id=p.id, referencia=alvo, anexo=anexo)
        if a.id is None:
            db.add(a)
        ant = anteriores.get(anexo)
        a.receita_anexo, a.rbt12, a.proporcional = receita, rbt, prop
        a.faixa, a.nominal = faixa(rbt), nominal(anexo, rbt)
        a.faixa_anterior = ant.faixa if ant else None
        a.origem, a.base_ref, a.calculada_em = origem, base, datetime.now()
        a.obs = "; ".join(obs)[:300]
        out.append(a)
    return out


def anexo_principal(linhas: list) -> str | None:
    """O anexo de serviço com mais receita (é o que vale como padrão da empresa na NFS-e)."""
    serv = [a for a in linhas if a.anexo in SERVICOS] or list(linhas)
    return max(serv, key=lambda a: a.receita_anexo or 0).anexo if serv else None


def auto_ligado(db: Session) -> bool:
    from .configuracoes import ler_config
    return (ler_config(db, CHAVE_AUTO) or "1") == "1"


def atualizar(db: Session, alvo: str | None = None, aplicar: bool | None = None) -> dict:
    """Calcula para todas as empresas do Simples. Com `aplicar`, grava a alíquota do anexo principal no cadastro."""
    from . import conferencia as cf
    alvo = alvo or date.today().strftime("%Y-%m")
    aplicar = auto_ligado(db) if aplicar is None else aplicar
    pastas = cf._pastas_empresas(cf.pasta_extratos(db))
    res = dict(calculadas=0, definitivas=0, mudaram=0, sem=[], aplicadas=0)
    for p in cf.empresas(db):
        linhas = calcular_empresa(db, p, alvo, pastas)
        if isinstance(linhas, str):
            res["sem"].append((p.codigo_fmt, linhas))
            continue
        res["calculadas"] += 1
        res["definitivas"] += any(a.origem == "DEFINITIVA" for a in linhas)
        res["mudaram"] += any(a.faixa_anterior and a.faixa_anterior != a.faixa for a in linhas)
        if aplicar and alvo == date.today().strftime("%Y-%m"):
            princ = next(a for a in linhas if a.anexo == anexo_principal(linhas))
            if Decimal(p.p_tot_trib_sn or 0) != princ.nominal:
                p.p_tot_trib_sn = princ.nominal
                res["aplicadas"] += 1
    db.commit()
    return res


# ---------------------------------------------------------------- uso na emissão

def anexo_do_servico(db: Session, p: models.Prestador, ctrib: str, linhas: list) -> str | None:
    disponiveis = {a.anexo for a in linhas}
    fixo = db.scalar(select(models.CodigoEmpresa.anexo_simples).where(
        models.CodigoEmpresa.prestador_id == p.id, models.CodigoEmpresa.tipo == "CTRIBNAC",
        models.CodigoEmpresa.codigo == (ctrib or ""), models.CodigoEmpresa.anexo_simples != ""))
    if fixo and fixo in disponiveis:
        return fixo
    serv = [a for a in disponiveis if a in SERVICOS]
    if len(serv) <= 1:
        return serv[0] if serv else anexo_principal(linhas)
    if (ctrib or "")[:4] in ANEXO_IV_ITENS and "IV" in disponiveis:
        return "IV"
    resto = [a for a in linhas if a.anexo in SERVICOS and a.anexo != "IV"]
    return max(resto, key=lambda a: a.receita_anexo or 0).anexo if resto else anexo_principal(linhas)


def aliquota_para_nota(db: Session, p: models.Prestador, ctrib: str, ref: str | None = None) -> Decimal | None:
    """% para o pTotTribSN da nota; None = usar o do cadastro da empresa."""
    if p.regime not in ("SIMPLES", "SIMPLES_EXCESSO"):
        return None
    ref = ref or date.today().strftime("%Y-%m")
    linhas = list(db.scalars(select(models.AliquotaSimples).where(models.AliquotaSimples.prestador_id == p.id,
                                                                  models.AliquotaSimples.referencia == ref)))
    if not linhas or not auto_ligado(db):
        return None
    anexo = anexo_do_servico(db, p, ctrib, linhas)
    return next((a.nominal for a in linhas if a.anexo == anexo), None)


def linhas_tela(db: Session, alvo: str) -> list[dict]:
    from . import conferencia as cf
    por = {}
    for a in db.scalars(select(models.AliquotaSimples).where(models.AliquotaSimples.referencia == alvo)):
        por.setdefault(a.prestador_id, []).append(a)
    out = []
    for p in cf.empresas(db):
        ls = sorted(por.get(p.id, []), key=lambda a: ("I", "II", "III", "IV", "V").index(a.anexo))
        princ = anexo_principal(ls) if ls else None
        out.append(dict(p=p, linhas=ls, principal=princ,
                        mudou=any(a.faixa_anterior and a.faixa_anterior != a.faixa for a in ls),
                        cadastro=Decimal(p.p_tot_trib_sn or 0),
                        difere=bool(princ) and Decimal(p.p_tot_trib_sn or 0) != next(a.nominal for a in ls if a.anexo == princ)))
    return out
