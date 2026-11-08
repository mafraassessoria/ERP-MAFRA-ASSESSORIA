"""Cliente HTTP do Integra Contador (Serpro).

Produção:  autenticação OAuth2 (client_credentials) em autenticacao.sapi.serpro.gov.br com mTLS
           pelo e-CNPJ do contratante → access_token + jwt_token; chamadas no gateway.
Demonstração: ambiente público de testes do Serpro (dados fictícios, token fixo, sem custo).
"""
from __future__ import annotations

import base64
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

import requests

log = logging.getLogger("emissor.serpro")

URL_AUTH = "https://autenticacao.sapi.serpro.gov.br/authenticate"
URL_PRODUCAO = "https://gateway.apiserpro.serpro.gov.br/integra-contador/v1"
URL_DEMONSTRACAO = "https://gateway.apiserpro.serpro.gov.br/integra-contador-trial/v1"
TOKEN_DEMONSTRACAO = "06aef429-a981-3ec5-a1f8-71d38d86481e"  # publicado na documentação do Serpro
TIPOS = ("Consultar", "Emitir", "Declarar", "Apoiar", "Monitorar")
TEMPO_LIMITE = 90


class ErroSerpro(Exception):
    """Falha de configuração, autenticação ou comunicação (a chamada não produziu resultado)."""


@dataclass
class Resposta:
    http: int
    status: int | None = None
    mensagens: list[dict] = field(default_factory=list)
    dados_brutos: str = ""
    dados: Any = None
    tempo_espera_ms: int = 0
    corpo: dict | None = None

    @property
    def ok(self) -> bool:
        return 200 <= self.http < 300 and self.http != 204 and (self.status is None or 200 <= self.status < 300)

    @property
    def em_processamento(self) -> bool:
        return self.http == 204 or bool(self.tempo_espera_ms)

    @property
    def texto_mensagens(self) -> str:
        partes = []
        for m in self.mensagens or []:
            cod, txt = (m.get("codigo") or "").strip(), (m.get("texto") or "").strip()
            partes.append(f"{cod} {txt}".strip())
        return " | ".join(p for p in partes if p)


def documento(numero: str) -> dict:
    n = re.sub(r"\D", "", numero or "")
    return {"numero": n, "tipo": 2 if len(n) == 14 else 1}


def interpretar(http: int, texto: str, cabecalhos: dict) -> Resposta:
    r = Resposta(http=http)
    etag = cabecalhos.get("ETag") or cabecalhos.get("etag") or ""
    m = re.search(r"tempoEspera\D*(\d+)", etag)
    if m:
        r.tempo_espera_ms = int(m.group(1))
    if not texto.strip():
        return r
    try:
        corpo = json.loads(texto)
    except ValueError:
        r.mensagens = [{"codigo": f"HTTP {http}", "texto": texto[:500]}]
        return r
    if not isinstance(corpo, dict):
        r.dados = corpo
        return r
    r.corpo = corpo
    r.status = corpo.get("status") if isinstance(corpo.get("status"), int) else None
    msgs = corpo.get("mensagens") or []
    r.mensagens = msgs if isinstance(msgs, list) else [{"codigo": "", "texto": str(msgs)}]
    dados = corpo.get("dados")
    if isinstance(dados, str):
        r.dados_brutos = dados
        try:
            r.dados = json.loads(dados) if dados.strip() else None
        except ValueError:
            r.dados = dados
    else:
        r.dados = dados
        r.dados_brutos = json.dumps(dados, ensure_ascii=False) if dados is not None else ""
    if isinstance(r.dados, dict) and r.dados.get("tempoEspera") and not r.tempo_espera_ms:
        try:
            r.tempo_espera_ms = int(r.dados["tempoEspera"])
        except (TypeError, ValueError):
            pass
    if not r.mensagens and http >= 400:
        r.mensagens = [{"codigo": f"HTTP {http}", "texto": corpo.get("message") or corpo.get("error") or texto[:300]}]
    return r


class ClienteIntegra:
    def __init__(self, *, modo: str, contratante: str, consumer_key: str = "", consumer_secret: str = "",
                 pfx: bytes | None = None, senha_pfx: str = "", sessao: requests.Session | None = None,
                 url_base: str | None = None, url_auth: str | None = None):
        if modo not in ("demonstracao", "producao"):
            raise ErroSerpro("Integra Contador desligado. Ative em Configurações → Receita Federal.")
        self.modo = modo
        self.contratante = re.sub(r"\D", "", contratante or "")
        if modo == "producao":
            if not (consumer_key and consumer_secret):
                raise ErroSerpro("Informe a Consumer Key e a Consumer Secret do Serpro em Configurações.")
            if not pfx:
                raise ErroSerpro("O escritório precisa de certificado A1 (e-CNPJ) cadastrado para autenticar no Serpro.")
            if len(self.contratante) != 14:
                raise ErroSerpro("Defina em Configurações qual empresa é o escritório (contratante do Integra Contador).")
        self.ck, self.cs = consumer_key, consumer_secret
        self.pfx, self.senha_pfx = pfx, senha_pfx
        self.sessao = sessao or requests.Session()
        self.url_base = url_base or (URL_PRODUCAO if modo == "producao" else URL_DEMONSTRACAO)
        self.url_auth = url_auth or URL_AUTH
        self._tokens: tuple[str, str, float] | None = None

    # ---- autenticação ------------------------------------------------------------------
    def _autenticar(self) -> tuple[str, str]:
        if self.modo == "demonstracao":
            return TOKEN_DEMONSTRACAO, ""
        if self._tokens and self._tokens[2] > time.time() + 60:
            return self._tokens[0], self._tokens[1]
        from requests_pkcs12 import Pkcs12Adapter
        s = requests.Session()
        s.mount("https://", Pkcs12Adapter(pkcs12_data=self.pfx, pkcs12_password=self.senha_pfx))
        basico = base64.b64encode(f"{self.ck}:{self.cs}".encode()).decode()
        try:
            r = s.post(self.url_auth, data={"grant_type": "client_credentials"}, timeout=TEMPO_LIMITE,
                       headers={"Authorization": f"Basic {basico}", "role-type": "TERCEIROS",
                                "Content-Type": "application/x-www-form-urlencoded"})
        except requests.RequestException as e:
            raise ErroSerpro(f"Sem conexão com a autenticação do Serpro ({type(e).__name__}).") from e
        if r.status_code != 200:
            raise ErroSerpro(f"Serpro recusou a autenticação (HTTP {r.status_code}): {r.text[:300]}")
        d = r.json()
        acesso, jwt = d.get("access_token", ""), d.get("jwt_token", "")
        if not acesso:
            raise ErroSerpro("Resposta de autenticação do Serpro sem access_token.")
        self._tokens = (acesso, jwt, time.time() + float(d.get("expires_in") or 1800))
        return acesso, jwt

    # ---- chamada -----------------------------------------------------------------------
    def corpo(self, contribuinte: str, id_sistema: str, id_servico: str, versao: str, dados: dict | str) -> dict:
        contratante = self.contratante if self.modo == "producao" else (self.contratante or "00000000000000")
        return {
            "contratante": documento(contratante),
            "autorPedidoDados": documento(contratante),
            "contribuinte": documento(contribuinte),
            "pedidoDados": {
                "idSistema": id_sistema, "idServico": id_servico, "versaoSistema": versao,
                "dados": dados if isinstance(dados, str) else (json.dumps(dados, ensure_ascii=False) if dados else ""),
            },
        }

    def chamar(self, tipo: str, contribuinte: str, id_sistema: str, id_servico: str, versao: str = "1.0",
               dados: dict | str = "") -> Resposta:
        if tipo not in TIPOS:
            raise ErroSerpro(f"Tipo de chamada inválido: {tipo}")
        acesso, jwt = self._autenticar()
        cab = {"Authorization": f"Bearer {acesso}", "Content-Type": "application/json", "accept": "text/plain"}
        if jwt:
            cab["jwt_token"] = jwt
        corpo = self.corpo(contribuinte, id_sistema, id_servico, versao, dados)
        try:
            r = self.sessao.post(f"{self.url_base}/{tipo}", json=corpo, headers=cab, timeout=TEMPO_LIMITE)
        except requests.RequestException as e:
            raise ErroSerpro(f"Sem resposta do Serpro ({type(e).__name__}). Tente de novo em instantes.") from e
        if r.status_code == 401 and self.modo == "producao" and self._tokens:
            self._tokens = None  # token expirou antes do previsto: renova uma vez
            return self.chamar(tipo, contribuinte, id_sistema, id_servico, versao, dados)
        return interpretar(r.status_code, r.text, dict(r.headers))
