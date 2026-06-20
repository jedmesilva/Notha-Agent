import asyncpg
from db.connection import DB


class TransactionRepository:
    def __init__(self, db: DB):
        self._db = db

    async def create(
        self,
        negotiation_id: int,
        valor_produto: float,
        modalidade_entrega: str,
        chave_pix_vendedor: str,
        valor_entrega: float = 0,
        entregador_id: int | None = None,
        chave_pix_entregador: str | None = None,
        taxa_notha: float = 0,
    ) -> asyncpg.Record:
        return await self._db.fetch_one(
            """
            INSERT INTO transactions
                (negotiation_id, valor_produto, valor_entrega, taxa_notha, modalidade_entrega,
                 chave_pix_vendedor, chave_pix_entregador, entregador_id,
                 status, status_retencao)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'pendente', 'retido_aguardando_entrega')
            RETURNING *
            """,
            negotiation_id,
            valor_produto,
            valor_entrega,
            taxa_notha,
            modalidade_entrega,
            chave_pix_vendedor,
            chave_pix_entregador,
            entregador_id,
        )

    async def find_by_id(self, transaction_id: int) -> asyncpg.Record | None:
        return await self._db.fetch_one(
            "SELECT * FROM transactions WHERE id = $1", transaction_id
        )

    async def find_by_negotiation(self, negotiation_id: int) -> asyncpg.Record | None:
        return await self._db.fetch_one(
            "SELECT * FROM transactions WHERE negotiation_id = $1", negotiation_id
        )

    async def set_asaas_charge(self, transaction_id: int, asaas_charge_id: str) -> None:
        await self._db.execute(
            "UPDATE transactions SET asaas_charge_id = $1, status = 'cobranca_criada', updated_at = now() WHERE id = $2",
            asaas_charge_id,
            transaction_id,
        )

    async def set_paid(self, transaction_id: int) -> None:
        await self._db.execute(
            "UPDATE transactions SET status = 'pago', updated_at = now() WHERE id = $1",
            transaction_id,
        )

    async def set_retention_status(self, transaction_id: int, status_retencao: str, prazo_estorno_automatico=None) -> None:
        await self._db.execute(
            """
            UPDATE transactions SET
                status_retencao = $1,
                prazo_estorno_automatico = $2,
                updated_at = now()
            WHERE id = $3
            """,
            status_retencao,
            prazo_estorno_automatico,
            transaction_id,
        )

    async def set_transfer_ids(
        self,
        transaction_id: int,
        transfer_id_vendedor: str | None = None,
        transfer_id_entregador: str | None = None,
    ) -> None:
        await self._db.execute(
            """
            UPDATE transactions SET
                asaas_transfer_id_vendedor = COALESCE($1, asaas_transfer_id_vendedor),
                asaas_transfer_id_entregador = COALESCE($2, asaas_transfer_id_entregador),
                updated_at = now()
            WHERE id = $3
            """,
            transfer_id_vendedor,
            transfer_id_entregador,
            transaction_id,
        )

    async def find_pending_refunds(self) -> list[asyncpg.Record]:
        return await self._db.fetch_all(
            """
            SELECT * FROM transactions
            WHERE status_retencao = 'retido_aguardando_decisao_pos_falha'
              AND prazo_estorno_automatico < now()
            """
        )

    async def get_total_retained(self) -> float:
        val = await self._db.fetch_val(
            """
            SELECT COALESCE(SUM(valor_produto + valor_entrega), 0)
            FROM transactions
            WHERE status_retencao IN ('retido_aguardando_entrega', 'retido_aguardando_decisao_pos_falha')
            """
        )
        return float(val or 0)
