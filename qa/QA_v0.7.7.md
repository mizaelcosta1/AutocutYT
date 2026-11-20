# QA v0.7.7 — downloads e armazenamento externo

## Escopo

Download autenticado da prévia e do final, automação opt-in de download no navegador, envio manual ou automático pelo servidor a Google Drive/GitHub e histórico por projeto. O trabalho mantém a revisão obrigatória da prévia antes do final e as proteções existentes do renderer.

## Verificações

- API de mídia: conteúdo e `Content-Disposition: attachment`, nome por qualidade e renderização, player inline, cache privado, bloqueio de IDs substituídos, paridade entre cortes e montagem.
- Fronteiras da API: conta derivada da sessão, rejeição de origem externa, corpo validado sem caminho de arquivo enviado pelo cliente, ausência de tokens nas respostas e bloqueio de automação sem destino conectado.
- OAuth: login normal somente OpenID; autorização Drive separada, state, nonce, PKCE, consentimento offline, escopo concedido, mesma conta/sessão iniciadora e retorno já registrado. Estados inválidos, cancelamento e troca de conta não conectam o Drive.
- Renderização: o resultado expõe o ID e o endereço do arquivo concluído; a fila captura esses mesmos bytes antes da limpeza de versões anteriores. Falha de armazenamento não transforma render concluído em erro.
- Persistência: fila criptografada, pin por hard link, isolamento por conta, deduplicação por destino/render/SHA-256, recuperação após reinício e liberação da reserva somente após sucesso confirmado ou cancelamento explícito.
- Provedores: testes com transporte HTTP controlado exercitam Drive resumable e GitHub Releases privados em rascunho. Integridade, limites, permissões, reconexão, redirecionamentos, falhas de rede e respostas ambíguas são tratados sem publicação de vídeos.
- Interface: downloads identificados por qualidade, botões ligados à versão correta, automação só no evento de conclusão, deduplicação de eventos, link manual de contingência e estados de conexão/erro sem sucesso fictício.

## Execução

Suíte completa do servidor: **357 testes passaram e 3 foram ignorados**, em 35,41 s. Os skips são os casos opcionais de imagem de rosto e CLI legado já existentes. Suíte completa DOM: **87 testes passaram**, sem falhas ou skips, em 6,99 s. Os novos casos incluem 55 de entrega persistente/provedores, 25 de OAuth, 11 de API/integração e 21 de interface.

As regressões finais também cobrem downloads de arquivos antigos sem UUID de renderização e preferências modificadas em outra aba. O editor confirma a preferência atual antes de iniciar um download automático; uma consulta malsucedida não reutiliza uma autorização antiga em cache. Os cortes antigos recebem identidade derivada da revisão real do arquivo sem reescrever sua aprovação.

A verificação visual em produção será registrada no checkpoint após a implantação. Os testes de integração com respostas controladas não equivalem a uploads reais nas contas do usuário.

## Configuração externa pendente

O usuário precisa autorizar o escopo Drive no botão de conexão ou informar o token e o repositório privado de armazenamento GitHub. Nenhum destino foi conectado por meio das credenciais do ChatGPT e nenhum vídeo foi publicado ou enviado externamente durante a implementação.
