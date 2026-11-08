"""Serviços do Integra Contador usados pelo escritório.

Cada serviço diz: grupo, nome, idSistema/idServico, tipo de chamada (que define o preço),
campos que a tela pede e como montar o objeto "dados". Fonte: catálogo e cenários da
documentação oficial do Serpro (apicenter.estaleiro.serpro.gov.br/documentacao/api-integra-contador).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Callable

# Preço de referência por chamada, faixa 1 (pós-pago). Ajustável em Configurações.
PRECOS_PADRAO = {"Consultar": "0,24", "Emitir": "0,32", "Declarar": "0,40", "Apoiar": "0,00", "Monitorar": "0,24"}

CATEGORIAS_DCTF = {
    "GERAL_MENSAL": "Geral – mensal",
    "GERAL_13o_SALARIO": "Geral – 13º salário",
    "PF_MENSAL": "Pessoa física – mensal",
    "PF_13o_SALARIO": "Pessoa física – 13º salário",
}


@dataclass
class Campo:
    nome: str
    rotulo: str
    tipo: str  # periodo (AAAA-MM) | ano | data | texto | opcoes
    obrigatorio: bool = True
    opcoes: dict | None = None
    padrao: Callable[[], str] | None = None
    ajuda: str = ""

    def valor_padrao(self) -> str:
        return self.padrao() if self.padrao else ""


@dataclass
class Servico:
    codigo: str
    grupo: str
    nome: str
    id_sistema: str
    id_servico: str
    tipo: str
    montar: Callable[[dict], dict | str]
    campos: list[Campo] = field(default_factory=list)
    versao: str = "1.0"
    versao_demo: str | None = None
    aplica: tuple[str, ...] = ("TODOS",)  # TODOS | SIMPLES | MEI
    descricao: str = ""
    lote: bool = False  # pode rodar para várias empresas de uma vez

    def versao_para(self, modo: str) -> str:
        return (self.versao_demo or self.versao) if modo == "demonstracao" else self.versao


# ---- valores padrão das telas ------------------------------------------------------

def _mes_anterior() -> str:
    h = date.today().replace(day=1)
    m = h.month - 1 or 12
    a = h.year if h.month > 1 else h.year - 1
    return f"{a:04d}-{m:02d}"


def _ano_atual() -> str:
    return str(date.today().year)


def _primeiro_dia_mes_anterior() -> str:
    return _mes_anterior() + "-01"


def _hoje() -> str:
    return date.today().isoformat()


def _pa(v: str) -> str:
    """'2026-10' → '202610'"""
    d = re.sub(r"\D", "", v or "")
    if len(d) != 6:
        raise ValueError("Informe o período no formato mês/ano.")
    return d


def _ano(v: str) -> str:
    d = re.sub(r"\D", "", v or "")
    if len(d) != 4:
        raise ValueError("Informe o ano com 4 dígitos.")
    return d


def _dctf(v: dict) -> dict:
    cat = v.get("categoria") or "GERAL_MENSAL"
    if cat not in CATEGORIAS_DCTF:
        raise ValueError("Categoria da DCTFWeb inválida.")
    pa = _pa(v.get("periodo"))
    d = {"categoria": cat, "anoPA": pa[:4]}
    if "13o" not in cat:
        d["mesPA"] = pa[4:]
    return d


PERIODO = Campo("periodo", "Período de apuração", "periodo", padrao=_mes_anterior)
ANO = Campo("ano", "Ano-calendário", "ano", padrao=_ano_atual)
CATEGORIA = Campo("categoria", "Categoria", "opcoes", opcoes=CATEGORIAS_DCTF, padrao=lambda: "GERAL_MENSAL")

SERVICOS: list[Servico] = [
    # ---- Situação fiscal
    Servico("sitfis", "Situação fiscal", "Relatório de situação fiscal (PDF)", "SITFIS", "RELATORIOSITFIS92", "Emitir",
            montar=lambda v: "", versao="2.0", versao_demo="1.0", lote=True,
            descricao="Pede o protocolo e baixa o relatório completo da situação fiscal (o mesmo do e-CAC). "
                      "Duas chamadas: Apoiar (protocolo) + Emitir (relatório)."),
    # ---- Simples Nacional
    Servico("pgdasd_declaracoes", "Simples Nacional", "Declarações PGDAS-D transmitidas no ano", "PGDASD", "CONSDECLARACAO13",
            "Consultar", montar=lambda v: {"anoCalendario": _ano(v.get("ano"))}, campos=[ANO], aplica=("SIMPLES",)),
    Servico("pgdasd_ultima", "Simples Nacional", "Última declaração e recibo do período (PDF)", "PGDASD", "CONSULTIMADECREC14",
            "Consultar", montar=lambda v: {"periodoApuracao": _pa(v.get("periodo"))}, campos=[PERIODO], aplica=("SIMPLES",)),
    Servico("pgdasd_das", "Simples Nacional", "Gerar DAS do período (PDF)", "PGDASD", "GERARDAS12", "Emitir",
            montar=lambda v: {"periodoApuracao": _pa(v.get("periodo"))}, campos=[PERIODO], aplica=("SIMPLES",), lote=True,
            descricao="Gera o DAS de um período já declarado no PGDAS-D."),
    Servico("pgdasd_extrato", "Simples Nacional", "Extrato do DAS", "PGDASD", "CONSEXTRATO16", "Consultar",
            montar=lambda v: {"numeroDas": re.sub(r"\D", "", v.get("numero_das") or "")},
            campos=[Campo("numero_das", "Número do DAS", "texto")], aplica=("SIMPLES",)),
    # ---- MEI
    Servico("pgmei_das", "MEI", "Gerar DAS do MEI (PDF)", "PGMEI", "GERARDASPDF21", "Emitir",
            montar=lambda v: {"periodoApuracao": _pa(v.get("periodo"))}, campos=[PERIODO], aplica=("MEI",), lote=True),
    Servico("pgmei_divida", "MEI", "Dívida ativa do MEI", "PGMEI", "DIVIDAATIVA24", "Consultar",
            montar=lambda v: {"anoCalendario": _ano(v.get("ano"))}, campos=[ANO], aplica=("MEI",)),
    # ---- DCTFWeb
    Servico("dctf_recibo", "DCTFWeb", "Recibo de transmissão (PDF)", "DCTFWEB", "CONSRECIBO32", "Consultar",
            montar=_dctf, campos=[CATEGORIA, PERIODO]),
    Servico("dctf_completa", "DCTFWeb", "Relatório da declaração completa (PDF)", "DCTFWEB", "CONSDECCOMPLETA33", "Consultar",
            montar=_dctf, campos=[CATEGORIA, PERIODO]),
    Servico("dctf_guia", "DCTFWeb", "Gerar guia (DARF) da declaração (PDF)", "DCTFWEB", "GERARGUIA31", "Emitir",
            montar=_dctf, campos=[CATEGORIA, PERIODO], lote=True),
    # ---- MIT (empresas fora do Simples)
    Servico("mit_lista", "DCTFWeb", "Apurações MIT do ano", "MIT", "LISTAAPURACOES317", "Consultar",
            montar=lambda v: {"anoApuracao": int(_ano(v.get("ano")))}, campos=[ANO], aplica=("OUTRO",),
            descricao="Lista as apurações do MIT (Módulo de Inclusão de Tributos) do ano e a situação de cada uma."),
    # ---- DEFIS
    Servico("defis_lista", "Simples Nacional", "Declarações DEFIS transmitidas", "DEFIS", "CONSDECLARACAO142", "Consultar",
            montar=lambda v: "", aplica=("SIMPLES",)),
    # ---- Pagamentos
    Servico("pagamentos", "Pagamentos", "Pagamentos no período", "PAGTOWEB", "PAGAMENTOS71", "Consultar",
            montar=lambda v: {"intervaloDataArrecadacao": {"dataInicial": v.get("de"), "dataFinal": v.get("ate")},
                              "primeiroDaPagina": 0, "tamanhoDaPagina": 100},
            campos=[Campo("de", "Arrecadação de", "data", padrao=_primeiro_dia_mes_anterior),
                    Campo("ate", "até", "data", padrao=_hoje)]),
    Servico("comprovante", "Pagamentos", "Comprovante de arrecadação (PDF)", "PAGTOWEB", "COMPARRECADACAO72", "Emitir",
            montar=lambda v: {"numeroDocumento": re.sub(r"\D", "", v.get("numero_documento") or "")},
            campos=[Campo("numero_documento", "Número do documento", "texto", ajuda="Número que aparece na lista de pagamentos")]),
    # ---- Parcelamentos
    Servico("parcsn_pedidos", "Parcelamentos", "Parcelamentos do Simples (ordinário)", "PARCSN", "PEDIDOSPARC163", "Consultar",
            montar=lambda v: "", aplica=("SIMPLES",)),
    Servico("parcsn_parcelas", "Parcelamentos", "Parcelas do Simples disponíveis para emitir", "PARCSN", "PARCELASPARAGERAR162",
            "Consultar", montar=lambda v: "", aplica=("SIMPLES",)),
    Servico("parcsn_das", "Parcelamentos", "DAS da parcela do Simples (PDF)", "PARCSN", "GERARDAS161", "Emitir",
            montar=lambda v: {"parcelaParaEmitir": int(_pa(v.get("periodo")))}, campos=[Campo("periodo", "Parcela (mês/ano)", "periodo",
                                                                                                 padrao=lambda: date.today().strftime("%Y-%m"))],
            aplica=("SIMPLES",)),
    Servico("parcmei_pedidos", "Parcelamentos", "Parcelamentos do MEI (ordinário)", "PARCMEI", "PEDIDOSPARC203", "Consultar",
            montar=lambda v: "", aplica=("MEI",)),
    Servico("parcmei_parcelas", "Parcelamentos", "Parcelas do MEI disponíveis para emitir", "PARCMEI", "PARCELASPARAGERAR202",
            "Consultar", montar=lambda v: "", aplica=("MEI",)),
    Servico("parcmei_das", "Parcelamentos", "DAS da parcela do MEI (PDF)", "PARCMEI", "GERARDAS201", "Emitir",
            montar=lambda v: {"parcelaParaEmitir": int(_pa(v.get("periodo")))}, campos=[Campo("periodo", "Parcela (mês/ano)", "periodo",
                                                                                                 padrao=lambda: date.today().strftime("%Y-%m"))],
            aplica=("MEI",)),
    # ---- Caixa postal
    Servico("caixa_indicador", "Caixa postal", "Há mensagens novas?", "CAIXAPOSTAL", "INNOVAMSG63", "Monitorar",
            montar=lambda v: "", lote=True),
    Servico("caixa_lista", "Caixa postal", "Lista de mensagens", "CAIXAPOSTAL", "MSGCONTRIBUINTE61", "Consultar",
            montar=lambda v: {"statusLeitura": v.get("status") or "0", "indicadorPagina": "0", "ponteiroPagina": "00000000000000"},
            campos=[Campo("status", "Mensagens", "opcoes", opcoes={"0": "Todas", "2": "Não lidas", "1": "Lidas"}, padrao=lambda: "2")]),
    Servico("caixa_msg", "Caixa postal", "Ler uma mensagem", "CAIXAPOSTAL", "MSGDETALHAMENTO62", "Consultar",
            montar=lambda v: {"isn": re.sub(r"\D", "", v.get("isn") or "")},
            campos=[Campo("isn", "Código da mensagem (isn)", "texto", ajuda="Aparece na lista de mensagens")]),
    # ---- Procurações
    Servico("procuracoes", "Procurações", "Procuração para o escritório (validade e serviços)", "PROCURACOES", "OBTERPROCURACAO41",
            "Consultar", montar=lambda v: v.get("_procuracao") or {}, lote=True,
            descricao="Confere se a empresa deu procuração eletrônica ao escritório e até quando vale."),
]

POR_CODIGO = {s.codigo: s for s in SERVICOS}
GRUPOS = list(dict.fromkeys(s.grupo for s in SERVICOS))


def servicos_para(regime: str) -> list[Servico]:
    """Serviços que fazem sentido para o regime da empresa."""
    tipo = "MEI" if regime == "MEI" else ("SIMPLES" if regime in ("SIMPLES", "SIMPLES_EXCESSO") else "OUTRO")
    return [s for s in SERVICOS if "TODOS" in s.aplica or tipo in s.aplica]
