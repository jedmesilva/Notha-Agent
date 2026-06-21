"""
Conversation Agent — única interface de linguagem natural com humanos.

Responsabilidades:
  - Conversar com o usuário usando histórico completo + tools (function calling)
  - O LLM decide quando chamar cada ferramenta com base no contexto da conversa
  - O código executa deterministicamente o que o LLM decidiu chamar

NÃO decide preços, NÃO acessa Asaas, NÃO mantém memória própria.
"""
import json
import logging
from llm import get_provider
from tools.builtin import ALL_BUILTIN_TOOLS

logger = logging.getLogger("notha.agent.conversation")

_SANITIZE_PROMPT = (
    "Você é um revisor de mensagens de WhatsApp. "
    "Analise a mensagem abaixo e verifique se ela começa com uma saudação "
    "(exemplos: 'Oi!', 'Olá!', 'Ei!', 'Opa!', 'Salve!', 'E aí!', 'Hey!', 'Bom dia!', "
    "'Boa tarde!', 'Boa noite!', 'Oi Jed!', 'Olá Maria!', ou qualquer variação em qualquer idioma ou gíria). "
    "Se começar com saudação: remova apenas a saudação e retorne o restante da mensagem, "
    "com a primeira letra maiúscula. "
    "Se NÃO começar com saudação: retorne a mensagem exatamente como está, sem nenhuma alteração. "
    "Retorne SOMENTE a mensagem final, sem explicações."
)


async def _sanitize_response(text: str, has_history: bool) -> str:
    """Usa o LLM para detectar e remover saudações do início da resposta.

    Chamada leve (temperatura 0, poucos tokens) — só executa quando há histórico,
    garantindo que nenhuma saudação chegue ao usuário no meio de uma conversa,
    independente de idioma, gíria ou variação cultural.
    """
    if not has_history or not text:
        return text

    try:
        resp = await get_provider().complete(
            messages=[
                {"role": "system", "content": _SANITIZE_PROMPT},
                {"role": "user", "content": text},
            ],
            temperature=0.0,
            max_tokens=600,
        )
        sanitized = resp.text or text
        if sanitized != text:
            logger.warning("Saudação removida pelo revisor: %r → %r", text[:50], sanitized[:50])
        return sanitized
    except Exception as e:
        logger.error("Erro no revisor de saudação: %s", e)
        return text

SYSTEM_PROMPT = """Você é o NOTHA — agente de compra e venda de produtos físicos 100% pelo WhatsApp.

━━━ IDENTIDADE E TOM ━━━
- Nome: NOTHA
- Tom: direto, humano, informal mas profissional — como um amigo de confiança que entende de negócios
- Linguagem: detecte o idioma da mensagem do usuário e responda SEMPRE no mesmo idioma — se o usuário escrever em inglês, responda em inglês; se escrever em espanhol, responda em espanhol; etc.
- Se o idioma não puder ser determinado, use português brasileiro coloquial
- Independente do idioma, mantenha o tom: direto, humano, informal mas profissional
- Evite frases genéricas como "Claro!", "Com certeza!", "Posso te ajudar?"
- Seja objetivo: no máximo 3 frases curtas quando possível
- Use emojis com moderação (1-2 por mensagem no máximo) quando natural
- Nunca use markdown (asteriscos, hashtags) — o WhatsApp formata diferente

━━━ CUMPRIMENTOS — REGRA CRÍTICA ━━━
- Cumprimente SOMENTE se a mensagem do usuário for APENAS um cumprimento ("oi", "olá", "bom dia", "boa tarde", "boa noite") E o histórico de mensagens estiver COMPLETAMENTE vazio (primeira interação)
- Na primeira interação com cumprimento: responda de forma calorosa, apresente-se brevemente e convide o usuário a continuar. Ex: "Oi! Sou o NOTHA, aqui você compra e vende qualquer coisa pelo WhatsApp 📦 O que você está precisando?"
- NUNCA responda a um cumprimento com algo seco ou impaciente como "Direto ao ponto." ou "O que você precisa?" — isso soa grosseiro
- Se já há qualquer mensagem anterior no histórico: responda direto ao assunto, sem saudação alguma — mesmo que o usuário mande "oi" de novo
- Usar o nome no meio de uma resposta é permitido: "Não encontrei nenhum balcão em BH, Jed." — OK
- O que é absolutamente proibido é ABRIR qualquer frase com saudação NO MEIO de uma conversa: "Oi!", "Oi Jed!", "Olá!" — ERRADO
- ERRADO: "Oi! No momento não tem calça disponível."
- ERRADO: "Oi Jed! No momento não temos nenhum balcão disponível."
- CERTO: "No momento não tem calça disponível em Minas Gerais. Quer que eu te avise assim que aparecer uma?"
- CERTO: "Não encontrei nenhum balcão disponível em Belo Horizonte agora, Jed."

━━━ COMO CHAMAR O USUÁRIO ━━━
- Se o contexto tiver "apelido: X" ou "nome: X" → você PODE usar esse nome quando soar natural no meio de uma frase
- Não há obrigação de usar o nome — omitir é sempre válido
- Nunca invente um nome que não esteja no contexto

━━━ NOME vs APELIDO ━━━
- nome: nome legal/completo — coletado uma vez no cadastro, não peça de novo se já tiver
- apelido: como o usuário quer ser chamado — pode mudar a qualquer hora
  Ex: "pode me chamar de Zé", "me chama de Cris", "quero mudar meu apelido para Beta"
  → Quando o usuário pedir isso, chame atualizar_apelido imediatamente

━━━ VERIFICAÇÃO DE IDENTIDADE ━━━
- status_identidade vem no contexto: nao_verificado | em_analise | verificado | rejeitado
- Se o usuário enviar foto de RG/CNH/passaporte: informe que o documento foi recebido e está em análise
- Não exija verificação para comprar/vender — é um diferencial, não obrigação
- Se verificado(✓): pode mencionar isso como selo de confiança na conversa se relevante

━━━ REGRAS INEGOCIÁVEIS ━━━
1. NUNCA revele o preço mínimo do vendedor ao comprador
2. NUNCA revele o limite máximo do comprador ao vendedor
3. NUNCA prometa valor, prazo ou condição que o sistema não confirmou
4. NUNCA peça informação já confirmada nessa conversa (verifique o contexto antes de pedir)
5. NUNCA mencione "inteligência artificial", "LLM", "GPT" ou "algoritmo" — você é o NOTHA
6. Se perguntarem se você é robô: confirme que é sistema automatizado, sem detalhes técnicos
7. Conflito ou reclamação grave: oriente o usuário a responder "SUPORTE"

━━━ SOBRE PAGAMENTOS ━━━
- Pagamentos via Pix (QR Code ou chave Pix)
- Valor fica retido até confirmação de entrega por ambas as partes
- Taxa do NOTHA já inclusa — não detalhe o valor da taxa

━━━ COLETA DE DADOS ━━━
- Se ainda não tiver o nome do usuário: peça de forma natural na primeira oportunidade
- Se ainda não tiver o CPF: explique que é só para emitir comprovante, é seguro
- Ao pedir chave Pix: "Qual sua chave Pix para receber? Pode ser CPF, e-mail, celular ou chave aleatória."
- Ao pedir endereço de retirada (vendedor): "Me passa o endereço de retirada (rua, número, bairro e cidade)."

━━━ TRÊS CONCEITOS DE ENDEREÇO — NUNCA CONFUNDA ━━━
1. ENDEREÇO DO USUÁRIO (onde ele mora) — salvo em atualizar_localizacao
   - Colete naturalmente: "Em qual cidade e bairro você mora?"
   - Serve para entrega de produtos comprados pelo usuário
   - Fica salvo no perfil; não precisa perguntar de novo se já tiver

2. REGIÃO DE BUSCA (onde buscar produtos) — passado em buscar_produto
   - Pode ser QUALQUER cidade/bairro, não precisa ser onde o usuário mora
   - Pergunte SEMPRE antes de buscar: "Em qual cidade ou bairro você quer buscar?"
   - Se o usuário disser "aqui", "perto de mim" → use o endereço dele como referência
   - Não armazene a região de busca — ela muda a cada busca
   - Passe cidade_busca e/ou bairro_busca para buscar_produto; se o usuário não quiser filtrar, deixe vazio

3. ENDEREÇO DE RETIRADA DO PRODUTO — por produto, não por usuário
   - Cada produto tem seu próprio endereço onde pode ser retirado
   - O fluxo de cadastro de produto coleta isso durante o anúncio

━━━ USO DAS FERRAMENTAS ━━━
Você tem acesso a ferramentas. Use-as sempre que o usuário:
- Fornecer ou corrigir o nome completo → chame atualizar_nome
- Quiser mudar como é chamado / fornecer apelido → chame atualizar_apelido
- Fornecer ou corrigir o CPF → chame atualizar_cpf
- Fornecer cidade e/ou bairro onde MORA → chame atualizar_localizacao
- Quiser VENDER um produto: palavras como "vender", "anunciar", "quero vender", "tenho pra vender", "colocar à venda" → chame listar_produto IMEDIATAMENTE, sem perguntar nada antes
- Quiser COMPRAR/BUSCAR um produto: palavras como "preciso de", "quero comprar", "estou procurando", "tem à venda", "onde acho", "procuro" → NÃO chame listar_produto; siga EXATAMENTE esta ordem:
  1. Se a descrição do produto for vaga (ex: só "bolsa", só "celular"), pergunte detalhes relevantes em UMA mensagem curta (ex: "Que tipo de bolsa? Tem preferência de cor ou estilo?")
  2. Pergunte a região de busca: "Em qual cidade ou bairro você quer buscar?"
  3. Só então chame buscar_produto com a descrição completa e a região
  ATENÇÃO: passos 1 e 2 podem ser combinados em uma só mensagem se fizer sentido. Nunca pule a coleta de detalhes — a busca e qualquer alerta futuro dependem de uma boa descrição.
- ATENÇÃO: "preciso de X", "quero um X", "estou precisando de X" = COMPRA → buscar_produto. NUNCA confunda com venda.
- Fornecer chave Pix → chame atualizar_chave_pix
- Fornecer endereço de retirada geral (perfil vendedor) → chame atualizar_endereco
- Quiser ser avisado quando aparecer um produto ("me avisa", "quero ser notificado", "sim, pode me avisar") → chame salvar_interesse
- Quiser cancelar alertas de busca ("cancela alertas", "não precisa mais me avisar") → chame cancelar_alertas

REGRA CRÍTICA: Quando o usuário quiser vender, NÃO faça perguntas sobre o produto antes de chamar listar_produto. O fluxo de cadastro faz todas as perguntas necessárias. Chame a ferramenta imediatamente.

━━━ REGRA CRÍTICA — DADOS FACTUAIS ━━━
NUNCA invente preços, cotações, cálculos, datas ou qualquer dado factual.
Você OBRIGATORIAMENTE deve usar as ferramentas abaixo para qualquer dado factual:
- Preço de produto, valor de mercado, quanto custa algo → pesquisar_web
- Conversão entre moedas (dólar, euro, real, etc.) → converter_moeda
- Qualquer cálculo numérico (desconto, porcentagem, divisão, etc.) → calcular
- Conversão de unidades (kg, km, polegadas, etc.) → converter_unidades
- Data ou hora atual → obter_data_hora

Se você não chamar a ferramenta e inventar um valor, estará causando prejuízo real ao usuário.

Contexto atual do usuário (dados reais do banco):
{contexto}
"""

NOTHA_TOOLS = [tool.to_openai_schema() for tool in ALL_BUILTIN_TOOLS] + [
    {
        "type": "function",
        "function": {
            "name": "atualizar_nome",
            "description": (
                "Salva ou corrige o nome legal/completo do usuário. "
                "Use quando o usuário informa o nome pela primeira vez ou corrige um nome incorreto. "
                "Exemplos: 'meu nome é João Silva', 'me chamo Maria', 'na verdade meu nome é Carlos'. "
                "NÃO use para apelidos — para isso use atualizar_apelido."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "nome": {
                        "type": "string",
                        "description": "Nome completo/legal do usuário como ele informou"
                    }
                },
                "required": ["nome"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "atualizar_apelido",
            "description": (
                "Salva ou muda o apelido do usuário — como ele quer ser chamado. "
                "Use quando o usuário indicar preferência de como ser chamado, "
                "mesmo que já tenha nome cadastrado. Pode ser usado a qualquer momento. "
                "Exemplos: 'pode me chamar de Zé', 'me chama de Cris', "
                "'quero mudar meu apelido para Beta', 'me chama só de João'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "apelido": {
                        "type": "string",
                        "description": "Apelido ou forma preferida de ser chamado"
                    }
                },
                "required": ["apelido"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "atualizar_cpf",
            "description": "Salva ou corrige o CPF do usuário.",
            "parameters": {
                "type": "object",
                "properties": {
                    "cpf": {
                        "type": "string",
                        "description": "CPF informado pelo usuário (pode ter pontos e traço ou só dígitos)"
                    }
                },
                "required": ["cpf"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "listar_produto",
            "description": (
                "Inicia o fluxo completo de cadastro de um produto para venda. "
                "CHAME IMEDIATAMENTE quando o usuário expressar qualquer intenção de vender um produto, "
                "como 'quero vender', 'tenho um X para vender', 'quero anunciar', 'vendo um X'. "
                "NÃO tente coletar mais informações antes de chamar — o fluxo de cadastro "
                "conduzirá o usuário por todas as perguntas necessárias. "
                "NÃO faça mais perguntas sobre o produto antes de chamar esta ferramenta."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "descricao": {
                        "type": "string",
                        "description": "Descrição do produto mencionada pelo usuário (pode ser parcial)"
                    }
                },
                "required": ["descricao"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "buscar_produto",
            "description": (
                "Busca produtos disponíveis para compra. "
                "Antes de chamar: (1) colete detalhes do produto se a descrição for vaga, "
                "(2) pergunte em qual cidade ou bairro o usuário quer buscar. "
                "Passe sempre uma descricao_busca completa — ela será reutilizada se precisar salvar alerta. "
                "Se o usuário não quiser filtrar por região, omita cidade_busca e bairro_busca."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "categoria": {
                        "type": "string",
                        "description": "Categoria ou tipo do produto buscado"
                    },
                    "descricao_busca": {
                        "type": "string",
                        "description": "Descrição do que o usuário quer comprar"
                    },
                    "cidade_busca": {
                        "type": "string",
                        "description": "Cidade onde o usuário quer buscar produtos (ex: 'São Paulo', 'Belo Horizonte'). Deixe vazio para buscar em todo o Brasil."
                    },
                    "bairro_busca": {
                        "type": "string",
                        "description": "Bairro específico onde o usuário quer buscar (ex: 'Pinheiros', 'Savassi'). Use junto com cidade_busca quando possível."
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "salvar_interesse",
            "description": (
                "Salva um alerta de interesse: o usuário será notificado via WhatsApp "
                "assim que aparecer um produto compatível. "
                "Use quando o usuário confirmar que quer ser avisado após uma busca sem resultado, "
                "ou quando mencionar explicitamente 'me avisa', 'quero ser notificado', etc. "
                "IMPORTANTE: use a descrição já coletada na busca anterior — NÃO peça de novo ao usuário. "
                "Passe a descrição completa e a região informada na busca."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "descricao_busca": {
                        "type": "string",
                        "description": "O que o usuário está procurando (ex: 'mesa redonda de madeira', 'iPhone 14')"
                    },
                    "categoria": {
                        "type": "string",
                        "description": "Categoria do produto, se identificada"
                    },
                    "cidade_busca": {
                        "type": "string",
                        "description": "Cidade de interesse (opcional — se quiser receber alertas só de uma cidade)"
                    },
                    "bairro_busca": {
                        "type": "string",
                        "description": "Bairro de interesse (opcional)"
                    }
                },
                "required": ["descricao_busca"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cancelar_alertas",
            "description": (
                "Cancela todos os alertas de busca ativos do usuário. "
                "Use quando o usuário pedir para parar de receber notificações de produtos."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "atualizar_localizacao",
            "description": (
                "Salva a cidade e/ou bairro do usuário para buscas por região. "
                "Use quando o usuário informar onde mora ou sua cidade/bairro. "
                "Exemplos: 'moro em São Paulo, Pinheiros', 'sou de Campinas', 'meu bairro é Copacabana'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "cidade": {
                        "type": "string",
                        "description": "Cidade do usuário (ex: 'São Paulo', 'Campinas', 'Rio de Janeiro')"
                    },
                    "bairro": {
                        "type": "string",
                        "description": "Bairro do usuário (ex: 'Pinheiros', 'Copacabana', 'Savassi')"
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "atualizar_chave_pix",
            "description": "Salva a chave Pix do usuário para receber pagamentos.",
            "parameters": {
                "type": "object",
                "properties": {
                    "chave": {
                        "type": "string",
                        "description": "Chave Pix (CPF, e-mail, celular ou chave aleatória)"
                    }
                },
                "required": ["chave"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "atualizar_endereco",
            "description": "Salva o endereço de entrega ou retirada do usuário.",
            "parameters": {
                "type": "object",
                "properties": {
                    "endereco": {
                        "type": "string",
                        "description": "Endereço completo (rua, número, bairro, cidade, CEP)"
                    }
                },
                "required": ["endereco"]
            }
        }
    },
]


INTENT_EXTRACTION_PROMPT = """Você é um extrator de intenção para o sistema NOTHA de negociação de produtos via WhatsApp.

Analise a mensagem abaixo e extraia a intenção estruturada em JSON.

Mensagem do usuário: "{mensagem}"
Contexto atual: {contexto}

━━━ INSTRUÇÕES ━━━
- Retorne SOMENTE JSON válido, sem texto extra
- Se houver um valor monetário escrito por extenso (ex: "duzentos reais", "mil e quinhentos"), converta para número
- Se o usuário confirmar com "sim", "pode ser", "tá bom", "fechou", "aceito", "combinado", "ok" → intencao: "confirmacao", aceitou: true
- Se o usuário recusar com "não", "caro demais", "não quero", "desisto", "cancelar" → intencao: "recusa", aceitou: false
- Se houver um valor mencionado em contexto de oferta ou contraproposta, extraia o número

━━━ EXEMPLOS ━━━

Confirmação simples:
{{"intencao": "confirmacao", "aceitou": true}}

Recusa simples:
{{"intencao": "recusa", "aceitou": false, "motivo": "achou caro"}}

Oferta / contraproposta de preço:
{{"intencao": "contraproposta", "valor_estimado": 350.0, "confianca": "alta"}}

Confirmação de entrega pelo comprador:
{{"intencao": "confirmar_entrega", "recebeu": true}}

Confirmação de entrega pelo vendedor:
{{"intencao": "confirmar_entrega_vendedor", "entregou": true}}

Outro:
{{"intencao": "outro", "descricao": "usuário perguntou sobre horário de funcionamento"}}
"""


class ConversationAgent:

    async def get_tool_calls(
        self,
        contexto: str,
        history: list[dict],
        user_message: str,
        tools: list[dict],
    ) -> tuple[list[dict], list[dict]]:
        """Fase 1 do tool calling: envia mensagens e retorna as tool calls que o LLM quer fazer.

        Retorna (messages_so_far, tool_calls).
        messages_so_far deve ser passado para get_reply_after_tools junto com os resultados reais.
        """
        system = SYSTEM_PROMPT.format(contexto=contexto)
        messages: list[dict] = [{"role": "system", "content": system}]
        for h in history[-20:]:
            messages.append(h)
        messages.append({"role": "user", "content": user_message})

        try:
            resp = await get_provider().complete(
                messages=messages,
                tools=tools,
                temperature=0.6,
                max_tokens=500,
            )
        except Exception as e:
            logger.error("Erro no get_tool_calls: %s", e)
            return messages, []

        tool_calls: list[dict] = [
            {"id": tc.id, "name": tc.name, "arguments": tc.args}
            for tc in resp.tool_calls
        ]

        messages.append({
            "role": "assistant",
            "content": resp.text,
            **({"tool_calls": [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"])},
                }
                for tc in tool_calls
            ]} if tool_calls else {}),
        })

        return messages, tool_calls

    async def get_reply_after_tools(
        self,
        messages: list[dict],
        tool_results: dict[str, str],
    ) -> str:
        """Fase 2 do tool calling: gera a resposta final com os resultados das ferramentas.

        Os resultados são injetados no system prompt como contexto adicional — não como
        mensagens role:'tool'. Isso garante que a mensagem do usuário permaneça como
        último item da cadeia, preservando a continuidade conversacional e evitando
        que o LLM "reinicie" a conversa com saudações.

        tool_results: dict de tool_call_id → resultado descritivo (dados reais do banco).
        """
        tool_context = "\n\n━━━ DADOS OBTIDOS PELAS FERRAMENTAS ━━━\n"
        for result in tool_results.values():
            tool_context += result + "\n"
        tool_context += "━━━ FIM DOS DADOS ━━━"

        rebuilt: list[dict] = []
        for msg in messages:
            if msg["role"] == "system":
                rebuilt.append({"role": "system", "content": msg["content"] + tool_context})
            elif msg["role"] == "assistant" and msg.get("tool_calls"):
                continue
            else:
                rebuilt.append(msg)

        has_history = sum(1 for m in rebuilt if m["role"] == "user") > 1

        try:
            resp = await get_provider().complete(
                messages=rebuilt,
                temperature=0.6,
                max_tokens=500,
            )
            reply = resp.text or "Feito!"
            return await _sanitize_response(reply, has_history)
        except Exception as e:
            logger.error("Erro no get_reply_after_tools: %s", e)
            return "Feito!"

    async def chat_with_tools(
        self,
        contexto: str,
        history: list[dict],
        user_message: str,
        tools: list[dict] | None = None,
    ) -> tuple[str, list[dict]]:
        """Atalho para quando não há tools ou não se precisa das duas fases separadas."""
        system = SYSTEM_PROMPT.format(contexto=contexto)
        messages: list[dict] = [{"role": "system", "content": system}]
        for h in history[-20:]:
            messages.append(h)
        messages.append({"role": "user", "content": user_message})

        has_history = len(history) > 0
        try:
            resp = await get_provider().complete(
                messages=messages,
                tools=tools or None,
                temperature=0.6,
                max_tokens=500,
            )
        except Exception as e:
            logger.error("Erro no chat_with_tools: %s", e)
            return "Tive um problema técnico agora. Me manda de novo em instantes!", []

        reply = resp.text or "Tive um problema técnico."
        return await _sanitize_response(reply, has_history), []

    async def respond(
        self,
        phone: str,
        user_message: str,
        history: list[dict],
        role: str = "geral",
        produto_info: str = "nenhum produto em contexto",
        status_negociacao: str = "sem negociação ativa",
        usuario_nome: str = "não informado ainda",
    ) -> str:
        contexto = (
            f"Nome: {usuario_nome} | Papel: {role} | "
            f"Produto: {produto_info} | Negociação: {status_negociacao}"
        )
        text, _ = await self.chat_with_tools(
            contexto=contexto,
            history=history,
            user_message=user_message,
            tools=None,
        )
        return text

    async def extract_intent(self, mensagem: str, contexto: str = "geral") -> dict:
        prompt = INTENT_EXTRACTION_PROMPT.format(mensagem=mensagem, contexto=contexto)
        try:
            resp = await get_provider().complete(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=300,
                json_mode=True,
            )
            return json.loads(resp.text or "{}")
        except Exception as e:
            logger.error("Erro ao extrair intenção: %s", e)
            return {"intencao": "outro", "descricao": mensagem}

    async def speak(
        self,
        instrucao: str,
        history: list[dict] | None = None,
        contexto: str = "",
    ) -> str:
        """Gera resposta ao usuário com histórico e contexto completos.

        O backend informa *o que* comunicar via instrucao; o agente decide
        *como* falar, mantendo tom e continuidade da conversa.
        Substitui build_reply e ask_confirmation.
        """
        history = history or []
        system = SYSTEM_PROMPT.format(contexto=contexto or "sem contexto disponível")
        system += (
            "\n\n━━━ INSTRUÇÃO DO SISTEMA ━━━\n"
            f"{instrucao}\n"
            "Transforme em mensagem natural para WhatsApp. Não cite termos técnicos."
        )
        messages: list[dict] = [{"role": "system", "content": system}]
        for h in history[-20:]:
            messages.append(h)

        has_history = len(history) > 0
        try:
            resp = await get_provider().complete(
                messages=messages,
                temperature=0.6,
                max_tokens=500,
            )
            reply = resp.text or instrucao
            return await _sanitize_response(reply, has_history)
        except Exception as e:
            logger.error("Erro no speak: %s", e)
            return instrucao
