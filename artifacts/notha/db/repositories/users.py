import asyncpg
from db.connection import DB


class UserRepository:
    def __init__(self, db: DB):
        self._db = db

    async def find_by_phone(self, telefone: str) -> asyncpg.Record | None:
        return await self._db.fetch_one(
            """
            SELECT u.* FROM users u
            JOIN user_phone_numbers p ON p.user_id = u.id
            WHERE p.telefone = $1 AND p.ativo = TRUE
            """,
            telefone,
        )

    async def find_by_cpf(self, cpf: str) -> asyncpg.Record | None:
        return await self._db.fetch_one("SELECT * FROM users WHERE cpf = $1", cpf)

    async def find_by_id(self, user_id: int) -> asyncpg.Record | None:
        return await self._db.fetch_one("SELECT * FROM users WHERE id = $1", user_id)

    async def create(self, nome: str | None = None, cpf: str | None = None) -> asyncpg.Record:
        return await self._db.fetch_one(
            "INSERT INTO users (nome, cpf) VALUES ($1, $2) RETURNING *",
            nome,
            cpf,
        )

    async def update(self, user_id: int, nome: str | None = None, cpf: str | None = None) -> None:
        await self._db.execute(
            "UPDATE users SET nome = COALESCE($1, nome), cpf = COALESCE($2, cpf), updated_at = now() WHERE id = $3",
            nome,
            cpf,
            user_id,
        )

    async def add_phone(self, user_id: int, telefone: str) -> None:
        async with self._db._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "UPDATE user_phone_numbers SET ativo = FALSE WHERE user_id = $1",
                    user_id,
                )
                await conn.execute(
                    """
                    INSERT INTO user_phone_numbers (user_id, telefone, ativo)
                    VALUES ($1, $2, TRUE)
                    ON CONFLICT (telefone) DO UPDATE SET user_id = $1, ativo = TRUE
                    """,
                    user_id,
                    telefone,
                )

    async def find_or_create_by_phone(self, telefone: str) -> asyncpg.Record:
        """Busca usuário pelo telefone; cria registro vazio se for o primeiro contato."""
        existing = await self.find_by_phone(telefone)
        if existing:
            return existing
        return await self.create_with_phone(telefone)

    async def create_with_phone(self, telefone: str, nome: str | None = None) -> asyncpg.Record:
        async with self._db._pool.acquire() as conn:
            async with conn.transaction():
                user = await conn.fetchrow(
                    "INSERT INTO users (nome) VALUES ($1) RETURNING *", nome
                )
                await conn.execute(
                    "INSERT INTO user_phone_numbers (user_id, telefone, ativo) VALUES ($1, $2, TRUE)",
                    user["id"],
                    telefone,
                )
                return user

    async def get_seller_profile(self, user_id: int) -> asyncpg.Record | None:
        return await self._db.fetch_one(
            "SELECT * FROM seller_profile WHERE user_id = $1", user_id
        )

    async def get_buyer_profile(self, user_id: int) -> asyncpg.Record | None:
        return await self._db.fetch_one(
            "SELECT * FROM buyer_profile WHERE user_id = $1", user_id
        )

    async def get_courier_profile(self, user_id: int) -> asyncpg.Record | None:
        return await self._db.fetch_one(
            "SELECT * FROM courier_profile WHERE user_id = $1", user_id
        )

    async def upsert_seller_profile(
        self,
        user_id: int,
        endereco_retirada: str | None = None,
        horarios_disponiveis=None,
        chave_pix: str | None = None,
        chave_pix_titular_confirmado: str | None = None,
    ) -> None:
        import json
        await self._db.execute(
            """
            INSERT INTO seller_profile (user_id, endereco_retirada, horarios_disponiveis, chave_pix, chave_pix_titular_confirmado)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (user_id) DO UPDATE SET
                endereco_retirada = COALESCE($2, seller_profile.endereco_retirada),
                horarios_disponiveis = COALESCE($3, seller_profile.horarios_disponiveis),
                chave_pix = COALESCE($4, seller_profile.chave_pix),
                chave_pix_titular_confirmado = COALESCE($5, seller_profile.chave_pix_titular_confirmado)
            """,
            user_id,
            endereco_retirada,
            json.dumps(horarios_disponiveis) if horarios_disponiveis else None,
            chave_pix,
            chave_pix_titular_confirmado,
        )

    async def upsert_buyer_profile(self, user_id: int, endereco_entrega: str | None = None) -> None:
        await self._db.execute(
            """
            INSERT INTO buyer_profile (user_id, endereco_entrega)
            VALUES ($1, $2)
            ON CONFLICT (user_id) DO UPDATE SET
                endereco_entrega = COALESCE($2, buyer_profile.endereco_entrega)
            """,
            user_id,
            endereco_entrega,
        )

    async def upsert_courier_profile(
        self,
        user_id: int,
        chave_pix: str | None = None,
        chave_pix_titular_confirmado: str | None = None,
        regiao_atuacao: str | None = None,
    ) -> None:
        await self._db.execute(
            """
            INSERT INTO courier_profile (user_id, chave_pix, chave_pix_titular_confirmado, regiao_atuacao)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (user_id) DO UPDATE SET
                chave_pix = COALESCE($2, courier_profile.chave_pix),
                chave_pix_titular_confirmado = COALESCE($3, courier_profile.chave_pix_titular_confirmado),
                regiao_atuacao = COALESCE($4, courier_profile.regiao_atuacao)
            """,
            user_id,
            chave_pix,
            chave_pix_titular_confirmado,
            regiao_atuacao,
        )

    async def check_missing_fields(self, user_id: int, acao: str) -> dict:
        user = await self.find_by_id(user_id)
        if not user:
            return {"falta": ["nome", "cpf"], "motivo": "usuario_nao_encontrado"}

        if not user["nome"] or not user["cpf"]:
            faltantes = []
            if not user["nome"]:
                faltantes.append("nome")
            if not user["cpf"]:
                faltantes.append("cpf")
            return {"falta": faltantes, "motivo": "identificacao_minima"}

        if acao == "listar_produto":
            seller = await self.get_seller_profile(user_id)
            faltantes = []
            for campo in ["endereco_retirada", "horarios_disponiveis", "chave_pix"]:
                if not seller or not seller[campo]:
                    faltantes.append(campo)
            if faltantes:
                return {"falta": faltantes, "motivo": "perfil_vendedor_incompleto"}

        return {"falta": [], "motivo": None}
