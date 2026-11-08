"""Restaura um backup do Emissor NFS-e.

Uso:   python -m scripts.restaurar caminho/do/emissor_nfse_AAAAMMDD_HHMMSS.zip [caminho/chave_mestra.key]

PARE o sistema antes de restaurar. Os dados atuais são movidos para dados_antes_restauracao_<data>.
"""
import shutil
import sys
import zipfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    zip_path = Path(sys.argv[1])
    chave = Path(sys.argv[2]) if len(sys.argv) > 2 else zip_path.parent / "chave_mestra.key"
    if not zip_path.exists() or not chave.exists():
        print("Arquivo de backup ou chave_mestra.key não encontrados.")
        sys.exit(1)

    from app.config import CHAVE_MESTRA_PATH, DATA_DIR  # importa só depois de validar

    if input(f"Restaurar {zip_path.name} sobre {DATA_DIR}? Digite SIM: ").strip() != "SIM":
        print("Cancelado.")
        return
    reserva = DATA_DIR.parent / f"dados_antes_restauracao_{datetime.now():%Y%m%d_%H%M%S}"
    shutil.move(str(DATA_DIR), reserva)
    DATA_DIR.mkdir(parents=True)
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(DATA_DIR)
    shutil.copy2(chave, CHAVE_MESTRA_PATH)
    sessao = reserva / "sessao.key"
    if sessao.exists():
        shutil.copy2(sessao, DATA_DIR / "sessao.key")
    print(f"Restaurado. Dados anteriores guardados em {reserva}")


if __name__ == "__main__":
    main()
