# AutoClips AI Studio v0.7.0 — Google Login + Timeline

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
