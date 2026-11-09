"""Abre o e-Contador num navegador do servidor para o login humano (usuário, senha e verificação)
e guarda a sessão no ERP. Uso: CONECTAR_ECONTADOR.bat (dar dois cliques no servidor)."""
import sys
import time

BASE = "https://econtador.alterdata.com.br"
ESPERA_MAX = 15 * 60


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Falta o componente 'playwright'. Aguarde a próxima atualização automática ou rode ATUALIZAR_SISTEMA.bat.")
        return 1
    from ..config import DATA_DIR
    from ..db import SessionLocal
    from .sessao import gravar_cookies
    perfil = DATA_DIR / "econtador_navegador"
    perfil.mkdir(parents=True, exist_ok=True)
    print("Abrindo o e-Contador... Faça o login normalmente (usuário, senha e código de verificação).")
    print("Quando a tela inicial do e-Contador aparecer, esta janela guarda a sessão e fecha sozinha.")
    with sync_playwright() as pw:
        ctx = None
        for canal in ("msedge", "chrome"):
            try:
                ctx = pw.chromium.launch_persistent_context(str(perfil), channel=canal, headless=False,
                                                            args=["--start-maximized"], no_viewport=True)
                break
            except Exception as e:  # noqa: BLE001
                ultimo = e
        if ctx is None:
            print(f"Não consegui abrir o Edge nem o Chrome: {ultimo}")
            return 1
        pagina = ctx.pages[0] if ctx.pages else ctx.new_page()
        pagina.goto(BASE, wait_until="domcontentloaded")
        inicio = time.time()
        while time.time() - inicio < ESPERA_MAX:
            try:
                url = pagina.url
            except Exception:  # noqa: BLE001 — janela fechada
                print("A janela foi fechada antes do login.")
                return 1
            if url.startswith(BASE) and any(p in url for p in ("/Principal", "/Documentos", "/Empresas")):
                break
            time.sleep(2)
        else:
            print("Tempo esgotado (15 minutos) sem concluir o login.")
            ctx.close()
            return 1
        pagina.goto(BASE + "/Documentos/Enviados", wait_until="domcontentloaded")
        cks = ctx.cookies()
        ctx.close()
    with SessionLocal() as db:
        gravar_cookies(db, cks, "CONECTAR_ECONTADOR.bat")
        db.commit()
    print("Pronto: sessão do e-Contador guardada no ERP (criptografada). Pode fechar esta janela.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
