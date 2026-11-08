"""Leitura e guarda dos certificados A1 (.pfx/.p12)."""
from dataclasses import dataclass
from datetime import datetime

from cryptography import x509
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID

from .documentos import limpar, validar_cnpj
from .seguranca import descriptografar

# OID ICP-Brasil que carrega o CNPJ dentro do certificado (otherName da SAN)
OID_CNPJ_ICP = "2.16.76.1.3.3"


class CertificadoInvalido(Exception):
    pass


@dataclass
class DadosCertificado:
    titular: str
    cnpj: str
    numero_serie: str
    emissora: str
    validade_inicio: datetime
    validade_fim: datetime


def _cnpj_do_certificado(cert: x509.Certificate, cn: str) -> str:
    # 1) SAN otherName ICP-Brasil
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        for item in san:
            if isinstance(item, x509.OtherName) and item.type_id.dotted_string == OID_CNPJ_ICP:
                bruto = item.value.decode("latin-1", "ignore")
                cand = limpar(bruto)[-14:]
                if validar_cnpj(cand):
                    return cand
    except x509.ExtensionNotFound:
        pass
    # 2) Padrão do CN: "RAZAO SOCIAL:12345678000199"
    if ":" in cn:
        cand = limpar(cn.rsplit(":", 1)[1])
        if validar_cnpj(cand):
            return cand
    return ""


def ler_pfx(conteudo: bytes, senha: str) -> DadosCertificado:
    try:
        chave, cert, _ = pkcs12.load_key_and_certificates(conteudo, senha.encode())
    except ValueError as e:
        raise CertificadoInvalido("Senha incorreta ou arquivo não é um certificado A1 válido.") from e
    if cert is None or chave is None:
        raise CertificadoInvalido("O arquivo não contém certificado e chave privada.")

    cns = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    cn = cns[0].value if cns else cert.subject.rfc4514_string()
    emis = cert.issuer.get_attributes_for_oid(NameOID.COMMON_NAME)
    return DadosCertificado(
        titular=str(cn),
        cnpj=_cnpj_do_certificado(cert, str(cn)),
        numero_serie=format(cert.serial_number, "x"),
        emissora=str(emis[0].value) if emis else cert.issuer.rfc4514_string(),
        validade_inicio=cert.not_valid_before_utc.replace(tzinfo=None),
        validade_fim=cert.not_valid_after_utc.replace(tzinfo=None),
    )


def abrir_certificado(registro) -> tuple[bytes, str]:
    """Devolve (bytes do .pfx, senha) já descriptografados — usar só na hora de assinar/enviar."""
    return descriptografar(registro.arquivo_cripto), descriptografar(registro.senha_cripto).decode()
