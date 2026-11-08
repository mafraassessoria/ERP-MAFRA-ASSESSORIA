"""Tipos de certidão do Pack CND (mesma tabela do Monitor e-Contador) e famílias de arquivo do Bot Arquivador."""
import re

TIPOS = {
    "Certidão de FGTS": "FGTS",
    "Certidão do TST Débitos Trabalhistas": "CNDT",
    "Certidão Eletrônica de Ações Trabalhistas - TRT 3ª Região": "CEAT_TRT3",
    "Certidão Negativa de Débitos - Belo Horizonte / MG": "MUNICIPAL_BH",
    "Certidão Cível de Falência e Concordata / MG": "FALENCIA_MG",
    "Certidão Negativa de Débitos - SEF / MG": "ESTADUAL_MG",
    "Certidão Conjunta de Tributos Federais e Dívida Ativa da União": "FEDERAL",
    "Certidão Negativa de Débitos de ISS - Belo Horizonte / MG": "MUNICIPAL_ISS_BH",
    "Certidão Negativa de Débitos - Sabará / MG": "MUNICIPAL_SABARA",
    "Certidão de Falência e Concordata - Tribunal de Justiça / ES": "FALENCIA_ES",
    "Certidão Negativa de Débitos - SEFAZ / ES": "ESTADUAL_ES",
    "Certidão de Débito e Consulta a Informações Processuais de Autos de Infração": "DEBITOS_AUTOS_INFRACAO",
}
# Escopo municipal de BH: só a quitação plena (orientação registrada no Monitor e-Contador).
EXCLUIDOS = {"Certidão Negativa de Débitos de ISS - Belo Horizonte / MG"}

ROTULOS = {"FGTS": "FGTS", "CNDT": "CNDT (TST)", "CEAT_TRT3": "Ações trabalhistas (TRT 3ª)", "MUNICIPAL_BH": "Municipal BH",
           "FALENCIA_MG": "Falência MG", "ESTADUAL_MG": "Estadual MG", "FEDERAL": "Federal", "MUNICIPAL_SABARA": "Municipal Sabará",
           "FALENCIA_ES": "Falência ES", "ESTADUAL_ES": "Estadual ES", "DEBITOS_AUTOS_INFRACAO": "Autos de infração"}

SITUACOES_OK = {"Negativa", "Positiva com Efeito Negativa", "Positiva com Efeitos de Negativa"}

# Prefixo do nome que o Bot Arquivador dá ao arquivo na pasta Certidões (motor.py) → família
FAMILIAS_ARQUIVO = [
    (r"^2\.1\s*-\s*CND FGTS", "FGTS"),
    (r"^2\.2\s*-\s*CND Federal", "FEDERAL"),
    (r"^2\.3\s*-\s*CNDT", "CNDT"),
    (r"^2\.4\s*-\s*CND Estadua", "ESTADUAL"),
    (r"^2\.5\s*-\s*CND Municipal", "MUNICIPAL"),
    (r"^2\.6\s*-\s*CEAT", "CEAT"),
    (r"^3\.2\s*-\s*CERTIDAO_FALENCIA", "FALENCIA"),
]


def tipo_de(nome: str) -> str:
    return TIPOS.get((nome or "").strip(), "")


def familia(tipo: str) -> str:
    if not tipo:
        return ""
    for pref, fam in (("MUNICIPAL", "MUNICIPAL"), ("ESTADUAL", "ESTADUAL"), ("FALENCIA", "FALENCIA"), ("CEAT", "CEAT")):
        if tipo.startswith(pref):
            return fam
    return tipo if tipo in ("FGTS", "CNDT", "FEDERAL") else ""


def arquivo_local(nome: str):
    """'2.1 - CND FGTS - val. 27-11-2026.pdf' → ('FGTS', date(2026, 11, 27)); None se não for reconhecido."""
    from datetime import datetime
    fam = next((f for pad, f in FAMILIAS_ARQUIVO if re.match(pad, nome, re.I)), None)
    m = re.search(r"val\.\s*(\d{2}-\d{2}-\d{4})", nome, re.I)
    if not fam or not m:
        return None
    try:
        return fam, datetime.strptime(m.group(1), "%d-%m-%Y").date()
    except ValueError:
        return None
