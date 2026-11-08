"""Validação e formatação de CPF/CNPJ (inclui o CNPJ alfanumérico — IN RFB 2.229/2024)."""
import re


def limpar(doc: str) -> str:
    """Remove máscara. Mantém letras (CNPJ alfanumérico) em maiúsculas."""
    return re.sub(r"[^0-9A-Za-z]", "", doc or "").upper()


def so_digitos(txt: str) -> str:
    return re.sub(r"\D", "", txt or "")


def validar_cpf(cpf: str) -> bool:
    cpf = so_digitos(cpf)
    if len(cpf) != 11 or cpf == cpf[0] * 11:
        return False
    for n in (9, 10):
        soma = sum(int(cpf[i]) * (n + 1 - i) for i in range(n))
        dv = (soma * 10) % 11 % 10
        if dv != int(cpf[n]):
            return False
    return True


def _valor(c: str) -> int:
    return ord(c) - 48  # '0'..'9' -> 0..9 ; 'A'..'Z' -> 17..42


def validar_cnpj(cnpj: str) -> bool:
    cnpj = limpar(cnpj)
    if not re.fullmatch(r"[0-9A-Z]{12}[0-9]{2}", cnpj) or cnpj == cnpj[0] * 14:
        return False
    pesos1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    pesos2 = [6] + pesos1
    for pesos, pos in ((pesos1, 12), (pesos2, 13)):
        soma = sum(_valor(cnpj[i]) * pesos[i] for i in range(pos))
        resto = soma % 11
        dv = 0 if resto < 2 else 11 - resto
        if dv != int(cnpj[pos]):
            return False
    return True


def validar_documento(doc: str) -> bool:
    d = limpar(doc)
    return validar_cpf(d) if len(d) == 11 else validar_cnpj(d)


def formatar_documento(doc: str) -> str:
    d = limpar(doc)
    if len(d) == 11:
        return f"{d[:3]}.{d[3:6]}.{d[6:9]}-{d[9:]}"
    if len(d) == 14:
        return f"{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:]}"
    return doc or ""
