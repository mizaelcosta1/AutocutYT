# Decisões de composição — v0.7.5

## Referência audiovisual e conselho

A referência fornecida foi [este vídeo de 5:13](https://www.youtube.com/watch?v=wMetiPcC0dM). A análise audiovisual automatizada retornou 44 cenas contíguas, de 0:00 a 5:13. A tentativa de obter legendas confirmou que não havia trilha de captions disponível. Trata-se de análise assistida do vídeo completo, sem transcrição oficial palavra por palavra.

Os trechos relevantes para o editor são 2:43–2:52, com câmera acima e jogo abaixo, e 3:11–3:47, com seleção de momentos por áudio, composição vertical e legenda. A apresentação também menciona título de abertura, legenda palavra por palavra e voz de comentário; esses três recursos adicionais não fazem parte desta entrega. A v0.7.5 entrega os layouts e a localização de rosto pedidos, preservando a seleção por áudio e a transcrição existentes.

O conselho teve três revisões independentes: GPT-6.2 Astra para arquitetura/renderização, GPT-6.2 Sol para detecção e dependências, e GPT-6.1 Terra para experiência de edição. A integração e a verificação final ficaram com o agente principal. As decisões abaixo refletem revisão de código e testes executados.

## Escolhas

- **Recurso nativo no repositório existente.** A orientação do Plugin Creator permite preservar a aplicação e a hospedagem existentes. O painel deve funcionar no navegador da plataforma e usar a autenticação já configurada. A detecção não depende de instalar um processo local no computador do usuário.
- **YuNet local, sem identidade facial.** OpenCV FaceDetectorYN localiza caixas de rostos em até oito quadros. Os candidatos são agrupados por posição; persistência e proximidade de bordas ajudam a sugerir uma câmera. O usuário escolhe a região, porque personagens e rostos no cenário também podem ser detectados.
- **Correção manual sempre disponível.** Desenhar, mover, redimensionar e informar porcentagens atende fontes sem rosto detectável, câmera pequena, oclusões e diferentes posições. As coordenadas seguem o quadro exibido, com rotação e proporção de pixels respeitadas.
- **Um renderizador nos dois editores.** `layout_config` é normalizado na API, no compositor e nas comparações da interface. Clipes e montagem usam a mesma geometria e as mesmas regiões congeladas. O detector não roda novamente durante a exportação final.
- **Preservar o jogo como padrão.** A área de gameplay usa ajuste integral por padrão. Preencher a área é uma opção explícita, pois pode cortar HUD e elementos nas extremidades.
- **Legenda aplicada depois da composição.** O ASS usa a resolução de referência do formato final. A prévia mantém tamanho e posição relativos, inclusive no formato horizontal. O modelo com faixa central reserva 10% da altura; o modelo de duas telas não reserva faixa.
- **Salvar por vídeo e reutilizar por modelo.** Preferências ficam no projeto; até 30 modelos personalizados ficam no volume persistente. Regiões de um modelo precisam ser revisadas ao aplicar em outra fonte.
- **Aprovação vinculada ao resultado.** Mudanças de composição, fala, recorte temporal ou ID da prévia impedem aproveitar uma aprovação anterior. O original é verificado por hash antes e depois de renderizar. Arquivos temporários e resultados inconsistentes são descartados.

## Limites intencionais

A localização usa amostras e regiões fixas; não há rastreamento contínuo nem alternância automática entre participantes. Se a posição da câmera mudar, o usuário pode criar outro corte com outra região. A camada de legenda permanece temporizada por grupos de palavras, como na versão anterior. Tela dividida significa rosto acima e jogo abaixo; PiP permite os quatro cantos. O motor permite 9:16 e 16:9.

## Referências primárias

- [OpenCV FaceDetectorYN](https://docs.opencv.org/4.x/d0/dd4/tutorial_dnn_face.html).
- [YuNet e licença no OpenCV Zoo](https://github.com/opencv/opencv_zoo/tree/47534e27c9851bb1128ccc0102f1145e27f23f98/models/face_detection_yunet).
- [Imagem de teste NASA disponibilizada pelo scikit-image](https://scikit-image.org/docs/stable/api/skimage.data.html#skimage.data.astronaut), documentada como domínio público.
