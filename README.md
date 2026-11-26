# TE · Transferência de estoque

Aplicação Django com visões por perfil, reservas transacionais e histórico de movimentação. Os requisitos funcionais estão em [contexto.md](contexto.md).

## Executar localmente

Use Python 3.12 ou superior. Na raiz do projeto, crie o ambiente com `python3 -m venv .venv`, ative-o e instale `pip install -r requirements.txt`. A variável `DATABASE_URL` seleciona o banco. Neste Mac a configuração em `.local/dburl` aponta para o PostgreSQL dedicado. Em uma cópia nova, sem configuração, o Django usa SQLite para desenvolvimento.

1. Execute `python manage.py migrate`.
2. Defina `TE_ADMIN_PASSWORD` no ambiente e execute `python manage.py seed_demo` para criar a rede de teste e a primeira conta. Repetir o comando preserva senhas, saldos e pedidos existentes.
3. Execute `python manage.py runserver 127.0.0.1:4875` e abra o endereço no navegador.

O PostgreSQL deste Mac usa um socket privado no projeto. Se precisar iniciá-lo novamente após reiniciar o computador, execute na pasta do projeto:

```sh
pg_ctl -D "$HOME/Library/Application Support/TE-Delcio/postgres" -l "$HOME/Library/Application Support/TE-Delcio/postgres/server.log" -o "-p 54875 -k '$PWD/.local/pgsocket' -c listen_addresses=''" start
```

## Preparar a Vercel

O projeto utiliza o runtime Python, o entrypoint WSGI em `estoque/wsgi.py` e arquivos estáticos coletados pelo Django. O estado persistente, incluindo sessões, fotos de recebimento e histórico, fica no PostgreSQL.

1. Crie um projeto PostgreSQL no Neon e conecte-o ao projeto da Vercel. Configure a URL PostgreSQL com pooling e TLS em `DATABASE_URL`. Use bancos ou branches distintos para Preview e Production.
2. Defina uma `SECRET_KEY` aleatória e exclusiva para cada ambiente. Para domínio próprio, inclua-o em `APP_HOSTS` (lista separada por vírgulas). Os domínios fornecidos nas variáveis de sistema da Vercel são reconhecidos automaticamente.
3. Aplique `python manage.py migrate --noinput` com a conexão do ambiente de destino, como etapa de release antes de liberar tráfego. Para um ambiente demonstrativo, execute também `python manage.py seed_demo` com uma senha inicial em `TE_ADMIN_PASSWORD`. Guarde os segredos na configuração do ambiente.
4. Importe a pasta na Vercel como projeto Django e publique. Confira login, arquivos estáticos, criação de pedido e leitura da foto no domínio publicado.

A emissão fiscal oficial depende de integração com o emissor da loja. A expedição aceita o número de uma nota externa ou gera uma referência simulada para teste. O comprovante interno pode ser aberto pelo histórico do pedido.

## Verificação

`python manage.py test estoque` usa um banco temporário. Para PostgreSQL, a conta de teste precisa de permissão para criar esse banco. A suíte cobre transições, saldos, reenvio, foto, perfis e escopo de loja.

As imagens ilustrativas de produtos são do catálogo DummyJSON. A fotografia de loja é do Unsplash. A tipografia é DM Sans.

## Avisos por e-mail aos gerentes

Ao reservar o estoque de um novo pedido, a mesma transação registra um aviso para cada gerente ativo da loja solicitante. A associação pode ser principal ou adicional. O primeiro envio ocorre após a confirmação da transação. A Vercel verifica pendências a cada cinco minutos. Antes de cada tentativa, o sistema confere a situação do pedido e o vínculo atual do destinatário. O identificador retornado pelo provedor registra a aceitação do e-mail para envio.

Configure `TE_RESEND_KEY` com uma chave de envio e `TE_EMAIL_FROM` com um remetente do domínio verificado no Resend. A rota interna do agendamento exige `CRON_SECRET`. Cadastre os segredos no ambiente de produção da Vercel. Para testes locais, a suíte simula o provedor.

## WhatsApp de aprovação

O conector persistente em `scripts/openwa_bridge.py` atende a sessão `te-delcio` do OpenWA local (API 0.23.7 em 127.0.0.1:2785). Ele faz chamadas de saída à Vercel. A credencial do OpenWA é lida no host em `OPENWA_CREDENTIALS`. O token específico da ponte deve coincidir em `TE_WHATSAPP_BRIDGE_TOKEN` na Vercel e no arquivo privado `token` de `TE_BRIDGE_DIR`. O diretório também guarda o registro local da tentativa atual. Rode uma instância persistente da ponte por instalação. Neste Mac ela é gerenciada pelo LaunchAgent `com.inventormiguel.te-openwa`.

Para conectar ou reconectar, abra Integração com WhatsApp como administrador e escaneie o QR pelo menu Aparelhos conectados. O painel mostra o estado dos últimos avisos. Um envio incerto precisa de conferência na conversa antes de qualquer reenvio manual.
