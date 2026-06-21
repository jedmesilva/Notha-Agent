from db.connection import DB


class RestrictionRepository:
    def __init__(self, db: DB):
        self._db = db

    async def check(self, product_description: str) -> list[dict]:
        """Check whether a product description matches any active restricted item.

        Uses ILIKE matching: any keyword in the table is searched within the
        product description string. Returns a list of matched restrictions
        (empty list means the product is allowed).
        """
        rows = await self._db.fetch_all(
            """
            SELECT id, category, description, reason, scope, state_code, municipality, keywords
            FROM restricted_items
            WHERE is_active = TRUE
              AND EXISTS (
                SELECT 1 FROM unnest(keywords) AS kw
                WHERE $1 ILIKE '%' || kw || '%'
              )
            ORDER BY category
            """,
            product_description,
        )
        return [dict(r) for r in rows]

    async def search_by_terms(
        self,
        terms: list[str],
        state_code: str | None = None,
        municipality: str | None = None,
    ) -> list[dict]:
        """Busca restrições usando termos gerados pelo LLM.

        Para cada termo, verifica se ele:
          - está entre as keywords do item (ILIKE exato sobre cada keyword)
          - aparece na descrição do item
          - aparece no nome da categoria

        Aplica filtro de escopo (nacional / estadual / municipal) se localização informada.
        Retorna apenas registros realmente encontrados — nunca a lista completa.
        """
        if not terms:
            return []

        # Normaliza termos para lowercase
        terms_lower = [t.lower().strip() for t in terms if t.strip()]

        rows = await self._db.fetch_all(
            """
            SELECT DISTINCT
                ri.id, ri.category, ri.description, ri.reason,
                ri.scope, ri.state_code, ri.municipality
            FROM restricted_items ri
            WHERE ri.is_active = TRUE
              AND (
                -- Termo encontrado nas keywords cadastradas
                EXISTS (
                    SELECT 1 FROM unnest(ri.keywords) AS kw
                    WHERE EXISTS (
                        SELECT 1 FROM unnest($1::text[]) AS term
                        WHERE LOWER(kw) ILIKE '%' || term || '%'
                           OR term ILIKE '%' || LOWER(kw) || '%'
                    )
                )
                OR
                -- Termo encontrado na descrição do item
                EXISTS (
                    SELECT 1 FROM unnest($1::text[]) AS term
                    WHERE LOWER(ri.description) ILIKE '%' || term || '%'
                )
              )
              AND (
                -- Restrição nacional se aplica a todos
                ri.scope = 'national'
                -- Restrição estadual: só se localização fornecida e bate
                OR (ri.scope = 'state'      AND $2::text IS NOT NULL AND ri.state_code = $2)
                -- Restrição municipal: só se localização fornecida e bate
                OR (ri.scope = 'municipal'  AND $3::text IS NOT NULL AND ri.municipality ILIKE $3)
              )
            ORDER BY ri.category
            """,
            terms_lower,
            state_code,
            municipality,
        )
        return [dict(r) for r in rows]

    async def list_all(self) -> list[dict]:
        """List all restriction records (admin use)."""
        rows = await self._db.fetch_all(
            """
            SELECT id, category, description, reason, scope,
                   state_code, municipality, is_active,
                   created_at, updated_at, created_by
            FROM restricted_items
            ORDER BY category, id
            """
        )
        return [dict(r) for r in rows]

    async def add(
        self,
        category: str,
        keywords: list[str],
        reason: str,
        description: str | None = None,
        scope: str = "national",
        state_code: str | None = None,
        municipality: str | None = None,
        created_by: str = "admin",
    ) -> dict:
        """Insert a new restriction record."""
        row = await self._db.fetch_one(
            """
            INSERT INTO restricted_items
                (category, keywords, description, reason,
                 scope, state_code, municipality, created_by)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            RETURNING *
            """,
            category,
            keywords,
            description,
            reason,
            scope,
            state_code,
            municipality,
            created_by,
        )
        return dict(row)

    async def deactivate(self, restriction_id: int) -> bool:
        """Soft-delete a restriction by setting is_active = FALSE."""
        result = await self._db.execute(
            """
            UPDATE restricted_items
            SET is_active = FALSE, updated_at = NOW()
            WHERE id = $1
            """,
            restriction_id,
        )
        return result == "UPDATE 1"

    async def update_keywords(self, restriction_id: int, keywords: list[str]) -> bool:
        """Replace the keyword list for an existing restriction."""
        result = await self._db.execute(
            """
            UPDATE restricted_items
            SET keywords = $2, updated_at = NOW()
            WHERE id = $1
            """,
            restriction_id,
            keywords,
        )
        return result == "UPDATE 1"
