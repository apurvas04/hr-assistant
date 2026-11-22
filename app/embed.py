from functools import lru_cache
import numpy as np
from sentence_transformers import SentenceTransformer

DIM = 384

@lru_cache(maxsize=1)
def _model():
    return SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")

def embed(texts: list[str]) -> np.ndarray:
    return _model().encode(texts, normalize_embeddings=True).astype(np.float32)