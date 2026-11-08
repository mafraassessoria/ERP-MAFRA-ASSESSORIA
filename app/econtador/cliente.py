"""Cliente da API ePlugin do e-Contador (Alterdata) — somente GET.

Adaptado do coletor do Monitor e-Contador (coletor_api.py): mesmos hosts autorizados, mesma conferência de
paginação, de CNPJ e de validade. A chave fica criptografada nas Configurações do sistema.
"""
from __future__ import annotations

import base64
import json
import re
from datetime import date, datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

HOSTS = {"cnd.pack.alterdata.com.br", "dp.pack.alterdata.com.br", "documentos.pack.alterdata.com.br"}
TAMANHO_MAX = 45 * 1024 * 1024
PAGINA = 25


class ErroAPI(ValueError):
    pass


class _SemRedirecionar(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def cnpj_numerico(v) -> str | None:
    s = str(v or "").strip()
    return s.zfill(14) if s.isdigit() and len(s) <= 14 else None


def data_api(v) -> date | None:
    """Só datas explícitas (AAAA-MM-DD ou DD/MM/AAAA); o resto fica para revisão."""
    if v is None or str(v).strip() == "":
        return None
    t = str(v).strip()
    try:
        if re.match(r"^\d{4}-\d{2}-\d{2}(?:$|[T ])", t):
            return date.fromisoformat(t[:10])
        if re.match(r"^\d{2}/\d{2}/\d{4}(?:$|[T ])", t):
            return datetime.strptime(t[:10], "%d/%m/%Y").date()
    except ValueError:
        return None
    return None


def extensao(raw: bytes) -> str:
    if raw.startswith(b"%PDF-"):
        return ".pdf"
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if raw.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    raise ErroAPI("Arquivo da API não é PDF/PNG/JPEG reconhecido")


class ClienteEContador:
    def __init__(self, token: str, abridor=None, tempo_limite: int = 40):
        token = (token or "").strip()
        if not token or "\n" in token or "\r" in token:
            raise ErroAPI("Chave da API do e-Contador não configurada (Configurações → e-Contador).")
        self.token = token
        self.abridor = abridor or build_opener(_SemRedirecionar())
        self.tempo_limite = tempo_limite
        self._tipos: dict[str, str] = {}

    # ---- base ---------------------------------------------------------------------
    def get(self, host: str, caminho: str, consulta: dict | None = None) -> dict:
        if host not in HOSTS or not caminho.startswith("/api/v1/"):
            raise ErroAPI("Destino da API não autorizado")
        url = f"https://{host}{caminho}" + ("?" + urlencode(consulta) if consulta else "")
        req = Request(url, headers={"Authorization": "Bearer " + self.token, "Accept": "application/json"}, method="GET")
        try:
            with self.abridor.open(req, timeout=self.tempo_limite) as resp:
                raw = resp.read(TAMANHO_MAX + 1)
        except HTTPError as e:
            if e.code == 401:
                raise ErroAPI("e-Contador recusou a chave (HTTP 401): gere uma nova em Configurações → ePlugin do e-Contador.") from None
            raise ErroAPI(f"API {host}: HTTP {e.code}; verificar permissão/chave ou serviço") from None
        except (URLError, TimeoutError, OSError):
            raise ErroAPI("Falha de conexão HTTPS com a API Alterdata") from None
        if len(raw) > TAMANHO_MAX:
            raise ErroAPI("Resposta excede o limite de 45 MB")
        try:
            dados = json.loads(raw)
        except (ValueError, UnicodeError):
            raise ErroAPI("Resposta da API não é JSON válido") from None
        if not isinstance(dados, dict) or "data" not in dados:
            raise ErroAPI("Formato inesperado da API")
        return dados

    def paginas(self, host: str, caminho: str, consulta: dict | None = None) -> list[dict]:
        consulta = dict(consulta or {})
        offset, vistos, registros = 0, set(), []
        for _ in range(200):
            q = dict(consulta)
            q.update({"page[limit]": PAGINA, "page[offset]": offset})
            payload = self.get(host, caminho, q)
            lote = payload["data"]
            if not isinstance(lote, list):
                raise ErroAPI("Listagem da API não é uma lista")
            for item in lote:
                if not isinstance(item, dict):
                    raise ErroAPI("Item da API inválido")
                ident = str(item.get("id", ""))
                if not ident or ident in vistos:
                    raise ErroAPI("ID repetido ou ausente; paginação incompleta")
                vistos.add(ident)
                registros.append(item)
            links = payload.get("links") or {}
            prox = links.get("next")
            total = (payload.get("meta") or {}).get("totalResourceCount")
            if total is not None:
                if not isinstance(total, int) or total < len(registros):
                    raise ErroAPI("Total inconsistente na API")
                if len(registros) == total:
                    return registros
            if prox:
                u = urlparse(prox)
                if u.scheme != "https" or u.hostname != host or u.path.rstrip("/") != caminho.rstrip("/"):
                    raise ErroAPI("Paginação aponta para destino inesperado")
            elif total is None and (len(lote) < PAGINA or "next" in links):
                return registros
            if not lote:
                raise ErroAPI("Página vazia antes de concluir a consulta")
            offset += len(lote)
        raise ErroAPI("Limite de páginas da API excedido")

    # ---- empresas -----------------------------------------------------------------
    def empresas(self) -> list[dict]:
        """Empresas ativas no e-Contador: codigo (= externoid, o código interno), nome, cnpj, id."""
        out = []
        for row in self.paginas("dp.pack.alterdata.com.br", "/api/v1/empresas", {"filter[empresas][ativa][EQ]": "true"}):
            a = row.get("attributes", {})
            if a.get("ativa") is False:
                continue
            externo = str(a.get("externoid", "")).strip()
            cnpj = cnpj_numerico(a.get("cpfcnpj")) or re.sub(r"\W", "", str(a.get("cpfCnpjAlfanumerico") or "")).upper() or None
            out.append(dict(codigo=str(int(externo)) if externo.isdigit() else "", nome=str(a.get("nome", "")).strip(),
                            cnpj=cnpj, id=str(row["id"])))
        if not out:
            raise ErroAPI("Nenhuma empresa ativa retornada pela API")
        return out

    # ---- certidões ----------------------------------------------------------------
    def nome_tipo(self, row: dict) -> tuple[str, str]:
        rel = (row.get("relationships", {}).get("tipoCertidao", {}).get("data") or {})
        ident = str(rel.get("id", ""))
        if not ident.isdigit():
            raise ErroAPI("Tipo de certidão sem ID válido")
        if ident not in self._tipos:
            d = self.get("cnd.pack.alterdata.com.br", "/api/v1/integracao/tipoCertidao/" + ident)["data"]
            if not isinstance(d, dict) or str(d.get("id")) != ident:
                raise ErroAPI("Tipo de certidão divergente")
            self._tipos[ident] = str(d.get("attributes", {}).get("nome", "")).strip()
        return ident, self._tipos[ident]

    def certidoes(self, cnpj: str) -> list[dict]:
        cnpj = cnpj_numerico(cnpj)
        if not cnpj:
            raise ErroAPI("Empresa sem CNPJ numérico; consulta bloqueada")
        out = []
        for rec in self.paginas("cnd.pack.alterdata.com.br", "/api/v1/integracao/certidoes",
                                {"filter[integracao/certidoes][empresaId][EQ]": cnpj}):
            a = rec.get("attributes", {})
            if cnpj_numerico(a.get("empresaId")) != cnpj:
                raise ErroAPI("API retornou CNPJ diferente do pedido")
            ident = str(rec.get("id", ""))
            if not ident.isdigit():
                raise ErroAPI("ID de certidão inválido")
            tipo_id, tipo_nome = self.nome_tipo(rec)
            emissao, venc = data_api(a.get("emissao")), data_api(a.get("vencimento"))
            avisos = [k for k, v in (("emissao", emissao), ("vencimento", venc)) if a.get(k) and v is None]
            out.append(dict(pack_id=ident, tipo_id=tipo_id, tipo_nome=tipo_nome, emissao=emissao, vencimento=venc,
                            situacao=str(a.get("status") or "").strip(), disponivel=bool(a.get("arquivo")),
                            avisos=avisos))
        return out

    def baixar(self, cnpj: str, pack_id: str, vencimento: date | None, situacao: str) -> tuple[bytes, str]:
        """Baixa o arquivo e confere se empresa, validade e situação não mudaram desde a listagem."""
        if not str(pack_id).isdigit():
            raise ErroAPI("ID inválido")
        d = self.get("cnd.pack.alterdata.com.br", "/api/v1/integracao/certidoes/" + str(pack_id),
                     {"filter[empresaId]": cnpj})["data"]
        if not isinstance(d, dict) or str(d.get("id")) != str(pack_id):
            raise ErroAPI("Certidão retornada não corresponde ao ID")
        a = d.get("attributes", {})
        if cnpj_numerico(a.get("empresaId")) != cnpj or data_api(a.get("vencimento")) != vencimento \
                or str(a.get("status") or "").strip() != situacao:
            raise ErroAPI("Empresa, validade ou situação mudou durante o download; consultar novamente")
        cod = a.get("arquivo")
        if not isinstance(cod, str) or not cod:
            raise ErroAPI("Certidão sem arquivo disponível na API")
        if cod.startswith("data:"):
            cod = cod.split(",", 1)[-1]
        try:
            raw = base64.b64decode(re.sub(r"\s+", "", cod), validate=True)
        except (ValueError, TypeError):
            raise ErroAPI("Arquivo da API não é Base64 válido") from None
        return raw, extensao(raw)
