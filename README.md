# AutoClips AI Studio 0.8.0 — início do editor visual intuitivo

## Editor visual organizado

Esta versão inicia a evolução do editor conforme `qa/QA_v0.8.0.md`: paleta preto/coral/branco, navegação contextual em Modelos, Composição e Rosto e jogo, e galeria de modelos em modal. A seleção de rosto/jogo continua editando regiões da gravação original; os quadros de saída independentes, camadas e edição de títulos/legendas no canvas entram nas próximas fases.

## Downloads e envios dos cortes

## Downloads e envios dos cortes

O editor de cortes e a montagem oferecem **Baixar prévia 720p** e **Baixar final 1080p** quando esses arquivos estão prontos. O endereço identifica a renderização exata; um arquivo substituído pede atualização do editor, sem baixar outra versão silenciosamente. Os players continuam abrindo o vídeo dentro do estúdio.

Em **Conexões → Downloads e armazenamento**, escolha as ações para os próximos arquivos gerados:

- **Baixar automaticamente neste dispositivo:** precisa da aba aberta. O navegador pode pedir permissão para vários downloads; o link manual permanece disponível. Recarregar a biblioteca não baixa todo o histórico.
- **Enviar automaticamente ao destino conectado:** o servidor mantém uma fila persistente e envia mesmo se você fechar a página. Escolha Google Drive ou GitHub e prévia, final ou ambos. O final continua exigindo a revisão e aprovação da prévia.
- **Enviar manualmente:** cada arquivo pronto tem uma ação para enviá-lo ao destino selecionado. O histórico do projeto mostra fila, progresso, falha, conclusão e link remoto. **Tentar novamente** repete um envio falho; **Cancelar envio** remove a reserva local de um envio que não está em execução, preservando o original e eventuais arquivos remotos.

### Conectar Google Drive

Clique em **Conectar Google Drive** e autorize a mesma conta usada para entrar no estúdio. A autorização de login não concede acesso ao Drive. A conexão solicita `drive.file` com acesso offline e cria/reutiliza a pasta **AutoClips**. Não permite consultar indiscriminadamente todo o Drive nem escolher uma pasta arbitrária por ID.

O servidor reutiliza as variáveis OAuth já existentes e o retorno registrado `/login/google/callback`. O projeto Google Cloud precisa ter a **Google Drive API** habilitada e permitir o escopo na configuração OAuth. Se a aplicação Google estiver no modo externo **Testing**, o token de atualização pode expirar em sete dias; reconecte a conta quando solicitado. Tokens ficam no cofre criptografado, sem aparecer no navegador ou no Git.

### Conectar GitHub

Informe um **repositório privado** no formato `proprietário/repositório` e um token de acesso restrito a ele, com **Contents: leitura e escrita**. O repositório precisa ter ao menos um commit. A conexão valida a conta e o destino, cria/reutiliza uma **Release em rascunho** e guarda os MP4s como anexos; não adiciona vídeos ao histórico Git. Cada arquivo deve ter menos de 2 GiB e uma Release admite até 1.000 anexos.

A autorização do conector GitHub no ChatGPT não é uma credencial do aplicativo implantado. A interface só exibe **Conectado** após validar uma credencial fornecida ao estúdio. Não há envio a repositório público nem publicação automática de Release.

### Persistência e limites

A automação começa desativada. Cada envio prende a versão exata do arquivo por hard link no mesmo volume, calcula SHA-256 e vincula conta, destino e ID de renderização. A fila e os dados de retomada ficam criptografados em `storage_outbox/jobs.enc`; credenciais usam o cofre existente. O worker serial só inicia no ciclo de vida da aplicação. Drive usa upload retomável; GitHub confere nome, tamanho e SHA-256 antes de repetir um envio de resultado incerto. Há até três tentativas automáticas com espera; depois, o histórico oferece nova tentativa manual.

O volume continua finito. Uma exportação reservada ocupa espaço enquanto seu envio não conclui ou é cancelado, mesmo se outro render substituir seu arquivo no editor. O sucesso do envio não apaga o original nem a cópia atual da biblioteca. Fechar o navegador interrompe a possibilidade de baixar para o dispositivo, mas não a fila de upload do servidor. As cotas e permissões das contas externas continuam valendo.

Nenhuma dependência nova de produção foi adicionada; o recurso usa `httpx`, `cryptography`, FastAPI e as bibliotecas padrão já instaladas. Detalhes de validação: [qa/QA_v0.7.7.md](qa/QA_v0.7.7.md).

Referências oficiais: [escopos do Drive](https://developers.google.com/workspace/drive/api/guides/api-specific-auth), [OAuth para aplicações web](https://developers.google.com/identity/protocols/oauth2/web-server), [expiração de tokens](https://developers.google.com/identity/protocols/oauth2), [uploads do Drive](https://developers.google.com/workspace/drive/api/guides/manage-uploads), [assets de Releases](https://docs.github.com/en/rest/releases/assets), [limites de Releases](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases).

## Correções da versão 0.7.6

- Os controles de edição, renderização e detecção compartilham a atividade do vídeo. O primeiro clique reserva a operação antes de salvar as opções; recarregar retoma o acompanhamento pelo servidor.
- A opção **Incluir legendas no vídeo** conserva as escolhas de ativar e desativar, junto do layout. Fonte, tamanho e cor continuam salvos por vídeo.
- Uma transcrição interrompida por reinício permite nova tentativa e preserva as palavras já gravadas.
- A exportação verifica espaço antes de entrar na fila e antes de executar. A estimativa considera duração, qualidade, áudio e as cópias necessárias à montagem. O tamanho real varia com o conteúdo e a compressão.
- Os MP4s são publicados somente depois das verificações. Uma falha remove os arquivos temporários da tentativa e preserva a exportação anterior.
- A decodificação e a codificação de vídeo usam até duas threads; áudio e filtros usam uma. O limite também vale para a validação completa dos arquivos, reduzindo o consumo em servidores pequenos sem mudar resolução, FPS, CRF ou preset de qualidade.
- Os controles do player ficam dentro da área visível. A timeline e a transcrição aparecem logo abaixo do vídeo, ao lado das opções de exportação em telas largas.

Se um pedido perder a resposta, **Atualizar atividade** consulta o servidor sem reenviar a operação. Quando o resultado continua incerto, confira os arquivos antes de liberar novas ações.

Detalhes da validação e limitações: [qa/QA_v0.7.6.md](qa/QA_v0.7.6.md).

## Formatos introduzidos na versão 0.7.5

Os vídeos da biblioteca agora têm um painel **Formato e composição**, compartilhado com a montagem de trechos. Escolha um dos oito modelos, ajuste o rosto e a área do jogo e salve seus próprios modelos para reutilizar.

## Cortes de games e outros formatos

1. Abra um vídeo já salvo na biblioteca. Em **Preparar exportação**, escolha **Game com legenda central**, **Game em duas telas**, tela cheia, fundo desfocado ou rosto na lateral.
2. Escolha **Vertical · 9:16** ou **Horizontal · 16:9**. A prévia usa 720×1280 ou 1280×720; o final usa 1080×1920 ou 1920×1080.
3. Nos layouts com câmera, clique em **Procurar rostos** e selecione um candidato. O detector local examina até oito quadros e sugere áreas. Ele não identifica pessoas nem acompanha continuamente o movimento.
4. Para corrigir o recorte, desenhe um retângulo sobre o quadro, arraste-o ou use a alça para redimensionar. Os campos percentuais permitem ajuste preciso. **Usar quadro do player** mostra o instante atual do vídeo.
5. Selecione **Jogo** para delimitar a área de gameplay. **Mostrar inteiro** preserva a área escolhida com bordas quando necessário; **Preencher área** pode recortar as extremidades. Ajuste a altura do rosto ou o tamanho e o canto da janela.
6. **Game com legenda central** reserva 10% da altura para a faixa entre rosto e jogo. Ative **Incluir legendas no vídeo** depois de transcrever. Em tela cheia e PiP, a legenda pode ficar embaixo, no meio ou em cima. Fonte, tamanho e cor continuam personalizáveis.
7. As alterações são salvas por vídeo. Para reutilizar em outro projeto, dê um nome e clique em **Salvar modelo**. Ao aplicar um modelo personalizado em uma fonte diferente, revise as áreas do rosto e do jogo.
8. Gere a prévia, assista e aprove antes de exportar. A aprovação é vinculada ao ID da prévia, ao layout, aos recortes e às legendas usados na renderização. Uma nova prévia substitui o final anterior.

O recurso foi integrado à aplicação existente, com `composition.py`, `face_detection.py` e um componente de interface compartilhado. As decisões estão em [docs/LAYOUTS_v0.7.5.md](docs/LAYOUTS_v0.7.5.md); resultados e limites em [qa/QA_v0.7.5.md](qa/QA_v0.7.5.md).

### Dependências da localização de rosto

O Docker instala `requirements-vision.txt` (**opencv-python-headless 4.12.0.88**) junto das dependências existentes. `scripts/prepare_face_model.py` prepara o YuNet `2023mar`, verifica SHA-256 e tamanho do modelo e da licença MIT e armazena ambos fora do volume de vídeos, em `/opt/autoclips-models`. Não há download do detector durante uma edição. `AUTOCLIPS_FACE_MODEL` permite configurar outro caminho para esse mesmo modelo.

Em uma instalação local: instale `requirements.txt`, `requirements-transcription.txt`, `requirements-vision.txt`, FFmpeg/ffprobe e execute `python scripts/prepare_face_model.py --output /caminho/face_detection_yunet_2023mar.onnx`. Configure `AUTOCLIPS_FACE_MODEL` para esse arquivo. Se o detector não estiver disponível, a seleção manual continua utilizável.

## Histórico v0.7.4 — transcrição, legendas e prévia da live

A gravação agora pode ser acompanhada por uma prévia da própria captura. Ao encerrar, o vídeo é salvo, transcrito localmente e preparado para cortes e legendas. O editor e a montagem compartilham a transcrição e as preferências de fonte, tamanho e cor.

## Como usar esta versão

1. Em **Novo projeto**, cole a URL em **Gravar uma live** ou use **Importar um link**, que identifica se o conteúdo está ao vivo ou gravado. Confirme o direito de uso.
2. **Gerar transcrição e legendas** vem marcado. **Sugerir cortes** é independente: é possível transcrever sem pedir cortes automáticos.
3. Durante a captura, acompanhe **Gravações em andamento**. A prévia mostra o mesmo sinal recebido pelo gravador, com atraso de transmissão. Use os controles do vídeo para ativar o áudio.
4. Clique em **Encerrar gravação**. A tela informa salvamento, transcrição, busca de momentos e preparação das prévias. O vídeo salvo continua acessível se a transcrição falhar; **Gerar legendas** permite tentar novamente.
5. No editor, escolha fonte, tamanho (18–96, referência de 1080p) e cor. O contorno escuro é fixo. Use **Baixar SRT** ou **Baixar VTT**, ou marque **Incluir legendas no vídeo** para gravá-las no MP4.
6. Gere e assista à prévia 720p, aprove-a e exporte o final 1080p. Alterar o estilo, enquadramento ou transcrição exige nova prévia. SRT/VTT são arquivos de texto e tempo; a personalização visual é aplicada ao MP4.

A interface foi reorganizada em ciano, vermelho e azul escuro, com instruções curtas, gravações em uma seção própria e opções técnicas em blocos expansíveis. As decisões estão em [docs/UX_v0.7.4.md](docs/UX_v0.7.4.md).

## Dependências e implantação v0.7.4

O Docker instala FFmpeg, Streamlink, yt-dlp, **faster-whisper 1.2.1** e as cinco famílias de fontes apresentadas no editor (DejaVu, Liberation e Noto). O build baixa o modelo multilíngue **tiny** para `/opt/autoclips-models`, fora do volume de vídeos, e o runtime usa CPU int8 com `AUTOCLIPS_WHISPER_LOCAL_ONLY=1`. A transcrição trabalha com áudio mono a 16 kHz em trechos de 180 s, com contexto nas bordas; apenas um modelo e uma inferência ficam ativos por processo. O áudio não é enviado a um serviço de transcrição externo.

**HLS.js 1.7.4** é servido pelo próprio aplicativo. O build valida a integridade SHA-512 da distribuição npm e guarda sua licença MIT; não depende de CDN no navegador. A captura abre uma única conexão Streamlink e usa FFmpeg para gravar TS e manter uma janela HLS curta, sem reencodificar o sinal. O player preserva a instância entre atualizações, tenta recuperar falhas limitadas e permite repetir somente a prévia.

Para instalação local, além de `requirements.txt`, instale `requirements-transcription.txt`, FFmpeg/ffprobe, yt-dlp, Streamlink e as fontes. Execute `python scripts/prepare_runtime_assets.py --hls-only` para preparar o player. Fora do Docker, o modelo é baixado no primeiro uso, salvo em `AUTOCLIPS_WHISPER_CACHE` (padrão: diretório temporário) e configurável por `AUTOCLIPS_WHISPER_MODEL`.

O armazenamento continua finito. Antes de iniciar e durante a gravação, o servidor verifica espaço; reserva o tamanho das capturas e uma margem de 64 MiB para salvar em MP4. Depois do salvamento, remove TS duplicado e segmentos de prévia. A duração disponível depende da taxa do vídeo e do espaço ocupado por outros projetos.

### Limites e evidência

- Prévia HLS tem atraso de alguns segundos, variável com rede e keyframes; não é uma promessa de latência zero. O navegador precisa suportar o codec da fonte. Uma falha na prévia não encerra a gravação.
- O editor exige faixa de vídeo. Uma fonte apenas de áudio é identificada e sinalizada; não é apresentada como projeto de vídeo pronto.
- Reconhecimento automático pode errar nomes e palavras. A amostra visual do navegador pode usar uma fonte substituta; a prévia MP4 usa a fonte instalada no servidor.
- A geração local e exportação foram verificadas com fala pública real em inglês; a qualidade em português e lives externas não foi medida nessa amostra. Casos YouTube/Twitch/Kick com fontes controladas não equivalem a capturas externas concluídas.
- A continuidade de uma captura não é garantida durante reinício ou nova implantação do servidor. Tarefas interrompidas são identificadas e gravações já salvas permanecem no volume.

Ver [qa/QA_v0.7.4.md](qa/QA_v0.7.4.md) para testes, evidências e limitações. Referências primárias: [faster-whisper](https://github.com/SYSTRAN/faster-whisper), [HLS.js](https://github.com/video-dev/hls.js), [FFmpeg HLS e tee](https://www.ffmpeg.org/ffmpeg-formats.html), [Streamlink CLI](https://streamlink.github.io/cli.html).

## Histórico v0.7.3 — catálogo gratuito e OpenRouter Free


Em **Conexões e modelos**, o catálogo agora distingue modelos gratuitos, pagos e de preço não informado. O filtro e a busca são aplicados antes da paginação: o limite antigo de 250 opções não esconde mais os modelos gratuitos. A lista completa vem da API do provedor, sem manter uma lista fixa de IDs antigos.

## IA gratuita em Conexões

- **OpenRouter:** a chave é validada em `/api/v1/key`; o catálogo público `/api/v1/models` é consultado separadamente, incluindo as variantes `:free` e o roteador oficial `openrouter/free`.
- **Usar automático gratuito:** configura momentos e títulos com `openrouter/free`. O OpenRouter escolhe um modelo gratuito disponível por pedido. As cotas da conta continuam valendo; trocar modelo ou chave não cria capacidade ilimitada.
- **Proteção de custo:** chamadas gratuitas do OpenRouter incluem `provider.max_price` zero para entrada, saída, requisição e imagem. Não há fallback pago nem plugins pagos. Preço ausente, incompleto ou inválido não é tratado como zero.
- **Testar IA gratuita:** faz somente um pedido curto ao roteador, mostra o modelo que respondeu e informa erros de chave, saldo, disponibilidade ou cota. Consome uma chamada da cota gratuita e não envia transcrições nem arquivos do usuário.
- **Limites:** erro 429 pausa a credencial inteira durante `Retry-After`; alternar o modelo não contorna a pausa. Erros não disparam novas tentativas automáticas. A cota diária aparece quando o provedor a informa, com referência à última atualização.
- **Catálogo Free LLM APIs:** presets para OpenRouter, Groq, Google Gemini, NVIDIA NIM e Mistral AI, derivados do diretório [awesome-freellm-apis](https://github.com/open-free-llm-api/awesome-freellm-apis), com URLs oficiais, links para criar a própria chave e notas sobre planos/cotas. O Gemini usa a base OpenAI compatível `/v1beta/openai`.

O repositório de referência é um catálogo, não um serviço universal de inferência. Não fornece chaves prontas, franquias ilimitadas ou rotação automática entre contas. Os outros provedores exigem a chave e o plano da própria conta; modelos sem preço informado ficam identificados como tal. Esta versão usa o roteamento gratuito nativo do OpenRouter e a escolha manual de provedor por tarefa.

### Atualização de conexões existentes

A migração v0.7.3 reaproveita as credenciais do cofre criptografado. Se o catálogo salvo já contém `openrouter/free` confirmado como gratuito, configura momentos e títulos para o roteador, preservando tarefas vinculadas a outros provedores e guardando as escolhas anteriores. A migração executa uma vez e não realiza inferência. Seleções gratuitas antigas mantêm a proteção caso o catálogo mude de preço. Conexões oficiais do OpenRouter registradas como personalizadas são normalizadas.

Se uma conexão antiga ainda não tiver o roteador no catálogo salvo, a atualização consulta uma vez o catálogo público atual, sem enviar a chave e sem fazer inferência, e prepara o roteador quando ele estiver disponível. Se essa consulta estiver indisponível, use **Atualizar catálogo** e **Usar automático gratuito**. Conectar uma chave nova tem essa configuração marcada por padrão; pode ser desmarcada. Os modelos pagos continuam consultáveis com rótulo explícito, mediante seleção manual.

### Como verificar

1. Abra **Conexões e modelos**. Reutilize a chave salva ou conecte sua chave OpenRouter.
2. Confira **Somente gratuitos** e **Automático · somente gratuitos (openrouter/free)**.
3. Clique em **Usar automático gratuito** e confira as tarefas momentos e títulos.
4. Clique em **Testar IA gratuita**. Uma resposta real depende da validade da chave, das cotas e da disponibilidade do OpenRouter.
5. No editor, a avaliação semântica de momentos continua exigindo transcrição e autorização de envio. Na v0.7.3, a instalação de transcrição local era opcional; a imagem v0.7.4 já inclui o recurso. Sem transcrição, o título usa a sugestão local, identificada como tal.

Nenhuma dependência de produção foi acrescentada. A integração usa `httpx` e o cofre já existentes; classificação de preços usa `decimal` da biblioteca padrão. A etapa de revisão foi retirada do seletor porque ainda não executa uma ação no editor.

Fontes: [modelos e preços](https://openrouter.ai/docs/api/api-reference/models/list-all-models-and-their-properties), [roteador gratuito](https://openrouter.ai/docs/guides/routing/routers/free-router), [limites](https://openrouter.ai/docs/api_reference/limits), [seleção de provedores e teto de preço](https://openrouter.ai/docs/guides/routing/provider-selection), [Gemini compatível com OpenAI](https://ai.google.dev/gemini-api/docs/openai). Validação e limites do teste: `qa/QA_FREE_MODELS_v0.7.3.md`.

## Histórico v0.7.2 — importa link de live ou vídeo automaticamente

Uma única caixa aceita links de YouTube, Twitch e Kick e consulta o estado da transmissão com `yt-dlp` antes de escolher gravação ao vivo ou VOD. Para VOD, a análise gera automaticamente até três prévias verticais 720p revisáveis; para uma live ativa, o usuário encerra a gravação antes de analisar. Não existe postagem automática. **A classificação com um link real depende da disponibilidade do provedor e de capacidade de armazenamento**; see `qa/QA_YOUTUBE_LIVE_CASE_2026-11-19.md` para o teste com um vídeo simulado. Transcrição semântica é opcional e a heurística de picos de áudio não garante bons highlights.

# AutoClips AI Studio v0.7.1 — Google Login + Timeline + Use-case QA

**Base:** código v0.5.0 fornecido pelo usuário, preservando upload, VOD, lives, clipes automáticos, OAuth de canal e publicação aprovada no YouTube; agora autenticação de acesso ao Studio via Google OIDC (separada da autorização YouTube).

**Novidade:** editor visual de cortes sequenciais inspirado *conceitualmente* no fluxo de trabalho do [OpenChatCut](https://github.com/0xsline/OpenChatCut). **Não há código ou assets copiados do OpenChatCut**. OpenChatCut usa AGPL-3.0; seu recurso não foi incorporado nem recebe alegações de paridade completa.

## Primeiras funções implementadas

- Linha do tempo com cartões de vídeo e áudio original associado, prévia sequencial no player.
- Corte dividido no playhead, alteração de início/fim, inclusão de intervalos do VOD, exclusão, reordenação, restaurar e desfazer/refazer (40 passos de histórico).
- Armazenamento não destrutivo em `AUTOCLIPS_DATA_ROOT/projects/{video-id}/timeline.json`.
- Busca de palavras na transcrição existente e adição de trechos a partir das falas.
- Transcrição local opcional com faster-whisper; cortes que comprimem pausas de fala e SRT com tempos recalculados após os cortes.
- Exportação **real** de vários trechos para MP4 720×1280 (prévia) e 1080×1920 (final), com opção de legendas incorporadas, 3 tipos de enquadramento, áudio original e checagem FFmpeg da decodificação.
- Exportação final depende de confirmação humana de uma prévia da **mesma versão**, com enquadramento/legendas idênticos. Alterações invalidam a aprovação da montagem anterior.
- Interface responsiva no navegador, incluindo acesso pelo editor original em **Abrir timeline profissional**.

### Limites

**É um editor sequencial, não o OpenChatCut completo.** As faixas V1/A1/C1 são uma representação informativa: o áudio original acompanha os trechos; a versão ainda **não** suporta áudio independente, transições multicamadas, filtros WebGL, chroma key, LUTs, keyframes, MCP externo, agente LLM embutido, editor motion graphics, FCPXML, tradução de legendas ou publicação multirrede.

Por segurança e previsibilidade de processamento: máximo de **80 segmentos** e **600 segundos** no projeto editado. Vídeos com mais de 60 segundos começam com 0–60 segundos; insira novos momentos do restante da gravação usando **Adicionar trecho**.

## Instalação local

Necessários Python 3.11+, FFmpeg/ffprobe, dependências em `requirements.txt`.

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
export AUTOCLIPS_DATA_ROOT="$PWD/local-data"
uvicorn server:app --host 127.0.0.1 --port 8765
```

Abra `http://127.0.0.1:8765`, envie um vídeo autorizado, entre no editor e clique em **Abrir timeline profissional**.

Para transcrição local, `pip install -r requirements-optional.txt`. O modelo Whisper `small` é baixado na primeira transcrição (demanda rede/disk/CPU), mas nenhum serviço cloud de IA recebe a transcrição sem uma integração adicional.

Para implantar na nuvem, use `Dockerfile` e **configure antes** os segredos de `.env.example` na hospedagem; são indispensáveis credenciais OAuth Google (uma vez no servidor), `AUTOCLIPS_SESSION_SECRET`, `AUTOCLIPS_ALLOWED_EMAILS`, HTTPS, volume persistente em `/data/autoclips` e limites de upload/CPU ajustados à capacidade real da VPS. **O Dockerfile padrão não instala faster-whisper.**

## Rotas acrescentadas

| Método | Rota | Descrição |
|---|---|---|
| GET | `/studio/{video_id}` | Editor visual |
| GET | `/api/videos/{id}/timeline` | Consulta/cria montagem |
| POST | `/api/videos/{id}/timeline` | Comando de edição com `expected_version` |
| GET | `/api/videos/{id}/transcript` | Palavras da transcrição local/analítica |
| POST | `/api/videos/{id}/transcript` | Tarefa de transcrição local, opcional |
| GET | `/api/videos/{id}/timeline.srt` | Legendas sincronizadas |
| POST | `/api/videos/{id}/timeline/render/{quality}` | Previa/final com autorizações |
| GET | `/api/videos/{id}/timeline/exports` | Renderizações desta versão |
| GET | `/api/videos/{id}/timeline/rendered/{quality}` | MP4 editado |

Comandos: `split`, `trim`, `add`, `remove`, `move`, `undo`, `redo`, `restore`, `remove_pauses`; sempre enviar `expected_version` para evitar edição silenciosa concorrente. `GET /api/config` informa se `faster-whisper` está disponível.

## Verificação

```bash
python -m py_compile server.py timeline.py timeline_export.py autoclips.py integrations.py
node --check static/app.js
node --check static/timeline.js
node --check static/connections.js
PYTHONPATH=. pytest -q tests/
```

Detalhes e limitações em `QA_V0.6.md`. Versão de prova local; **não foi implantada em produção**. A integração OAuth do YouTube herdada da v0.5 não foi testada contra um canal real nesta etapa.

## Referência e atribuição

Fluxos de trabalho usados como inspiração: OpenChatCut, `https://github.com/0xsline/OpenChatCut`, repositório AGPL-3.0; README revisado em 19/11/2026. Este projeto não inclui seu código-fonte, dependências Remotion, React, licenças ou recursos originais.

## Login Google do próprio Studio (v0.7.0)

O usuário final acessa `/login` e clica **Entrar com Google**. O login usa Google OpenID Connect (escopos `openid email profile`), PKCE, state, nonce e cookies assinados e HttpOnly/Secure. **Não utiliza as permissões do YouTube para entrar no aplicativo.** Depois do login, o usuário conecta seu canal separadamente pela aba Conexões.

**Configuração única pelo administrador do aplicativo, no Railway Variables** (nunca no GitHub ou na página de login):

- `AUTOCLIPS_GOOGLE_CLIENT_ID` e `AUTOCLIPS_GOOGLE_CLIENT_SECRET`: aplicativo OAuth web criado no Google Cloud Console;
- `AUTOCLIPS_PUBLIC_URL`: endereço HTTPS público exato do Railway, sem barra final;
- `AUTOCLIPS_SESSION_SECRET`: string aleatória exclusiva com 32 caracteres ou mais;
- `AUTOCLIPS_ALLOWED_EMAILS`: endereços Google autorizados, separados por vírgula (obrigatório);
- `AUTOCLIPS_YOUTUBE_CLIENT_ID` e `AUTOCLIPS_YOUTUBE_CLIENT_SECRET`: podem ser os mesmos valores do aplicativo Google OAuth, configurados separadamente para autorização do canal (dependem dos escopos habilitados no Google Cloud).

Cadastre **ambos** os URIs autorizados de redirecionamento para o mesmo cliente OAuth do tipo Aplicativo da Web:

1. `https://SEU-DOMINIO/login/google/callback` — login no AutoClips;
2. `https://SEU-DOMINIO/auth/youtube/callback` — autorização do canal.

Sem configuração completa, a página externa mostra uma mensagem de configuração pendente e não libera acesso ao Studio. O Google pode exigir cadastro de usuários de teste, revisão da tela de consentimento e verificação para escopos do YouTube.

**Privacidade:** o repositório público não pode conter credenciais, senha ou tokens. Acesso autorizado apenas para emails da allowlist. O token YouTube continua sendo mantido no cofre criptografado sobre volume persistente.

**Validação:** testes locais com OAuth simulado, sem conexão a conta Google real. Testar OAuth e upload YouTube com uma conta real antes de declarar produção totalmente funcional.

## Auditoria de ações, limites e Comfy Cloud (v0.7.1)

- A edição e a geração de título **sem um modelo de IA configurado** são explicitamente rotuladas como sugestão heurística; não há geração paga implícita.
- Importar VOD aceita somente links HTTPS normais de Twitch, YouTube e Kick. Upload grande demais recebe erro antes de iniciar a transferência, preservando espaço para renderização no volume.
- As renderizações de prévia e final não podem usar o intervalo antigo quando um ajuste foi rejeitado. A exportação final requer aprovação explícita da prévia na versão atual.
- Envio ao YouTube mantém consentimento expresso, impede publicação duplicada e protege o clipe de edição/exclusão enquanto o envio está pendente. Reenvio resumível respeita bytes efetivamente confirmados pelo Google.
- Ao mudar o intervalo, o serviço remove arquivos de renderização **gerenciados** que perderam validade. Não apaga mídia de caminhos externos.
- Em **Conexões e modelos**, um catálogo de workflows Comfy Cloud pode ser configurado com `AUTOCLIPS_COMFY_WORKFLOWS_JSON` no Railway, por exemplo, `[ {"title":"Frame vertical 9:16","workflow_id":"UUID-DO-WORKFLOW"} ]`. O botão **Abrir workflow** leva ao canvas oficial da sua conta Comfy Cloud. **Não** executa geração por dentro do AutoClips; OAuth do MCP do ChatGPT não autoriza o backend hospedado e créditos podem estar indisponíveis.
- Transcrição local exige `faster-whisper` (instalação opcional, não fornecida pelo Dockerfile de produção) e o modelo Whisper; os respectivos controles sinalizam indisponibilidade no servidor sem ele.
- O volume Railway utilizado é de 500 MB, inadequado para produção prolongada com lives extensas, upload de vários vídeos grandes ou múltiplas exportações; utilize armazenamento de objetos/VPS ou amplie o volume e teste os limites operacionais antes de abrir o serviço a outros usuários.
- Nenhuma parte do OpenChatCut AGPL-3.0 foi copiada neste código. A interface oferece somente funções explicitamente listadas; ainda não fornece faixas de áudio independentes, motion graphics avançado, agente MCP remoto ou publicação automática multirrede.

**Matriz de botões, testes e ressalvas:** consulte [`qa/QA_REPORT_2026-11-19.md`](qa/QA_REPORT_2026-11-19.md).

Testar localmente a release: `python -m pytest -q tests` (suite backend) e `python qa/smoke_browser.py` (Chromium + FastAPI TestClient + FFmpeg; não requer login ou serviços pagos). `pytest` e `playwright` são ferramentas de teste, não dependências do container de produção; instale-os num ambiente de QA quando for executar o smoke. O teste Chromium contorna a restrição de loopback da sandbox redirecionando Fetch para TestClient e não prova navegação real ao Railway.
