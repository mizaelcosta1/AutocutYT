# AutoClips v0.8.2 — armazenamento e arquivamento no Google Drive

Data: 20/11/2026 (America/Sao_Paulo).

## Objetivo e situação encontrada

Base: `fb35946e7f05d9dc01f8e8fd0afaf9a2b48eaf36`, branch `main` de `mizaelcosta1/AutocutYT`.

O serviço Railway `autoclips-studio-v07` possui um volume persistente de **500 MB** montado em `/data/autoclips`. Não foram encontrados buckets nesse ambiente. O recurso anterior enviava cópias dos exports, mas mantinha os MP4s no disco. Portanto, ativar apenas `auto_upload` não liberava o espaço de trabalho.

Esta versão implementa arquivamento de finais no Drive e preserva o acesso aos resultados. O volume não foi redimensionado e nenhum plano ou recurso pago foi contratado. A conta Drive precisa autorizar o próprio AutoClips; a integração do ChatGPT não fornece credenciais ao serviço.

## Alterações

- Painel de capacidade local e quota Drive, consultada com `about.get` e o escopo `drive.file` existente. A disponibilidade usa `storageQuota.limit - storageQuota.usage`; valores desconhecidos continuam desconhecidos.
- **Usar Drive e liberar espaço** verifica a conexão/quota, ativa envio e arquivamento de finais e inclui os finais atuais da biblioteca. Fontes, gravações originais, prévias e legendas são preservadas.
- Upload com retomada e confirmação de tamanho, SHA-256, identidade do envio e pasta. Arquivamento também exige permissão de download e salva a referência remota antes de liberar a cópia local.
- Proteção contra troca de conta, desligamento da automação, substituição de render, fonte compartilhada, arquivo ainda usado como prévia, tarefas ativas e reservas de outros envios.
- Falha ao arquivar mantém o envio como concluído e informa por que a cópia local foi preservada. O worker tenta novamente; uma versão substituída deixa de ser candidata à remoção.
- Downloads e players preservam os endereços atuais. Finais arquivados são servidos do Drive por streaming com suporte a `Range`, sem baixar o MP4 inteiro para o volume.
- Publicação no YouTube restaura temporariamente o final, confirma seu hash e remove o temporário ao concluir ou falhar. A restauração exige o tamanho do arquivo mais 64 MiB de reserva.
- Limpeza de restaurações interrompidas ocorre uma vez na inicialização, antes dos workers, e não toca fontes nem reservas de uploads.
- Indicadores e atualização de pendências na interface, com acentos cyan e alertas vermelhos.

## Validação executada

```text
Backend: 499 passed, 3 skipped in 51.29s
DOM:     112 tests, 112 pass, 0 fail (8.10s)
git diff --check: aprovado
```

Os 107 novos testes backend cobrem capacidade, arquivamento, streaming, restauração e integração por HTTP. Depois da suíte geral, a revisão acrescentou quatro cenários de caminhos compartilhados e um de substituição de arquivo legado. A regressão direcionada final de arquivamento/download/API passou com 82 testes em 3,03 s. Assim, foram executados 504 cenários distintos de backend; a suíte geral acima precede essa correção dirigida. Os 11 novos testes DOM cobrem indicadores, pré-condições, ação de otimização, estados e atualização das pendências.

Os testes de integração usam sessões assinadas de contas fictícias e APIs Drive/YouTube simuladas. Exercitam o ciclo completo de upload, confirmação, remoção local, download na mesma URL e publicação com restauração temporária. Não comprovam a quota ou o consentimento de uma conta real.

Nenhuma dependência nova de produção: foram usados FastAPI/Starlette, httpx e os módulos padrão já disponíveis. A suíte também mantém os testes reais de FFmpeg e das funções de edição existentes.

## Verificação externa e limites

O login de produção foi concluído pela tela oficial do Google, com confirmação de identidade no dispositivo do usuário. A interface autenticada foi aberta e informou 245,9 MB livres no disco de edição. Em Conexões, o Google Drive ainda estava Não conectado. A ativação da automação, a quota real do Drive, o upload real e a revisão visual do novo painel ainda dependem da publicação e da autorização Drive no AutoClips.

Não afirmar que o armazenamento da conta já foi ampliado ou que a automação está ativa sem essa confirmação. O deploy e seu estado terminal serão registrados no checkpoint de entrega.

Um final já arquivado pode ser baixado ou publicado no YouTube com recuperação temporária. Para enviar esse mesmo final a outro provedor, é necessária uma nova exportação local; a API informa essa condição explicitamente.

O Drive armazena os finais; **500 MB continuam sendo o limite do volume de processamento**. Lives longas e fontes grandes ainda exigem mais espaço de trabalho. O contador de bytes arquivados representa o tamanho dos finais no Drive; a ocupação real do disco é medida separadamente, pois uma leitura em andamento pode manter o inode aberto até terminar.

## Referências oficiais

- [Drive: consulta de quota e escopos](https://developers.google.com/workspace/drive/api/reference/rest/v3/about/get).
- [Drive: campos de storageQuota](https://developers.google.com/workspace/drive/api/reference/rest/v3/about).
- [Drive: metadados e checksums](https://developers.google.com/workspace/drive/api/reference/rest/v3/files).
- [Drive: downloads e intervalos de bytes](https://developers.google.com/workspace/drive/api/guides/manage-downloads).
- [Railway: limites e redimensionamento de volumes](https://docs.railway.com/volumes/reference).
