# QA v0.7.5 — composição e rosto

Data: 19/11/2026. Código e testes locais; implantação deve ser confirmada pelo checkpoint de publicação.

## Casos verificados

| Área | Verificação |
| --- | --- |
| Layouts | 9:16 e 16:9, prévia e final, tela inteira, split, faixa central e os quatro cantos PiP. Pixels do resultado comprovam posição e dimensão. |
| Regiões | Valores finitos, mínimo 2%, limites do quadro; rejeição de valores extras e regiões fora do vídeo; rotação de 90° e pixels não quadrados. |
| Legendas | Fonte/cor/tamanho preservados; posição relativa na prévia e no final; centro da faixa nos dois formatos. |
| Montagem | Dois trechos renderizados e concatenados, áudio AAC e checagem integral da decodificação. |
| Rosto | Inferência YuNet real com retrato público em duas posições sobre vídeo sintético; fonte sem rosto; agrupamento e contexto da câmera. |
| Persistência | Layout por vídeo, oito modelos de fábrica, criar/aplicar/excluir modelo pessoal, limites e validação. |
| Aprovação | Rejeitar regiões ou formato diferentes, ID de prévia antigo e transcrição alterada; nova prévia invalida final antigo. |
| Troca de vídeo | Descartar respostas atrasadas, remover eventos antigos e cartões do projeto anterior; falha de carga permite abrir outro vídeo. |
| Interface | Desenho, movimento e alça de redimensionamento; salvamentos serializados; integração dos dois editores com o painel real. |
| Acesso | Quadro original, presets e análise de rosto exigem a mesma autenticação do estúdio. |
| Dependências | OpenCV 4.12.0, NumPy 2.2.6 e faster-whisper 1.2.1 coexistem; transcrição offline do áudio público de 11 s produziu 22 palavras. |

## Execução

Suíte Python completa: **222 passaram, 3 foram pulados**. Dois skips eram os smokes opcionais com imagem; com modelo e imagem configurados, ambos foram executados separadamente e passaram. Total de casos verificados: **224 passaram, 1 skip legado**. O skip remanescente é a interface CLI antiga, removida anteriormente.

Suítes de DOM: **58 passaram**. Usam HTML, componente e scripts reais, com APIs simuladas; não equivalem a inspeção visual autenticada no navegador.

Comandos reproduzíveis, após instalar as dependências de execução e `pytest`, `Pillow` e `jsdom` de QA:

```sh
python -m pytest -q
AUTOCLIPS_FACE_MODEL=/caminho/face_detection_yunet_2023mar.onnx AUTOCLIPS_FACE_QA_PORTRAIT=/caminho/astronaut.png python -m pytest -q tests/test_face_detection.py -k real_yunet
node --test qa/test_*dom.cjs
```

Os testes de troca de arquivo durante renderização comprovam descarte da exportação alterada. A fixture HTTP foi isolada para não deixar um executor encerrado para a próxima suíte.

## Evidência e limites

Os exemplos visuais de composição usam vídeo sintético e uma imagem de domínio público. Demonstram arquivos realmente renderizados e localização real do rosto; não são capturas de uma live externa nem prints do editor autenticado. A amostra de fala usada para regressão de dependências está em inglês. Não se infere qualidade de transcrição em português, acurácia universal de rostos nem acompanhamento contínuo.

A inspeção visual da área autenticada em produção depende de uma sessão válida do usuário. As evidências da página pública, do deploy e do link de teste são registradas no checkpoint, após observar a publicação. A v0.7.5 não altera as cotas de provedores nem a capacidade de armazenamento do serviço.
