"""
Orchestrator — central message routing.

The LLM receives the full conversation history + available tools and decides
on its own when to call each tool. The code deterministically executes what
the LLM decided. Principle: LLM decides, code persists.
"""
import logging
import re
import time as _time
from db.connection import DB, get_db
from db.repositories import (
    UserRepository,
    ConversationRepository, PhoneInfoRepository,
)
from agents.conversation import ConversationAgent, NOTHA_TOOLS
from engine.turn_state import TurnStateService
from db.repositories.pending_confirmations import PendingConfirmationsRepository
from tools.builtin import web_search, currency, math, units, datetime_tool, restriction_check
from phone_info import parse_phone, get_timezone
from agents.reviewer import ScopeReviewerAgent
from tools.schema_validator import validate_understand, validate_assess


def _heuristic_steps(text: str) -> list[dict]:
    """Deterministic fallback planner for common user-data messages.

    Called when the LLM planner returns 0 steps but needs_tools=True.
    Uses simple regex matching to detect explicit user-provided data and
    returns the appropriate tool steps without an LLM call.

    Covers: name, gender, date-of-birth, street address, alerts, profile view.
    Does NOT cover product searches (those rely on the LLM planner working correctly).
    """
    t = text.strip()
    tl = t.lower()
    steps: list[dict] = []

    # ── Name ──────────────────────────────────────────────────────────────────
    name_match = re.search(
        r"(?:me\s+chamo|meu\s+nome\s+[eé]\s*[:\-]?|my\s+name\s+is|i(?:'|')?m\s+|mi\s+nombre\s+es)\s+([A-ZÁÉÍÓÚÂÊÎÔÛÃÕÀÈÌÒÙÇ][a-záéíóúâêîôûãõàèìòùç]+(?:\s+[A-ZÁÉÍÓÚÂÊÎÔÛÃÕÀÈÌÒÙÇ][a-záéíóúâêîôûãõàèìòùç]+){1,4})",
        t, re.IGNORECASE | re.UNICODE,
    )
    if name_match:
        steps.append({
            "step": len(steps) + 1,
            "tool": "update_name",
            "args": {"name": name_match.group(1).strip()},
            "reason": "heuristic: user stated their name",
            "user_message": None,
        })

    # ── Gender + DOB (combined into one update_profile call) ──────────────────
    profile_args: dict = {}

    gender_male = re.search(
        r"\b(sou\s+homem|soy\s+hombre|i(?:'m|\s+am)\s+male|masculino)\b", tl
    )
    gender_female = re.search(
        r"\b(sou\s+mulher|soy\s+mujer|i(?:'m|\s+am)\s+female|feminino)\b", tl
    )
    if gender_male:
        profile_args["gender"] = "M"
    elif gender_female:
        profile_args["gender"] = "F"

    dob_match = re.search(
        r"(?:nasci\s+em|data\s+de\s+nascimento|minha\s+data\s+[eé]|birthday\s+is|born\s+on|nacido\s+el)\s*[:\-]?\s*(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4})",
        tl,
    )
    if dob_match:
        raw = dob_match.group(1).replace("-", "/").replace(".", "/")
        parts = raw.split("/")
        if len(parts) == 3 and len(parts[2]) == 2:
            parts[2] = "19" + parts[2] if int(parts[2]) > 24 else "20" + parts[2]
        profile_args["date_of_birth"] = "/".join(parts)

    if profile_args:
        steps.append({
            "step": len(steps) + 1,
            "tool": "update_profile",
            "args": profile_args,
            "reason": "heuristic: user provided profile data",
            "user_message": None,
        })

    # ── Street address ─────────────────────────────────────────────────────────
    has_street = re.search(r"\b(rua|avenida|av\.|alameda|travessa|estrada|rodovia|praça|street|road|avenue)\b", tl)
    has_number = re.search(r"\b(?:n[°º]?\.?\s*|número\s*)(\d+)\b", tl)
    has_cep = re.search(r"(?:cep|zip|postal)\s*[:\-]?\s*(\d{4,8}[-\s]?\d{0,3})", tl)
    has_state_abbr = re.search(
        r"\b(ac|al|ap|am|ba|ce|df|es|go|ma|mt|ms|mg|pa|pb|pr|pe|pi|rj|rn|ro|rr|rs|sc|sp|se|to)\b",
        tl,
    )

    if has_street or has_cep:
        addr_args: dict = {}
        if has_street:
            street_m = re.search(
                r"(rua|avenida|av\.|alameda|travessa|estrada|rodovia|praça|street|road|avenue)\s+([\w\s]+?)(?:\s*,|\s+\d|\s*$)",
                tl, re.IGNORECASE,
            )
            if street_m:
                addr_args["street"] = street_m.group(0).split(",")[0].strip()
        if has_number:
            addr_args["street_number"] = has_number.group(1)
        if has_cep:
            addr_args["zip_code"] = re.sub(r"[\s\-]", "", has_cep.group(1))
        if has_state_abbr:
            addr_args["state"] = has_state_abbr.group(1).upper()
        if addr_args:
            steps.append({
                "step": len(steps) + 1,
                "tool": "update_full_address",
                "args": addr_args,
                "reason": "heuristic: user provided street address",
                "user_message": None,
            })

    # ── View profile ───────────────────────────────────────────────────────────
    if re.search(r"\b(ver\s+(meu\s+)?perfil|meus\s+dados|my\s+profile|show\s+profile|ver\s+meus\s+dados)\b", tl):
        steps.append({
            "step": len(steps) + 1,
            "tool": "get_my_profile",
            "args": {},
            "reason": "heuristic: user wants to see their profile",
            "user_message": None,
        })

    return steps


_BUILTIN_TOOL_MAP = {
    web_search.name:        web_search,
    currency.name:          currency,
    math.name:              math,
    units.name:             units,
    datetime_tool.name:     datetime_tool,
    restriction_check.name: restriction_check,
}

logger = logging.getLogger("notha.orchestrator")

# Conversation history persisted in DB via ConversationRepository.
# This dict is fallback only when the DB is unavailable.
_MEMORY_HISTORY: dict[str, list[dict]] = {}
_MAX_MEMORY = 20

PROCESSED_MESSAGE_IDS: set[str] = set()
MAX_PROCESSED_IDS = 1000

# Per-phone language store (ISO 639-1 code, e.g. "pt", "en", "es").
# Populated from understand() on every message and used by localize() to
# translate hardcoded system strings before they reach the user.
_USER_LANGUAGE: dict[str, str] = {}


async def localize(text: str, phone: str) -> str:
    """Translates an English template string into the user's detected language.

    Returns the original text unchanged if:
    - The user's language is unknown (not yet detected).
    - The target language is English (no translation needed).
    - The LLM call fails for any reason (safe fallback to original).
    """
    lang = _USER_LANGUAGE.get(phone)
    if not lang or lang == "en":
        return text
    try:
        from llm import get_provider
        resp = await get_provider().complete(
            messages=[{
                "role": "user",
                "content": (
                    f"Translate the following message naturally into the language "
                    f"with ISO 639-1 code '{lang}'. Preserve emojis and punctuation. "
                    f"Return ONLY the translation, nothing else:\n\n{text}"
                ),
            }],
            temperature=0.1,
            max_tokens=300,
        )
        return resp.text.strip() or text
    except Exception:
        return text


def _looks_like_cpf(text: str) -> bool:
    cleaned = re.sub(r"[\.\-\s]", "", text)
    return cleaned.isdigit() and len(cleaned) == 11


_INVALID_NAME_WORDS = {
    "oi", "olá", "ola", "opa", "ei", "hey",
    "sim", "não", "nao", "talvez", "ok", "okay",
    "tudo", "bem", "bom", "dia", "tarde", "noite",
    "boa", "boas", "certo", "claro", "pode", "vou",
    "quero", "queria", "preciso", "ajuda", "help",
    "alô", "alo", "eai", "eaí", "ae",
}


def _is_valid_name(name: str) -> bool:
    name = name.strip()
    if len(name) < 2 or len(name) > 60:
        return False
    if name.isdigit():
        return False
    words = name.lower().split()
    if not words:
        return False
    if all(w in _INVALID_NAME_WORDS for w in words):
        return False
    _NAME_PREPOSITIONS = {"de", "da", "do", "das", "dos", "e"}
    significant_words = [w for w in words if w not in _NAME_PREPOSITIONS]
    if not significant_words:
        return False
    if any(len(w) < 2 for w in significant_words):
        return False
    return True


def _detect_document_type(caption: str) -> str:
    """Infers document type from the caption sent with the image.

    Returns: 'national_id' | 'drivers_license' | 'passport' | 'unknown'
    """
    text = caption.lower()
    if any(p in text for p in ("rg", "identidade", "registro geral", "carteira de identidade")):
        return "national_id"
    if any(p in text for p in ("cnh", "habilitação", "habilitacao", "carteira de motorista")):
        return "drivers_license"
    if "passaporte" in text:
        return "passport"
    return "unknown"


def _memory_add(phone: str, role: str, content: str) -> None:
    """Adds to in-memory history (DB-unavailable fallback)."""
    hist = _MEMORY_HISTORY.setdefault(phone, [])
    hist.append({"role": role, "content": content})
    if len(hist) > _MAX_MEMORY:
        _MEMORY_HISTORY[phone] = hist[-_MAX_MEMORY:]


def _memory_get(phone: str) -> list[dict]:
    return _MEMORY_HISTORY.get(phone, [])


import asyncio as _asyncio

async def _gather(*coros):
    """Runs independent coroutines in parallel."""
    return await _asyncio.gather(*coros)


class Orchestrator:
    def __init__(self, db: DB | None = None):
        self._db = db
        self._conv = ConversationAgent()
        self._reviewer = ScopeReviewerAgent()

    def _repos(self, db: DB):
        return (
            UserRepository(db),
            ConversationRepository(db),
        )

    # Tools that may take a while and justify a "please wait" message.
    _SLOW_TOOLS = {"web_search"}

    # Wait message fallback by tool
    _WAIT_MSG_FALLBACK = {
        "web_search":     "🌐 Looking up information online, one moment...",
    }

    async def handle_message(self, phone: str, text: str, send_fn=None) -> str:
        """Processes the user message and returns the final reply.

        send_fn: optional coroutine (phone, text) → None.
        When provided, intermediate messages (e.g. "please wait, searching...")
        are proactively sent before slow tools without waiting for the user to ask again.
        """
        db = self._db or get_db()

        if db is None:
            return await self._no_db_fallback(phone, text)

        user_repo, conv_repo = self._repos(db)

        user    = await user_repo.find_or_create_by_phone(phone)
        user_id = user["id"]

        # ── Pluggy: Open Finance trigger ──────────────────────────────────────
        _PLUGGY_EXACT = ("open finance", "openfinance", "open banking")
        _PLUGGY_ACTIONS = ("conectar", "vincular", "ligar", "autorizar", "connect", "link")
        _PLUGGY_TARGETS = ("banco", "conta", "bancária", "bancario", "bank", "financeiro")
        _text_lower = text.strip().lower()
        _has_action = any(a in _text_lower for a in _PLUGGY_ACTIONS)
        _has_target = any(t in _text_lower for t in _PLUGGY_TARGETS)
        _pluggy_triggered = (
            any(kw in _text_lower for kw in _PLUGGY_EXACT)
            or (_has_action and _has_target)
        )
        if _pluggy_triggered:
            try:
                from pluggy_flow import initiate_bank_connection
                await initiate_bank_connection(phone=phone, user_id=user_id)
                _pluggy_reply = (
                    "🏦 Vou te enviar um link agora mesmo para conectar sua conta bancária!\n\n"
                    "_Aguarde a mensagem com o link logo abaixo_ 👇"
                )
                await conv_repo.add(user_id, "user", text)
                await conv_repo.add(user_id, "assistant", _pluggy_reply)
                return _pluggy_reply
            except Exception as _e:
                logger.error("Pluggy trigger failed for phone=%s: %s", phone, _e)

        # ── AuthUser: session check + re-authentication ───────────────────────
        from agents.auth_user import AuthUserAgent as _AuthUserAgent
        from db.repositories.sessions import SessionRepository as _SessionRepo
        import os as _os
        _session_repo = _SessionRepo(db)
        _auth_agent   = _AuthUserAgent()
        _session_ok, _auth_reply = await _auth_agent.check_and_handle(
            user=user, phone=phone, text=text,
            session_repo=_session_repo, user_repo=user_repo,
            base_url=_os.environ.get("APP_BASE_URL", ""),
        )
        if not _session_ok:
            if _auth_reply:
                await conv_repo.add(user_id, "user", text)
                await conv_repo.add(user_id, "assistant", _auth_reply)
            return _auth_reply or ""
        await _session_repo.touch(phone)

        # Parse phone number info on first contact
        phone_info_repo = PhoneInfoRepository(db)
        if await phone_info_repo.needs_parsing(phone):
            try:
                info = parse_phone(phone)
                await phone_info_repo.save(phone, info)
                logger.info(
                    "Phone info saved for user_id=%s: iso=%s country=%s region=%s tz=%s carrier=%s",
                    user_id, info.country_iso, info.country_name,
                    info.region, info.timezone, info.carrier,
                )
            except Exception as e:
                logger.warning("Could not save phone info for %s: %s", phone, e)

        # Load data in parallel
        history, phone_row, guardrail_ctx = await _gather(
            conv_repo.get_history(user_id),
            PhoneInfoRepository(db).get(phone),
            user_repo.build_guardrail_context(user_id),
        )

        # Rich context with real DB data
        context = self._build_context(
            user, phone=phone, phone_row=phone_row,
            guardrail_context=guardrail_ctx,
        )

        # ── Turn State: check for pending question from previous turn ─────────
        _ts_service = TurnStateService(db)
        _pending_turn = await _ts_service.get_pending(phone)
        _exhausted_field: str | None = None

        if _pending_turn and _ts_service.is_exhausted(_pending_turn):
            logger.info(
                "Circuit breaker: field=%s exhausted after %d attempt(s) — clearing",
                _pending_turn["pending_field"],
                _pending_turn.get("attempt_count", 0),
            )
            await _ts_service.clear(phone)
            _exhausted_field = _pending_turn["pending_field"]
            _pending_turn = None

        if _pending_turn:
            context = context + "\n\n" + _ts_service.build_context_note(_pending_turn)

        # ══════════════════════════════════════════════════════════════════════
        # 4-PHASE AGENTIC PIPELINE
        # ══════════════════════════════════════════════════════════════════════
        _USER_DATA_TOOLS = {
            "update_name", "update_nickname", "update_tax_id",
            "update_pix_key", "update_address", "update_location",
            "update_full_address", "update_profile",
            "get_my_profile",
        }

        _pipeline_start = _time.monotonic()

        # ── Phase 0: Understand ───────────────────────────────────────────────
        understanding = await self._conv.understand(
            user_message=text,
            history=history,
            context=context,
            pending_turn=_pending_turn,
        )
        understanding = validate_understand(understanding)
        objective   = understanding.get("objective", text)
        intent      = understanding.get("intent", "other")
        flow        = understanding.get("flow", "other")
        needs_tools = understanding.get("needs_tools", True)

        detected_lang = understanding.get("language", "")
        if detected_lang:
            _USER_LANGUAGE[phone] = detected_lang

        # ── Phase 1: Plan ─────────────────────────────────────────────────────
        steps = []
        if needs_tools:
            steps = self._deterministic_route(intent, flow, understanding, text)
            h_steps = _heuristic_steps(text)
            for h in h_steps:
                if not any(s["tool"] == h["tool"] for s in steps):
                    steps.append(h)

        if _pending_turn:
            p_res   = understanding.get("pending_resolution", "no")
            p_val   = understanding.get("pending_value", "")
            p_field = _pending_turn["pending_field"]
            if p_res == "yes" and p_val:
                p_step = self._pending_to_tool_step(p_field, p_val, _pending_turn)
                if p_step:
                    steps = [p_step] + steps
                    await _ts_service.clear(phone)
            elif p_res == "ambiguous":
                p_val = p_val or text.strip()
                p_step = self._pending_to_tool_step(p_field, p_val, _pending_turn)
                if p_step:
                    steps = [p_step] + steps
                    await _ts_service.clear(phone)

        # ── Phase 2: Execute ──────────────────────────────────────────────────
        results = []
        for i, step in enumerate(steps):
            t_name = step["tool"]
            t_args = step.get("args", {})
            
            wait_msg = step.get("user_message") or self._WAIT_MSG_FALLBACK.get(t_name)
            if wait_msg and send_fn:
                await send_fn(phone, await localize(wait_msg, phone))

            t_res, complex_reply = await self._execute_tool(
                name=t_name, args=t_args, user=user, user_repo=user_repo,
                history=history, context=context, text=text, phone=phone,
                pipeline_intent=intent, pipeline_objective=objective,
                step_number=i+1,
            )
            if complex_reply:
                return complex_reply
            results.append(f"Tool '{t_name}' result: {t_res}")

        # ── Phase 3: Synthesize ───────────────────────────────────────────────
        synthesis_context = context
        if results:
            synthesis_context += "\n\nTOOL RESULTS:\n" + "\n".join(results)

        final_reply, _ = await self._conv.chat_with_tools(
            contexto=synthesis_context,
            history=history,
            user_message=text,
            tools=NOTHA_TOOLS,
        )

        await self._maybe_set_turn_state(phone, final_reply, intent, flow, _ts_service, _pending_turn, _exhausted_field)
        await conv_repo.add(user_id, "user", text)
        await conv_repo.add(user_id, "assistant", final_reply)
        return final_reply

    async def _execute_tool(
        self, name: str, args: dict, user, user_repo,
        history, context, text, phone,
        pipeline_intent: str = "", pipeline_objective: str = "",
        step_number: int = 0,
    ) -> tuple[str, str | None]:
        db = self._db or get_db()

        if name == "update_name":
            new_name = args.get("name", "").strip()
            if _is_valid_name(new_name):
                await user_repo.update(user["id"], full_name=new_name)
                result = f"Name updated to '{new_name}'."
            else:
                result = f"Provided name '{new_name}' is invalid."
            return result, None

        if name == "update_nickname":
            nick = args.get("nickname", "").strip()
            await user_repo.update(user["id"], nickname=nick)
            return f"Nickname updated to '{nick}'.", None

        if name == "update_tax_id":
            tax_id = re.sub(r"[^\d]", "", args.get("tax_id", ""))
            if len(tax_id) == 11:
                await user_repo.update(user["id"], tax_id=tax_id)
                result = "CPF updated."
            else:
                result = "Invalid CPF format."
            return result, None

        if name == "update_pix_key":
            pix = args.get("pix_key", "").strip()
            await user_repo.update_pix_key(user["id"], pix)
            return f"Pix key updated to '{pix}'.", None

        if name == "update_location":
            city = args.get("city", "").strip()
            nb   = args.get("neighborhood", "").strip()
            await user_repo.update(user["id"], city=city or None, neighborhood=nb or None)
            return f"Location updated: city={city}, neighborhood={nb}.", None

        if name == "get_my_profile":
            profile = await user_repo.get_full_profile(user["id"])
            if not profile:
                return "Profile not found.", None
            parts = []
            if profile.get("full_name"): parts.append(f"Nome: {profile['full_name']}")
            if profile.get("nickname"):  parts.append(f"Apelido: {profile['nickname']}")
            if profile.get("tax_id"):    parts.append(f"CPF: {profile['tax_id']}")
            result = "User profile data:\n" + "\n".join(parts)
            return result, None

        if name == "update_profile":
            await user_repo.update_profile(
                user["id"],
                gender=args.get("gender"),
                date_of_birth=args.get("date_of_birth"),
                preferred_language=args.get("preferred_language"),
            )
            return "Profile updated.", None

        if name == "update_full_address":
            await user_repo.update_full_address(
                user["id"],
                street=args.get("street"),
                street_number=args.get("street_number"),
                neighborhood=args.get("neighborhood"),
                city=args.get("city"),
                state=args.get("state"),
                country=args.get("country"),
                zip_code=args.get("zip_code"),
            )
            return "Address updated.", None

        if name in _BUILTIN_TOOL_MAP:
            try:
                result = await _BUILTIN_TOOL_MAP[name].execute(**args)
            except Exception as e:
                result = f"ERROR: {e}"

            if name == "check_restriction" and result.startswith("ALLOWED"):
                result = (
                    f"{result}\n\n"
                    "SYSTEM: Product cleared. Do NOT send any message to the user. "
                    "Immediately call relevant tool right now."
                )
            return result, None

        logger.warning("Unknown tool called by LLM: %s", name)
        return f"unknown tool '{name}'", None

    async def _no_db_fallback(self, phone: str, text: str) -> str:
        messages, _ = await self._conv.get_tool_calls(
            contexto="no database available — memory-only mode",
            history=_memory_get(phone),
            user_message=text,
            tools=NOTHA_TOOLS,
        )
        last_assistant = next(
            (m["content"] for m in reversed(messages) if m["role"] == "assistant"),
            "I had a technical problem. Please try again in a moment!",
        )
        _memory_add(phone, "user", text)
        _memory_add(phone, "assistant", last_assistant)
        return last_assistant

    def _build_context(
        self, user,
        phone: str = "", phone_row=None, guardrail_context: str = "",
    ) -> str:
        parts = []
        parts.append(f"name: {user.get('full_name') or 'not set'}")
        parts.append(f"nickname: {user.get('nickname') or 'not set'}")
        parts.append(f"tax_id: {'registered' if user.get('tax_id') else 'not registered'}")
        parts.append(f"user_id: {user.get('id')}")
        if guardrail_context:
            parts.append(guardrail_context)
        return " | ".join(parts)

    async def handle_media(self, phone: str, media_id: str, mime_type: str, caption: str) -> str:
        from whatsapp import download_media_as_base64
        from agents.vision import ImageAnalysisAgent

        db = self._db or get_db()
        if db is None:
            return await localize("I received your image but I'm having a technical issue right now.", phone)

        user_repo, conv_repo = self._repos(db)
        user = await user_repo.find_or_create_by_phone(phone)
        
        try:
            _data_uri = await download_media_as_base64(media_id, mime_type)
        except Exception as e:
            return await localize("I received your image but couldn't load it.", phone)

        vision_agent = ImageAnalysisAgent()
        clf = await vision_agent.classify_image(_data_uri)

        if not clf.error:
            if clf.image_type == "identity_document":
                return await self.handle_identity_document(phone, media_id, mime_type, caption, data_uri=_data_uri)
            elif clf.image_type == "selfie":
                from agents.auth_user import AuthUserAgent
                from db.repositories.sessions import SessionRepository
                auth_agent = AuthUserAgent()
                session_repo = SessionRepository(db)
                session = await session_repo.get_active(phone)
                if session and session.get("reauth_tier") == "selfie":
                    import base64
                    media_bytes = base64.b64decode(_data_uri.split(",")[-1])
                    ok, reply = await auth_agent._handle_selfie_tier(
                        user=dict(user), phone=phone, session=dict(session),
                        session_repo=session_repo, user_repo=user_repo,
                        media_bytes=media_bytes, media_mime=mime_type,
                        attempts=session.get("reauth_attempts", 0)
                    )
                    return reply

        return await self.handle_product_photo(phone, media_id, mime_type, caption, user, db, user_repo, conv_repo, _data_uri)

    async def handle_product_photo(self, phone, media_id, mime_type, caption, user, db, user_repo, conv_repo, data_uri) -> str:
        return await localize("I received your photo. How can I help with it?", phone)

    async def handle_identity_document(self, phone, media_id, mime_type, caption, detected_doc_type=None, data_uri=None) -> str:
        from storage.identity import process_identity_document
        db = self._db or get_db()
        user_repo, _ = self._repos(db)
        user = await user_repo.find_or_create_by_phone(phone)
        doc_type = detected_doc_type or _detect_document_type(caption or "")
        await process_identity_document(user_id=user["id"], media_id=media_id, doc_type=doc_type, user_repo=user_repo, run_ocr=True, data_uri=data_uri)
        return await localize("Document received and is under review.", phone)

    def _deterministic_route(self, intent: str, flow: str, understanding: dict, text: str) -> list[dict]:
        objective = understanding.get("objective", text)
        if intent == "info":
            return [{"step": 1, "tool": "web_search", "args": {"query": objective}, "reason": "info request"}]
        return []

    def _pending_to_tool_step(self, field: str, value: str, pending: dict) -> dict | None:
        _MAP = {"full_name": "update_name", "nickname": "update_nickname", "tax_id": "update_tax_id", "pix_key": "update_pix_key"}
        tool = _MAP.get(field)
        if tool:
            return {"step": 0, "tool": tool, "args": {field.replace("full_", ""): value}, "reason": "turn state resolution"}
        return None

    async def _maybe_set_turn_state(self, phone, reply, intent, flow, ts_service, current_pending, exhausted_field=None) -> None:
        if current_pending: return
        text_lower = reply.lower()
        field = None
        if "nome" in text_lower: field = "full_name"
        elif "cpf" in text_lower: field = "tax_id"
        if field and field != exhausted_field:
            await ts_service.set_pending(phone, field, f"{flow}/{intent}")

    async def reset(self, phone: str) -> None:
        db = self._db or get_db()
        _MEMORY_HISTORY.pop(phone, None)
        if db:
            await PendingConfirmationsRepository(db).clear(phone)
            ts_service = TurnStateService(db)
            await ts_service.clear(phone)
            user_repo, conv_repo = self._repos(db)
            user = await user_repo.find_by_phone(phone)
            if user: await conv_repo.clear(user["id"])
