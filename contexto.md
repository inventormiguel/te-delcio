# TE — Transferência de Estoque

## Objetivo

Permitir que uma loja solicite a transferência de um produto de outra unidade quando um cliente procura um item que está em falta na loja onde foi atendido.

## Fluxo da transferência

1. A vendedora da loja que atende o cliente busca o produto no sistema pelo código ou pelo nome.
2. O sistema consulta o estoque das unidades e mostra a unidade mais próxima que tem o produto disponível.
3. Ao solicitar a transferência, o sistema registra a pessoa autenticada como vendedora solicitante e a loja do seu cadastro como destino. O pedido aparece na fila dos usuários com perfil Gerente vinculados a essa mesma loja e filial. Basta que um deles aprove. O histórico identifica quem tomou a decisão.
4. Após a aprovação, o pedido chega à vendedora da unidade que tem o produto e ao almoxarifado dessa unidade.
5. O almoxarifado emite a nota e encaminha o produto à transportadora.
6. A transportadora entrega o produto na loja solicitante. A pessoa que fez o pedido recebe o produto e registra o recebimento com uma foto.

## Aplicação web e perfis

O TE funciona pelo navegador, com identidade visual em tons de azul e logo próprio. No cadastro, a administração escolhe a combinação de filial e loja de cada usuário. Cada loja pertence a uma filial. A pessoa vê as áreas correspondentes aos seus perfis. A conta de testes de Miguel tem acesso às quatro visões e ao cadastro de usuários.

- **Vendedor** busca por nome ou código, escolhe a unidade de origem e a quantidade, solicita a transferência e confirma o recebimento com foto.
- **Gerente** analisa os pedidos da loja solicitante e aprova ou recusa. A aprovação libera o pedido para o almoxarifado da origem.
- **Almoxarifado** registra a nota e encaminha a mercadoria à transportadora. O teste permite gerar um documento de expedição simulado.
- **Transportadora** consulta a fila de transporte e confirma a entrega. O recebimento com foto cabe à pessoa que solicitou a peça.

## Dados e hospedagem

O cenário de demonstração usa cinco filiais fictícias em São Paulo, cada uma com uma loja, e um catálogo comum de 12 produtos com saldos por loja. A loja mais próxima é calculada pela distância em linha reta a partir da loja solicitante. O pedido reserva o saldo na origem, a recusa libera a reserva e o recebimento soma as peças ao saldo do destino.

A aplicação foi desenvolvida para hospedagem na Vercel com Django e PostgreSQL. A integração com um emissor fiscal e a conexão com estoques reais dependem dos sistemas que as lojas utilizarem.

## Manutenção do contexto

Este arquivo é a referência para as tarefas e decisões do projeto. Consulte-o antes de executar uma tarefa e atualize-o sempre que uma mudança no fluxo ou nos requisitos for definida.
