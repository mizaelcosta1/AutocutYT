# QA v0.7.6 — atividade, legendas e exportação

## Problemas reproduzidos

- No editor de clipes, dois acionamentos antes da primeira gravação disparavam dois pedidos de renderização. A reprodução usou scripts reais e respostas controladas em DOM.
- Na montagem, o botão de detectar rostos podia disputar o vídeo com uma renderização; recarregar perdia o acompanhamento da tarefa.
- No projeto autenticado em produção, a opção de incluir legendas voltava desmarcada após recarregar; fonte, tamanho e cor continuavam salvos.
- No navegador em produção, o elemento de vídeo media 436,89 px de altura dentro de uma caixa de 390 px, cortando parte dos controles. O painel de exportação também empurrava a timeline para depois de um grande espaço vazio.

## Correções

`video-activity.js` reserva a operação antes do primeiro `await`, compartilha a ocupação entre componentes e recupera a tarefa por `GET /api/videos/{id}/activity`. O fim de uma tarefa consulta outras tarefas do mesmo vídeo antes de liberar controles. Erros de rede não são apresentados como conclusão; pedidos de resultado desconhecido exigem conferência, sem reenvio automático. HTTP 507 de preflight libera uma tentativa menor imediatamente.

A escolha de incluir legendas é salva no layout e preservada quando um cliente antigo omite o campo. Uma transcrição sem tarefa correspondente após reinício permite nova tentativa e mantém o texto existente.

O servidor estima espaço antes de enfileirar e antes de executar: 3 Mb/s para prévia, 8 Mb/s para final, áudio a 160 kb/s, mais 8 MiB de margem. Montagens consideram as partes e a cópia unida. CRF torna o tamanho real variável; a checagem não garante um tamanho máximo.

O renderer valida em diretório temporário próprio e só então substitui o destino. Falhas preservam a exportação anterior e limpam os arquivos que pertencem à tentativa. O servidor também remove o SRT novo de um render que falhou.

O player tem limites de dimensão explícitos. A timeline e a transcrição ficam na coluna do vídeo em telas largas e antes da exportação no fluxo de uma coluna.

## Validação automatizada

- DOM: 66 casos passaram, sem falhas ou skips. Inclui submissão duplicada, detecção concorrente, retomada, múltiplas tarefas, falha de rede, armazenamento insuficiente e persistência de legendas nos dois editores.
- API: 11 casos focados passaram, incluindo autenticação, transição de tarefas, espaço, reinício e persistência.
- Render e legendas: 52 casos focados passaram, incluindo 31 cenários novos de atomicidade e limpeza.
- Suíte Python completa: 260 casos passaram e 3 foram ignorados, sem falhas (25,80 s). Os skips são duas verificações opcionais com imagem de rosto e um caso legado de CLI; a detecção facial também foi executada no projeto real. Os testes de publicação simulam o envio, mantendo o agendador real, e não publicam no YouTube.

## Teste de produção realizado na versão anterior

Foi acessado o projeto solicitado com a conta autorizada. A fonte tem 314,006 segundos e 1920×1080. A montagem tinha quatro trechos somando 90 segundos. Foram verificados reprodução/pausa, busca por palavra com salto para 25,26 s, localização facial (dois candidatos), seleção manual de regiões, modelos personalizados, estilo das legendas e downloads SRT/VTT. Os dois arquivos baixados têm 27 blocos, com tempos válidos entre 13,84 e 89,90 segundos. O ajuste temporário do primeiro corte foi desfeito; seu fim voltou a 38,972 segundos.

Foi criado um clipe adicional de QA de seis segundos (12,5–18,5 s) na mesma gravação. Não foi excluído nenhum clipe anterior. A montagem original e seus intervalos foram preservados.

## Verificação após implantação

As evidências da versão publicada, reprodução da prévia e exportação final são entregues em checkpoint separado depois da implantação. Não confundir testes de DOM com teste visual do site publicado. A checagem inicial indicou aproximadamente 42 MiB livres, insuficientes pela estimativa para renderizar a montagem completa de 90 s; não foi aumentado o volume nem apagada mídia do usuário.
