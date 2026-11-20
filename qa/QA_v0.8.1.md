# AutoClips v0.8.1 — checkpoint de implementação e QA

Data: 20/11/2026 (America/Sao_Paulo).

## Base e escopo

- Repositório: `mizaelcosta1/AutocutYT`, branch `main`.
- Base verificada no GitHub e no Railway: `0128c67e80f590009a26c18dff2e3998033906fa`, v0.8.0.
- O login OIDC e a autorização YouTube já eram reais; esta entrega conclui recuperação de acesso, retorno ao projeto, prévia de composição e clareza do fluxo de conexão.

## Login Google

- Botão reconhecível abre o Google com escopos de identidade, sem solicitar chaves ao usuário.
- Preserva `/studio/<id>` após entrar, por destino interno validado e cookie OAuth assinado.
- Rejeita redirecionamentos externos e caminhos de ação.
- Cancelamento, expiração e erros recebem HTML recuperável no navegador; APIs mantêm JSON e status correto.
- PKCE, state, nonce, conta autorizada e cookie Secure/HttpOnly permanecem ativos. Nonce Unicode é rejeitado sem erro500.

## Editor

- Quadro real usado para a prévia visual de vertical/horizontal, split, faixa central, PiP e modos de tela cheia.
- Controles atualizam recortes, proporções, posição do PiP e ajuste contain/cover.
- Legenda de amostra acompanha fonte, tamanho, cor e posição. A interface informa que tipografia/quebras podem variar; prévia720p continua sendo a validação do vídeo com áudio.
- Botão para usar o quadro atual do player, incluindo recuperação no fim do vídeo.
- Modelos prontos preservam recortes; modelos pessoais restauram seus dados.
- Arraste bloqueado durante processamento; erros antigos de coordenadas não impedem aplicar um modelo válido.

## YouTube

- Consulta real do canal apresentada em feedback acessível na página, sem alertas modais.
- Início/callback ligados ao usuário do Studio e navegador que iniciaram a autorização.
- Callback inválido não consome uma tentativa legítima de outro navegador.
- Cancelamento limpa a tentativa e preserva a conexão anterior.
- Erros não refletem exceções ou tokens do provedor.
- URI, credenciais de servidor e escopos existentes preservados. Upload continua resumível, privado por padrão e mediante aprovação.

## Testes executados

```text
Python: 397 passed, 3 skipped in 40.02s
DOM:    101 tests, 101 pass, 0 fail (6.90s)
git diff --check: aprovado
```

Os testes backend incluem renderizações reais com FFmpeg, legendas, composição, timeline, integridade de downloads e proteções de autorização. Os testes de provedor OAuth/YouTube usam transporte simulado. DOM em jsdom testa eventos e geometria do canvas; não substitui revisão visual no navegador.

Comandos:

```bash
python -m pytest -q tests
node --test qa/test_*dom.cjs
```

Ferramentas de QA: pytest e jsdom. Dependências de produção existentes: FastAPI, Pydantic, httpx, PyJWT, itsdangerous, cryptography, FFmpeg, OpenCV/YuNet e faster-whisper. Nenhuma nova dependência de produção foi adicionada.

## Evidência de conta real e limite do checkpoint

O botão da versão publicada abriu `accounts.google.com` com o cliente do AutoClips, reconheceu a conta autorizada e solicitou confirmação de identidade no dispositivo do usuário. A confirmação ainda estava pendente quando este checkpoint foi preparado. Não foi fabricada sessão nem alterada a autenticação do Railway para testar.

Não há nesta evidência confirmação de novo login concluído, consulta de canal ou upload real no YouTube nesta sessão. O próximo passo é concluir a verificação no Google, abrir o editor publicado, renderizar um clipe curto, baixar o arquivo e verificar o envio privado autorizado. O status definitivo do deployment e as capturas de produção são registrados no relatório de entrega.

## Referências técnicas

- [Google OAuth para aplicações web](https://developers.google.com/identity/protocols/oauth2/web-server).
- [Autorização YouTube para aplicações web](https://developers.google.com/youtube/v3/guides/auth/server-side-web-apps).
- [YouTube videos.insert](https://developers.google.com/youtube/v3/docs/videos/insert).
