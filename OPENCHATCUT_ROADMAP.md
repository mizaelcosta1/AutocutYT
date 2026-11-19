# Mapa de recursos — OpenChatCut x AutoClips Studio v0.6.0

Fonte de referência: https://github.com/0xsline/OpenChatCut (README revisado em 19/11/2026). OpenChatCut possui licença **AGPL-3.0 ou posterior**. Nossa implementação é original, apenas inspirada em conceitos de fluxo de trabalho; não se distribui código de terceiros.

| Área OpenChatCut | Estado no AutoClips v0.6.0 | Próxima etapa desejada |
|---|---|---|
| Timeline com split, trim, reordenar, undo/redo | **Implementado** para um único vídeo original (segmentos sequenciais) | Multi-source, múltiplas pistas independentes e ripple avançado |
| Legendas automáticas / SRT | **Parcial:** SRT e queima FFmpeg com transcrição disponível, faster-whisper opcional | Estilos editáveis, auto-caption no worker padrão e tradução |
| Transcript-driven editing | **Parcial:** pesquisa, inserção em torno de palavras, remoção de pausas longas | Remover frases por seleção textual e revisão com falante |
| Áudio multifaixas, voiceover, ducking, separação vocal | **Não implementado** (áudio original acompanha trechos) | Importação segura de música e locução, ganho e ducking |
| Motion graphics e biblioteca de templates | **Não implementado** | HyperFrames / templates licenciados, com render testado |
| LUTs, chroma key, shaders WebGL e efeitos | **Não implementado** | Pipeline de efeitos controlados por projeto e preview fiel |
| Zoom e reframing automático com pessoa/face | **Não implementado** | Detecção segura de assunto e compatibilidade 9:16 |
| Agente conversacional e propostas revisáveis | **Não implementado no novo editor**; v0.5 já tem IA para ranquear clipes e gerar títulos | Introduzir comandos de edição propostos, confirmação explícita, histórico e políticas de custo |
| MCP externo / Agent Skills | **Não implementado** no servidor v0.6 | Endpoint MCP autenticado com ações limitadas e modo de proposta (sem publicar) |
| Geração de imagem/vídeo/voz/música/SFX | **Não implementado** | Adaptadores opcionais a Comfy Cloud/ElevenLabs, controlando custo e direitos |
| Export MP4/áudio/SRT/FCPXML | **Parcial:** MP4 e SRT | Áudio isolado, FCPXML, projeto portátil e render distribuído |
| YouTube e redes sociais | **Parcial:** v0.5 inclui OAuth e uploader YouTube de clipe individual, não validado em conta real | Publicar montagem após aprovação com histórico e compatibilidade TikTok/Instagram |
| Persistência / multi-projetos | **Parcial:** arquivos locais e JSON por vídeo | Banco de dados, volumes persistentes, multiusuário e backup |

## Proposta de fases de desenvolvimento

**Módulo 0.7 (próximo):** importação de áudio e imagens autorizadas, timeline com pistas reais, composição de títulos/legendas e exportação de projeto, testes FFmpeg.

**Módulo 0.8:** interface conversacional usando comandos tipados e propostas, endpoint MCP protegido, política de aprovações, análise de transcrição contextual.

**Módulo 0.9:** efeitos visuais e motion graphics integrados ao render, geração opcional via provedores, export avançado e otimização GPU/CPU.

**Produção:** apenas depois de confirmar workspace Railway, custos, storage, certificados HTTPS, OAuth e limites de volume. O serviço remoto não foi alterado nesta etapa.

## Duas estratégias para a paridade completa

- **Extensão própria, incremental (atual):** mantém arquitetura Python simples, autonomia de licenciamento e foco em cortes em massa; exige desenvolver e testar cada função.
- **OpenChatCut lado a lado como editor avançado:** oferece maior cobertura de funções em menor tempo, mas traz infraestrutura Node/React/Electron/Remotion, conversão de formato e requisitos de AGPL-3.0 para modificações e disponibilização por rede. Requer revisão legal/operacional antes de disponibilizar externamente.

Não declare qualquer recurso como pronto sem testes independentes na versão implantada.
