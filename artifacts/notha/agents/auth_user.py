"""
AuthUser — User authentication and session management agent.

Intercepts every incoming message to verify the user's identity before
routing to domain agents. Manages session lifecycle and multi-tier
re-authentication based on inactivity period.

Re-auth tiers (based on inactivity since last_activity_at):
  < 7 days    → active, no re-auth
  7–30 days   → CPF confirmation (lightweight, low friction)
  30–90 days  → Selfie holding identity document (GPT-4o vision comparison)
  > 90 days   → Verification link with face-api.js liveness check in browser
"""
import base64
import json
import logging
import re

logger = logging.getLogger("notha.agent.auth_user")


class AuthUserAgent:

    async def check_and_handle(
        self,
        user: dict,
        phone: str,
        text: str,
        session_repo,
        user_repo,
        base_url: str = "",
        media_bytes: bytes | None = None,
        media_mime: str = "",
    ) -> tuple[bool, str | None]:
        """
        Main entry point — called before any domain agent for every message.

        Returns:
          (True, None)         → session valid, proceed with normal pipeline.
          (False, reply_str)   → session blocked; reply_str should be sent to user.
        """
        session = await session_repo.get_session(phone)

        # ── First contact: no session yet ────────────────────────────────────
        if session is None:
            await session_repo.create_session(user["id"], phone)
            logger.info("New session created — phone=%s user_id=%s", phone, user["id"])
            return True, None

        # ── Revoked session ──────────────────────────────────────────────────
        if session["status"] == "revoked":
            logger.warning("Revoked session access attempt — phone=%s", phone)
            return False, (
                "⛔ Sua conta está temporariamente bloqueada por tentativas de "
                "acesso inválidas. Entre em contato com o suporte para regularizar."
            )

        # ── Session pending re-auth: route to appropriate handler ────────────
        if session["status"] == "pending_reauth":
            return await self._handle_reauth(
                user=user, phone=phone, text=text, session=session,
                session_repo=session_repo, user_repo=user_repo,
                base_url=base_url,
                media_bytes=media_bytes, media_mime=media_mime,
            )

        # ── Active session: check inactivity ─────────────────────────────────
        tier = session_repo.determine_tier(session)
        if tier is None:
            return True, None  # still fresh — orchestrator calls touch() after this

        # Inactivity threshold exceeded — transition to re-auth
        days = session_repo.inactivity_days(session)
        logger.info(
            "Session inactivity (%.0f days) → tier=%s — phone=%s", days, tier, phone
        )
        updated = await session_repo.start_reauth(phone, tier)
        session = updated or session
        session["status"] = "pending_reauth"
        session["reauth_tier"] = tier
        session["reauth_attempts"] = 0

        prompt = await self._reauth_prompt(tier, user, phone, session, session_repo, base_url)
        return False, prompt

    async def _handle_reauth(
        self,
        user: dict, phone: str, text: str, session: dict,
        session_repo, user_repo,
        base_url: str,
        media_bytes: bytes | None = None,
        media_mime: str = "",
    ) -> tuple[bool, str | None]:
        """Processes user input during an active re-auth flow."""
        tier     = session.get("reauth_tier", "cpf")
        attempts = session.get("reauth_attempts", 0)

        if tier == "cpf":
            return await self._handle_cpf_tier(
                user=user, phone=phone, text=text,
                session_repo=session_repo, attempts=attempts,
            )

        if tier == "selfie":
            if media_bytes:
                return await self._handle_selfie_tier(
                    user=user, phone=phone, session=session,
                    session_repo=session_repo, user_repo=user_repo,
                    media_bytes=media_bytes, media_mime=media_mime,
                    attempts=attempts,
                )
            # User sent text when we expect a photo
            return False, (
                "🤳 Para confirmar sua identidade, envie uma selfie segurando seu "
                "documento de identidade (RG, CNH ou passaporte) com o rosto e o "
                "documento visíveis na mesma foto."
            )

        if tier == "link":
            # Re-generate the link whenever user messages anything
            token = await session_repo.create_verification_token(
                session["id"], user["id"], phone
            )
            verify_url = f"{base_url}/verificar/{token}"
            return False, (
                f"🔐 Para confirmar sua identidade após um longo período sem atividade, "
                f"acesse o link abaixo e siga as instruções de verificação facial:\n\n"
                f"🔗 {verify_url}\n\n"
                f"_O link expira em 15 minutos. Após a verificação, você poderá "
                f"continuar normalmente por aqui._"
            )

        # Unknown tier — just let through (fail-open for safety)
        logger.error("Unknown reauth tier=%s for phone=%s — letting through", tier, phone)
        await session_repo.complete_reauth(phone)
        return True, None

    async def _handle_cpf_tier(
        self,
        user: dict, phone: str, text: str,
        session_repo, attempts: int,
    ) -> tuple[bool, str | None]:
        """Validates CPF input for the 7–30 day inactivity tier."""
        raw        = re.sub(r"[\.\-\s]", "", text.strip())
        stored_cpf = re.sub(r"[\.\-\s]", "", user.get("tax_id") or "")

        if raw.isdigit() and len(raw) == 11 and stored_cpf and raw == stored_cpf:
            await session_repo.complete_reauth(phone)
            logger.info("CPF re-auth successful — phone=%s", phone)
            name = user.get("nickname") or (user.get("full_name") or "").split()[0] or "você"
            return True, f"✅ Identidade confirmada! Bem-vindo de volta, {name}! Como posso ajudar?"

        # Wrong CPF (or user hasn't sent CPF yet — first interaction after reauth prompt)
        if not (raw.isdigit() and len(raw) == 11):
            # Likely first reply to our prompt — give them instructions
            return False, (
                "Por favor, informe seu CPF cadastrado (apenas os 11 números, "
                "sem pontos ou traços) para continuar."
            )

        # Wrong CPF number
        new_attempts = await session_repo.increment_reauth_attempts(phone)
        if new_attempts >= session_repo.MAX_REAUTH_ATTEMPTS:
            await session_repo.revoke(phone)
            logger.warning("CPF re-auth failed — session revoked — phone=%s", phone)
            return False, (
                "⛔ Número de tentativas excedido. Por segurança, sua conta foi bloqueada. "
                "Entre em contato com o suporte para desbloquear."
            )

        remaining = session_repo.MAX_REAUTH_ATTEMPTS - new_attempts
        return False, (
            f"❌ CPF incorreto. Informe o CPF cadastrado nesta conta.\n"
            f"({remaining} tentativa{'s' if remaining > 1 else ''} restante{'s' if remaining > 1 else ''})"
        )

    async def _handle_selfie_tier(
        self,
        user: dict, phone: str, session: dict,
        session_repo, user_repo,
        media_bytes: bytes, media_mime: str, attempts: int,
    ) -> tuple[bool, str | None]:
        """Compares the received selfie+document against the stored identity document via GPT-4o vision."""
        from db.connection import get_db
        from llm import get_provider

        db = get_db()
        if not db:
            return False, "❌ Erro interno. Tente novamente em instantes."

        doc = await db.fetch_one(
            """
            SELECT image_url FROM identity_documents
            WHERE user_id = $1
              AND status IN ('approved', 'under_review')
            ORDER BY created_at DESC
            LIMIT 1
            """,
            user["id"],
        )

        if not doc or not doc["image_url"]:
            # No document on file — fall back to CPF tier
            await db.execute(
                "UPDATE sessions SET reauth_tier = 'cpf' WHERE phone = $1 AND status = 'pending_reauth'",
                phone,
            )
            return False, (
                "Não encontramos um documento de identidade cadastrado em sua conta. "
                "Por favor, informe seu CPF para confirmar sua identidade."
            )

        selfie_b64 = base64.b64encode(media_bytes).decode()
        media_type = media_mime if media_mime.startswith("image/") else "image/jpeg"

        try:
            provider = get_provider()
            resp = await provider.complete(
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "You are a facial verification assistant for a financial security system.\n"
                                "Compare the two images below:\n"
                                "• Image 1: Identity document (RG, CNH or passport) registered previously.\n"
                                "• Image 2: New selfie sent by the user, supposedly holding their identity document.\n\n"
                                "Analyze carefully:\n"
                                "1. Is a real human face clearly visible in Image 2?\n"
                                "2. Does the person in Image 2 appear to be the same as in Image 1?\n"
                                "3. Is the person in Image 2 physically holding a document (not a screen photo)?\n\n"
                                'Respond ONLY with valid JSON:\n'
                                '{"face_detected":true/false,"faces_match":true/false,'
                                '"holding_document":true/false,"confidence":0.0-1.0,"reason":"brief"}'
                            ),
                        },
                        {"type": "image_url", "image_url": {"url": doc["image_url"]}},
                        {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{selfie_b64}"}},
                    ],
                }],
                temperature=0.0,
                max_tokens=200,
            )
            result_text = resp.text.strip()
            json_match  = re.search(r"\{.*\}", result_text, re.DOTALL)
            result      = json.loads(json_match.group()) if json_match else {}

            faces_match  = result.get("faces_match", False)
            face_detected = result.get("face_detected", False)
            confidence   = float(result.get("confidence", 0))

            logger.info(
                "Selfie re-auth vision — phone=%s face=%s match=%s conf=%.2f",
                phone, face_detected, faces_match, confidence,
            )

            if faces_match and face_detected and confidence >= 0.70:
                await session_repo.complete_reauth(phone)
                name = user.get("nickname") or (user.get("full_name") or "").split()[0] or "você"
                return True, f"✅ Identidade confirmada! Bem-vindo de volta, {name}! Como posso ajudar?"

        except Exception as exc:
            logger.error("Selfie re-auth vision error — phone=%s: %s", phone, exc)

        # Failed
        new_attempts = await session_repo.increment_reauth_attempts(phone)
        if new_attempts >= session_repo.MAX_REAUTH_ATTEMPTS:
            await session_repo.revoke(phone)
            return False, (
                "⛔ Não conseguimos confirmar sua identidade após várias tentativas. "
                "Por segurança, sua conta foi bloqueada. Entre em contato com o suporte."
            )

        remaining = session_repo.MAX_REAUTH_ATTEMPTS - new_attempts
        return False, (
            f"❌ Não conseguimos confirmar sua identidade pela foto enviada. "
            f"Certifique-se de que seu rosto e o documento estejam visíveis e bem iluminados.\n"
            f"({remaining} tentativa{'s' if remaining > 1 else ''} restante{'s' if remaining > 1 else ''})"
        )

    async def _reauth_prompt(
        self,
        tier: str, user: dict, phone: str, session: dict,
        session_repo, base_url: str,
    ) -> str:
        """Returns the initial re-auth prompt for the user."""
        name = user.get("nickname") or (user.get("full_name") or "").split()[0] or "você"

        if tier == "cpf":
            return (
                f"👋 Bem-vindo de volta, {name}! "
                f"Por segurança, após alguns dias sem atividade precisamos confirmar que é você. "
                f"Por favor, informe seu CPF cadastrado (apenas números)."
            )

        if tier == "selfie":
            return (
                f"👋 Bem-vindo de volta, {name}! "
                f"Faz um tempo que não nos vemos. Para confirmar sua identidade, "
                f"envie uma selfie segurando seu documento de identidade (RG, CNH ou passaporte) "
                f"com o rosto e o documento visíveis na mesma foto."
            )

        # link tier
        token = await session_repo.create_verification_token(
            session["id"], user["id"], phone
        )
        verify_url = f"{base_url}/verificar/{token}"
        return (
            f"👋 Bem-vindo de volta! Após um longo período sem atividade, "
            f"precisamos verificar sua identidade por reconhecimento facial.\n\n"
            f"Acesse o link abaixo e siga as instruções (expira em 15 minutos):\n"
            f"🔗 {verify_url}\n\n"
            f"_Após a verificação, volte aqui e continue normalmente._"
        )

    async def handle_verification_result(
        self,
        token: str,
        success: bool,
        result: dict,
        session_repo,
        send_fn=None,
    ) -> dict:
        """
        Called by the /verificar/{token}/submit endpoint when the browser
        sends the facial recognition result.

        Returns {"ok": bool, "message": str}.
        """
        from db.connection import get_db
        db = get_db()
        if not db:
            return {"ok": False, "message": "Erro interno — tente novamente."}

        pv = await session_repo.get_pending_verification(token)
        if not pv:
            return {"ok": False, "message": "Link inválido ou expirado."}

        phone = pv["phone"]
        await session_repo.complete_verification(token, success, result)

        if success:
            await session_repo.complete_reauth(phone)
            logger.info("Link-based re-auth successful — phone=%s", phone)
            message = "✅ Identidade verificada com sucesso! Volte ao WhatsApp para continuar."
            if send_fn:
                try:
                    await send_fn(
                        phone,
                        "✅ Verificação facial concluída! Bem-vindo de volta. Como posso ajudar?",
                    )
                except Exception as exc:
                    logger.warning("Could not send re-auth confirmation to %s: %s", phone, exc)
        else:
            new_attempts = await session_repo.increment_reauth_attempts(phone)
            if new_attempts >= session_repo.MAX_REAUTH_ATTEMPTS:
                await session_repo.revoke(phone)
                message = "⛔ Número de tentativas excedido. Sua conta foi bloqueada."
                if send_fn:
                    try:
                        await send_fn(
                            phone,
                            "⛔ Não conseguimos verificar sua identidade. "
                            "Por segurança, sua conta foi bloqueada. "
                            "Entre em contato com o suporte.",
                        )
                    except Exception:
                        pass
            else:
                remaining = session_repo.MAX_REAUTH_ATTEMPTS - new_attempts
                message = f"❌ Verificação não concluída. Tente novamente ({remaining} tentativa(s) restante(s))."

        return {"ok": success, "message": message}
