"""Consulta de CNPJ (dados abertos da Receita Federal) e de CEP.

Fontes gratuitas, tentadas em ordem (se uma falhar ou estiver no limite, tenta a próxima):
  1. BrasilAPI        — https://brasilapi.com.br/api/cnpj/v1/{cnpj}
  2. Minha Receita    — https://minhareceita.org/{cnpj}     (mesmo formato da BrasilAPI)
  3. CNPJ.ws pública  — https://publica.cnpj.ws/cnpj/{cnpj} (limite de 3 consultas/minuto)
CEP: ViaCEP (traz o código IBGE) e, em seguida, BrasilAPI.

Os dados vêm da base pública do CNPJ, atualizada pela Receita mensalmente — podem estar
alguns dias/semanas atrasados. CPF não é consultável (dado pessoal protegido pela LGPD).
"""
from __future__ import annotations

import logging
import time

import requests

from .documentos import limpar, so_digitos, validar_cnpj

log = logging.getLogger("emissor.consulta")
TIMEOUT = 10
CABECALHOS = {"User-Agent": "EmissorNFSe-MR/3.0", "Accept": "application/json"}
_cache: dict[str, tuple[float, dict]] = {}
CACHE_SEGUNDOS = 6 * 3600


class ConsultaErro(Exception):
    pass


def _get(url: str) -> dict | None:
    """GET JSON. None = não encontrado (404). Exceção = falha (tenta a próxima fonte)."""
    r = requests.get(url, headers=CABECALHOS, timeout=TIMEOUT)
    if r.status_code == 404:
        return None
    if r.status_code != 200:
        raise ConsultaErro(f"HTTP {r.status_code}")
    return r.json()


def _texto(v) -> str:
    return str(v).strip() if v not in (None, "") else ""


def _telefone(v: str) -> str:
    d = so_digitos(v)
    if len(d) == 10:
        return f"({d[:2]}) {d[2:6]}-{d[6:]}"
    if len(d) == 11:
        return f"({d[:2]}) {d[2:7]}-{d[7:]}"
    return _texto(v)


def _do_formato_brasilapi(d: dict, fonte: str) -> dict:
    tipo = _texto(d.get("descricao_tipo_de_logradouro"))
    logr = _texto(d.get("logradouro"))
    if tipo and logr and not logr.upper().startswith(tipo.upper()):
        logr = f"{tipo} {logr}"
    return {
        "razao_social": _texto(d.get("razao_social")),
        "nome_fantasia": _texto(d.get("nome_fantasia")),
        "cep": so_digitos(_texto(d.get("cep"))),
        "logradouro": logr,
        "numero": _texto(d.get("numero")),
        "complemento": _texto(d.get("complemento")),
        "bairro": _texto(d.get("bairro")),
        "municipio": _texto(d.get("municipio")),
        "uf": _texto(d.get("uf")),
        "codigo_municipio_ibge": so_digitos(_texto(d.get("codigo_municipio_ibge"))),
        "telefone": _telefone(_texto(d.get("ddd_telefone_1"))),
        "email": _texto(d.get("email")).lower(),
        "situacao": _texto(d.get("descricao_situacao_cadastral")).upper(),
        "simples": d.get("opcao_pelo_simples"),
        "mei": d.get("opcao_pelo_mei"),
        "cnae": f"{_texto(d.get('cnae_fiscal'))} - {_texto(d.get('cnae_fiscal_descricao'))}".strip(" -"),
        "fonte": fonte,
    }


def _do_formato_cnpjws(d: dict) -> dict:
    est = d.get("estabelecimento") or {}
    cidade = est.get("cidade") or {}
    estado = est.get("estado") or {}
    simples = d.get("simples") or {}
    tel = f"{_texto(est.get('ddd1'))}{_texto(est.get('telefone1'))}"
    ativ = est.get("atividade_principal") or {}
    tipo = _texto(est.get("tipo_logradouro"))
    logr = _texto(est.get("logradouro"))
    return {
        "razao_social": _texto(d.get("razao_social")),
        "nome_fantasia": _texto(est.get("nome_fantasia")),
        "cep": so_digitos(_texto(est.get("cep"))),
        "logradouro": f"{tipo} {logr}".strip() if tipo and not logr.upper().startswith(tipo.upper()) else logr,
        "numero": _texto(est.get("numero")),
        "complemento": _texto(est.get("complemento")),
        "bairro": _texto(est.get("bairro")),
        "municipio": _texto(cidade.get("nome")),
        "uf": _texto(estado.get("sigla")),
        "codigo_municipio_ibge": so_digitos(_texto(cidade.get("ibge_id"))),
        "telefone": _telefone(tel),
        "email": _texto(est.get("email")).lower(),
        "situacao": _texto(est.get("situacao_cadastral")).upper(),
        "simples": (_texto(simples.get("simples")).upper() == "SIM") if simples else None,
        "mei": (_texto(simples.get("mei")).upper() == "SIM") if simples else None,
        "cnae": f"{_texto(ativ.get('subclasse'))} - {_texto(ativ.get('descricao'))}".strip(" -"),
        "fonte": "CNPJ.ws",
    }


def consultar_cnpj(cnpj: str) -> dict:
    cnpj = limpar(cnpj)
    if not validar_cnpj(cnpj):
        raise ConsultaErro("CNPJ inválido.")
    agora = time.time()
    if cnpj in _cache and agora - _cache[cnpj][0] < CACHE_SEGUNDOS:
        return _cache[cnpj][1]

    fontes = [
        (f"https://brasilapi.com.br/api/cnpj/v1/{cnpj}", lambda d: _do_formato_brasilapi(d, "BrasilAPI")),
        (f"https://minhareceita.org/{cnpj}", lambda d: _do_formato_brasilapi(d, "Minha Receita")),
        (f"https://publica.cnpj.ws/cnpj/{cnpj}", _do_formato_cnpjws),
    ]
    nao_encontrado = 0
    falhas = []
    for url, converter in fontes:
        try:
            d = _get(url)
        except Exception as e:  # noqa: BLE001 — fonte fora do ar ou no limite: tenta a próxima
            falhas.append(f"{url.split('/')[2]}: {type(e).__name__}")
            log.warning("consulta CNPJ falhou em %s: %s", url, e)
            continue
        if d is None:
            nao_encontrado += 1
            continue
        dados = converter(d)
        if dados["razao_social"]:
            _cache[cnpj] = (agora, dados)
            return dados
    if nao_encontrado and not falhas:
        raise ConsultaErro("CNPJ não encontrado na base pública da Receita. Se foi aberto há poucos dias, "
                           "ainda pode não constar; preencha manualmente.")
    raise ConsultaErro("Não foi possível consultar agora (" + "; ".join(falhas) + "). Tente de novo em "
                       "instantes ou preencha manualmente.")


def consultar_cep(cep: str) -> dict:
    cep = so_digitos(cep)
    if len(cep) != 8:
        raise ConsultaErro("CEP deve ter 8 dígitos.")
    chave = f"cep:{cep}"
    agora = time.time()
    if chave in _cache and agora - _cache[chave][0] < CACHE_SEGUNDOS:
        return _cache[chave][1]
    try:
        d = _get(f"https://viacep.com.br/ws/{cep}/json/")
        if d and not d.get("erro"):
            dados = {"cep": cep, "logradouro": _texto(d.get("logradouro")), "bairro": _texto(d.get("bairro")),
                     "municipio": _texto(d.get("localidade")), "uf": _texto(d.get("uf")),
                     "codigo_municipio_ibge": so_digitos(_texto(d.get("ibge"))), "fonte": "ViaCEP"}
            _cache[chave] = (agora, dados)
            return dados
        if d and d.get("erro"):
            raise ConsultaErro("CEP não encontrado.")
    except ConsultaErro:
        raise
    except Exception as e:  # noqa: BLE001
        log.warning("ViaCEP falhou: %s", e)
    try:
        d = _get(f"https://brasilapi.com.br/api/cep/v1/{cep}")
    except Exception as e:  # noqa: BLE001
        raise ConsultaErro(f"Não foi possível consultar o CEP agora ({type(e).__name__}).") from e
    if not d:
        raise ConsultaErro("CEP não encontrado.")
    dados = {"cep": cep, "logradouro": _texto(d.get("street")), "bairro": _texto(d.get("neighborhood")),
             "municipio": _texto(d.get("city")), "uf": _texto(d.get("state")), "codigo_municipio_ibge": "",
             "fonte": "BrasilAPI"}
    _cache[chave] = (agora, dados)
    return dados
