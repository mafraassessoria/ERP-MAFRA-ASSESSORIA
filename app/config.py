"""Configurações do Emissor NFS-e.

Todas podem ser alteradas por variáveis de ambiente (ver README.md).
"""
import os
import secrets
from pathlib import Path

from cryptography.fernet import Fernet

BASE_DIR = Path(__file__).resolve().parent.parent

# Pasta de dados: banco, XMLs, PDFs e chaves. NUNCA versionar.
DATA_DIR = Path(os.getenv("NFSE_DATA_DIR", BASE_DIR / "dados"))
XML_DIR = DATA_DIR / "documentos"  # XML/PDF das notas, por CNPJ/ano/mês
BACKUP_DIR = Path(os.getenv("NFSE_BACKUP_DIR", BASE_DIR / "backups"))

# Banco: SQLite por padrão. Para PostgreSQL, por exemplo:
# NFSE_DB_URL=postgresql+psycopg://usuario:senha@localhost/emissor_nfse
DB_URL = os.getenv("NFSE_DB_URL", f"sqlite:///{DATA_DIR / 'emissor.db'}")

# Chave mestra que criptografa os certificados A1 e suas senhas.
# Sem ela, os certificados guardados no banco ficam ilegíveis:
# o backup copia esta chave para um arquivo separado.
CHAVE_MESTRA_PATH = Path(os.getenv("NFSE_CHAVE_MESTRA", DATA_DIR / "chave_mestra.key"))
SESSAO_KEY_PATH = DATA_DIR / "sessao.key"

# Coloque 1 quando o sistema estiver atrás de HTTPS (recomendado, pois é acessível pela internet).
HTTPS_ONLY = os.getenv("NFSE_HTTPS_ONLY", "0") == "1"

# Dias de antecedência para avisar vencimento de certificado.
DIAS_AVISO_CERTIFICADO = int(os.getenv("NFSE_DIAS_AVISO_CERTIFICADO", "30"))

# Quantos backups manter na pasta local.
BACKUPS_MANTIDOS = int(os.getenv("NFSE_BACKUPS_MANTIDOS", "30"))
# Pasta extra para cópia do backup (ex.: pasta sincronizada do Google Drive).
BACKUP_COPIA_DIR = os.getenv("NFSE_BACKUP_COPIA_DIR", "")


def _ler_ou_criar(path: Path, gerar) -> bytes:
    if path.exists():
        return path.read_bytes().strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    valor = gerar()
    path.write_bytes(valor)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return valor


def preparar_pastas() -> None:
    for p in (DATA_DIR, XML_DIR, BACKUP_DIR):
        p.mkdir(parents=True, exist_ok=True)


preparar_pastas()
CHAVE_MESTRA = _ler_ou_criar(CHAVE_MESTRA_PATH, Fernet.generate_key)
SESSAO_SECRET = _ler_ou_criar(SESSAO_KEY_PATH, lambda: secrets.token_hex(32).encode()).decode()
