"""The questions the pipeline asks the contract index. The retrieval eval asks the same
ones (data/generate.py imports them), so Recall@5 measures what the pipeline does."""

PRICE_QUESTION = {
    "id": "Apakah {vendor} boleh menaikkan harga satuan {item}?",
    "en": "Can {vendor} raise the unit price of {item}?",
}
