"""Embedder поверх OpenAI Embeddings API (подходит и для совместимых API через `base_url`).

Тексты отправляются батчами по `batch_size`; порядок восстанавливается по `index` ответа.
Параметр `dimensions` в API не передаётся (его принимают не все совместимые серверы):
размерность ответа сверяется с настроенной, расхождение — `EmbeddingError`.
"""

from collections.abc import Sequence

from openai import APIError, AsyncOpenAI

from app.modules.knowledge.domain.errors import EmbeddingError


def create_openai_embedder(
    api_key: str,
    *,
    model: str,
    dimensions: int,
    base_url: str | None = None,
    batch_size: int = 128,
    timeout_s: float = 60.0,
    max_retries: int = 2,
) -> "OpenAIEmbedder":
    client = AsyncOpenAI(
        api_key=api_key, base_url=base_url, timeout=timeout_s, max_retries=max_retries
    )
    return OpenAIEmbedder(client, model=model, dimensions=dimensions, batch_size=batch_size)


class OpenAIEmbedder:
    def __init__(
        self, client: AsyncOpenAI, *, model: str, dimensions: int, batch_size: int = 128
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        self._client = client
        self.model = model
        self.dimensions = dimensions
        self.batch_size = batch_size

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            vectors.extend(await self._embed_batch(texts[start : start + self.batch_size]))
        return vectors

    async def _embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        try:
            response = await self._client.embeddings.create(model=self.model, input=list(texts))
        except APIError as error:
            raise EmbeddingError(f"{self.model}: {type(error).__name__}: {error}") from error
        by_index = {item.index: item.embedding for item in response.data}
        if sorted(by_index) != list(range(len(texts))):
            raise EmbeddingError(
                f"{self.model}: ожидалось {len(texts)} векторов, получено {len(response.data)}"
            )
        vectors = [by_index[i] for i in range(len(texts))]
        for vector in vectors:
            if len(vector) != self.dimensions:
                raise EmbeddingError(
                    f"{self.model}: размерность {len(vector)} вместо {self.dimensions}"
                )
        return vectors

    async def aclose(self) -> None:
        await self._client.close()
