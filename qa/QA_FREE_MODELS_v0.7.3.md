# QA — Free LLM APIs e OpenRouter Free — v0.7.3

Data: 19/11/2026 (America/Sao_Paulo). Base revisada: `51bc986e82b80f0800638e829919cce2a57fd348`.

## Mudanças verificadas

1. Catálogo OpenRouter completo, com filtro gratuito antes da paginação e busca; páginas de 100 no cliente.
2. Preço zero exige valores explícitos e válidos. Preço positivo tem prioridade sobre o nome `:free`; preço desconhecido não é tratado como gratuito.
3. Roteador oficial `openrouter/free`, escolha por tarefa e botão que configura momentos/títulos.
4. Teto zero de preço nas chamadas gratuitas; sem fallback pago ou repetição automática após erro. A pausa 429/Retry-After vale para a credencial inteira.
5. Chave validada separadamente do catálogo público. Consulta do catálogo não envia a chave; cofre continua criptografado.
6. Presets para OpenRouter, Groq, Gemini, NVIDIA NIM e Mistral, com links oficiais e chave própria por provedor.
7. Migração das conexões antigas e preservação da intenção gratuita em mudança posterior de preço. URLs oficiais com porta HTTPS explícita são normalizadas.
8. UI só anuncia roteador gratuito quando tipo, catálogo e seleção confirmam o modo protegido. Revisão sem ação implementada saiu do seletor.

## Resultado automatizado

| Verificação | Resultado |
| --- | --- |
| `python -m pytest -q` | 125 passaram, 1 ignorado |
| Testes novos de metadados do catálogo | 55 passaram |
| Testes novos de integração, custo e migração | 19 passaram |
| Testes do JavaScript com DOM simulado | 10 passaram |
| Sintaxe de `app.js`, `connections.js`, `timeline.js` | Aprovada |
| Compilação Python dos módulos alterados | Aprovada |
| `git diff --check` | Aprovado |
| Padrões de segredos reais nos arquivos alterados | Nenhum encontrado |

O teste ignorado é legado, já presente na base; os testes Python anteriores continuam passando. O ambiente local usou Python 3.12; o Dockerfile de produção continua em Python 3.11. Dois avisos de depreciação do ambiente de testes (anyio/Starlette) não geraram falhas. Não foi acrescentada dependência de produção.

### Testes de interface

`qa/test_connections_dom.cjs` executa o HTML/JavaScript reais em jsdom, com respostas controladas de fetch. Verifica preset/endpoint, limpeza de chave ao trocar provedor, filtro gratuito inicial, paginação 100+100, debounce, respostas fora de ordem, salvamento por tarefa, um único POST no teste após duplo clique, erro de chave sem eco, ID ambíguo de roteador em provedor personalizado e resultado vazio com ação desativada.

Reprodução (dependência de desenvolvimento isolada, fora da imagem de produção):

```bash
npm install --prefix /tmp/autoclips-dom-qa jsdom@31.0.1
NODE_PATH=/tmp/autoclips-dom-qa/node_modules node --test qa/test_connections_dom.cjs
python -m pytest -q
```

**Isso não é validação visual em navegador.** O navegador conectado à conversa estava indisponível; o navegador de nuvem não alcançou o servidor loopback local (`ERR_CONNECTION_REFUSED`), e a inspeção da aba de erro foi recusada pela política de URL/protocolo. Não se tentou contornar a política. Após a publicação, o navegador acessou a página pública de login do Studio e registrou um print real. Não há novos prints autenticados de Conexões nesta rodada, nem promessa de teste visual completo concluído.

O primeiro deployment passou no healthcheck, mas a migração baseada apenas no catálogo salvo não registrou tarefas configuradas. Foi acrescentada a atualização única de catálogos antigos: um GET público sem chave pode atualizar todas as conexões OpenRouter elegíveis, preservar credenciais e preparar o roteador. Três regressões adicionais confirmam o caso antigo, ausência de alvos sem rede e falha recuperável sem alterar o cofre. Essa etapa não executa inferência.

`qa/connections_fixture.py` permanece como ambiente local reproduzível, com chave fictícia e upstream simulado, identificado por faixa de QA. Não é importado pelo servidor de produção e não utiliza a conta real.

## Consulta real de catálogo, sem inferência

GET público: `https://openrouter.ai/api/v1/models`, consulta em 19/11/2026.

- 479 entradas retornadas.
- 16 entradas classificadas com preço zero, incluindo o roteador (o roteador não é um modelo independente).
- 455 com preço positivo; 8 sem dados suficientes para confirmar o preço.
- `openrouter/free` presente e com preço zero de entrada e saída.

Esses números são um retrato do catálogo consultado e podem mudar. Não indicam que todos os modelos foram executados nem que a conta do usuário tem cota. Nenhuma chave real foi usada nessa consulta e nenhuma inferência externa foi executada nesta QA.

## Limites e próximo teste na conta

O botão **Testar IA gratuita** executa uma chamada curta real quando utilizado no Studio com a conexão do usuário. Os testes automatizados simularam o provedor; não verificaram a chave real armazenada em produção. Um catálogo acessível não garante uma inferência disponível.

A análise semântica de vídeo ainda depende da transcrição e da autorização de envio. Este release não instala Whisper, não cria chaves de terceiros, não executa geração Comfy nem altera a publicação aprovada dos vídeos. A rotação é a seleção gratuita nativa do OpenRouter; os demais provedores são escolhidos manualmente por tarefa e têm cotas e condições próprias.

O deployment e a versão pública devem ser verificados após publicar o commit. O checkpoint de entrega registra o SHA e o estado terminal retornado pelo Railway; este relatório registra a validação anterior à publicação.

## Fontes técnicas

- https://github.com/open-free-llm-api/awesome-freellm-apis
- https://openrouter.ai/docs/api/api-reference/models/list-all-models-and-their-properties
- https://openrouter.ai/docs/guides/routing/routers/free-router
- https://openrouter.ai/docs/guides/routing/model-variants/free
- https://openrouter.ai/docs/guides/routing/provider-selection
- https://openrouter.ai/docs/api_reference/limits
- https://ai.google.dev/gemini-api/docs/openai
