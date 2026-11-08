"""Emissão em lote por planilha (ex.: consultório que emite as notas do mês num único dia).

Colunas (a ordem não importa; o cabeçalho identifica): CPF/CNPJ, Nome/Empresa, Data, Histórico, Valor, E-mail.
"""
from __future__ import annotations

import csv
import io
import json
import re
import secrets
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from ..config import DATA_DIR
from ..documentos import formatar_documento, limpar, validar_documento
from .tabelas import sem_acento

COLUNAS = {
    "cpf": ("cpf/cnpj", "cpf / cnpj", "cpf ou cnpj", "cpf cnpj", "cpf", "cnpj", "documento", "doc"),
    "nome": ("nome/empresa", "nome / empresa", "nome ou empresa", "nome", "empresa", "razao social",
             "nome/razao social", "cliente", "paciente", "tomador", "nome do cliente"),
    "data": ("data", "competencia", "data competencia", "data do servico", "data de competencia"),
    "historico": ("historico", "descricao", "servico", "descricao do servico"),
    "valor": ("valor", "valor r$", "valor (r$)", "valor do servico"),
    "email": ("email", "e-mail", "e mail"),
}
PASTA = DATA_DIR / "tmp"


def _cel(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


def _linhas(conteudo: bytes, nome: str) -> list[list]:
    if nome.lower().endswith(".xlsx"):
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(conteudo), read_only=True, data_only=True)
        return [list(r) for r in wb.worksheets[0].iter_rows(values_only=True)]
    txt = conteudo.decode("utf-8-sig", errors="replace")
    if "�" in txt:
        txt = conteudo.decode("latin-1")
    sep = ";" if txt.count(";") >= txt.count(",") else ","
    return [r for r in csv.reader(io.StringIO(txt), delimiter=sep)]


def _data(v) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, (int, float)) and 20000 < v < 80000:  # número de série do Excel
        return date(1899, 12, 30) + timedelta(days=int(v))
    t = _cel(v)
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(t[:10], fmt).date()
        except ValueError:
            pass
    return None


def _valor(v) -> Decimal | None:
    if isinstance(v, (int, float, Decimal)):
        return Decimal(str(v)).quantize(Decimal("0.01"))
    t = re.sub(r"[^\d,.-]", "", _cel(v))
    if not t:
        return None
    if "," in t:
        t = t.replace(".", "").replace(",", ".")
    try:
        return Decimal(t).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


def ler(conteudo: bytes, nome: str) -> tuple[list[dict], list[str]]:
    """Devolve (linhas, problemas gerais). Cada linha: cpf, nome, data, historico, valor, email, erros[]."""
    linhas = [r for r in _linhas(conteudo, nome) if any(_cel(c) for c in r)]
    if not linhas:
        return [], ["Planilha vazia."]
    cab = [sem_acento(_cel(c)).strip(" :*") for c in linhas[0]]
    idx = {}
    for campo, nomes in COLUNAS.items():
        for i, c in enumerate(cab):
            if c in nomes:
                idx[campo] = i
                break
    faltam = [c for c in ("cpf", "nome", "historico", "valor") if c not in idx]
    if faltam:
        nomes = {"cpf": "CPF/CNPJ", "nome": "Nome/Empresa", "historico": "Histórico", "valor": "Valor"}
        return [], [f"Não encontrei a(s) coluna(s): {', '.join(nomes[c] for c in faltam)}. A primeira linha deve ter os títulos "
                    "CPF/CNPJ, Nome/Empresa, Data, Histórico, Valor e E-mail."]
    out, hoje = [], date.today()
    for n, r in enumerate(linhas[1:], start=2):
        r = list(r) + [None] * 10
        doc = limpar(_cel(r[idx["cpf"]]))
        if doc.isdigit() and len(doc) < 11:
            doc = doc.zfill(11)  # CPF que o Excel guardou como número (zeros à esquerda somem)
        elif doc.isdigit() and 11 < len(doc) < 14:
            doc = doc.zfill(14)  # idem para CNPJ
        item = {
            "linha": n, "cpf": doc, "nome": _cel(r[idx["nome"]])[:150],
            "data": _data(r[idx["data"]]) if "data" in idx else None,
            "historico": _cel(r[idx["historico"]])[:2000], "valor": _valor(r[idx["valor"]]),
            "email": _cel(r[idx["email"]])[:120] if "email" in idx else "", "erros": [],
        }
        if not validar_documento(doc):
            item["erros"].append("CPF/CNPJ inválido")
        if not item["nome"]:
            item["erros"].append("sem nome/empresa")
        if not item["historico"]:
            item["erros"].append("sem histórico")
        if item["valor"] is None or item["valor"] <= 0:
            item["erros"].append("valor inválido")
        if "data" in idx and item["data"] is None and _cel(r[idx["data"]]):
            item["erros"].append("data inválida")
        if item["data"] and item["data"] > hoje:
            item["erros"].append("data futura")
        if item["email"] and not re.fullmatch(r"[^@\s;,]+@[^@\s;,]+\.[^@\s;,]+", item["email"]):
            item["erros"].append("e-mail inválido")
        out.append(item)
    return out, []


def salvar_previa(dados: dict) -> str:
    PASTA.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(12)
    (PASTA / f"lote_{token}.json").write_text(json.dumps(dados, default=str, ensure_ascii=False), encoding="utf-8")
    return token


def abrir_previa(token: str) -> dict | None:
    if not re.fullmatch(r"[0-9a-f]{24}", token or ""):
        return None
    arq = PASTA / f"lote_{token}.json"
    if not arq.exists():
        return None
    dados = json.loads(arq.read_text(encoding="utf-8"))
    return dados


def apagar_previa(token: str) -> None:
    if re.fullmatch(r"[0-9a-f]{24}", token or ""):
        (PASTA / f"lote_{token}.json").unlink(missing_ok=True)


def modelo_xlsx() -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font
    wb = Workbook()
    ws = wb.active
    ws.title = "Notas"
    ws.append(["CPF/CNPJ", "Nome/Empresa", "Data", "Histórico", "Valor", "E-mail"])
    for c in ws[1]:
        c.font = Font(bold=True)
    ws.append(["529.982.247-25", "Maria da Silva", date.today().replace(day=1), "Consulta odontológica e limpeza", 250.00, "maria@exemplo.com"])
    ws.append(["11.222.333/0001-81", "Empresa Exemplo Ltda", date.today().replace(day=1), "Atendimento ocupacional", 1200.00, "financeiro@exemplo.com"])
    ws["C3"].number_format = "DD/MM/YYYY"
    ws["E3"].number_format = "#,##0.00"
    ws["C2"].number_format = "DD/MM/YYYY"
    ws["E2"].number_format = "#,##0.00"
    for col, larg in zip("ABCDEF", (18, 34, 12, 50, 12, 30)):
        ws.column_dimensions[col].width = larg
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def doc_fmt(d: str) -> str:
    return formatar_documento(d)
