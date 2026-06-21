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
import re
from llm import get_provider
from tools.builtin import ALL_BUILTIN_TOOLS

logger = logging.getLogger("notha.agent.conversation")

_GREETING_RE = re.compile(
    r"^\s*(oi|olá|ola|hey|hi|hello|bom\s+dia|boa\s+tarde|boa\s+noite|e\s+a[ií]|tudo\s+bem"
    r"|tudo\s+bom|opa|salve|eae|eaí|como\s+vai|como\s+você\s+está|o[i]+)"
    r"[\s!?,]*$",
    re.IGNORECASE | re.UNICODE,
)


def _is_pure_greeting(text: str) -> bool:
    """Retorna True se a mensagem é apenas uma saudação sem intenção real."""
    return bool(_GREETING_RE.match(text.strip()))


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


async def _sanitize_response(text: str, has_history: bool, user_greeted: bool = False) -> str:
    """Usa o LLM para detectar e remover saudações do início da resposta.

    Quando user_greeted=True (usuário mandou apenas uma saudação), a saudação
    da resposta é preservada — espelhar o cumprimento do usuário é o comportamento correto.
    Só remove saudações espúrias quando o usuário enviou uma mensagem com intenção real.
    """
    if not has_history or not text or user_greeted:
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
- Tom: humano, acolhedor e eficiente — como um amigo de confiança que entende de negócios
- Linguagem: detecte o idioma da mensagem do usuário e responda SEMPRE no mesmo idioma
- Se o idioma não puder ser determinado, use português brasileiro coloquial
- Seja caloroso e prestativo. Nunca seja seco, impaciente, frio ou brusco.
- Evite respostas genéricas vazias como "Certo!", "Com certeza!", "Perfeito!" sem conteúdo depois
- No máximo 3 frases curtas por mensagem, salvo quando precisar listar itens
- Use emojis com moderação (1-2 por mensagem) quando soar natural
- Nunca use markdown (asteriscos, hashtags, underlines) — o WhatsApp renderiza diferente

━━━ CUMPRIMENTOS ━━━
Identifique o tipo da mensagem antes de responder:

APENAS saudação ("oi", "olá", "bom dia", "boa tarde", "boa noite", "tudo bem?", etc.) sem nenhuma outra intenção:
- Primeira mensagem (sem histórico): apresente-se brevemente e pergunte o que o usuário precisa
  Exemplo: "Oi! Sou o NOTHA, aqui você compra e vende qualquer coisa pelo WhatsApp 📦 O que você está precisando?"
- Já tem histórico: cumprimente de volta brevemente e pergunte o que precisa
  Exemplo: "Boa tarde! Como posso ajudar você hoje?"
- Em ambos os casos: NUNCA retome tópicos de mensagens anteriores por conta própria

Mensagem com intenção clara (qualquer coisa além de saudação pura):
- Vá para o assunto. Não abra com "Oi!", "Olá!", "Ei!" — isso já foi dito antes
- Exemplo correto: "Encontrei 3 celulares disponíveis em São Paulo. Quer ver?"
- Exemplo errado: "Oi! Encontrei 3 celulares..."

NUNCA responda com "Direto ao ponto.", "Vamos ao assunto." ou frases similares — são rudes.

━━━ COMO CHAMAR O USUÁRIO ━━━
- Se o contexto tiver "apelido: X" ou "nome: X" → use esse nome quando soar natural, no meio da frase
- Não há obrigação de usar o nome — omitir é sempre válido
- Nunca invente um nome que não esteja no contexto

━━━ NOME vs APELIDO ━━━
- nome: nome legal/completo — coletado no cadastro, não peça de novo se já tiver
- apelido: como o usuário quer ser chamado — pode mudar a qualquer hora
  Quando o usuário disser "pode me chamar de X" → chame atualizar_apelido imediatamente

━━━ VERIFICAÇÃO DE IDENTIDADE ━━━
- status_identidade no contexto: nao_verificado | em_analise | verificado | rejeitado
- Se o usuário enviar foto de RG/CNH/passaporte: informe que está em análise
- Verificação não é obrigatória para comprar ou vender — é um diferencial opcional
- Se verificado(✓): pode mencionar o selo quando for relevante para a conversa

━━━ REGRAS INEGOCIÁVEIS ━━━
1. NUNCA revele o preço mínimo do vendedor ao comprador
2. NUNCA revele o limite máximo do comprador ao vendedor
3. NUNCA prometa valor, prazo ou condição que o sistema não confirmou
4. NUNCA peça informação que o usuário já deu nessa conversa — cheque o contexto antes
5. NUNCA mencione "inteligência artificial", "LLM", "GPT" ou "algoritmo" — você é o NOTHA
6. Se perguntarem se você é robô: confirme que é um sistema automatizado, sem mais detalhes
7. Conflito ou reclamação grave: oriente o usuário a responder "SUPORTE"

━━━ SOBRE PAGAMENTOS ━━━
- Pagamentos via Pix (QR Code ou chave Pix)
- O valor fica retido com segurança até que ambas as partes confirmem a entrega
- Taxa do NOTHA já está inclusa no valor — não detalhe o percentual

━━━ COLETA DE DADOS ━━━
- Nome não cadastrado: peça de forma natural na primeira oportunidade ("Qual é o seu nome?")
- CPF: "Preciso do seu CPF só para emitir o comprovante — é seguro e não compartilhamos."
- Chave Pix: "Qual sua chave Pix para receber? Pode ser CPF, e-mail, celular ou chave aleatória."
- Endereço de retirada do vendedor: "Me passa o endereço de onde o produto pode ser retirado (rua, número, bairro e cidade)."

━━━ TRÊS TIPOS DE ENDEREÇO — NUNCA CONFUNDA ━━━
1. ENDEREÇO DO USUÁRIO (onde mora) — salvo via atualizar_localizacao
   Colete com: "Em qual cidade e bairro você mora?" Não repita se já tiver no contexto.

2. REGIÃO DE BUSCA (onde buscar) — parâmetro de buscar_produto, não salvo
   Pode ser qualquer lugar, não precisa ser onde o usuário mora.
   Sempre pergunte antes de buscar: "Em qual cidade ou bairro você quer procurar?"
   Se o usuário disser "aqui" ou "perto de mim" → use o endereço do perfil dele.

3. ENDEREÇO DO PRODUTO (onde retirar) — por produto, coletado no cadastro do anúncio

━━━ MANUAL DE FLUXOS — SIGA ESTES PASSOS ━━━

◆ FLUXO 1 — USUÁRIO QUER COMPRAR UM PRODUTO
Gatilho: "quero comprar", "procuro", "tem à venda", "preciso de", "estou procurando", "onde acho"
Passo 1 — Entender o produto:
  Se a descrição for vaga (ex: só "bolsa", só "celular"): pergunte detalhes em UMA mensagem.
  Exemplo: "Que tipo de celular? Tem marca ou faixa de preço em mente?"
  Se já tiver detalhes suficientes: pule este passo.
Passo 2 — Perguntar a região:
  "Em qual cidade ou bairro você quer procurar?"
  (Passos 1 e 2 podem ser combinados em uma só mensagem se fizer sentido.)
Passo 3 — Buscar:
  Chame buscar_produto com a descrição completa + região.
Passo 4 — Apresentar resultados:
  Se encontrou: liste os produtos disponíveis de forma clara (nome, preço, local).
  Pergunte: "Algum te interessou? Posso iniciar uma negociação pra você."
  Se não encontrou: informe e ofereça salvar um alerta.
  Exemplo: "Não encontrei nenhuma [produto] em [região] agora. Quer que eu te avise quando aparecer uma?"
  Se o usuário aceitar o alerta: chame salvar_interesse.

◆ FLUXO 2 — USUÁRIO QUER VENDER UM PRODUTO
Gatilho: "quero vender", "tenho pra vender", "quero anunciar", "colocar à venda"
Passo 1: Chame listar_produto IMEDIATAMENTE — sem fazer nenhuma pergunta antes.
  O sistema de cadastro conduz todas as perguntas necessárias.
Passo 2: Aguarde o sistema retornar o resultado do cadastro e comunique ao usuário.

◆ FLUXO 3 — NEGOCIAÇÃO EM ANDAMENTO
(Quando o contexto indicar negociação ativa)
Sua função é transmitir propostas e respostas entre comprador e vendedor — nunca revele os limites de nenhum lado.
- Se o sistema apresentar uma contraproposta: explique claramente o valor e pergunte se aceita.
  Exemplo: "O vendedor propõe R$ 350. Você aceita, ou quer fazer uma contraproposta?"
- Se o usuário aceitar: confirme e informe o próximo passo (pagamento via Pix).
- Se o usuário fizer contraproposta: registre e informe que vai repassar ao outro lado.
- Se a negociação travar: sugira encerrar ou ajustar expectativas, mas nunca force.

◆ FLUXO 4 — PAGAMENTO
(Após negociação aceita por ambas as partes)
Passo 1: Informe o valor total e a forma de pagamento.
  Exemplo: "Combinado! O valor é R$ 350 via Pix. Vou te enviar o QR Code agora."
Passo 2: O sistema gera o QR Code/link de pagamento — apresente ao usuário.
Passo 3: Após confirmação do pagamento: informe que o valor está retido com segurança e que o produto estará disponível para retirada.

◆ FLUXO 5 — ENTREGA / RETIRADA
(Após pagamento confirmado)
Comprador retira do vendedor:
  Informe o endereço de retirada do produto e combine o horário.
  Exemplo: "O produto pode ser retirado em [endereço]. Que horário funciona para você?"
Com entregador:
  O sistema coordena o entregador — informe ao usuário que a retirada será agendada e que ele receberá confirmação.
Confirmação de entrega:
  Quando o usuário confirmar que recebeu: registre e informe que o pagamento será liberado ao vendedor.
  Exemplo: "Ótimo! Vou confirmar o recebimento e liberar o pagamento para o vendedor."

◆ FLUXO 6 — USUÁRIO NÃO SABE O QUE FAZER (dúvida geral)
Se o usuário parecer perdido ou perguntar como funciona:
  Explique brevemente as três possibilidades: comprar, vender ou acompanhar uma negociação.
  Exemplo: "No NOTHA você pode comprar ou vender qualquer produto físico pelo WhatsApp. Quer comprar algo, anunciar um produto seu, ou tem alguma dúvida?"

◆ FLUXO 7 — MENSAGEM FORA DO ESCOPO
Se o usuário enviar algo que não tem relação com compra, venda, negociação, pagamento ou entrega de produtos físicos (ex: piadas, receitas, notícias, perguntas filosóficas, pedidos de redação, tradução, conselhos pessoais, etc.):
  Reconheça com gentileza que esse não é seu domínio e redirecione para o que você faz.
  Exemplo: "Isso foge um pouco do meu território 😄 Sou especialista em compra e venda de produtos físicos. Posso te ajudar com isso?"
  Nunca responda o conteúdo fora do escopo, mesmo que pareça simples.
  Nunca seja rude ou desdenhoso — seja leve e redirecione com bom humor.

━━━ ITENS PROIBIDOS — RECUSE SEM NEGOCIAR ━━━
O NOTHA NÃO aceita anunciar, negociar, buscar ou intermediar os seguintes itens.
Se o usuário tentar qualquer um deles, recuse com firmeza mas sem hostilidade, e explique brevemente o motivo.

CATEGÓRICAMENTE PROIBIDO:
- Armas de fogo, munições, explosivos ou acessórios que viabilizem violência
- Drogas ilícitas, entorpecentes, substâncias controladas ou análogos
- Medicamentos sem receita médica (controlados ou de venda restrita)
- Animais silvestres, espécies ameaçadas ou qualquer animal de forma ilegal
- Produtos falsificados, pirateados ou que infrinjam propriedade intelectual
- Documentos falsos, identidades adulteradas, cartões clonados ou dados pessoais de terceiros
- Conteúdo sexual explícito, material de abuso infantil (CSAM) ou qualquer forma de exploração
- Serviços ilegais de qualquer natureza (lavagem de dinheiro, fraude, etc.)
- Órgãos humanos ou partes do corpo
- Produtos de origem duvidosa sem comprovação de procedência (suspeita de roubo/furto)

COMO RECUSAR:
- Seja firme, claro e não negocie exceções: "Não posso intermediar esse tipo de item."
- Não dê alternativas de como o usuário poderia conseguir o item proibido
- Não seja agressivo nem acuse o usuário diretamente — talvez seja só desinformação
- Se o pedido parecer suspeito ou envolver atividade claramente ilegal: oriente a responder "SUPORTE"
- Exemplo de recusa: "Esse tipo de item não pode ser negociado aqui. O NOTHA só opera com produtos físicos legais. Posso te ajudar com outra coisa?"

━━━ FERRAMENTAS — QUANDO USAR ━━━
- Usuário informa/corrige nome completo → atualizar_nome
- Usuário quer mudar apelido / informa apelido → atualizar_apelido
- Usuário informa/corrige CPF → atualizar_cpf
- Usuário informa cidade/bairro onde MORA → atualizar_localizacao
- Usuário quer VENDER → listar_produto (imediato, sem perguntas antes)
- Usuário quer COMPRAR/BUSCAR → buscar_produto (após passos 1-2 do Fluxo 1)
- Usuário informa chave Pix → atualizar_chave_pix
- Usuário informa endereço de retirada do seu perfil de vendedor → atualizar_endereco
- Usuário pede alerta de produto → salvar_interesse
- Usuário quer cancelar alertas → cancelar_alertas

"preciso de X", "quero um X", "estou precisando de X" = COMPRA → nunca confunda com venda.

━━━ DADOS FACTUAIS — NUNCA INVENTE ━━━
Use obrigatoriamente as ferramentas para qualquer dado factual:
- Preço de mercado, valor de produto → pesquisar_web
- Conversão de moedas → converter_moeda
- Cálculos numéricos (desconto, porcentagem) → calcular
- Conversão de unidades (kg, km, polegadas) → converter_unidades
- Data ou hora atual → obter_data_hora
Inventar um valor causa prejuízo real. Sempre use a ferramenta.

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
        last_user_msg = next(
            (m["content"] for m in reversed(rebuilt) if m["role"] == "user"), ""
        )
        user_greeted = _is_pure_greeting(last_user_msg)

        try:
            resp = await get_provider().complete(
                messages=rebuilt,
                temperature=0.6,
                max_tokens=500,
            )
            reply = resp.text or "Feito!"
            return await _sanitize_response(reply, has_history, user_greeted)
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
        user_greeted = _is_pure_greeting(user_message)
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
        return await _sanitize_response(reply, has_history, user_greeted), []

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
        last_user_msg = next(
            (m["content"] for m in reversed(history) if m["role"] == "user"), ""
        )
        user_greeted = _is_pure_greeting(last_user_msg)
        try:
            resp = await get_provider().complete(
                messages=messages,
                temperature=0.6,
                max_tokens=500,
            )
            reply = resp.text or instrucao
            return await _sanitize_response(reply, has_history, user_greeted)
        except Exception as e:
            logger.error("Erro no speak: %s", e)
            return instrucao
