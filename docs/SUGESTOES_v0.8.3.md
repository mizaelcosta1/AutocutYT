# AutoClips 0.8.3 — Sugestões do dia e da semana

## Funcionalidades
- Nova aba Sugestões no menu desktop e mobile.
- Hoje: criadores da Twitch em português, por audiência simultânea observada; jogos/categorias agregados na amostra. YouTube: vídeos populares do segmento Games no Brasil, com posição no gráfico e visualizações acumuladas.
- 7 dias: média das medições reais armazenadas pelo AutoClips nos últimos 7 dias. Sempre apresenta quantos dias foram efetivamente medidos; a janela fica marcada como parcial enquanto não houver 7 dias.
- Prévia ampliada da thumbnail pública com campo editável para testar texto. A prévia não salva, exporta ou autoriza reutilização da imagem de terceiros.
- Filtros Todas / Twitch / YouTube e botão Copiar pauta. Links apontam para as fontes.

## Fontes oficiais e variáveis
- Twitch: Helix Get Streams, filtrada por language=pt e type=live. Variáveis no Railway: AUTOCLIPS_TWITCH_CLIENT_ID e AUTOCLIPS_TWITCH_CLIENT_SECRET. Token de aplicativo obtido por client credentials.
- YouTube: Data API v3 videos.list com chart=mostPopular, regionCode=BR e videoCategoryId=20 (Gaming). Usa AUTOCLIPS_YOUTUBE_API_KEY; sem chave tenta token OAuth de leitura do canal conectado no próprio AutoClips.
- Se credenciais ou autorizações estiverem ausentes, o painel mostra Não conectada em vez de inventar ranking.
- Métricas de views YouTube são ACUMULADAS; não são ganhos por dia. O sinal de Twitch é um snapshot, não crescimento histórico garantido.

## Armazenamento, privacidade e limitações
- Cache de 30 minutos e atualização manual limitada a uma por 5 minutos. Histórico público resumido em /data/autoclips/trend_observations.json. São guardados no máximo 15 dias; a visão semanal usa os últimos 7.
- O arquivo de tendências não guarda senhas, chaves ou tokens. Dados públicos podem mudar entre atualizações.
- A amostra Twitch corresponde aos primeiros 100 streams em português devolvidos pela API (não é um censo completo).
- Não há download automático de vídeos de terceiros. Identificar hype não significa possuir os direitos de reutilização.
- Não se trata de leads comerciais verificados nem de previsão garantida de viralização.

## Validação
- Python: python -m pytest -q tests/test_trend_suggestions.py
- JS: node --check static/suggestions.js
- DOM: node --test qa/test_suggestions_dom.cjs (com jsdom@26).
- Na produção: confirmar login, aba móvel, estados sem dados, métricas, links de origem e prévia no navegador autenticado.

## Documentação oficial
- https://dev.twitch.tv/docs/api/reference/#get-streams
- https://dev.twitch.tv/docs/authentication/getting-tokens-oauth/
- https://developers.google.com/youtube/v3/docs/videos/list
- https://developers.google.com/youtube/v3/determine_quota_cost