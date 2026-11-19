# QA — AutoClips AI Studio v0.7.4

Data: 19/11/2026 (Brasília). Escopo: transcrição após gravação, legendas personalizadas, prévia da captura e interface.

## Critérios e resultado

| Caso | Evidência |
|---|---|
| Encerrar gravação, salvar MP4 e transcrever sem pedir cortes | API + FFmpeg real com mídia sintética; ASR controlado; ordem de etapas e biblioteca verificados |
| Transcrever gravação existente após análise sem texto | Documento compartilhado entre editor e timeline atualizado; regressão do cache engine:none corrigida |
| ASR real, modelo local offline | Fixture pública jfk.flac do Whisper; 11 s de áudio em MP4, 22 palavras, SRT/VTT e prévia legendada 720×1280 |
| Sincronismo da legenda | Extração FFmpeg com áudio deslocado 2 s preserva silêncio inicial; timestamps de trechos de 180 s rebased e sem duplicações de borda |
| Fonte, tamanho e cor | Valores validados; cinco fontes permitidas; render FFmpeg de duas fontes/cores e escala proporcional 720p/1080p |
| SRT e VTT | Download do vídeo completo e da montagem; palavras cortadas nas bordas preservadas e tempos reposicionados |
| Aprovação da prévia | Alteração de estilo/layout/legenda/transcrição rejeita final antigo; player usa identificador de render para evitar cache antigo |
| Captura e HLS simultâneos | FFmpeg real, uma entrada, TS completo e playlist decodificável; janela de 6 segmentos publicados + 1 retido; sem reencodificação |
| Falha isolada da prévia | Falha forçada no HLS mantém arquivo TS completo; retries do player não encerram a captura |
| Segurança da prévia | Assets específicos, sem travessia/symlink/arquivos temporários; endpoint passa pela autenticação do Studio |
| Espaço para salvar | Monitor reserva tamanho das capturas + 64 MiB; TS duplicado e janela HLS removidos após finalização |
| Corte sem fala | Não solicita legenda queimada nesse intervalo e segue gerando outras prévias |
| Processamento concorrente | Transcrição repetida, exclusão e publicação bloqueadas enquanto o vídeo está em processamento |
| Falha de transcrição | Vídeo preservado, erro visível e opção de tentar novamente |
| Interação das telas | Testes DOM de estilos, persistência, tarefas longas, inclusão de legendas, live, player estável, estados de erro e aprovação |

## Dependências verificadas

- Python 3.12 do ambiente de teste; imagem de produção baseada em Python 3.11-slim.
- faster-whisper 1.2.1; CPU int8; modelo tiny multilíngue previamente baixado; `local_files_only=True` no teste real.
- Foi identificada incompatibilidade de decodificação entre faster-whisper e PyAV 19.0.2. O caminho novo lê o WAV curto em NumPy float32 e entrega o array ao Whisper, mantendo o áudio limitado a um trecho e usando FFmpeg para decodificação.
- HLS.js 1.7.4 obtido do npm com SHA-512 verificado. A configuração do componente foi instanciada com a biblioteca real.
- FFmpeg/ffprobe reais em testes de extração, remux, HLS e render. DejaVu, Liberation e Noto fazem parte da imagem.

## Suítes

```bash
python -m pytest -q
NODE_PATH=/path/to/jsdom/node_modules node --test \
  qa/test_connections_dom.cjs qa/test_editor_captions_dom.cjs \
  qa/test_live_preview_dom.cjs qa/test_timeline_subtitles_dom.cjs
```

A execução completa final passou com **176 testes Python + 1 ignorado**, incluindo as regressões de espaço, corte sem fala e mensagem de falha. A suíte DOM passou com **42 casos** (10 conexões, 6 editor, 13 live, 13 timeline). Verificações de sintaxe JavaScript e `git diff --check` também passaram.

O caso Python ignorado é o CLI `render_manifest` removido; os renders reais são cobertos pelas APIs e pela timeline. Um aviso externo pytest/anyio sobre depreciação não representa falha funcional.

## Limites da validação

1. Os testes DOM executam JavaScript em ambiente simulado. **Não são prints nem testes visuais de navegador.**
2. O navegador conectado à extensão não estava disponível. O navegador compartilhado alcançou a tela de login da produção; as telas internas requerem uma sessão Google autorizada. O checkpoint informa exatamente quais telas foram observadas após publicação.
3. Não houve captura de uma live externa YouTube/Twitch/Kick nesta rodada. O caso do link fornecido pelo usuário continua usando fixtures; as plataformas podem exigir acesso ou bloquear download.
4. A amostra de ASR real era em inglês. Não foi obtida amostra brasileira para medir qualidade em português; nenhuma alegação de precisão foi feita.
5. Não houve upload de conteúdo a redes sociais, gasto com inferência paga nem uso de uma credencial do usuário para os testes.
6. O volume existente tem 500 MB. A proteção de espaço encerra a captura quando necessário; não aumenta capacidade de armazenamento.

## Fontes da validação

- Fixture pública: https://github.com/openai/whisper/blob/main/tests/jfk.flac
- API e licença do transcritor: https://github.com/SYSTRAN/faster-whisper
- Distribuição e licença do player: https://github.com/video-dev/hls.js
- Muxers: https://www.ffmpeg.org/ffmpeg-formats.html
- Captura: https://streamlink.github.io/cli.html

O commit e a implantação são registrados no checkpoint entregue junto ao link de teste.
