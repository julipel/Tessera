"""Гибридный поиск QdrantChunkIndex + KnowledgeSearch (P4-07a) на настоящем Qdrant (`make up`).

Dense-векторы даёт `ConceptEmbedder`: слова → «смысловые» оси по словарю основ. Так dense
находит перефразировки без общих слов («сдать вещь обратно» → «Возврат товара»), а BM25 —
формы слов и редкие термы вне словаря (SPF, артикулы). Мини-корпус проверяет, что гибрид
с RRF ставит нужный документ первым в обоих случаях.
"""

import uuid
from collections.abc import AsyncIterator, Sequence

import pytest

from app.modules.knowledge.public import (
    IndexedChunk,
    IndexedDocument,
    KnowledgeSearch,
    QdrantChunkIndex,
    VectorIndexError,
    create_qdrant_index,
)
from app.modules.shared.public import TenantId
from test_qdrant_index import qdrant_url

TENANT_A = TenantId(uuid.uuid4())
TENANT_B = TenantId(uuid.uuid4())
SOURCE = uuid.uuid4()

# Ось → основы слов. Последняя ось — постоянная: у текста без понятий ненулевой вектор.
CONCEPTS: tuple[tuple[str, ...], ...] = (
    ("достав", "курьер", "привез", "отправ"),
    ("возврат", "вернут", "верн", "сдать", "обратно", "обмен"),
    ("оплат", "карт", "плат", "налич", "рассроч"),
    ("гарант", "ремонт", "брак", "неисправ"),
    ("кож", "крем", "уход", "сыворот", "солнц"),
    ("бонус", "балл", "лояльн", "скидк"),
    ("сертификат", "подар"),
    ("адрес", "магазин", "телефон", "контакт", "часы"),
)
DIMENSIONS = len(CONCEPTS) + 1

CORPUS = {
    "delivery": (
        "Доставка",
        "Курьерская доставка по Москве стоит 300 рублей и занимает 1-2 дня. "
        "Бесплатно при заказе от 5000 рублей. Отправляем Почтой России по всей стране.",
    ),
    "returns": (
        "Возврат товара",
        "Вернуть товар надлежащего качества можно в течение 14 дней с чеком. "
        "Обмен и возврат оформляются в любом магазине сети.",
    ),
    "payment": (
        "Оплата",
        "Принимаем банковские карты Visa, Mastercard и Мир, оплату наличными курьеру "
        "и рассрочку на 4 платежа без переплаты.",
    ),
    "warranty": (
        "Гарантия",
        "На технику действует гарантия 12 месяцев. Ремонт по гарантии бесплатный, "
        "брак заменяем в течение 15 дней.",
    ),
    "spf": (
        "Солнцезащитные средства",
        "Крем SPF 50 защищает кожу от ультрафиолета. Наносите за 20 минут до выхода на солнце.",
    ),
    "loyalty": (
        "Программа лояльности",
        "Начисляем за каждую покупку бонусные баллы: 5% от суммы. Баллами можно "
        "оплатить до 30% следующего заказа.",
    ),
    "gift": (
        "Подарочные сертификаты",
        "Подарочный сертификат номиналом от 1000 до 30000 рублей действует один год.",
    ),
    "contacts": (
        "Контакты",
        "Адрес магазина: Тверская, 7. Часы работы 10:00-22:00, телефон +7 495 000-00-00.",
    ),
}


class ConceptEmbedder:
    dimensions = DIMENSIONS

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [concept_vector(text) for text in texts]


def concept_vector(text: str) -> list[float]:
    words = [w.strip(".,:;!?«»()").lower() for w in text.split()]
    vector = [
        float(sum(1 for w in words for stem in stems if w.startswith(stem))) for stems in CONCEPTS
    ]
    return [*vector, 0.1]


@pytest.fixture
async def index() -> AsyncIterator[QdrantChunkIndex]:
    index = create_qdrant_index(
        qdrant_url(), collection=f"test_{uuid.uuid4().hex}", dimensions=DIMENSIONS
    )
    try:
        await index.ensure_collection()
    except VectorIndexError as e:
        await index.aclose()
        pytest.fail(f"Qdrant недоступен ({e}). Запусти `make up`.", pytrace=False)
    yield index
    await index.http.delete(f"/collections/{index.collection}")
    await index.aclose()


async def put(
    index: QdrantChunkIndex,
    tenant_id: TenantId,
    external_id: str,
    title: str,
    text: str,
    *,
    section: str | None = None,
) -> IndexedDocument:
    document = IndexedDocument(
        document_id=uuid.uuid4(),
        external_id=external_id,
        title=title,
        url=f"https://shop.example/{external_id}",
        chunks=[
            IndexedChunk(
                chunk_id=uuid.uuid4(),
                ord=0,
                text=text,
                dense=concept_vector(f"{title} {text}"),
                section=section,
            )
        ],
    )
    await index.replace_document(tenant_id, SOURCE, document)
    return document


async def corpus(index: QdrantChunkIndex, tenant_id: TenantId) -> None:
    for external_id, (title, text) in CORPUS.items():
        await put(index, tenant_id, external_id, title, text)


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("Сколько стоит доставкой курьером?", "delivery"),  # формы слов: BM25 + dense
        ("Как сдать вещь обратно, если не подошла?", "returns"),  # перефразировка: только dense
        ("Можно оплатить картой Мир?", "payment"),
        ("Что делать, если техника сломалась по гарантии", "warranty"),
        ("есть SPF 50?", "spf"),  # редкий терм вне словаря: только BM25
        ("Как копить бонусные баллы", "loyalty"),
        ("подарочный сертификат на 5000", "gift"),
        ("где находится магазин и часы работы", "contacts"),
    ],
)
async def test_quality_on_mini_corpus(index: QdrantChunkIndex, query: str, expected: str) -> None:
    await corpus(index, TENANT_A)
    search = KnowledgeSearch(ConceptEmbedder(), index)

    hits = await search.search(TENANT_A, query, top_k=3, rerank=False)

    assert hits[0].url == f"https://shop.example/{expected}", [h.title for h in hits]


async def test_hit_carries_payload(index: QdrantChunkIndex) -> None:
    document = await put(
        index, TENANT_A, "delivery", "Доставка", "Доставка курьером", section="Москва > Сроки"
    )

    [hit] = await index.search(TENANT_A, "доставка", concept_vector("доставка"), 5)

    assert hit.chunk_id == document.chunks[0].chunk_id
    assert hit.document_id == document.document_id
    assert hit.source_id == SOURCE
    assert (hit.title, hit.text, hit.url, hit.section) == (
        "Доставка",
        "Доставка курьером",
        "https://shop.example/delivery",
        "Москва > Сроки",
    )
    assert hit.score > 0


async def test_limit(index: QdrantChunkIndex) -> None:
    await corpus(index, TENANT_A)

    hits = await index.search(TENANT_A, "доставка", concept_vector("доставка"), 2)

    assert len(hits) == 2


async def test_tenant_isolation(index: QdrantChunkIndex) -> None:
    """Чанки другого тенанта не находятся ни по dense, ни по BM25, даже при точном совпадении."""
    await corpus(index, TENANT_A)
    secret = await put(index, TENANT_B, "secret", "Тайна", "Секретный промокод ЛЕТО2026")
    query = "Секретный промокод ЛЕТО2026"

    hits_a = await index.search(TENANT_A, query, concept_vector(query), 20)
    hits_b = await index.search(TENANT_B, query, concept_vector(query), 20)
    empty = await index.search(TenantId(uuid.uuid4()), query, concept_vector(query), 20)

    assert secret.chunks[0].chunk_id not in {h.chunk_id for h in hits_a}
    assert all(h.url and "secret" not in h.url for h in hits_a)
    assert [h.chunk_id for h in hits_b] == [secret.chunks[0].chunk_id]
    assert empty == []


async def test_rejected_query_raises(index: QdrantChunkIndex) -> None:
    with pytest.raises(VectorIndexError, match="points/query"):
        await index.search(TENANT_A, "текст", [1.0, 2.0], 5)
