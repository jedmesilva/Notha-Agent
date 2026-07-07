"""
InvestorMatchingEngine — seleção, distribuição e notificação de investidores.

Fluxo após create_opportunity:
  1. match_and_notify(db, opportunity_id, ...)
     ├── Busca candidatos via InvestorProfileRepository.find_candidates()
     ├── Enriquece com saldo real da wallet
     ├── Calcula score de compatibilidade
     ├── Distribui amount_needed (first-fit decreasing por score)
     ├── auto_invest=True  → accept_investment() imediato
     └── auto_invest=False → cria investment_offer + WhatsApp

  2. Investidor responde no WhatsApp
     └── process_offer_response(db, offer_id, user_id, action, custom_amount)

Princípio: distribuição e scoring são 100% determinísticos, nunca via LLM.
"""
import logging
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timezone, timedelta

logger = logging.getLogger("notha.investor_matching")

_ZERO      = Decimal("0")
OFFER_TTL_HOURS = 24   # oferta expira em 24 horas


# ── Scoring ───────────────────────────────────────────────────────────────────

def _compute_score(profile: dict, capacity: Decimal, amount_needed: Decimal) -> float:
    """
    Score de 0.0 a 1.0 para ranking de candidatos.

    Pesos:
      40% — capacidade: quanto do amount_needed o candidato pode cobrir
      30% — histórico: total_invested_lifetime normalizado (teto = R$100k)
      20% — utilização da faixa de prazo: quanto mais centrado, melhor (não usado aqui,
             pois o filtro de prazo já foi aplicado na query — todos os candidatos
             estão dentro da faixa)
      10% — bonus auto_invest (prioridade operacional)
    """
    cap_score  = min(float(capacity / amount_needed), 1.0) if amount_needed > _ZERO else 0.0
    history    = float(profile.get("total_invested_lifetime") or 0)
    hist_score = min(history / 100_000, 1.0)
    auto_bonus = 0.10 if profile.get("auto_invest") else 0.0

    return cap_score * 0.40 + hist_score * 0.30 + auto_bonus


# ── Distribuição ──────────────────────────────────────────────────────────────

def _allocate(candidates: list[dict], amount_needed: Decimal) -> list[dict]:
    """
    First-fit decreasing por score.

    Para cada candidato (ordenado por score desc):
      suggested = min(capacity, remaining)
      inclui somente se suggested ≥ min_investment_amount do perfil

    Retorna lista com campo 'suggested_amount' preenchido.
    """
    remaining  = amount_needed
    result     = []

    for c in sorted(candidates, key=lambda x: x["score"], reverse=True):
        if remaining <= _ZERO:
            break
        capacity   = c["capacity"]
        min_amount = Decimal(str(c.get("min_investment_amount") or 50))
        if capacity < min_amount:
            continue

        suggested = min(capacity, remaining).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if suggested < min_amount:
            continue

        result.append({**c, "suggested_amount": suggested})
        remaining -= suggested

    return result


# ── Notificação WhatsApp ──────────────────────────────────────────────────────

async def _send_offer_notification(
    phone: str,
    offer_id: int,
    suggested_amount: Decimal,
    expires_at: datetime,
    opp: dict,
    maturity_at: datetime,
) -> None:
    from whatsapp import send_message

    rate_pct   = float(opp.get("expected_rate", 0)) * 100
    group_name = opp.get("group_name") or f"Grupo #{opp.get('group_id', '?')}"
    mat_str    = maturity_at.strftime("%d/%m/%Y %H:%M") if maturity_at else "—"
    exp_str    = expires_at.strftime("%d/%m %H:%M") if expires_at else "—"

    text = (
        f"📊 *Nova oportunidade de investimento!*\n\n"
        f"  Fundo: *{group_name}*\n"
        f"  Valor reservado para você: *R$ {suggested_amount:,.2f}*\n"
        f"  Taxa: {rate_pct:.2f}% a.a.\n"
        f"  Vencimento: {mat_str}\n\n"
        f"Selecionamos você com base no seu perfil de investidor. 🎯\n\n"
        f"Responda aqui:\n"
        f"  ✅ *confirmar* — aceitar R$ {suggested_amount:,.2f}\n"
        f"  ✏️ *alterar 200* — investir outro valor (ex: R$ 200)\n"
        f"  ❌ *recusar* — não investir agora\n\n"
        f"⏰ Oferta válida até {exp_str}.\n"
        f"_Referência: oferta #{offer_id}_"
    )
    await send_message(phone, text)


# ── Ponto de entrada ──────────────────────────────────────────────────────────

async def match_and_notify(
    db,
    opportunity_id: int,
    group_id: int,
    amount_needed: Decimal,
    expected_rate: Decimal,
    maturity_at: datetime,
    risk_level: str = "moderate",
) -> dict:
    """
    Seleciona investidores compatíveis, distribui o valor e notifica.

    Para auto_invest=True  → accept_investment() chamado imediatamente.
    Para auto_invest=False → cria investment_offer + envia mensagem WhatsApp.

    Retorna: {total_allocated, coverage_pct, auto_invested, notified, underfunded}
    """
    from db.repositories.investor_profiles import InvestorProfileRepository
    from db.repositories.wallets import WalletRepository
    from db.repositories.opportunities import OpportunityRepository

    profile_repo = InvestorProfileRepository(db)
    wallet_repo  = WalletRepository(db)
    opp_repo     = OpportunityRepository(db)

    now = datetime.now(timezone.utc)
    maturity_tz = (
        maturity_at if maturity_at.tzinfo
        else maturity_at.replace(tzinfo=timezone.utc)
    )
    term_days = max(int((maturity_tz - now).total_seconds() / 86400), 1)

    # Usuários que já têm participação ou oferta nesta oportunidade
    already = await db.fetch_all(
        """
        SELECT investor_user_id AS uid FROM investments WHERE opportunity_id = $1
        UNION
        SELECT user_id AS uid FROM investment_offers
        WHERE opportunity_id = $1 AND status = 'pending'
        """,
        opportunity_id,
    )
    exclude = [r["uid"] for r in already]

    # Candidatos compatíveis
    candidates = await profile_repo.find_candidates(
        group_id=group_id,
        maturity_days=term_days,
        risk_level=risk_level,
        exclude_user_ids=exclude,
    )

    if not candidates:
        logger.info("match_and_notify opp=%d: sem candidatos compatíveis.", opportunity_id)
        return {"total_allocated": _ZERO, "coverage_pct": 0.0,
                "auto_invested": 0, "notified": 0, "underfunded": True}

    # Enriquece com saldo e capacidade reais
    enriched = []
    for p in candidates:
        wallet   = await wallet_repo.get_or_create("user", p["user_id"])
        balance  = await wallet_repo.true_balance(wallet["id"])
        max_cap  = p.get("max_investment_amount")
        capacity = (
            min(balance, Decimal(str(max_cap)))
            if max_cap is not None else balance
        )
        if capacity <= _ZERO:
            continue
        enriched.append({
            **dict(p),
            "wallet_id":      wallet["id"],
            "wallet_balance": balance,
            "capacity":       capacity,
            "score":          _compute_score(p, capacity, amount_needed),
        })

    if not enriched:
        logger.info("match_and_notify opp=%d: candidatos sem saldo suficiente.", opportunity_id)
        return {"total_allocated": _ZERO, "coverage_pct": 0.0,
                "auto_invested": 0, "notified": 0, "underfunded": True}

    allocations     = _allocate(enriched, amount_needed)
    total_allocated = sum(a["suggested_amount"] for a in allocations)
    coverage_pct    = float(total_allocated / amount_needed * 100) if amount_needed > _ZERO else 0.0

    opp        = await opp_repo.get_by_id(opportunity_id)
    opp_dict   = dict(opp) if opp else {}
    expires_at = now + timedelta(hours=OFFER_TTL_HOURS)

    auto_count   = 0
    notify_count = 0

    for alloc in allocations:
        user_id   = alloc["user_id"]
        suggested = alloc["suggested_amount"]
        phone     = alloc.get("phone") or ""

        if alloc.get("auto_invest"):
            # ── Investimento automático ────────────────────────────────────
            from engine.investment_engine import accept_investment
            try:
                res = await accept_investment(
                    db=db,
                    opportunity_id=opportunity_id,
                    investor_user_id=user_id,
                    amount=suggested,
                    maturity_at=maturity_tz,
                )
                if res.get("ok"):
                    auto_count += 1
                    logger.info(
                        "auto_invest: user=%d opp=%d amount=R$%.2f inv=%d",
                        user_id, opportunity_id, float(suggested), res["investment_id"],
                    )
                    if phone:
                        from whatsapp import send_message
                        try:
                            await send_message(
                                phone,
                                f"✅ Investimento automático realizado!\n\n"
                                f"  Oportunidade #{opportunity_id}\n"
                                f"  Valor: R$ {suggested:,.2f}\n"
                                f"  Juros no vencimento: R$ {res['interest_at_maturity']:,.2f}\n"
                                f"  Vencimento: {maturity_tz.strftime('%d/%m/%Y')}\n\n"
                                f"Use *consultar_investimentos* para ver sua posição. 📊",
                            )
                        except Exception:
                            pass
                else:
                    logger.warning(
                        "auto_invest falhou user=%d opp=%d: %s",
                        user_id, opportunity_id, res.get("error"),
                    )
            except Exception as exc:
                logger.error("auto_invest erro user=%d opp=%d: %s", user_id, opportunity_id, exc)

        else:
            # ── Oferta pendente + notificação WhatsApp ─────────────────────
            try:
                offer_id = await db.fetch_val(
                    """
                    INSERT INTO investment_offers
                        (opportunity_id, user_id, group_id, suggested_amount,
                         maturity_at, expires_at, status)
                    VALUES ($1, $2, $3, $4, $5, $6, 'pending')
                    ON CONFLICT (opportunity_id, user_id) WHERE status = 'pending' DO NOTHING
                    RETURNING id
                    """,
                    opportunity_id, user_id, group_id,
                    suggested, maturity_tz, expires_at,
                )
                if offer_id:
                    if phone:
                        await _send_offer_notification(
                            phone=phone,
                            offer_id=offer_id,
                            suggested_amount=suggested,
                            expires_at=expires_at,
                            opp=opp_dict,
                            maturity_at=maturity_tz,
                        )
                        await db.execute(
                            "UPDATE investment_offers SET message_sent_at = NOW() WHERE id = $1",
                            offer_id,
                        )
                    notify_count += 1
                    logger.info(
                        "offer criada: id=%d user=%d opp=%d amount=R$%.2f",
                        offer_id, user_id, opportunity_id, float(suggested),
                    )
            except Exception as exc:
                logger.error(
                    "offer notify erro user=%d opp=%d: %s",
                    user_id, opportunity_id, exc,
                )

    logger.info(
        "match_and_notify opp=%d: alocado=R$%.2f (%.1f%%) auto=%d notificados=%d",
        opportunity_id, float(total_allocated), coverage_pct, auto_count, notify_count,
    )
    return {
        "total_allocated": total_allocated,
        "coverage_pct":    coverage_pct,
        "auto_invested":   auto_count,
        "notified":        notify_count,
        "underfunded":     total_allocated < amount_needed,
    }


# ── Processar resposta do investidor ──────────────────────────────────────────

async def process_offer_response(
    db,
    offer_id: int,
    user_id: int,
    action: str,                       # 'accept' | 'modify' | 'decline'
    custom_amount: Decimal | None = None,
) -> dict:
    """
    Processa a resposta de um investidor a uma oferta pendente.

      accept  → investe o valor sugerido na oferta
      modify  → investe custom_amount (validado contra limites do perfil)
      decline → marca oferta como recusada
    """
    now = datetime.now(timezone.utc)

    # ── CAS atômico: tenta reclamar a oferta antes de fazer qualquer operação ──
    # Um único UPDATE ... WHERE status='pending' AND expires_at > NOW() garante
    # que dois requests concorrentes não aceitem a mesma oferta simultaneamente.
    if action == "decline":
        claimed = await db.fetch_val(
            """
            UPDATE investment_offers
               SET status = 'declined', responded_at = NOW()
             WHERE id = $1 AND user_id = $2
               AND status = 'pending' AND expires_at > NOW()
            RETURNING id
            """,
            offer_id, user_id,
        )
        if not claimed:
            row = await db.fetch_one(
                "SELECT status FROM investment_offers WHERE id = $1 AND user_id = $2",
                offer_id, user_id,
            )
            if not row:
                return {"ok": False, "error": "Oferta não encontrada ou não pertence a você."}
            return {"ok": False, "error": f"Oferta indisponível (status: {row['status']})."}
        return {"ok": True, "action": "declined", "message": "Oferta recusada com sucesso."}

    # Para accept / modify: atômica transição pending → processing antes de investir
    claimed = await db.fetch_val(
        """
        UPDATE investment_offers
           SET status = 'processing', responded_at = NOW()
         WHERE id = $1 AND user_id = $2
           AND status = 'pending' AND expires_at > NOW()
        RETURNING id
        """,
        offer_id, user_id,
    )
    if not claimed:
        row = await db.fetch_one(
            "SELECT status FROM investment_offers WHERE id = $1 AND user_id = $2",
            offer_id, user_id,
        )
        if not row:
            return {"ok": False, "error": "Oferta não encontrada ou não pertence a você."}
        status = row["status"]
        if status == "processing":
            return {"ok": False, "error": "Esta oferta já está sendo processada. Aguarde."}
        return {"ok": False, "error": f"Oferta indisponível (status: {status})."}

    # Carrega dados completos (perfil + oferta) após a posse da oferta
    row = await db.fetch_one(
        """
        SELECT o.*, ip.min_investment_amount, ip.max_investment_amount
        FROM investment_offers o
        LEFT JOIN investor_profiles ip ON ip.user_id = o.user_id
        WHERE o.id = $1
        """,
        offer_id,
    )

    if not row:
        return {"ok": False, "error": "Oferta não encontrada ou não pertence a você."}

    # accept ou modify
    final_amount = (
        Decimal(str(custom_amount))
        if action == "modify" and custom_amount is not None
        else Decimal(str(row["suggested_amount"]))
    )

    min_inv = Decimal(str(row["min_investment_amount"] or 50))
    max_inv = row.get("max_investment_amount")

    if final_amount < min_inv:
        return {"ok": False, "error": f"Valor mínimo do seu perfil: R$ {min_inv:,.2f}."}
    if max_inv and final_amount > Decimal(str(max_inv)):
        return {"ok": False, "error": f"Valor máximo do seu perfil: R$ {Decimal(str(max_inv)):,.2f}."}

    from engine.investment_engine import accept_investment
    result = await accept_investment(
        db=db,
        opportunity_id=row["opportunity_id"],
        investor_user_id=user_id,
        amount=final_amount,
        maturity_at=row["maturity_at"],
    )

    if result.get("ok"):
        await db.execute(
            """
            UPDATE investment_offers SET
                status        = 'accepted',
                final_amount  = $2,
                investment_id = $3
            WHERE id = $1
            """,
            offer_id, final_amount, result["investment_id"],
        )
        return {
            "ok":                   True,
            "action":               "accepted",
            "investment_id":        result["investment_id"],
            "final_amount":         final_amount,
            "interest_at_maturity": result["interest_at_maturity"],
            "maturity_at":          result["maturity_at"],
        }

    # Reverte status processing → pending para que o investidor possa tentar novamente
    await db.execute(
        "UPDATE investment_offers SET status = 'pending' WHERE id = $1 AND status = 'processing'",
        offer_id,
    )
    return {"ok": False, "error": result.get("error", "Erro ao registrar investimento.")}
