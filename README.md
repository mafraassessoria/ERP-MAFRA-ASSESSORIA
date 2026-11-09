# ERP MAFRA ASSESSORIA — M & R Assessoria Contábil

Sistema do escritório: emissor multiempresa de NFS-e, Receita Federal (Integra Contador), e-Contador e certidões. O emissor é integrado ao Sistema Nacional (Sefin Nacional / ADN).
Versão atual: **Etapa 3**. Inclui cadastros, certificados, auditoria e backup; a **emissão, a consulta e o
cancelamento de NFS-e**, com DANFSe em PDF; e também a **importação das notas pelo ADN**, o **envio por e-mail**
e os **relatórios por competência**.

## Dois tipos de prestador

| Modo | Uso |
|---|---|
| **Emissão pelo escritório** | Os usuários da M & R emitem as notas no sistema com o certificado A1 do cliente. |
| **Somente importação** | A empresa emite as próprias notas. O sistema só baixa do ADN (pelo NSU), com o certificado da empresa, para visualizar e arquivar o XML e o PDF. |

## Instalação no servidor (Windows)

1. Instale o Python 3.11 ou mais recente (marque "Add python.exe to PATH").
2. Descompacte esta pasta em `C:\emissor_nfse`.
3. Execute `iniciar.bat` uma vez. Ele cria o ambiente e instala os componentes. Pode fechar a janela depois.
4. Execute **`INSTALAR_INICIO_AUTOMATICO.bat`** e confirme a pergunta do Windows ("Sim"). O script:
   - cria a tarefa **"Emissor NFS-e - Servidor"**, que inicia o sistema junto com o Windows, sem janela, mesmo que ninguém faça login. Se o sistema parar, volta sozinho em 10 segundos;
   - cria a tarefa **"Emissor NFS-e - Backup diario"**, que roda todo dia às 23h. Se o servidor estiver desligado nesse horário, o backup roda quando ele ligar;
   - cria a tarefa **"Emissor NFS-e - Importacao ADN"**, que importa as notas todo dia às 07h e às 13h;
   - já sobe o sistema e faz um primeiro backup de teste.
5. Acesse pelo navegador **no servidor** (via RDP): `http://localhost:8800`.
6. **Outras contas do Windows no servidor:** o emissor roda para todas as contas (a tarefa usa a conta do sistema).
   Entre com a conta da pessoa e dê dois cliques em `CRIAR_ATALHO.bat` para criar o atalho "Emissor NFS-e"
   na Área de Trabalho dela. Depois crie o login dela em **Usuários** (dentro do emissor).

| Arquivo | Para quê |
|---|---|
| `INSTALAR_INICIO_AUTOMATICO.bat` | cria (ou recria) as duas tarefas |
| `ATUALIZAR_SISTEMA.bat` | depois de copiar uma versão nova: para o sistema, instala componentes novos e sobe de novo |
| `REINICIAR_SISTEMA.bat` | só reinicia o sistema |
| `REMOVER_INICIO_AUTOMATICO.bat` | para o sistema e remove as tarefas (não apaga dados) |
| `backup.bat` | faz um backup na hora, com dois cliques |
| `importar.bat` | importa as notas do ADN na hora (todas as empresas) |
| `logs\servidor.log` / `backup.log` / `importacao.log` | registros, para conferir erros |

As tarefas também aparecem no **Agendador de Tarefas** do Windows.

### Atualizar para uma nova versão

1. Rode `backup.bat` (por segurança).
2. Descompacte a nova versão por cima da pasta `C:\emissor_nfse` e aceite substituir os arquivos.
3. Execute `ATUALIZAR_SISTEMA.bat` e confirme com "Sim".
4. Se a versão trouxer tarefa agendada nova (a Etapa 3 traz a importação ADN), execute também
   `INSTALAR_INICIO_AUTOMATICO.bat`. Ele recria as tarefas, sem perder dados.

As pastas `dados`, `backups` e `.venv` não são afetadas. O banco é atualizado sozinho na primeira
execução: o sistema acrescenta as colunas novas e não apaga nada.

### Segurança do acesso

O sistema escuta apenas em `127.0.0.1`, ou seja, só pode ser aberto **de dentro do servidor**.
Como a equipe acessa o servidor por RDP, a porta 8800 não fica exposta na rede nem na internet,
e não é necessário configurar HTTPS. **Não** crie redirecionamento da porta 8800 no roteador.

Com isso, a segurança do emissor passa a depender da segurança do RDP. Recomendações:
- um usuário do Windows por pessoa, com senha forte;
- RDP com NLA ativado e, de preferência, acessível só por VPN ou com porta trocada e bloqueio de tentativas;
- Windows sempre atualizado.

## Onde ficam os dados

```
dados/
  emissor.db          banco de dados (SQLite)
  chave_mestra.key    chave que criptografa certificados e senhas (MUITO IMPORTANTE)
  sessao.key          chave das sessões de login
  documentos/         XML e PDF das notas, por CNPJ/ano/mês
backups/
```

**Sem o arquivo `chave_mestra.key`, os certificados guardados ficam ilegíveis.** Guarde uma cópia dele em
local seguro, separada dos backups (pen drive no cofre, gerenciador de senhas etc.).

## Backup

```
python -m scripts.backup
```

O backup gera `backups/emissor_nfse_AAAAMMDD_HHMMSS.zip` com o banco e os documentos, e uma cópia da chave mestra.
O banco é copiado de forma íntegra mesmo com o sistema em uso, e a cópia é conferida.
Por padrão, o sistema guarda os 30 backups mais recentes.

**Cópia no Google Drive:** no Google Drive para computador (ícone perto do relógio > engrenagem > Preferências >
"Meu computador" > "Adicionar pasta"), adicione a pasta `C:\emissor_nfse\backups`. Cada backup novo vai
automaticamente para a nuvem. Use este caminho, e não a unidade `G:`: a tarefa de backup roda como
SYSTEM e não enxerga a unidade `G:` do seu usuário.

O backup diário é agendado pelo `INSTALAR_INICIO_AUTOMATICO.bat` (ver acima). O painel mostra a data do último backup e avisa se passar de 2 dias sem backup.

**Restaurar** (pare o sistema antes com `REMOVER_INICIO_AUTOMATICO.bat`; depois instale de novo):
```
cd C:\emissor_nfse
C:\emissor_nfse\.venv\Scripts\python -m scripts.restaurar backups\emissor_nfse_20261102_230000.zip backups\chave_mestra.key
```

Faça um teste de restauração uma vez por mês em outra pasta ou máquina.

## Variáveis de configuração (opcionais)

| Variável | Padrão | Para quê |
|---|---|---|
| `NFSE_DATA_DIR` | `./dados` | pasta de dados |
| `NFSE_DB_URL` | SQLite em `dados/` | ex.: `postgresql+psycopg://user:senha@localhost/emissor` |
| `NFSE_BACKUP_DIR` | `./backups` | pasta dos backups |
| `NFSE_BACKUP_COPIA_DIR` | (vazio) | segunda cópia (ex.: Google Drive) |
| `NFSE_BACKUPS_MANTIDOS` | 30 | quantos backups manter |
| `NFSE_DIAS_AVISO_CERTIFICADO` | 30 | aviso de vencimento do A1 |

## Empresa selecionada

Ao abrir uma empresa, aparece uma barra azul-clara com o nome dela e os atalhos **Notas**, **Emitir nota**,
**Emitir em lote**, **Cadastros** e **Configurações**. Dá para navegar com voltar/avançar do navegador.
**Sair da empresa** volta para a lista de prestadores (ordenada pelo código interno; a busca filtra ao digitar).

A tela da empresa tem três abas:

- **Notas fiscais**: filtro completo (situação, tipo, competência, período, pesquisa), colunas *ISS retido* e
  *INSS retido* quando houver, caixas de seleção e as ações **Exportar selecionadas**, **Clonar selecionadas** e
  **Transmitir selecionadas**. Cada nota prestada tem também a ação **clonar**.
- **Cadastros**: tomadores, **códigos de serviço da empresa** (código de tributação nacional e NBS) e intermediários.
- **Configurações da empresa**: dados, certificado A1, procuração, importação pelo ADN e **Editar dados**.

A tela geral **Notas** (todas as empresas) tem seleção e exportação, mas não clona nem transmite.

## Códigos de serviço (tabelas oficiais)

O sistema já vem com as tabelas oficiais do Portal da NFS-e: lista de serviços nacional (cTribNac, LC 116),
NBS e a correlação item × NBS (Anexo B e Anexo VIII). Em **Configurações → Tabelas de serviços** dá para
importar uma versão nova das planilhas quando o governo publicar.

Cada empresa tem a sua lista de códigos (aba Cadastros). Só eles aparecem na emissão; os códigos das notas
importadas e dos antigos "serviços padrão" entram sozinhos. Na emissão também dá para pesquisar outro código
da tabela nacional; ele passa a fazer parte da lista da empresa.

## Emitir uma nota (tela em etapas)

1. **Pessoas**: tomador (pesquisa ao digitar; vazio = consumidor não identificado), competência e intermediário (opcional).
2. **Serviço**: país (Brasil) e município da prestação (pesquisa pelo nome), código de tributação, imunidade/não incidência,
   NBS (com sugestões da tabela de correlação), descrição (até 2.000 caracteres) e informações complementares.
   Se o código for de obra (subitens definidos em Configurações, padrão 7.02, 7.04, 7.05 e 7.19), aparece o grupo
   **Informações para obra**: obra já cadastrada do tomador ou nova (CNO, CIB ou endereço) — a nova vira cadastro do tomador.
3. **Valores**: valor, valor recebido pelo intermediário, descontos, retenção do ISS (pelo tomador ou pelo intermediário,
   alíquota **entre 1,80% e 5,00%**), redução da base (só obra), base e ISS calculados na hora, retenções federais
   (tipo de retenção PIS/COFINS/CSLL, IRRF, CSLL e contribuição previdenciária) e valor aproximado dos tributos
   (alíquota do Simples do cadastro, valores em R$ ou percentuais).
4. **Emitir NFS-e**: resumo para conferir, **Emitir NFS-e** ou **Salvar rascunho**.

O botão **Avançar** só libera quando os campos obrigatórios da etapa estiverem preenchidos (o que falta aparece no rodapé),
e cada campo libera depois que o obrigatório anterior for preenchido.

## Emissão em lote por planilha

**Emitir em lote** (na barra da empresa): envie uma planilha .xlsx ou .csv com as colunas **CPF/CNPJ, Nome/Empresa, Data, Histórico,
Valor e E-mail** (há um modelo para baixar). Vale para pessoa física e empresa. Escolha o código de serviço, a NBS e o município, confira a prévia
(linhas com problema são mostradas e ficam de fora) e clique em **Emitir** — ou só crie os rascunhos.
Os clientes entram no cadastro de tomadores e cada um recebe o PDF + XML por e-mail, se tiver e-mail.
Notas de obra ou com ISS retido devem ser emitidas individualmente.

## Clonar uma nota

Na lista de notas da empresa ou no detalhe de uma nota prestada, **clonar** cria um rascunho igual
(tomador, código, valores, obra, retenções, intermediário), com competência de hoje. Ajuste o que mudou e emita.

## Cadastro automático do tomador na importação

Ao importar notas pelo ADN, o tomador de cada nota prestada é cadastrado automaticamente (ou tem os campos vazios
completados), junto com a obra quando for o caso. Os códigos de serviço e NBS da nota entram na lista da empresa.

## Nota de obra (construção civil)

- **Obras:** cadastre na tela do **tomador** (seção "Obras deste tomador"), com CNO/CEI, CIB ou endereço,
  e o **código IBGE do município da obra**.
  Também dá para informar a obra direto na tela de emissão (vira cadastro do tomador).
- **Serviço de obra:** é identificado pelo código de tributação (subitens em Configurações).
- **Na nota:** escolha a obra. O local da prestação passa a ser o município dela. Informe também, quando
  houver:
  - **redução da base de cálculo** (valor ou percentual);
  - **alíquota do ISS**, quando o ISS é retido (entre 1,80% e 5,00%);
  - **retenções** de INSS, IRRF e CSLL ("calcular 11%" sugere o INSS, você confere).
- **Validação:** o XML gerado foi validado contra o **XSD oficial da DPS v1.01**.

## Preenchimento automático pelo CNPJ e pelo CEP

Nos cadastros de prestador e de tomador, digite o CNPJ e clique em **Buscar dados**. O sistema preenche
a razão social, o nome fantasia, o endereço, o município com o código IBGE, o telefone e o e-mail. Em
prestador novo, também sugere o regime (MEI ou Simples). A tela mostra a situação cadastral e fica em
vermelho se a empresa não estiver ATIVA.

Ao digitar um **CEP** e sair do campo, o endereço e o código IBGE são completados. Isso também serve
para tomador pessoa física.

- **Fontes:** BrasilAPI, Minha Receita e CNPJ.ws, nessa ordem. Todas são gratuitas e usam os **dados
  abertos do CNPJ** publicados pela Receita Federal. Os CEPs vêm do ViaCEP.
- **Atualização:** a base pública é atualizada mensalmente. Empresas abertas ou alteradas há poucos
  dias podem não aparecer ou aparecer desatualizadas. Confira sempre antes de salvar.
- **CPF:** não é consultável, porque é dado pessoal protegido pela LGPD. Preencha à mão.
- **Para escala maior:** a consulta oficial paga da Receita é a API do Serpro (Consulta CNPJ).

## Como emitir uma nota (resumo)

1. **Prestador:** confira o regime, o código IBGE e a inscrição municipal. A inscrição municipal só deve ser
   preenchida se constar no cadastro nacional (CNC) do município. Para ME/EPP, informe também o
   **% aproximado de tributos do Simples**. Marque "município aderente" e "autorização do certificado".
2. **Certificado A1** enviado e válido.
3. **Códigos de serviço da empresa** (aba Cadastros), se ainda não vieram das notas importadas.
4. **Emitir nota**: siga as 4 etapas e clique em **Emitir NFS-e** (ou salve o rascunho).
5. Se faltar algo, a nota fica como rascunho e a tela dela lista o que corrigir; depois clique em **Transmitir**.
6. Autorizada, a nota fica com o **DANFSe (PDF)** e o **XML** disponíveis para download.

**Proteção contra nota duplicada:** o número da DPS fica gravado na nota antes do envio. Se a internet
cair no meio da transmissão, a nota fica como "Enviada / aguardando". Clique em **Transmitir** de novo:
antes de reenviar, o sistema pergunta à Sefin se aquela DPS já virou nota. Se já virou, recupera a nota
em vez de emitir outra.

**Homologação × produção:** cada prestador tem seu ambiente e um contador de DPS para cada um.
Em homologação, o DANFSe sai com a marca "SEM VALIDADE JURÍDICA". Para emitir em produção, a
tela pede confirmação.

**Arquivos:** ficam em `dados\documentos\<CNPJ>\<ano>\<mês>\`: a DPS assinada, a NFS-e, o PDF e o
XML do cancelamento, quando houver. Notas de homologação levam o prefixo `HOMOLOG_`.

**Limitação desta versão:** o Regime Regular (Lucro Presumido/Real) ainda está bloqueado para emissão, porque
exige o grupo IBS/CBS, que entra numa próxima etapa. A importação funciona para qualquer regime.

## Importação pelo ADN

O ADN é a "caixa postal" de cada CNPJ no Sistema Nacional. A importação traz:
- as notas que a empresa **emitiu**, inclusive pelo portal da prefeitura ou por outro sistema (papel "Prestado");
- as notas que ela **recebeu** como tomadora (papel "Tomado"), úteis para a escrita fiscal;
- os **cancelamentos**, que atualizam a situação das notas já guardadas.

A leitura é incremental: cada consulta continua de onde a anterior parou (NSU). Ela roda sozinha
2x por dia e também pode ser feita pelos botões "Importar agora" (no prestador) e "Importar notas do ADN"
(em Relatórios).

**Procuração eletrônica (emissão, cancelamento e importação):** desde 01/11/2026, as APIs de emissão e de
distribuição da NFS-e Nacional aceitam operar em nome de terceiros. Quando a empresa está marcada para
"usar o certificado do escritório", o sistema usa o e-CNPJ do escritório na conexão e na assinatura da DPS
e do cancelamento. A DPS continua com o CNPJ do cliente como prestador. Requisitos:
- o cliente concede a procuração ao CNPJ do escritório no **serviço de NFS-e**, em Procurações Eletrônicas
  do Portal de Serviços da Receita;
- a empresa do escritório fica escolhida em Configurações, com o certificado enviado.

**Qual certificado:** por padrão, o da própria empresa. Para clientes que não entregam o certificado,
marque no prestador "Importar com o certificado do escritório (procuração)". Depois, em **Configurações**,
escolha qual empresa é o escritório. Ela precisa estar cadastrada como prestador e ter o e-CNPJ enviado.
O cliente precisa conceder ao escritório **procuração eletrônica** para a NFS-e. Sem ela, o ADN recusa a
consulta (erro E2245) e o sistema mostra o motivo.

O ambiente da importação é o do prestador. Para empresas que só importam, use **Produção**.

## Envio por e-mail

Em **Configurações** (perfil administrador), preencha o SMTP. No Gmail: `smtp.gmail.com`, porta 587,
STARTTLS, com uma **senha de app**, não a senha normal da conta. Use o botão de e-mail de teste para conferir.

- Na tela da nota, "Enviar PDF + XML" envia ao tomador. Dá para incluir mais de um endereço.
- Marque no prestador "Enviar a nota por e-mail automaticamente" para enviar assim que a nota for autorizada.
- Opcional: cópia oculta de todo envio para um e-mail do escritório.

## Exportar PDF + XML das notas

Em **Notas**, filtre por empresa, situação, prestadas/tomadas, mês ou período (de/até) e pesquisa.
Depois clique em **Exportar PDF + XML (ZIP)**. O ZIP traz o DANFSe (PDF), o XML da nota e o XML
de cancelamento, quando houver, de **todas** as notas do filtro, mesmo acima de 500. Os arquivos ficam
organizados em `CNPJ_Empresa\prestadas` e `CNPJ_Empresa\tomadas`. O arquivo `LEIA-ME.txt` resume
os filtros e as quantidades.

As notas importadas do ADN chegam só com o XML. O PDF delas é gerado na primeira exportação e
fica guardado para as próximas.

## Relatórios e exportação

**Relatórios** mostra, por empresa e competência: notas prestadas (quantidade, valor, ISS, ISS retido),
canceladas e tomadas. Também é possível:
- **Baixar planilha (CSV)**, que abre direto no Excel, com todas as notas do mês;
- **Baixar XML (ZIP)**, com os XML do mês organizados por CNPJ e prestadas/tomadas, para a escrita fiscal.

**Cópia automática para outra pasta:** em Configurações, informe uma pasta (ex.: `C:\NF-Stock`).
Cada XML de produção, emitido ou importado, também é copiado para `pasta\CNPJ\`, sem duplicar.

A montagem da DPS, a assinatura digital, a comunicação com a Sefin e o DANFSe usam a biblioteca
[PyNFSeNacionalGT](https://github.com/Gomes343/PyNFSeNacional) (licença MIT), já usada em produção.
A versão fica fixada no `requirements.txt` para não mudar sem teste.

## Cadastro geral de empresas e módulos

O menu **Empresas** é o cadastro único do escritório. Em cada empresa, a seção **Módulos e integrações** diz o que é usado para ela:
NFS-e, Receita Federal (Integra Contador), e-Contador e Certidões. O que está desmarcado some da tela da empresa e das rotinas
automáticas (importação ADN, consultas em lote). A lista de empresas filtra por módulo e liga/desliga módulos em lote.

Em Configurações → Cadastro geral de empresas, importe o `empresas.json` do Bot Arquivador: traz a pasta de cada empresa no Drive,
CNPJs adicionais e outros nomes. Empresas novas entram sem o módulo NFS-e. Nada é apagado; conflitos (mesmo código com CNPJ diferente)
são listados para conferência. Pode importar de novo sempre que o cadastro do arquivador mudar.

Organização do código: `app/` (núcleo: empresas, usuários, certificados, configurações), `app/nfse/` (emissor e importação ADN)
`app/serpro/` (Receita Federal – Integra Contador), `app/econtador/` (API do e-Contador) e `app/certidoes/`. Os módulos usam o núcleo e não dependem uns dos outros.

## Guias e baixa

Menu **Guias**: gera pelo Integra Contador só guias vencidas (DAS do Simples e do MEI com multa e juros até a data escolhida,
DARF da DCTFWeb) e parcelas de parcelamento ("Gerar parcelas do mês" usa as empresas que o Monitor e-CAC indicou com
parcelamento em andamento). Uma guia nova da mesma empresa, tipo e competência substitui a anterior. Opcionalmente, cada guia é
entregue ao Bot Arquivador. A baixa é automática quando a rotina Pagamentos do Monitor encontra o pagamento: pelo número do
documento ou, se não houver, pelo valor exato pago até o vencimento (quando há mais de um pagamento possível, fica para conferir).

## Honorários

Menu **Honorários**: importe o XLS de títulos baixados do financeiro, marque os recebimentos e gere os rascunhos de NFS-e da
empresa do escritório (competência = data do pagamento; descrição "HONORÁRIOS REFERENTES A PRESTAÇÃO DE SERVIÇOS CONTÁBEIS NO MÊS
DE <MÊS>/<ANO>" do pagamento). O cliente é achado pelo código interno e vira tomador automaticamente. Um recebimento nunca gera
duas notas. Os rascunhos são conferidos e transmitidos na aba Notas fiscais.

## Monitor e-CAC (substituto do eRadar)

Menu **Monitor e-CAC**: painel com situação fiscal (regular / com pendência, lida do PDF do relatório), parcelamentos,
caixa postal (não lidas), pagamentos (em dia / em atraso) e entregas do mês anterior — PGDAS-D, DCTFWeb, MIT (empresas fora
do Simples) e DEFIS (ano anterior). Clique num quadro para ver as empresas; cada linha abre a consulta que originou o resultado.

Rotinas automáticas (no fim do painel): cada uma tem agenda semanal, mensal ou anual, roda para as empresas com o módulo Receita
Federal ligado e mostra o custo estimado por execução. Começam desligadas. Tudo usa as consultas do módulo Receita Federal
(mesmo limite de gasto mensal e mesmo histórico). A DCTFWeb reconsulta só os meses ainda em aberto (até 3) para economizar.
A leitura do PDF da situação fiscal usa o pacote `pypdf` (instalado pelo ATUALIZAR_SISTEMA.bat).

## e-Contador e certidões (Pack CND)

Configurações → e-Contador e certidões: cole a chave da API (e-Contador → Configurações → ePlugin), confira com "Testar conexão"
e use "Sincronizar empresas" (liga cada empresa pelo CNPJ e confere o código interno; nada é criado nem apagado).

O menu **Certidões** e a aba Certidões de cada empresa mostram o que está no Pack CND: válida, vence em até 15 dias, vencida,
positiva (conferir), sem arquivo no Pack. O PDF das certidões válidas fica guardado em `dados/certidoes/`.

Entrega ao Drive: a certidão mais nova de cada empresa e tipo, válida e negativa, é copiada para a pasta de entrada do
Bot Arquivador (padrão `C:\Monitor de Arquivos`); o arquivador renomeia e guarda na pasta Certidões da empresa. Não entrega se
o sistema já entregou outra igual ou mais nova, nem se a pasta Certidões do Drive (quando acessível) já tiver uma igual ou mais nova.
Ao começar, se o Monitor e-Contador já tinha entregue as certidões atuais, use "Considerar as atuais como já arquivadas" uma vez,
e desative a tarefa de sexta do Monitor antes de ligar a entrega automática.

A consulta automática roda dentro do próprio servidor (padrão: toda sexta às 17h). Se o servidor estiver desligado no horário,
roda quando ligar, no mesmo dia.

## Receita Federal (Integra Contador – Serpro)

Consultas ao e-CAC das empresas pela API oficial do Serpro, sem entrar no e-CAC. Fica na aba **Receita Federal** de cada empresa e no menu **Receita Federal** (uso do mês, últimas consultas e consulta em lote).

Serviços: situação fiscal (relatório PDF), PGDAS-D (declarações, último recibo, gerar DAS, extrato), MEI (DAS e dívida ativa), DCTFWeb (recibo, declaração completa, guia), pagamentos e comprovante de arrecadação, parcelamentos do Simples e do MEI, caixa postal e procuração do escritório. Os PDFs ficam em `dados/receita/<CNPJ>/<AAAA-MM>/`.

Configuração (Configurações → Receita Federal):
1. **Demonstração** – ambiente de testes público do Serpro, respostas fictícias, sem custo. Use para conhecer as telas.
2. **Produção** – exige o contrato do *Integra Contador* na Loja Serpro, feito com o e-CNPJ do escritório. Copie a Consumer Key e a Consumer Secret da Área do cliente. O certificado usado é o do escritório procurador.
3. Cada cliente precisa ter dado **procuração eletrônica no e-CAC** ao escritório com os serviços que serão consultados. A consulta "Procuração para o escritório" confere isso.
4. Ajuste os preços por chamada conforme a faixa do contrato e o **limite mensal**. O sistema soma o custo, avisa o estimado antes de um lote e bloqueia ao atingir o limite.

Repetir a mesma consulta no mesmo dia abre o resultado guardado (sem custo). Para forçar, use "Consultar de novo". Toda consulta fica registrada na auditoria.

## Próximas etapas

4. **Regime Regular** com o grupo IBS/CBS.
