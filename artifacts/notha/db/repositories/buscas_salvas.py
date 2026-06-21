import asyncpg
from db.connection import DB


class BuscaSalvaRepository:
    def __init__(self, db: DB):
        self._db = db

    async def criar(
        self,
        user_id: int,
        phone: str,
        descricao_busca: str,
        categoria: str | None = None,
        cidade_busca: str | None = None,
        bairro_busca: str | None = None,
    ) -> asyncpg.Record:
        """Salva uma busca de interesse do usuário."""
        return await self._db.fetch_one(
            """
            INSERT INTO buscas_salvas
                (user_id, phone, descricao_busca, categoria, cidade_busca, bairro_busca)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING *
            """,
            user_id, phone, descricao_busca, categoria, cidade_busca, bairro_busca,
        )

    async def listar_ativas(self) -> list[asyncpg.Record]:
        """Retorna todas as buscas ativas para verificar matches."""
        return await self._db.fetch_all(
            "SELECT * FROM buscas_salvas WHERE status = 'ativa' ORDER BY created_at ASC"
        )

    async def listar_por_user(self, user_id: int) -> list[asyncpg.Record]:
        return await self._db.fetch_all(
            "SELECT * FROM buscas_salvas WHERE user_id = $1 AND status = 'ativa' ORDER BY created_at DESC",
            user_id,
        )

    async def cancelar(self, busca_id: int) -> None:
        await self._db.execute(
            "UPDATE buscas_salvas SET status = 'cancelada' WHERE id = $1",
            busca_id,
        )

    async def cancelar_todas_do_user(self, user_id: int) -> None:
        await self._db.execute(
            "UPDATE buscas_salvas SET status = 'cancelada' WHERE user_id = $1 AND status = 'ativa'",
            user_id,
        )

    async def registrar_notificacao(self, busca_id: int) -> None:
        """Atualiza o timestamp da última notificação enviada."""
        from datetime import datetime, timezone
        await self._db.execute(
            "UPDATE buscas_salvas SET ultima_notificacao = $1 WHERE id = $2",
            datetime.now(timezone.utc), busca_id,
        )

    def matches(self, busca: asyncpg.Record, listing: dict) -> bool:
        """
        Verifica se um listing recém-criado bate com uma busca salva.

        Critérios:
        - Pelo menos uma palavra significativa da descricao_busca aparece
          no descricao ou categoria do listing (case-insensitive)
        - Se a busca tem cidade_busca: listing.cidade_vendedor deve ser compatível
          (ou cidade_vendedor é nulo/vazio, benefício da dúvida)
        """
        descricao_listing = (listing.get("descricao") or "").lower()
        categoria_listing = (listing.get("categoria") or "").lower()
        texto_listing = f"{descricao_listing} {categoria_listing}"

        # Palavras da busca com >= 3 letras (ignora artigos, preposições)
        _STOP = {"de", "da", "do", "das", "dos", "em", "um", "uma", "e", "ou", "para", "com"}
        palavras = [
            p for p in busca["descricao_busca"].lower().split()
            if len(p) >= 3 and p not in _STOP
        ]
        if not palavras:
            return False

        match_texto = any(p in texto_listing for p in palavras)
        if not match_texto:
            return False

        # Filtro geográfico: se busca tem cidade, o listing deve ser dessa cidade (ou sem cidade)
        cidade_busca = (busca["cidade_busca"] or "").lower().strip()
        cidade_listing = (listing.get("cidade_vendedor") or "").lower().strip()
        if cidade_busca and cidade_listing and cidade_busca not in cidade_listing and cidade_listing not in cidade_busca:
            return False

        return True
