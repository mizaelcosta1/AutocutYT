# AutoClips AI Studio v0.6.0

Aplicativo de edição de vídeos com timeline não destrutiva, exportação por FFmpeg e integração preparada para autenticação OAuth.

## Estrutura de publicação

A release completa está armazenada em `release/AutoClips_AI_Studio_v0.6.0.tar.gz` (distribuição compactada do código-fonte). O `Dockerfile` da raiz extrai o pacote durante o build e executa a aplicação FastAPI com FFmpeg.

## Deploy no Railway

- Builder: Dockerfile
- Porta: `8765` (a aplicação também respeita `PORT`)
- Healthcheck: `/health`
- Armazenamento: configure um volume persistente em `/data/autoclips` antes de usar vídeos importantes.
- Segurança: defina `AUTOCLIPS_PASSWORD` como variável de ambiente no Railway. Não coloque credenciais no GitHub.

## Configuração OAuth

Para conectar um canal do YouTube, configure `AUTOCLIPS_YOUTUBE_CLIENT_ID`, `AUTOCLIPS_YOUTUBE_CLIENT_SECRET` e `AUTOCLIPS_PUBLIC_URL` no Railway e cadastre o callback `/auth/youtube/callback` no Google Cloud Console.

## Limites conhecidos da v0.6.0

Consulte `OPENCHATCUT_ROADMAP.md` e `QA_V0.6.md` dentro do pacote de release. A suíte local da release registrou 25 testes aprovados e 1 teste legado ignorado; OAuth real, publicação real no YouTube e persistência em produção ainda precisam de validação com credenciais e armazenamento configurados.

## Licenciamento

O repositório contém a implementação do AutoClips e não incorpora código-fonte do OpenChatCut. Verifique as licenças das dependências antes de redistribuir.