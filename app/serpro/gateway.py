"""Cliente das APIs "de prateleira" da Loja Serpro (gateway.apiserpro.serpro.gov.br).

São contratos separados do Integra Contador, mas a autenticação é a mesma ideia (OAuth2
client_credentials com Consumer Key/Secret), sem certificado digital:
  - Consulta CPF v3  GET  /consulta-cpf-df/v3/cpf/{cpf}/{DDMMAAAA}
  - Consulta CND v1  POST /consulta-cnd/v1/certidao
No modo demonstração usa os endereços "-trial" com o token público de testes do Serpro.
"""
from __future__ import annotations

import base64
import time

import requests

URL_GATEWAY = "https://gateway.apiserpro.serpro.gov.br"
URL_TOKEN = URL_GATEWAY + "/token"
TOKEN_TRIAL = "06aef429-a981-3ec5-a1f8-71d38d86481e"   # token público do ambiente de testes (documentação Serpro)
TEMPO_LIMITE = 60
CAMINHOS = {
    "CPF": {"producao": "/consulta-cpf-df/v3/cpf", "demonstracao": "/consulta-cpf-df-trial/v3/cpf"},
    "CND": {"producao": "/consulta-cnd/v1/certidao", "demonstracao": "/consulta-cnd-trial/v1/certidao"},
}


class ErroGateway(Exception):
    """Falha de comunicação/autenticação (nada a ver com o conteúdo consultado)."""


class ClienteGateway:
    def __init__(self, *, modo: str, consumer_key: str = "", consumer_secret: str = "",
                 sessao: requests.Session | None = None):
        if modo not in ("demonstracao", "producao"):
            raise ErroGateway("API desligada. Ative em Configurações → APIs do Serpro.")
        if modo == "producao" and not (consumer_key and consumer_secret):
            raise ErroGateway("Informe a Consumer Key e a Consumer Secret do contrato em Configurações → APIs do Serpro.")
        self.modo = modo
        self.ck, self.cs = consumer_key, consumer_secret
        self.sessao = sessao or requests.Session()
        self._token: tuple[str, float] | None = None

    def token(self) -> str:
        if self.modo == "demonstracao":
            return TOKEN_TRIAL
        if self._token and self._token[1] > time.time() + 60:
            return self._token[0]
        basico = base64.b64encode(f"{self.ck}:{self.cs}".encode()).decode()
        try:
            r = self.sessao.post(URL_TOKEN, data={"grant_type": "client_credentials"}, timeout=TEMPO_LIMITE,
                                 headers={"Authorization": f"Basic {basico}",
                                          "Content-Type": "application/x-www-form-urlencoded"})
        except requests.RequestException as e:
            raise ErroGateway(f"Sem conexão com o Serpro ({type(e).__name__}).") from e
        if r.status_code != 200:
            raise ErroGateway(f"Serpro recusou as chaves (HTTP {r.status_code}). Confira a Consumer Key/Secret do contrato.")
        d = r.json()
        if not d.get("access_token"):
            raise ErroGateway("Resposta de autenticação do Serpro sem access_token.")
        self._token = (d["access_token"], time.time() + float(d.get("expires_in") or 3600))
        return self._token[0]

    def _chamar(self, metodo: str, url: str, **kw) -> requests.Response:
        for tentativa in (1, 2):
            cab = {"Authorization": f"Bearer {self.token()}", "Accept": "application/json"}
            try:
                r = self.sessao.request(metodo, url, headers=cab, timeout=TEMPO_LIMITE, **kw)
            except requests.RequestException as e:
                raise ErroGateway(f"Sem resposta do Serpro ({type(e).__name__}). Tente de novo em instantes.") from e
            if r.status_code == 401 and self.modo == "producao" and tentativa == 1:
                self._token = None   # token venceu antes do previsto: renova uma vez
                continue
            return r
        return r

    def cpf(self, cpf: str, nascimento_ddmmaaaa: str) -> requests.Response:
        return self._chamar("GET", f"{URL_GATEWAY}{CAMINHOS['CPF'][self.modo]}/{cpf}/{nascimento_ddmmaaaa}")

    def cnd(self, corpo: dict) -> requests.Response:
        return self._chamar("POST", URL_GATEWAY + CAMINHOS["CND"][self.modo], json=corpo)
