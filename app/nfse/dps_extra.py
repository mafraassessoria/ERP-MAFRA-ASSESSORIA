"""Grupos da DPS que a biblioteca base não monta: obra, dedução/redução, alíquota do ISS
retido e retenções federais (INSS/IRRF/CSLL).

Cada elemento é inserido na posição exata do leiaute oficial (XSD DPS v1.01):
  serv:     locPrest → cServ → comExt? → obra? → atvEvento? → explRod? → infoCompl?
  valores:  vServPrest → vDescCondIncond? → vDedRed? → trib
  tribMun:  tribISSQN → cPaisResult? → tpImunidade? → exigSusp? → BM? → tpRetISSQN → pAliq?
  tribFed:  piscofins? → vRetCP? → vRetIRRF? → vRetCSLL?
  obra:     inscImobFisc? → (cObra | cCIB | end)
  end:      (CEP | endExt) → xLgr → nro → xCpl? → xBairro
"""
from __future__ import annotations

from decimal import Decimal

import re

from lxml import etree

NS = "http://www.sped.fazenda.gov.br/nfse"


def _q(tag: str) -> str:
    return f"{{{NS}}}{tag}"


def _el(tag: str, texto: str | None = None):
    e = etree.Element(_q(tag))
    if texto is not None:
        e.text = texto
    return e


def _dec(v) -> Decimal:
    return Decimal(v or 0).quantize(Decimal("0.01"))


def _inserir_antes(pai, novo, antes_de: tuple[str, ...]) -> None:
    """Insere `novo` antes do primeiro filho cujo nome esteja em `antes_de`; senão, no fim."""
    for i, filho in enumerate(pai):
        if etree.QName(filho).localname in antes_de:
            pai.insert(i, novo)
            return
    pai.append(novo)


class DadosExtras:
    """O que a nota pede além do básico (vem de NotaFiscal + Obra)."""

    def __init__(self, *, obra=None, ded_tipo: str = "", ded_valor=0, aliquota_iss=0,
                 tp_ret_iss: int = 1, ret_inss=0, ret_irrf=0, ret_csll=0, intermediario=None,
                 valor_recebido_intermediario=0, desconto_incondicionado=0, desconto_condicionado=0,
                 tipo_imunidade=None, tot_trib_tipo: str = "SN", tot_trib_fed=0, tot_trib_est=0, tot_trib_mun=0):
        self.intermediario = intermediario
        self.vreceb = _dec(valor_recebido_intermediario)
        self.desc_incond = _dec(desconto_incondicionado)
        self.desc_cond = _dec(desconto_condicionado)
        self.tipo_imunidade = tipo_imunidade
        self.tot_trib_tipo = tot_trib_tipo or "SN"
        self.tot_trib = (_dec(tot_trib_fed), _dec(tot_trib_est), _dec(tot_trib_mun))
        self.obra = obra
        self.ded_tipo = ded_tipo or ""
        self.ded_valor = _dec(ded_valor)
        self.aliquota_iss = _dec(aliquota_iss)
        self.tp_ret_iss = tp_ret_iss or 1
        self.ret_inss = _dec(ret_inss)
        self.ret_irrf = _dec(ret_irrf)
        self.ret_csll = _dec(ret_csll)

    @property
    def tem_retencao_federal(self) -> bool:
        return any(v > 0 for v in (self.ret_inss, self.ret_irrf, self.ret_csll))


def obra_xml(obra):
    o = _el("obra")
    if obra.inscricao_imobiliaria:
        o.append(_el("inscImobFisc", obra.inscricao_imobiliaria.strip()[:30]))
    if obra.cno:
        o.append(_el("cObra", obra.cno.strip()[:30]))
    elif obra.cib:
        o.append(_el("cCIB", obra.cib.strip()[:8]))
    else:
        end = _el("end")
        end.append(_el("CEP", obra.cep))
        end.append(_el("xLgr", obra.logradouro[:255]))
        end.append(_el("nro", (obra.numero or "S/N")[:60]))
        if obra.complemento:
            end.append(_el("xCpl", obra.complemento[:156]))
        end.append(_el("xBairro", obra.bairro[:60]))
        o.append(end)
    return o


def pessoa_xml(tag: str, p) -> etree._Element:
    """TCInfoPessoa (intermediário): CPF|CNPJ → IM? → xNome → end? → fone? → email?"""
    import re
    e = _el(tag)
    doc = re.sub(r"\D", "", p.documento or "")
    e.append(_el("CNPJ" if len(doc) == 14 else "CPF", doc))
    im = re.sub(r"\D", "", p.inscricao_municipal or "")
    if im:
        e.append(_el("IM", im))
    e.append(_el("xNome", p.nome.strip()[:300]))
    if p.codigo_municipio_ibge and p.cep and p.logradouro and p.bairro:
        end = _el("end")
        nac = _el("endNac")
        nac.append(_el("cMun", p.codigo_municipio_ibge))
        nac.append(_el("CEP", re.sub(r"\D", "", p.cep)))
        end.append(nac)
        end.append(_el("xLgr", p.logradouro[:255]))
        end.append(_el("nro", (p.numero or "S/N")[:60]))
        if p.complemento:
            end.append(_el("xCpl", p.complemento[:156]))
        end.append(_el("xBairro", p.bairro[:60]))
        e.append(end)
    fone = re.sub(r"\D", "", p.telefone or "")
    if 6 <= len(fone) <= 20:
        e.append(_el("fone", fone))
    if p.email and "@" in p.email:
        e.append(_el("email", p.email.strip()[:80]))
    return e


def problemas_obra(obra) -> list[str]:
    if obra is None:
        return []
    if obra.cno or obra.cib:
        if obra.cib and len(obra.cib.strip()) != 8:
            return ["O código CIB da obra tem 8 caracteres."]
        return []
    faltam = [n for n, v in (("CEP", len(obra.cep or "") == 8), ("logradouro", obra.logradouro),
                             ("bairro", obra.bairro)) if not v]
    return [f"Obra sem CNO/CIB: o endereço precisa de {', '.join(faltam)}."] if faltam else []


def complementar(xml_dps: str, extras: DadosExtras) -> str:
    """Recebe a DPS montada (sem assinatura) e devolve com os grupos extras."""
    raiz = etree.fromstring(xml_dps.encode("utf-8"))
    inf = raiz.find(_q("infDPS"))

    # 0) Intermediário: depois de prest/toma, antes de serv
    if extras.intermediario is not None:
        _inserir_antes(inf, pessoa_xml("interm", extras.intermediario), ("serv",))

    # 1) Obra
    if extras.obra is not None:
        serv = inf.find(_q("serv"))
        _inserir_antes(serv, obra_xml(extras.obra), ("atvEvento", "explRod", "infoCompl"))

    valores = inf.find(_q("valores"))
    # 1b) Valor recebido pelo intermediário (vReceb vem antes de vServ) e descontos
    if extras.vreceb > 0:
        vsp = valores.find(_q("vServPrest"))
        vsp.insert(0, _el("vReceb", f"{extras.vreceb:.2f}"))
    if extras.desc_incond > 0 or extras.desc_cond > 0:
        dsc = _el("vDescCondIncond")
        if extras.desc_incond > 0:
            dsc.append(_el("vDescIncond", f"{extras.desc_incond:.2f}"))
        if extras.desc_cond > 0:
            dsc.append(_el("vDescCond", f"{extras.desc_cond:.2f}"))
        _inserir_antes(valores, dsc, ("vDedRed", "trib"))
    # 2) Dedução / redução (percentual ou valor)
    if extras.ded_tipo in ("PERCENTUAL", "VALOR") and extras.ded_valor > 0:
        ded = _el("vDedRed")
        ded.append(_el("pDR" if extras.ded_tipo == "PERCENTUAL" else "vDR", f"{extras.ded_valor:.2f}"))
        _inserir_antes(valores, ded, ("trib",))

    trib = valores.find(_q("trib"))
    # 2b) Tipo de imunidade (só quando tribISSQN = 2), logo depois de tribISSQN/cPaisResult
    tm = trib.find(_q("tribMun"))
    if extras.tipo_imunidade is not None and tm.findtext(_q("tribISSQN")) == "2":
        _inserir_antes(tm, _el("tpImunidade", str(int(extras.tipo_imunidade))), ("exigSusp", "BM", "tpRetISSQN"))
    # 2c) Valor aproximado dos tributos: valores (R$) ou percentuais, no lugar do pTotTribSN
    if extras.tot_trib_tipo in ("VALOR", "PERCENTUAL"):
        tt = trib.find(_q("totTrib"))
        for filho in list(tt):
            tt.remove(filho)
        if extras.tot_trib_tipo == "VALOR":
            g = _el("vTotTrib")
            for tag, v in zip(("vTotTribFed", "vTotTribEst", "vTotTribMun"), extras.tot_trib):
                g.append(_el(tag, f"{v:.2f}"))
        else:
            g = _el("pTotTrib")
            for tag, v in zip(("pTotTribFed", "pTotTribEst", "pTotTribMun"), extras.tot_trib):
                g.append(_el(tag, f"{v:.2f}"))
        tt.append(g)
    # 3) Alíquota do ISS — enviada quando o ISS é retido (E0621 para ME/EPP)
    if extras.tp_ret_iss in (2, 3) and extras.aliquota_iss > 0:
        tm = trib.find(_q("tribMun"))
        tp = tm.find(_q("tpRetISSQN"))
        tp.addnext(_el("pAliq", f"{extras.aliquota_iss:.2f}"))

    # 4) Retenções federais
    if extras.tem_retencao_federal:
        tf = trib.find(_q("tribFed"))
        if tf is None:
            raise ValueError("Retenções federais não podem ser informadas para MEI (E0676).")
        for tag, valor in (("vRetCP", extras.ret_inss), ("vRetIRRF", extras.ret_irrf), ("vRetCSLL", extras.ret_csll)):
            if valor > 0:
                tf.append(_el(tag, f"{valor:.2f}"))

    limpar_textos(raiz)
    return etree.tostring(raiz, encoding="unicode")


_CONTROLE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def limpar_texto(t: str) -> str:
    """Texto livre seguro para a assinatura: sem CR (vira &#13; e a Sefin rejeita com E0714),
    sem tabulação e sem caracteres de controle; espaços repetidos no fim/início das linhas saem."""
    t = (t or "").replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    t = _CONTROLE.sub("", t)
    linhas = [" ".join(linha.split()) for linha in t.split("\n")]
    while linhas and not linhas[-1]:
        linhas.pop()
    return "\n".join(linhas).strip()


def limpar_textos(raiz) -> None:
    for el in raiz.iter():
        if isinstance(el.tag, str) and el.text and len(el) == 0:
            novo = limpar_texto(el.text)
            if novo != el.text:
                el.text = novo
