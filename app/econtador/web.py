"""e-Contador pela tela (sessão de login humano): o que a API ePlugin não faz.

Mapeado em 09/11/2026 na conta do escritório (versão 2.00.179.0):
  - lista       GET  /Documentos/Enviados?Texto=&Categoria=&Status=Todos&EmpresaId=&Pagina=N   (linhas tr[data-id], data-pago)
  - detalhe     GET  /Documentos/Visualizar/{id}   tabela: Descrição, Valor, Data de vencimento, Empresa "00075 - NOME",
                                                   Pago "Sim." | "Pagamento pendente.", Data de Pagamento
  - baixa       POST /Documentos/ConfirmarPagamentoEmBloco  Ids=..&Ids=..&DataPagamento=dd/mm/aaaa  → JSON {success, idsProcessados, partial, message}
  - exclusão    POST /Documentos/Excluir  data=id1,id2&motivo=texto   (irreversível)
  - envio       POST /Documentos/UploadArquivos (multipart, campo Binarios) e depois POST /Documentos/Novo (formulário)
  - empresas    GET  /Documentos/Novo  → select EmpresasIds: value = id interno, texto "00075 - NOME"
Categorias: 0 Guias, 5 Cobranças. Toda gravação é conferida relendo o documento depois.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

import requests
from lxml import html

BASE = "https://econtador.alterdata.com.br"
TEMPO = 40
CATEGORIA_GUIAS, CATEGORIA_COBRANCAS = 0, 5


class ErroSessao(Exception):
    """Sem sessão ou sessão vencida: reconectar em /econtador/conectar."""


class ErroWeb(Exception):
    """O e-Contador recusou ou respondeu algo inesperado. Nada é presumido."""


def _txt(el) -> str:
    return " ".join(el.text_content().split()) if el is not None else ""


def _valor(t: str) -> Decimal | None:
    m = re.search(r"(-?[\d.]+,\d{2})", t or "")
    if not m:
        return None
    try:
        return Decimal(m[1].replace(".", "").replace(",", "."))
    except InvalidOperation:
        return None


def _data(t: str) -> date | None:
    m = re.search(r"(\d{2})/(\d{2})/(\d{4})", t or "")
    if not m:
        return None
    try:
        return date(int(m[3]), int(m[2]), int(m[1]))
    except ValueError:
        return None


@dataclass
class Documento:
    id: str
    empresa_codigo: str = ""
    empresa_nome: str = ""
    descricao: str = ""
    valor: Decimal | None = None
    vencimento: date | None = None
    pago: bool = False
    pago_em: date | None = None
    categoria: str = ""

    @property
    def titulo(self) -> str:
        m = re.search(r"T[íi]tulo:\s*(\d+)", self.descricao)
        return m[1] if m else ""

    @property
    def periodo(self) -> tuple[date | None, date | None]:
        m = re.search(r"Per[íi]odo de (\d{2}/\d{2}/\d{4}) at[ée] (\d{2}/\d{2}/\d{4})", self.descricao)
        return (_data(m[1]), _data(m[2])) if m else (None, None)


class ClienteWeb:
    def __init__(self, cookies: list[dict], sessao: requests.Session | None = None):
        if not cookies:
            raise ErroSessao("O e-Contador não está conectado. Clique em “Conectar e-Contador” e entre com seu usuário, senha e código.")
        self.s = sessao or requests.Session()
        for c in cookies:
            dom = c.get("domain") or "econtador.alterdata.com.br"
            if "alterdata.com.br" not in dom:
                continue
            self.s.cookies.set(c["name"], c["value"], domain=dom, path=c.get("path") or "/")
        self.s.headers.update({"User-Agent": "ERP-MAFRA-ASSESSORIA", "Referer": BASE + "/Documentos/Enviados", "Origin": BASE})

    # ---- transporte ------------------------------------------------------------------------
    def _conferir(self, r) -> None:
        url = getattr(r, "url", "") or ""
        if not url.startswith(BASE):
            raise ErroWeb("O e-Contador redirecionou para fora do endereço esperado.")
        corpo = r.text if hasattr(r, "text") else ""
        if "/Login" in url or "/Autenticacao" in url or ('type="password"' in corpo and "Informe a senha" in corpo):
            raise ErroSessao("A sessão do e-Contador venceu. Clique em “Conectar e-Contador” e entre de novo (usuário, senha e código).")
        if r.status_code >= 400:
            raise ErroWeb(f"O e-Contador respondeu HTTP {r.status_code}.")

    def _get(self, caminho: str, **params) -> html.HtmlElement:
        try:
            r = self.s.get(BASE + caminho, params=params or None, timeout=TEMPO)
        except requests.RequestException as e:
            raise ErroWeb(f"Sem conexão com o e-Contador ({type(e).__name__}).") from e
        self._conferir(r)
        return html.fromstring(r.text or "<html></html>")

    def _post(self, caminho: str, dados, arquivos=None, json_esperado: bool = False):
        try:
            r = self.s.post(BASE + caminho, data=dados, files=arquivos, timeout=TEMPO * 2,
                            headers={"X-Requested-With": "XMLHttpRequest"} if json_esperado else None)
        except requests.RequestException as e:
            raise ErroWeb(f"Sem resposta do e-Contador ({type(e).__name__}). Confira no e-Contador antes de repetir.") from e
        self._conferir(r)
        if json_esperado:
            try:
                return r.json()
            except ValueError as e:
                raise ErroWeb("O e-Contador respondeu num formato inesperado. Confira no e-Contador antes de repetir.") from e
        return r

    # ---- leitura ---------------------------------------------------------------------------
    def testar(self) -> int:
        """Confere a sessão; devolve quantas empresas o e-Contador lista para envio."""
        return len(self.empresas())

    def empresas(self) -> dict[str, str]:
        """código interno (sem zeros à esquerda) → id do e-Contador."""
        arv = self._get("/Documentos/Novo")
        out = {}
        for o in arv.xpath('//select[@name="EmpresasIds"]/option'):
            m = re.match(r"\s*(\d+)\s*-", o.text_content() or "")
            if m and (o.get("value") or "").isdigit():
                out[m[1].lstrip("0") or "0"] = o.get("value")
        if not out:
            raise ErroWeb("A tela de novo documento do e-Contador não trouxe a lista de empresas.")
        return out

    def listar(self, texto: str = "", categoria: int | None = None, empresa_id: str = "", paginas: int = 3) -> list[Documento]:
        docs = []
        for pag in range(1, paginas + 1):
            params = {"Texto": texto, "Status": "Todos", "Pagina": pag}
            if categoria is not None:
                params["Categoria"] = categoria
            if empresa_id:
                params["EmpresaId"] = empresa_id
            arv = self._get("/Documentos/Enviados", **params)
            linhas = arv.xpath("//tbody/tr[@data-id]")
            for tr in linhas:
                cel = {td.get("data-title"): _txt(td) for td in tr.xpath("./td")}
                emp = cel.get("Empresa", "")
                m = re.match(r"(\d+)\s*-\s*(.*)", emp)
                docs.append(Documento(id=tr.get("data-id"), empresa_codigo=(m[1].lstrip("0") if m else ""), empresa_nome=(m[2] if m else emp),
                                      descricao=tr.get("data-valor") or cel.get("Resumo", ""), valor=_valor(cel.get("Valor", "")),
                                      vencimento=_data(cel.get("Vencimento", "")), pago=(tr.get("data-pago") == "true"),
                                      categoria=cel.get("Categoria", "")))
            if len(linhas) < 10 or not arv.xpath(f'//a[contains(@href,"Pagina={pag + 1}")]'):
                break
        return docs

    def visualizar(self, doc_id: str) -> Documento:
        if not re.fullmatch(r"\d{1,12}", str(doc_id)):
            raise ErroWeb("Número de documento do e-Contador inválido.")
        arv = self._get(f"/Documentos/Visualizar/{doc_id}")
        campos = {}
        for tr in arv.xpath("//table//tr"):
            cel = tr.xpath("./th|./td")
            if len(cel) >= 2:
                campos[_txt(cel[0]).rstrip(": ").lower()] = _txt(cel[1])
        if "valor" not in campos or "empresa" not in campos:
            raise ErroWeb(f"O documento {doc_id} não foi encontrado no e-Contador ou a tela mudou.")
        m = re.match(r"(\d+)\s*-\s*(.*)", campos.get("empresa", ""))
        pago_txt = campos.get("pago", "")
        return Documento(id=str(doc_id), empresa_codigo=(m[1].lstrip("0") if m else ""), empresa_nome=(m[2] if m else ""),
                         descricao=campos.get("descrição", campos.get("descricao", "")), valor=_valor(campos.get("valor", "")),
                         vencimento=_data(campos.get("data de vencimento", "")), pago=pago_txt.lower().startswith("sim"),
                         pago_em=_data(campos.get("data de pagamento", "")))

    def localizar_cobranca(self, titulo: str, empresa_codigo: str) -> list[Documento]:
        """Cobranças cujo detalhe traz "Título: N" e a empresa do recebimento."""
        return self.localizar_cobranca_diag(titulo, empresa_codigo)[0]

    def localizar_cobranca_diag(self, titulo: str, empresa_codigo: str, empresa_id: str = "", valor: Decimal | None = None,
                                periodo: tuple[date | None, date | None] = (None, None)) -> tuple[list[Documento], str]:
        """Procura a cobrança em etapas e devolve (achados, diagnóstico legível quando não acha):
        1) pelo número do título nas Cobranças; 2) pelo título em qualquer categoria;
        3) nas Cobranças da empresa (EmpresaId do e-Contador), pelo valor e período."""
        titulo, cod = str(titulo), str(empresa_codigo).lstrip("0")
        notas, vistos = [], set()

        def conferir(lista, rotulo):
            achados, outras = [], []
            for d in lista:
                if d.id in vistos:
                    continue
                vistos.add(d.id)
                det = self.visualizar(d.id)
                if det.titulo == titulo and det.empresa_codigo == cod:
                    achados.append(det)
                elif det.titulo == titulo:
                    outras.append(f"doc {det.id} na empresa {det.empresa_codigo} - {det.empresa_nome[:30]}")
            notas.append(f"{rotulo}: {len(lista)} resultado(s)" + (f" — título {titulo} achado em outra empresa: {'; '.join(outras)}" if outras else ""))
            return achados

        achados = conferir(self.listar(texto=titulo, categoria=CATEGORIA_COBRANCAS, paginas=5), f"busca '{titulo}' em Cobranças")
        if achados:
            return achados, ""
        achados = conferir(self.listar(texto=titulo, paginas=3), f"busca '{titulo}' em todas as categorias")
        if achados:
            return achados, ""
        if empresa_id:
            candidatos = []
            lista = self.listar(categoria=CATEGORIA_COBRANCAS, empresa_id=empresa_id, paginas=3)
            for d in lista:
                if valor is not None and d.valor != valor:
                    continue
                det = self.visualizar(d.id)
                if det.empresa_codigo != cod:      # o id do cadastro pode não ser o mesmo da tela: confere o código
                    continue
                ini, fim = det.periodo
                if periodo[0] and periodo[1] and ini and fim and (ini, fim) != periodo:
                    continue
                candidatos.append(det)
            notas.append(f"Cobranças da empresa no e-Contador: {len(lista)}, com o mesmo valor e período: {len(candidatos)}")
            if len(candidatos) == 1:
                return candidatos, "achada pelo valor e período (o número do título não aparece na cobrança)"
            if candidatos:
                return candidatos, ""
        else:
            notas.append("empresa sem ID do e-Contador no cadastro: busca pela empresa não feita")
        return [], " · ".join(notas)

    # ---- gravação (sempre seguida de releitura) ------------------------------------------------
    def confirmar_pagamento(self, ids: list[str], data: date) -> list[str]:
        if not ids:
            return []
        dados = [("Ids", str(i)) for i in ids] + [("DataPagamento", data.strftime("%d/%m/%Y"))]
        r = self._post("/Documentos/ConfirmarPagamentoEmBloco", dados, json_esperado=True)
        if not isinstance(r, dict) or not r.get("success"):
            raise ErroWeb("O e-Contador não aceitou a baixa: " + str((r or {}).get("message") or "sem mensagem"))
        return [str(x) for x in (r.get("idsProcessados") or [])]

    def excluir(self, ids: list[str], motivo: str) -> None:
        if not ids:
            return
        self._post("/Documentos/Excluir", {"data": ",".join(str(i) for i in ids), "motivo": motivo[:250]})

    def enviar(self, empresa_id: str, titulo: str, descricao: str, categoria: int, vencimento: date | None,
               valor: Decimal | None, nome_arquivo: str, conteudo: bytes) -> str:
        """Publica um documento para a empresa e devolve o id criado (achado pela lista, pelo título)."""
        up = self._post("/Documentos/UploadArquivos", {}, arquivos={"Binarios": (nome_arquivo, conteudo, "application/pdf")})
        try:
            resp = up.json()
            if isinstance(resp, dict) and any(f.get("error") for f in resp.get("files", []) if isinstance(f, dict)):
                raise ErroWeb("O e-Contador recusou o arquivo: " + "; ".join(str(f.get("error")) for f in resp["files"] if f.get("error")))
        except ValueError:
            pass
        dados = {"PossuiUsuario": "false", "Documento.Titulo": titulo[:150], "Documento.Descricao": descricao[:1000],
                 "IsOrigemEDrive": "False", "IsOrigemTarefa": "False", "Documento.CategoriaDocumento": str(categoria),
                 "Documento.CategoriaPersonalizada": "Guias" if categoria == CATEGORIA_GUIAS else "",
                 "Documento.Vencimento": vencimento.strftime("%d/%m/%Y") if vencimento else "",
                 "Documento.Valor": f"{valor:.2f}".replace(".", ",") if valor is not None else "0",
                 "Documento.Expira": "", "EmpresasIds": str(empresa_id), "CriarOutro": "false"}
        self._post("/Documentos/Novo", dados)
        novos = [d for d in self.listar(texto=titulo[:60], categoria=categoria, empresa_id=empresa_id, paginas=1)
                 if d.descricao.strip().startswith(titulo[:40].strip())]
        if len(novos) != 1:
            raise ErroWeb("O documento foi enviado, mas não consegui confirmar na lista do e-Contador. Confira lá antes de reenviar.")
        return novos[0].id


def cookies_de_json(texto: str) -> list[dict]:
    try:
        d = json.loads(texto or "[]")
    except ValueError:
        return []
    return d.get("cookies", []) if isinstance(d, dict) else d if isinstance(d, list) else []


def agora_txt() -> str:
    return datetime.now().strftime("%d/%m/%Y %H:%M")
