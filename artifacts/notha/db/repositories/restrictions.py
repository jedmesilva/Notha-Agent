from db.connection import DB


class RestrictionRepository:
    def __init__(self, db: DB):
        self._db = db

    async def verificar(self, descricao: str) -> list[dict]:
        """Verifica se a descrição do produto bate com algum item restrito ativo.

        Usa duas estratégias:
        1. Qualquer palavra-chave da tabela aparece na descrição (ILIKE)
        2. Qualquer palavra da descrição aparece no array palavras_chave do banco

        Retorna lista de restrições encontradas (vazia = produto permitido).
        """
        rows = await self._db.fetch_all(
            """
            SELECT id, categoria, descricao AS descricao_restricao, motivo,
                   abrangencia, estado, municipio, palavras_chave
            FROM restricted_items
            WHERE ativo = TRUE
              AND EXISTS (
                SELECT 1 FROM unnest(palavras_chave) AS kw
                WHERE $1 ILIKE '%' || kw || '%'
              )
            ORDER BY categoria
            """,
            descricao,
        )
        return [dict(r) for r in rows]

    async def listar_categorias(self) -> list[dict]:
        """Lista todas as categorias restritas ativas (para uso administrativo)."""
        rows = await self._db.fetch_all(
            """
            SELECT id, categoria, descricao, motivo, abrangencia,
                   estado, municipio, ativo, criado_em, atualizado_em, criado_por
            FROM restricted_items
            ORDER BY categoria, id
            """
        )
        return [dict(r) for r in rows]

    async def adicionar(
        self,
        categoria: str,
        palavras_chave: list[str],
        motivo: str,
        descricao: str | None = None,
        abrangencia: str = "nacional",
        estado: str | None = None,
        municipio: str | None = None,
        criado_por: str = "admin",
    ) -> dict:
        row = await self._db.fetch_one(
            """
            INSERT INTO restricted_items
                (categoria, palavras_chave, descricao, motivo,
                 abrangencia, estado, municipio, criado_por)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            RETURNING *
            """,
            categoria,
            palavras_chave,
            descricao,
            motivo,
            abrangencia,
            estado,
            municipio,
            criado_por,
        )
        return dict(row)

    async def desativar(self, restriction_id: int) -> bool:
        result = await self._db.execute(
            "UPDATE restricted_items SET ativo = FALSE, atualizado_em = NOW() WHERE id = $1",
            restriction_id,
        )
        return result == "UPDATE 1"
