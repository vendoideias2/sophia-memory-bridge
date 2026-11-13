# Sophia Memory Bridge

Camada de memória de curto prazo entre o WhatsApp (WAHA) e o Hermes.

O problema que ela resolve: hoje cada mensagem chega ao Hermes como um run novo, sem histórico. A Sophia esquece o que foi dito na mensagem anterior e volta a perguntar “qual agenda?”. Esta ponte mantém uma janela de conversa por chat, gera um resumo rolante do que ficou para trás e só reinicia o contexto por inatividade (TTL) ou comando explícito.

## Arquitetura

```text
WhatsApp
   │
   ▼
WAHA  ──webhook──▶  sophia-memory-bridge  ──HTTP──▶  Hermes API
   ▲                        │
   └────── sendText ────────┘
                            │
                       SQLite (/data/memory.db)
```

- **Chave da conversa:** `chat_id` do WhatsApp. Cada contato/grupo tem seu próprio contexto.
- **Janela:** últimas `WINDOW_MESSAGES` mensagens (padrão 20).
- **Resumo:** quando a janela estoura, as mensagens mais antigas viram um resumo e saem do histórico ativo.
- **TTL:** `CONTEXT_TTL_MINUTES` (padrão 6h) sem atividade → contexto novo.
- **Reset explícito:** `/novo`, `novo`, `nova conversa`, `reiniciar` ou `reset`.
- **Sem trava de assunto:** o histórico é contexto; a última mensagem do usuário manda. O prompt do sistema deixa isso explícito.

## Como funciona a chamada ao Hermes

Modo padrão (`HERMES_MODE=chat`):

```text
POST /p/fabiano/v1/chat/completions
{
  "model": "hermes-agent",
  "messages": [
    {"role": "system", "content": "..."},
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": "..."},
    ...
    {"role": "user", "content": "mensagem atual"}
  ]
}
```

O serviço envia a janela explicitamente. É previsível, funciona em qualquer versão do Hermes e não depende do estado interno da API.

Opcional (`HERMES_MODE=responses`): usa `/v1/responses` com `conversation=wa_<chat_id>` e `store=true`, deixando o Hermes encadear as respostas. Se a API não suportar, cai automaticamente para o modo `chat`.

## Configuração

Copie `.env.example` para `.env` e preencha:

| Variável | Para que serve |
|---|---|
| `HERMES_BASE_URL` | `http://hermes:8642` na rede do Coolify |
| `HERMES_PROFILE` | `fabiano` (listener atual) ou `default` (orquestrador Sophia) |
| `HERMES_API_KEY` | Bearer do API server do Hermes |
| `HERMES_MODE` | `chat` (recomendado) ou `responses` |
| `WAHA_BASE_URL` | `https://waha.vendoideias.com` |
| `WAHA_API_KEY` | API key do WAHA |
| `WAHA_SESSION` | `FABIANO` |
| `WEBHOOK_SECRET` | segredo do webhook; vai no path |
| `BRIDGE_API_KEY` | protege `/v1/chat` |
| `ADMIN_API_KEY` | protege `/admin/*` |
| `CONTEXT_TTL_MINUTES` | tempo de inatividade para resetar |
| `WINDOW_MESSAGES` | quantas mensagens ficam no contexto ativo |
| `SELF_CHAT_IDS` | número do self-chat (ex.: `5517999999999@c.us`) |
| `SUMMARY_*` | endpoint/modelo que gera o resumo rolante |

`SUMMARY_*` é opcional. Se o endpoint falhar, a conversa continua funcionando; o serviço apenas mantém as mensagens antigas até o limite de armazenamento.

## Rodar localmente

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements-dev.txt
copy .env.example .env
# edite o .env
uvicorn app.main:app --host 127.0.0.1 --port 8080
```

Teste sem WhatsApp:

```bash
curl -X POST http://127.0.0.1:8080/v1/chat ^
  -H "X-Api-Key: change-me-bridge" ^
  -H "Content-Type: application/json" ^
  -d "{\"chat_id\":\"teste\",\"text\":\"Meu nome é Fabiano. Agende a solenidade do CRECI em 24/11 às 14:00.\"}"

curl -X POST http://127.0.0.1:8080/v1/chat ^
  -H "X-Api-Key: change-me-bridge" ^
  -H "Content-Type: application/json" ^
  -d "{\"chat_id\":\"teste\",\"text\":\"Qual é o compromisso e o horário?\"}"
```

A segunda chamada deve responder usando a primeira. Se não responder, o problema está na chamada ao Hermes (chave, perfil ou URL), não na memória.

Health:

```bash
curl http://127.0.0.1:8080/health
curl "http://127.0.0.1:8080/health?deep=true"
```

## Testes

```bash
pytest -q
```

Os testes cobrem TTL, janela, resumo, orçamento de contexto, deduplicação e proteção contra eco.

## Deploy no Coolify

1. Crie uma aplicação a partir deste repositório (build pack **Dockerfile**), porta **8080**.
2. Adicione o volume persistente `/data`.
3. Configure as variáveis do `.env`.
4. Defina o domínio, por exemplo `https://sophia-memory.vendoideias.com`.
5. Faça o deploy e confirme `GET /health` e `GET /health?deep=true`.

O serviço precisa estar na mesma rede Docker do Hermes para usar `http://hermes:8642`. No Coolify, mantenha `connect_to_docker_network` habilitado.

### Webhook do WAHA

Na sessão `FABIANO` do WAHA, configure o webhook:

```text
URL: https://sophia-memory.vendoideias.com/webhook/waha/<WEBHOOK_SECRET>
Eventos: message, message.any
```

Se o WAHA permitir headers customizados, envie também `X-Webhook-Secret: <WEBHOOK_SECRET>`. O segredo no path já basta.

> **Cloudflare Access:** se o hostname estiver atrás do Access, o WAHA não conseguirá chamar o webhook. Crie uma exceção (bypass) para o path `/webhook/waha/*` ou use um hostname sem Access para esse endpoint. Não exponha `/admin/*` sem proteção.

### Cutover seguro

1. Suba a ponte e valide `/health?deep=true`.
2. Teste `/v1/chat` com o `chat_id` real e duas mensagens encadeadas.
3. Pare o listener antigo:
   ```bash
   sudo systemctl stop sophia-selfchat.service
   sudo systemctl disable sophia-selfchat.service
   ```
4. Troque o webhook da sessão `FABIANO` para a URL da ponte.
5. Envie uma mensagem de teste e confirme que a resposta chega e que a segunda mensagem mantém o contexto.
6. Se algo falhar, restaure o webhook antigo e reative o `sophia-selfchat.service`.

## Operação

- **Ver contexto de um chat:** `GET /admin/conversations/<chat_id>` com `X-Api-Key`.
- **Resetar um chat:** `POST /admin/reset/<chat_id>` com `X-Api-Key`.
- **Deduplicação:** mensagens repetidas do WAHA são ignoradas por `message.id`.
- **Eco:** respostas enviadas pela própria ponte são registradas e ignoradas quando o WAHA devolve o evento `fromMe`.
- **Banco:** SQLite em `/data/memory.db`, WAL. Faça backup do volume.
- **Escala:** rode com **1 worker** (UVicorn). Para múltiplas réplicas, troque o SQLite por Postgres e o lock em memória por um lock distribuído.

## Limites conhecidos

- Áudio/imagem não são transcritos nem interpretados por padrão (`PROCESS_MEDIA=false`). A ponte responde pedindo texto.
- O resumo rolante depende de um endpoint OpenAI-compatible; se não estiver configurado, a janela continua funcionando sem resumo.
- O modo `responses` depende do suporte do Hermes e do limite de respostas armazenadas; o modo `chat` é o mais previsível.
- Grupos ficam desativados por padrão (`ALLOW_GROUPS=false`).
