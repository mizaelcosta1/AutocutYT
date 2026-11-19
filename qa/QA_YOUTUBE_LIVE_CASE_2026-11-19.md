# AutoClips AI Studio 0.7.2 — teste de link YouTube `/live/`

## Escopo exato

- URL recebida: `https://www.youtube.com/live/hOOiQagTyTA?is=-WdBBCQjg7sX0w54`
- Requisito: reconhecer automaticamente se o conteúdo está ao vivo ou é replay arquivado, produzir propostas de cortes e prévias revisáveis, mantendo o original intacto e nunca publicando sem aprovação.
- Ambiente de execução: cópia isolada da release AutoClips 0.7.1; Python 3.13; FastAPI TestClient; FFmpeg/ffprobe; Chromium/Playwright (dispositivo simulado de 390 × 844).
- A fonte REAL **não foi baixada**. O container não conseguiu resolver `www.youtube.com`; a tentativa de inspeção via Browser Use falhou por falta de créditos. Não conhecemos o título, a duração ou se essa transmissão está atualmente ativa.

## Diferença entre teste e fato

Os testes abaixo usam literalmente o mesmo endereço da entrada fornecida, mas um arquivo MP4 sintético gerado por FFmpeg substitui a resposta do download externo. Um mock de metadados substitui a consulta yt-dlp. Isto verifica a arquitetura, as rotas, a renderização e a interface **sem provar a disponibilidade da mídia real**.

## Casos verificados

| ID | Caso de uso | Evidência | Resultado |
| --- | --- | --- | --- |
| YT-01 | O link `/live/hOOiQagTyTA` é aceito como domínio YouTube | `platform(URL)` | PASS |
| YT-02 | A mesma URL com `live_status=was_live` segue a rota VOD | Classificador com metadados simulados | PASS |
| YT-03 | A mesma URL com `live_status=is_live` segue para Streamlink | Classificador e requisição FastAPI, processo Streamlink simulado | PASS |
| YT-04 | URL externa ou sem autorização é rejeitada antes de buscar o vídeo | Respostas HTTP 400 | PASS |
| YT-05 | VOD simulado gera propostas e prévia MP4 automaticamente | FFmpeg renderiza e decodifica vídeo vertical **720×1280** | PASS |
| YT-06 | O editor mobile abre depois do único botão de importação | Chromium Playwright + FastAPI/TestClient, 12 requisições interceptadas | PASS |
| YT-07 | Iniciar uma segunda captura da mesma live ativa é bloqueado | HTTP 409 e apenas um processo simulado | PASS |
| YT-08 | O fluxo independente de upload, edição e render final ainda funciona | Browser smoke original; 31 requisições | PASS |
| YT-09 | Todos os testes Python da release | `python -m pytest -q tests` | **51 passed, 1 skipped** |
| YT-10 | Download real da URL YouTube pelo yt-dlp | Rede indisponível no container; browser sem créditos | **NÃO EXECUTADO** |
| YT-11 | Gravação real Streamlink, transcrição e seleção semântica nesse vídeo | Mídia não acessível, sem arquivo real/transcrição | **NÃO EXECUTADO** |
| YT-12 | Twitch/Kick em provedores reais | Apenas host permitido em código, sem acesso às streams | **NÃO EXECUTADO** |

## Implementação aplicada na cópia 0.7.2

- `source_detect.py`: detecta `is_live`, `was_live`, `not_live`, `is_upcoming`, `post_live` usando metadados yt-dlp, sem baixar a mídia.
- `POST /api/source/auto`: exige confirmação de direito de editar e valida a URL, decide entre VOD e gravação e não publica nada.
- Após importar um replay VOD: analisa momentos e gera automaticamente até **três prévias 720p**, com FFmpeg e decodificação integral; as demais propostas continuam para revisão no editor.
- Live em andamento: inicia pedido de gravação Streamlink; a análise e as prévias ocorrem **depois de encerrar a gravação**. O status `starting` não é confundido com captura comprovada.
- Dupla captura da mesma URL durante uma gravação ativa retorna **HTTP 409**.
- Interface mobile: o botão de importação aceita um link de live OU replay e informa a diferença. Exportação final e publicação no YouTube continuam exigindo aprovações separadas.

## Limites de produto e infraestrutura

1. O servidor atual no Railway executa **o commit 0.7.0** (`c2e5c63`), não esta versão 0.7.2. A conexão Railway→GitHub já apresentou erro de permissão `User does not have access to the repo`; só após corrigi-la é possível validar este fluxo em produção.
2. O volume Railway configurado anteriormente é de **500 MB**, insuficiente para garantir importação integral de lives longas ou muitos cortes. Precisa de armazenamento maior/objeto ou captura por janelas para produção.
3. O Dockerfile instala `yt-dlp`, `streamlink`, `ffmpeg` e `ffprobe` (confirmado nos logs de build anteriores). O ambiente local de QA não tem `yt-dlp`/`streamlink` instalados; as chamadas reais ficaram explicitamente isoladas/mocadas.
4. A geração automática de momentos sem transcrição é **heurística baseada em áudio**, não entendimento semântico. O extra `faster-whisper` não é instalado pelo `requirements.txt` principal, portanto legendas verbatim e transcrição word-level não podem ser presumidas em produção.
5. O Comfy Cloud é independente dos cortes FFmpeg e não foi acionado; no teste anterior o serviço respondeu que as gerações gratuitas acabaram.
6. Nenhum conteúdo foi enviado ao YouTube, e nenhuma conta ou gasto foi utilizado no teste.

## Como reproduzir

```bash
python -m pytest -q tests/test_auto_source_live_vod.py
python -m pytest -q tests
python qa/smoke_auto_youtube_link.py
python qa/smoke_browser.py
```

**Resultado esperado após publicar 0.7.2:** abrir AutoClips, colar o link no painel 'Importar link de vídeo ou live', confirmar autorização e escolher 'Gerar sugestões e prévias'. Se for replay, baixar, analisar e gerar até três prévias 720p. Se estiver ao vivo, iniciar captura e exibir o controle 'Encerrar gravação', após o qual serão produzidas as prévias. O conteúdo real ainda precisa ser testado com permissão e conectividade na versão publicada.
