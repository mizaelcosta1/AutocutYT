# AutoClips v0.6 — Editor de linha do tempo (proposta independente)

## Objetivo
Adicionar ao AutoClips AI Studio v0.5 um editor visual de cortes sequenciais inspirado no fluxo de trabalho do OpenChatCut, sem incorporar código AGPL do repositório de terceiros e sem regredir OAuth YouTube, importação VOD e cortes automáticos.

## Aproximações avaliadas
1. Substituir o app pelo OpenChatCut: maior cobertura, mas demandaria abandonar FastAPI, adaptar React/Remotion, manter Node 24 e avaliar AGPL integral.
2. Rodar OpenChatCut como segundo serviço: reaproveita editor existente, mas cria dois formatos de projeto e dois sistemas de login/storage, além de exigir revisão jurídica de distribuição e serviço em rede.
3. **Implementar funções essenciais com código próprio e formato editável do AutoClips**: menor risco e custos de CPU previsíveis, mas não oferece paridade imediata de funcionalidades. Escolha operacional para o primeiro marco, a validar antes de publicação.

## Limites do primeiro marco (v0.6.0 local)
- Editor separado, aberto pelo editor da v0.5; preservar a interface e os endpoints existentes.
- Projeto por vídeo: segmentos ordenados e não destrutivos que apontam para intervalos do arquivo original; salvar/consultar, dividir, recortar, remover, mover, adicionar, desfazer/refazer, restaurar.
- Mapear a reprodução da timeline para os instantes do arquivo; indicação de áudio original vinculado, não um mixer multifaixas.
- Transcrição por palavra consultável quando disponível, botão para transcrever com faster-whisper (opcional), trechos inseridos a partir de palavras e exportação SRT sincronizada após os cortes.
- Limpeza opcional de pausas longas por intervalos de fala. Nunca executar sem clique explícito.
- Render da timeline em dois níveis (prévia 720p, final 1080p) via FFmpeg, respeitando aprovação explícita da prévia e original intacto.
- Endpoints e UI com os mesmos limites de acesso do servidor v0.5; sem chamadas de IA pagas ou publicação de vídeo automática.

## Exclusões explícitas da v0.6.0
WebGL, LUTs, chroma key, MCP server externo, agente LLM, faixas áudio independentes, efeitos com keyframes, traduções automáticas de legendas, integração Upload-Post multi-rede e import/export FCPXML. Não há reivindicação de paridade com OpenChatCut.

## Segurança e integridade
- Limitar comandos e duração de render, validar trechos contra duração original, não aceitar caminhos arbitrários, executar ffmpeg por array de argumentos, impedir publicação sem aprovação.
- Gerar arquivos de projeto no armazenamento local do AutoClips; qualquer envio ao provedor de transcrição remoto requer autorização independente (não implementado).
- Manter o banco JSON e mídia de cada usuário protegidos pelo Basic Auth preexistente; deployment multiusuário fora do escopo.
- Este trabalho não copia nem modifica o código-fonte AGPL do OpenChatCut, apenas usa seu README como referência de requisitos.

## Aceite
- Testes unitários de comandos (incluindo limites e histórico), testes API com cliente local e render real usando mídia de teste sintética.
- Arquivo ZIP instalável localmente com testes e documentação de diferenças para o OpenChatCut.
- Não publicar em Railway, GitHub ou YouTube nesta etapa; hospedagem deve ser revisada/confirmada separadamente.
