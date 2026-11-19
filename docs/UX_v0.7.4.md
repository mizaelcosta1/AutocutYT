# Interface e jornada — AutoClips Studio v0.7.4

Data: 2026-11-19.

## Objetivo

Levar a pessoa do conteúdo original ao clipe revisado em um caminho claro: **importar ou gravar → editar e legendar → revisar e exportar**. A interface mantém a navegação e os projetos existentes, com a identidade visual em ciano, vermelho e azul escuro.

Este documento registra as decisões de interface. A comprovação de transcrição, captura, renderização e implantação pertence ao checkpoint técnico da versão; a existência de um controle na tela não comprova a execução do serviço.

## Aplicado nesta interface

### 1. Novo projeto e acompanhamento

- Três entradas com verbos específicos: **Enviar arquivo**, **Importar um link** e **Gravar uma live**.
- Transcrição e sugestão de cortes aparecem como escolhas distintas. Uma gravação pode gerar legendas sem precisar gerar cortes.
- As opções deixam claro quando o processamento acontece: depois da importação ou ao encerrar a gravação.
- As gravações ficam em uma seção própria, com espaço para a prévia, estado da captura e ação de encerramento. A área não disputa largura com o formulário da URL.
- O estado vazio explica como iniciar. A descrição da prévia informa que ela acompanha a captura com atraso; não promete latência zero.
- Termos de implementação como remux, YT-DLP e blocos de upload saíram da jornada principal.

### 2. Editor de clipes

- Vídeo original, seleção manual e legendas ficam na coluna principal. Sugestões de momentos e preparação da exportação ficam ao lado em telas largas.
- **Gerar legendas** é uma ação direta sobre o vídeo selecionado. O retorno deve indicar processamento, conclusão ou problema sem perder o contexto do editor.
- Downloads SRT e VTT ficam junto ao estado da transcrição e só aparecem quando houver arquivos disponíveis.
- Fonte, tamanho e cor têm controles independentes. A amostra visual é explicitamente identificada como **amostra do estilo**; o arquivo MP4 de prévia é a referência para aprovação.
- A inclusão das legendas no vídeo permanece uma escolha explícita na exportação.
- O texto explica que SRT e VTT guardam conteúdo e tempo; o estilo visual é aplicado à renderização do vídeo.
- Configuração de modelo local e detalhes de resolução ficam em blocos expansíveis.

### 3. Montagem de trechos

- O acesso à timeline se chama **Abrir montagem de trechos**. A tela descreve a tarefa sem sugerir ferramentas que ela não oferece.
- A ação de divisão se chama **Dividir neste instante**, no lugar de “Dividir no playhead”. Os controles de ordem dizem **Mover antes** e **Mover depois**.
- O painel de exportação apresenta os mesmos controles de estilo do editor.
- Os textos explicam a sequência: gerar a prévia, assistir, aprovar e exportar o arquivo final.
- A área de transcrição mantém busca de palavras, inclusão de falas, redução de pausas e downloads. O efeito da redução de pausas está explicado em um bloco expansível.

### 4. Conexões

- As contas aparecem primeiro, seguidas dos modelos de IA e dos workflows do Comfy Cloud.
- Conexões já salvas aparecem antes dos formulários para adicionar chaves.
- O filtro inicial de modelos gratuitos e os controles de paginação foram preservados.
- As explicações distinguem catálogo, conexão e escolha por tarefa. Modelos gratuitos continuam sujeitos às cotas de cada conta.
- Configuração administrativa de login foi recolhida em **Como conectar uma conta?**.
- O Comfy Cloud é descrito como acesso ao editor oficial; não há indicação de execução automática de workflows pelo AutoClips.

## Identidade visual

| Papel | Cor principal | Uso |
|---|---|---|
| Fundo | `#08121D` | Fundo geral e base do estúdio |
| Superfícies | `#102030` / `#152B3D` | Painéis e agrupamentos |
| Texto principal | `#EFF7FC` | Títulos, valores e conteúdo |
| Texto secundário | `#A9BBCA` | Explicações e metadados |
| Ciano | `#42D5E7` | Ações primárias, foco, seleção e links |
| Texto sobre ciano | `#06222C` | Leitura de botões primários |
| Vermelho | `#FB6272` | Gravação ao vivo e sinalização de captura |
| Bordas | `#2B4255` | Separação entre controles e superfícies |

Verde e amarelo continuam disponíveis para estados de sucesso e atenção. Um estado importante deve ter texto, não depender apenas de uma cor.

## Legendas: contrato visual

As duas telas usam os mesmos valores para o estilo:

```json
{
  "font_family": "DejaVu Sans",
  "font_size": 56,
  "color": "#FFFFFF"
}
```

- Fontes: DejaVu Sans, DejaVu Serif, Liberation Sans, Liberation Serif e Noto Sans. O ambiente de renderização precisa instalar as famílias apresentadas.
- Tamanho: de 18 a 96, referido ao vídeo final de 1080 × 1920. A prévia deve conservar a proporção do estilo.
- Cor: selecionada pelo usuário, com contorno escuro fixo para ajudar na leitura.
- A amostra no navegador pode usar a fonte substituta do dispositivo. A prévia MP4 usa a fonte instalada no servidor e deve ser conferida antes da aprovação final.

## Responsividade e acesso

- Colunas usam larguras flexíveis e `min-width: 0` para evitar que campos e nomes de arquivo ampliem a página.
- Em telas estreitas, cartões e editor passam para uma coluna. A navegação principal permanece acessível na barra inferior.
- Inputs e selects têm pelo menos 16 px em telas móveis; ações principais têm área de toque de pelo menos 44 px.
- Os controles mantêm indicação de foco, rótulos e feedback por texto. Avisos de tarefa usam regiões de status.
- A navegação por teclado conta com atalho para o conteúdo.
- A preferência de movimento reduzido desativa animações decorativas.
- A interface usa fontes de sistema; não exige download de fontes do Google para carregar.

## Contratos DOM preservados

Todos os 91 IDs existentes em `index.html` e os 44 IDs existentes em `timeline.html` foram preservados. A alteração não substitui os contratos da camada de interação.

| Tela | Novos controles relevantes |
|---|---|
| Início | `live-transcribe`, `vod-transcribe`, `lives-empty` |
| Editor | `btn-transcribe`, `subtitle-status`, `subtitle-font`, `subtitle-size`, `subtitle-color`, `subtitle-sample`, `subtitle-download-srt`, `subtitle-download-vtt` |
| Timeline | `subtitle-font`, `subtitle-size`, `subtitle-color`, `subtitle-sample`, `download-vtt` |

`lives-list` passou para uma seção independente após os formulários. O estado vazio é ocultado por CSS quando a lista tem conteúdo. Na timeline, `transcript-info` continua sendo a região de status da transcrição.

## Verificações desta etapa

- IDs anteriores preservados e ausência de IDs duplicados nas duas páginas.
- Os dez testes DOM existentes de conexões passaram após a reorganização. Eles verificam os handlers do catálogo, filtros, paginação, seleção e erros de chave em um DOM simulado.
- A verificação de diferenças não encontrou erros de whitespace.
- Esses resultados não substituem capturas reais do navegador nem comprovam ausência de problemas visuais. A revisão visual e a integração de áudio/vídeo devem constar do checkpoint final com suas limitações.

## Próximas melhorias a avaliar após o teste do usuário

1. Reunir tarefas longas em uma fila persistente com cancelamento e retomada bem definidos, caso o uso com gravações extensas demonstre essa necessidade.
2. Oferecer edição do texto de cada legenda e atalhos para ajustar os tempos. Esta versão apresenta geração, estilo e download; não anuncia um editor completo de legendas.
3. Avaliar presets de estilo após observar quais fontes e tamanhos o usuário prefere. Primeiro validar o resultado das três opções explícitas: fonte, tamanho e cor.

Esses itens são possibilidades de evolução; não são anunciados como recursos já entregues.


A integração final passou por 13 testes DOM de timeline, 6 de editor, 13 de acompanhamento da live e 10 de conexões. São verificações de comportamento em DOM simulado; a evidência visual autenticada é registrada separadamente no checkpoint.
