# AutoClips v0.8.0 — início da evolução do editor visual

## Base inspecionada
- Repositório: `mizaelcosta1/AutocutYT`, branch `main`.
- Base: commit `ace0337e83a531d00aa77b555d7febed7785860d` (v0.7.7).
- Editor compartilhado: `static/layout-editor.js` e `static/layout-editor.css`.
- Composição e validação: `composition.py`, `layout_presets.py`, `autoclips.py`, `timeline_export.py`.
- Dois clientes: editor de clipes (`static/index.html`) e montagem (`static/timeline.html`).
- O recorte de origem do rosto/jogo já usa coordenadas normalizadas e Pointer Events; os modelos são salvos por vídeo.

## Implementado neste checkpoint
- Nova identidade visual baseada na referência anexada: preto `#000000`, coral `#FF422A`, branco `#FFFFFF`, com tons intermediários de cinza para hierarquia e legibilidade.
- Editor reorganizado em três etapas contextuais: **Modelos**, **Composição** e **Rosto e jogo**.
- Galeria de modelos em modal, com seleção de preset e opção de salvar modelo pessoal.
- Controles de composição e recorte da gravação original separados, reduzindo a sobreposição de opções.
- A seleção de um preset leva à etapa de composição; quando o modelo exige rosto e ainda não existe região, leva à etapa Rosto e jogo.
- A atualização preserva o formato JSON existente e os presets já salvos.

## Ainda não implementado nesta fase
- Manipulação direta dos elementos no quadro final (legenda, título, quadro de rosto e quadro do jogo).
- Camadas, lista de elementos, guias, alinhamento, bloqueio e desfazer/refazer visual.
- Títulos com intervalo e animação, e controle contextual completo das legendas.
- Comparação de pixels entre prévia e render final para todos os novos elementos.

## Testes adicionados
- DOM: navegação entre etapas, estado de painéis, abertura/fechamento do modal e preservação do stage de seleção.
- Testes de backend/renderizador existentes permanecem aplicáveis, pois esta fase não altera o schema de composição nem o FFmpeg.

## Critério para próximo checkpoint
O próximo incremento implementará um quadro de composição final com seleção e arraste de elementos, mantendo separados o retângulo de saída e o recorte da mídia original. O renderer só receberá novos campos depois que os contratos e testes de composição forem definidos.
