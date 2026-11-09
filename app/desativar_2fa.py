"""Remove a verificação em duas etapas de um usuário (para quem perdeu o celular).
Uso no servidor:  DESATIVAR_2FA.bat <login>"""
import sys

from sqlalchemy import select

from . import models
from .db import SessionLocal


def main() -> int:
    if len(sys.argv) != 2:
        print("Uso: DESATIVAR_2FA.bat <login>")
        return 1
    login = sys.argv[1].strip().lower()
    with SessionLocal() as db:
        u = db.scalar(select(models.Usuario).where(models.Usuario.login == login))
        if not u:
            print(f"Usuário {login} não encontrado.")
            return 1
        u.totp_segredo, u.totp_ultimo = "", 0
        db.add(models.Auditoria(usuario_login="servidor", acao="ZERAR_2FA", entidade="usuario", entidade_id=u.id,
                                detalhes=f"login={login} (pelo DESATIVAR_2FA.bat)", ip="local"))
        db.commit()
    print(f"Verificação em duas etapas de {login} removida. Entre só com a senha e ative de novo.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
