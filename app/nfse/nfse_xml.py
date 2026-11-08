"""Leitura dos campos de uma NFS-e (XML do ADN/Sefin) usados em cadastros e clonagem."""
from __future__ import annotations

from lxml import etree

from ..documentos import limpar


def _t(el, caminho: str) -> str:
    if el is None:
        return ""
    x = el.find(caminho)
    return (x.text or "").strip() if x is not None and x.text else ""


def _doc(el) -> str:
    for tag in ("CNPJ", "CPF", "NIF"):
        v = _t(el, f"{{*}}{tag}")
        if v:
            return limpar(v)
    return ""


def extrair(xml: str) -> dict | None:
    """Campos da NFS-e úteis para cadastro (a partir da DPS embutida)."""
    try:
        raiz = etree.fromstring(xml.encode("utf-8"))
    except etree.XMLSyntaxError:
        return None
    inf = raiz.find(".//{*}infNFSe")
    dps = raiz.find(".//{*}infDPS")
    if inf is None or dps is None:
        return None
    toma = dps.find("{*}toma")
    serv = dps.find("{*}serv")
    obra = serv.find("{*}obra") if serv is not None else None
    trib_mun = dps.find(".//{*}trib/{*}tribMun")
    trib_fed = dps.find(".//{*}trib/{*}tribFed")
    end_t = toma.find("{*}end") if toma is not None else None
    end_nac = end_t.find("{*}endNac") if end_t is not None else None
    d = {
        "xTribNac": _t(inf, "{*}xTribNac"),
        "vServ": _t(dps, ".//{*}vServPrest/{*}vServ"),
        "toma_doc": _doc(toma) if toma is not None else "",
        "toma_nome": _t(toma, "{*}xNome"),
        "toma_im": _t(toma, "{*}IM"),
        "toma_email": _t(toma, "{*}email"),
        "toma_fone": _t(toma, "{*}fone"),
        "toma_cmun": _t(end_nac, "{*}cMun"),
        "toma_cep": _t(end_nac, "{*}CEP"),
        "toma_lgr": _t(end_t, "{*}xLgr"),
        "toma_nro": _t(end_t, "{*}nro"),
        "toma_cpl": _t(end_t, "{*}xCpl"),
        "toma_bairro": _t(end_t, "{*}xBairro"),
        "loc": _t(serv, ".//{*}locPrest/{*}cLocPrestacao"),
        "ctn": _t(serv, ".//{*}cServ/{*}cTribNac"),
        "ctm": _t(serv, ".//{*}cServ/{*}cTribMun"),
        "nbs": _t(serv, ".//{*}cServ/{*}cNBS"),
        "desc": _t(serv, ".//{*}cServ/{*}xDescServ"),
        "trib": _t(trib_mun, "{*}tribISSQN") or "1",
        "ret": _t(trib_mun, "{*}tpRetISSQN") or "1",
        "aliq": _t(trib_mun, "{*}pAliq"),
        "ret_cp": _t(trib_fed, "{*}vRetCP"),
        "ret_irrf": _t(trib_fed, "{*}vRetIRRF"),
        "ret_csll": _t(trib_fed, "{*}vRetCSLL"),
        "ded_pdr": _t(dps, ".//{*}valores/{*}vDedRed/{*}pDR"),
        "ded_vdr": _t(dps, ".//{*}valores/{*}vDedRed/{*}vDR"),
        "info": _t(serv, ".//{*}infoCompl/{*}xInfComp"),
        "viss": _t(inf.find("{*}valores"), "{*}vISSQN"),
        "obra": None,
    }
    if obra is not None:
        oe = obra.find("{*}end")
        d["obra"] = {
            "inscricao_imobiliaria": _t(obra, "{*}inscImobFisc"), "cno": _t(obra, "{*}cObra"), "cib": _t(obra, "{*}cCIB"),
            "cep": _t(oe, "{*}CEP"), "logradouro": _t(oe, "{*}xLgr"), "numero": _t(oe, "{*}nro"),
            "complemento": _t(oe, "{*}xCpl"), "bairro": _t(oe, "{*}xBairro"),
            "codigo_municipio_ibge": d["loc"],
        }
    return d
