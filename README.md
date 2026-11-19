# AutoClips AI Studio 0.7.3 — catálogo gratuito e OpenRouter Free

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
5. No editor, a avaliação semântica de momentos continua exigindo transcrição e autorização de envio. A instalação de transcrição local permanece opcional. Sem transcrição, o título usa a sugestão local, identificada como tal.

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
