# AutoClips 0.9.0

## Cadastro público

Em um serviço de teste HTTPS com volume próprio, configure `AUTOCLIPS_PUBLIC_URL`, uma `AUTOCLIPS_SESSION_SECRET` aleatória com ao menos 32 caracteres e `AUTOCLIPS_PUBLIC_SIGNUP=1`. Configure o servidor de saída em `AUTOCLIPS_SMTP_HOST`, `_PORT`, `_FROM`, `_USER` e `_PASSWORD` (prefixo completo `AUTOCLIPS_SMTP_`). Use `AUTOCLIPS_SMTP_TLS=starttls` na porta 587 ou `implicit` na porta 465. O endereço de origem precisa estar autorizado no provedor de e-mail. Guarde as credenciais nas variáveis do serviço.

O cadastro envia um código de seis dígitos válido por dez minutos, vinculado à tentativa no navegador de origem. O reenvio substitui o código anterior. A confirmação abre a sessão. O próximo acesso aceita e-mail e senha. O Google continua disponível quando configurado. O recurso público desligado conserva o modo anterior. Use um único worker Uvicorn, como no Dockerfile do projeto.

Cada nova conta tem catálogo, uploads, renderizações, biblioteca, cofre de conexões e tarefas próprios. Para manter acesso ao catálogo da instalação anterior, defina `AUTOCLIPS_LEGACY_OWNER_EMAIL` com o e-mail do proprietário. Se havia exatamente um endereço permitido, ele é usado como padrão. Com vários endereços, o catálogo anterior aguarda a identificação explícita de seu proprietário. Faça backup do volume, do cofre e de sua chave antes de migrar a instalação.

## Espaço de edição

Para ampliar a capacidade, monte um volume persistente com o tamanho escolhido. No volume montado crie o arquivo vazio `.autoclips-volume` e configure `AUTOCLIPS_EXPANDED_VOLUME` com o caminho absoluto. O marcador identifica o destino disponível e evita criar estúdios de usuários em um diretório vazio durante uma falha de montagem. O painel calcula o espaço a partir do sistema de arquivos.

Novos estúdios gravam todos os seus arquivos nesse volume. Cada local é registrado no volume principal. Alterar a configuração preserva a localização de contas existentes. Para a biblioteca legada, montar o volume como filho direto da raiz de dados, por exemplo `/data/autoclips/expanded`, também direciona novos uploads, importações e gravações para esse disco. Projetos existentes continuam acessíveis nos caminhos originais. Metadados e montagens da timeline do proprietário legado continuam no volume original. Uma alternativa em infraestrutura com volume único é ampliar o volume já montado no mesmo caminho.

O espaço do Google Drive para finais arquivados continua separado. A instalação desta branch por si só não contrata capacidade no provedor.

## Validação

Execute `python -m pytest -q` com as dependências da aplicação e pytest. Para o teste ponta a ponta instale também Playwright, aiosmtpd, Chromium do Playwright, OpenSSL e FFmpeg e execute `python qa/public_studio_browser.py`. O teste inicia um SMTP local com STARTTLS, confirma o código, entra novamente, envia um MP4 e abre o editor nos três formatos. A entrega com um domínio real precisa ser validada no ambiente de homologação.
