"""Contract clauses in Postgres: full-text search for exact words (clause numbers, item
and vendor names) and pgvector for meaning, including across Indonesian and English."""

import re
from collections.abc import Sequence

import psycopg

from lunas.models import Contract, ContractClause

TABLE = "contract_clauses"
# Postgres text-search configuration per contract language: stemming and stopwords.
TEXT_SEARCH_CONFIG = {"id": "indonesian", "en": "english"}
_COLUMNS = "clause_id, contract_id, vendor_id, number, article, article_title, text"


CLAUSE_WORD = {"id": "Pasal", "en": "Clause"}


def chunk_text(contract: Contract, clause: ContractClause) -> str:
    """What is searched and embedded for a clause: its text under a short header naming the
    vendor, the clause number and the article, so a clause can be found by who it is with,
    where it is, and what it is about."""
    word = CLAUSE_WORD[contract.language]
    return f"{contract.vendor_name}. {word} {clause.number} {clause.article_title}. {clause.text}"


class ClauseStore:
    def __init__(self, database_url: str, table: str = TABLE) -> None:
        if not re.fullmatch(r"[a-z_][a-z0-9_]*", table):
            raise ValueError(f"invalid table name {table!r}")
        self.table = table
        self._connection = psycopg.connect(database_url, autocommit=True)

    def close(self) -> None:
        self._connection.close()

    def reset(self, dimensions: int) -> None:
        """Drop and recreate the index table. It holds derived data only."""
        with self._connection.cursor() as cursor:
            cursor.execute("CREATE EXTENSION IF NOT EXISTS vector")
            cursor.execute(f"DROP TABLE IF EXISTS {self.table}")
            cursor.execute(
                f"""
                CREATE TABLE {self.table} (
                    clause_id text PRIMARY KEY,
                    contract_id text NOT NULL,
                    vendor_id text NOT NULL,
                    number text NOT NULL,
                    article text NOT NULL,
                    article_title text NOT NULL,
                    text text NOT NULL,
                    chunk text NOT NULL,
                    language regconfig NOT NULL,
                    search tsvector GENERATED ALWAYS AS (to_tsvector(language, chunk)) STORED,
                    embedding vector({int(dimensions)}) NOT NULL
                )
                """
            )
            cursor.execute(f"CREATE INDEX ON {self.table} USING gin (search)")
            cursor.execute(f"CREATE INDEX ON {self.table} (vendor_id)")

    def add(self, contract: Contract, embeddings: Sequence[Sequence[float]]) -> None:
        rows = [
            (
                clause.clause_id,
                clause.contract_id,
                clause.vendor_id,
                clause.number,
                clause.article,
                clause.article_title,
                clause.text,
                chunk_text(contract, clause),
                TEXT_SEARCH_CONFIG[contract.language],
                _vector(vector),
            )
            for clause, vector in zip(contract.clauses, embeddings, strict=True)
        ]
        with self._connection.cursor() as cursor:
            cursor.executemany(
                f"INSERT INTO {self.table} ({_COLUMNS}, chunk, language, embedding) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::regconfig, %s::vector)",
                rows,
            )

    def keyword_search(
        self, query: str, vendor_id: str | None = None, limit: int = 20
    ) -> list[ContractClause]:
        """Any query word may match (OR), ranked by term frequency. The query is parsed
        with both language configurations because its language is not known."""
        # Keeps clause numbers such as 5.1 whole, as the Postgres parser does.
        words = re.findall(r"\w+(?:\.\w+)*", query.lower())
        if not words:
            return []
        terms = " | ".join(words)
        return self._select(
            "search @@ q",
            "ts_rank(search, q) DESC",
            "(SELECT to_tsquery('indonesian', %(terms)s) || to_tsquery('english', %(terms)s)"
            " AS q) AS query",
            {"terms": terms},
            vendor_id,
            limit,
        )

    def vector_search(
        self, vector: Sequence[float], vendor_id: str | None = None, limit: int = 20
    ) -> list[ContractClause]:
        """Exact cosine distance. A few thousand clauses need no approximate index."""
        return self._select(
            "TRUE",
            "embedding <=> %(vector)s::vector",
            None,
            {"vector": _vector(vector)},
            vendor_id,
            limit,
        )

    def contract_language(self, vendor_id: str) -> str | None:
        """'id' or 'en' for the vendor's contract, or None if it has none indexed."""
        with self._connection.cursor() as cursor:
            cursor.execute(
                f"SELECT language::text FROM {self.table} WHERE vendor_id = %s LIMIT 1",
                (vendor_id,),
            )
            row = cursor.fetchone()
        languages = {config: code for code, config in TEXT_SEARCH_CONFIG.items()}
        return languages[row[0]] if row else None

    def article_clauses(self, articles: Sequence[tuple[str, str]]) -> list[ContractClause]:
        """Every clause of the given (contract_id, article) pairs: the parents of a hit.
        Returned in the order the articles are given, clauses in number order."""
        if not articles:
            return []
        with self._connection.cursor() as cursor:
            cursor.execute(
                f"SELECT {_COLUMNS} FROM {self.table} "
                "WHERE (contract_id, article) IN (SELECT * FROM unnest(%s::text[], %s::text[]))",
                ([c for c, _ in articles], [a for _, a in articles]),
            )
            names = [column.name for column in cursor.description]
            clauses = [ContractClause(**dict(zip(names, row, strict=True))) for row in cursor]
        order = {pair: index for index, pair in enumerate(articles)}
        return sorted(
            clauses,
            key=lambda c: (
                order[(c.contract_id, c.article)],
                [int(p) for p in c.number.split(".")],
            ),
        )

    def _select(
        self,
        where: str,
        order: str,
        source: str | None,
        params: dict,
        vendor_id: str | None,
        limit: int,
    ) -> list[ContractClause]:
        sources = f"{self.table}" + (f", {source}" if source else "")
        if vendor_id is not None:
            where += " AND vendor_id = %(vendor_id)s"
        sql = f"SELECT {_COLUMNS} FROM {sources} WHERE {where} ORDER BY {order} LIMIT %(limit)s"
        with self._connection.cursor() as cursor:
            cursor.execute(sql, params | {"vendor_id": vendor_id, "limit": limit})
            names = [column.name for column in cursor.description]
            return [ContractClause(**dict(zip(names, row, strict=True))) for row in cursor]


def _vector(values: Sequence[float]) -> str:
    return "[" + ",".join(f"{float(value):.7g}" for value in values) + "]"
