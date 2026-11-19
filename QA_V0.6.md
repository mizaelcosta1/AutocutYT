# QA local — AutoClips AI Studio v0.6.0 (19/11/2026)

## Escopo executado
- Adição de `timeline.py`, `timeline_export.py`, UI `timeline.html/js/css`, endpoints novos em `server.py`; suporte a navegação da interface v0.5.
- Render real com FFmpeg de amostra **sintética** e áudio AAC, concatenando dois recortes, com FFprobe conferindo resolução 720×1280 e duração.
- Regressões da v0.4 adaptadas à API v0.5 (sem chamadas reais para YouTube, TikTok ou serviços pagos).
- Revisão de segurança básica: não incluir projetos privados de gravação no ZIP; persistência isolada por vídeo e autenticação da v0.5 mantida.

## Testes de backend
- 25 aprovados, 1 teste legado ignorado. Teste legado ignorado se referia ao antigo CLI `render_manifest`, removido na v0.5, com testes alternativos via endpoints e FFmpeg.
- Em `test_integrations_legacy.py`, escopo OAuth simulado foi atualizado aos escopos reais do Google; teste de sigilo de prompts foi ajustado para verificar ausência de caminhos privados, evitando falsos positivos por descrição genérica da tarefa.

## Frontend
- `node --check` para os três scripts JS do frontend.
- Capturas estáticas de desktop 1280px e mobile 390px; viewport sem overflow horizontal.
- Smoke de eventos JS com respostas simuladas: carregamento de montagem e comando de inserção enviado com `expected_version`.
- Tentativa de teste via Chromium navegando para o servidor local 127.0.0.1 foi **bloqueada pelo ambiente de execução** (`ERR_BLOCKED_BY_ADMINISTRATOR`), por isso **não se declara E2E visual real em navegador conectado ao backend**.

## Não validado
- OAuth com canal YouTube real, publicação externa, VPS/Railway, balanceamento em produção e custos de edição de horas de vídeo.
- Transcrição com faster-whisper instalado/modelo baixado (dependência opcional).
- Render de vídeo com dezenas de segmentos, legendas com caracteres exóticos, navegador Safari/iPhone e simultaneidade multiusuário.

## Conformidade de escopo
- Não copia código-fonte do OpenChatCut (licença AGPL-3.0).
- Não inclui a gravação privada contida no dossiê original.
- Não é um substituto completo para o OpenChatCut; demais recursos ficam para etapas planejadas.
