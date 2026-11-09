"""Importação do relatório de recebidos (XLS "Baixados") e geração dos rascunhos de NFS-e do escritório.

Regras combinadas (07/11/2026):
- todo recebimento gera nota; um recebimento nunca vira duas notas;
- competência = data do pagamento;
- descrição: HONORÁRIOS REFERENTES A PRESTAÇÃO DE SERVIÇOS CONTÁBEIS NO MÊS DE <MÊS DO PAGAMENTO>/<ANO>.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..configuracoes import ler_config

MESES = ["JANEIRO", "FEVEREIRO", "MARÇO", "ABRIL", "MAIO", "JUNHO", "JULHO", "AGOSTO", "SETEMBRO", "OUTUBRO",
         "NOVEMBRO", "DEZEMBRO"]
DESCRICAO_PADRAO = "HONORÁRIOS REFERENTES A PRESTAÇÃO DE SERVIÇOS CONTÁBEIS NO MÊS DE {MES}/{ANO}"


def descricao(pagamento: date, modelo: str | None = None) -> str:
    return (modelo or DESCRICAO_PADRAO).replace("{MES}", MESES[pagamento.month - 1]).replace("{ANO}", str(pagamento.year))


def _cod(v) -> str:
    s = str(v if v is not None else "").strip()
    if s.endswith(".0"):
        s = s[:-2]
    return str(int(s)) if s.isdigit() else s


@dataclass
class ResultadoImportacao:
    novos: int = 0
    ja_existiam: int = 0
    alterados: int = 0
    sem_empresa: list[str] = field(default_factory=list)


def ler_xls(conteudo: bytes) -> list[dict]:
    """Relatório 'Baixados' do financeiro (mesmo layout usado pelo Monitor e-Contador)."""
    try:
        import xlrd
    except ImportError as e:
        raise ValueError("Componente de leitura de XLS não instalado (xlrd).") from e
    try:
        book = xlrd.open_workbook(file_contents=conteudo)
    except Exception as e:  # noqa: BLE001
        raise ValueError("Arquivo não é uma planilha XLS válida.") from e
    folhas = [s for s in book.sheets() if s.nrows and s.ncols >= 24 and str(s.cell_value(0, 4)).strip() == "Título"
              and str(s.cell_value(0, 12)).strip() == "Baixa"]
    if len(folhas) != 1:
        raise ValueError("Layout da planilha não reconhecido (esperado o relatório de títulos baixados).")
    sh = folhas[0]

    def data(v):
        if v in ("", None):
            return None
        if isinstance(v, (int, float)):
            return datetime(*xlrd.xldate_as_tuple(v, book.datemode)).date()
        return datetime.strptime(str(v).strip()[:10], "%d/%m/%Y").date()

    def valor(v):
        if isinstance(v, (int, float)):
            return Decimal(str(v)).quantize(Decimal("0.01"))
        s = str(v).strip().replace(".", "").replace(",", ".")
        return Decimal(s or "0").quantize(Decimal("0.01"))

    linhas = []
    for i in range(1, sh.nrows):
        v = sh.row_values(i)
        if str(v[1]).strip() != "Receber" or str(v[2]).strip() != "Baixado":
            continue
        linhas.append(dict(codigo=_cod(v[6]), nome=str(v[7]).strip(), titulo=_cod(v[4]), categoria=str(v[9]).strip(),
                           vencimento=data(v[11]), pagamento=data(v[12]), valor=valor(v[13]), recebido=valor(v[23]),
                           inicio=data(v[16]), fim=data(v[17])))
    if not linhas:
        raise ValueError("Nenhum título recebido (Receber / Baixado) na planilha.")
    return linhas


def empresa_por_codigo(db: Session, codigo: str) -> models.Prestador | None:
    for p in db.scalars(select(models.Prestador).where(models.Prestador.codigo_interno != "")):
        if _cod(p.codigo_interno) == codigo:
            return p
    return None


def importar(db: Session, linhas: list[dict], arquivo: str) -> ResultadoImportacao:
    res = ResultadoImportacao()
    for l in linhas:
        r = db.scalar(select(models.Recebimento).where(models.Recebimento.codigo_empresa == l["codigo"],
                                                       models.Recebimento.titulo == l["titulo"]))
        p = empresa_por_codigo(db, l["codigo"])
        if p is None:
            res.sem_empresa.append(f"{l['codigo']} — {l['nome']} (título {l['titulo']})")
        if r is None:
            r = models.Recebimento(codigo_empresa=l["codigo"], titulo=l["titulo"])
            db.add(r)
            res.novos += 1
        elif r.nota_id:
            res.ja_existiam += 1   # já virou nota: não mexe
            continue
        elif (r.pagamento, r.recebido) != (l["pagamento"], l["recebido"]):
            res.alterados += 1
        else:
            res.ja_existiam += 1
        r.nome, r.categoria, r.vencimento, r.pagamento = l["nome"][:150], l["categoria"][:60], l["vencimento"], l["pagamento"]
        r.valor, r.recebido, r.periodo_inicio, r.periodo_fim = l["valor"], l["recebido"], l["inicio"], l["fim"]
        r.arquivo_origem = arquivo[:200]
        r.prestador_id = p.id if p else None
    db.flush()
    return res


# ---- rascunhos de NFS-e ------------------------------------------------------------------------

def escritorio(db: Session) -> models.Prestador | None:
    pid = ler_config(db, "honorarios_prestador_id") or ler_config(db, "prestador_procurador_id")
    return db.get(models.Prestador, int(pid)) if pid and str(pid).isdigit() else None


def _codigo_padrao(db: Session, p: models.Prestador, tipo: str, chave: str) -> str:
    v = ler_config(db, chave)
    if v:
        return v
    c = db.scalar(select(models.CodigoEmpresa).where(models.CodigoEmpresa.prestador_id == p.id, models.CodigoEmpresa.tipo == tipo))
    return c.codigo if c else ""


def _tomador(db: Session, esc: models.Prestador, cliente: models.Prestador) -> models.Tomador:
    t = db.scalar(select(models.Tomador).where(models.Tomador.prestador_id == esc.id, models.Tomador.documento == cliente.cnpj))
    if t is None:
        t = models.Tomador(prestador_id=esc.id, documento=cliente.cnpj, nome=cliente.razao_social[:150],
                           inscricao_municipal=cliente.inscricao_municipal or "", email=cliente.email or "",
                           telefone=cliente.telefone or "", cep=cliente.cep or "", logradouro=cliente.logradouro or "",
                           numero=cliente.numero or "", complemento=cliente.complemento or "", bairro=cliente.bairro or "",
                           codigo_municipio_ibge=cliente.codigo_municipio_ibge or "", municipio=cliente.municipio or "",
                           uf=cliente.uf or "")
        db.add(t)
        db.flush()
    return t


def gerar_rascunhos(db: Session, ids: list[int], usuario: str) -> tuple[list[models.NotaFiscal], list[str]]:
    esc = escritorio(db)
    if esc is None or not esc.emite_nfse:
        raise ValueError("Defina a empresa do escritório (que emite as notas de honorários) na tela de Honorários.")
    ctrib = _codigo_padrao(db, esc, "CTRIBNAC", "honorarios_ctribnac")
    if not ctrib:
        raise ValueError("Defina o código de tributação nacional das notas de honorários (ou cadastre-o nos códigos da empresa do escritório).")
    nbs = _codigo_padrao(db, esc, "NBS", "honorarios_nbs")
    modelo = ler_config(db, "honorarios_descricao") or DESCRICAO_PADRAO
    notas, avisos = [], []
    for r in db.scalars(select(models.Recebimento).where(models.Recebimento.id.in_(ids))).all():
        rot = f"{r.codigo_empresa} — {r.nome} (título {r.titulo})"
        if r.nota_id:
            avisos.append(f"{rot}: já tem nota (rascunho ou emitida); não foi gerada outra.")
            continue
        if r.prestador is None:
            avisos.append(f"{rot}: empresa não encontrada no cadastro pelo código.")
            continue
        if not r.pagamento or not r.recebido:
            avisos.append(f"{rot}: sem data de pagamento ou valor recebido.")
            continue
        t = _tomador(db, esc, r.prestador)
        n = models.NotaFiscal(
            prestador_id=esc.id, origem="EMITIDA", papel="PRESTADO", situacao="RASCUNHO", ambiente=esc.ambiente,
            criada_por=usuario, competencia=r.pagamento, tomador_id=t.id, tomador_documento=t.documento, tomador_nome=t.nome,
            valor_servicos=r.recebido, descricao_servico=descricao(r.pagamento, modelo),
            codigo_tributacao_nacional=ctrib, codigo_nbs=nbs, municipio_prestacao=esc.codigo_municipio_ibge,
            tributacao_issqn=1, tipo_retencao_iss=1, tot_trib_tipo="SN")
        db.add(n)
        db.flush()
        r.nota_id = n.id
        notas.append(n)
    return notas, avisos
