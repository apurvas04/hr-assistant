import redis
from redis.commands.search.field import TextField, TagField, VectorField
from redis.commands.search.index_definition import IndexDefinition, IndexType
from app.embed import DIM

INDEX = "idx:policy"
PREFIX = "chunk:"
r = redis.Redis(host="localhost", port=6379)  # keep bytes: we store raw vectors

def reset_index():
    try:
        r.ft(INDEX).dropindex(delete_documents=False)
    except redis.ResponseError:
        pass
    for key in r.scan_iter(f"{PREFIX}*"):
        r.delete(key)
    r.ft(INDEX).create_index(
        [
            TagField("doc_id"),
            TagField("access"),
            TagField("status"),
            TextField("title"),
            TextField("section"),
            TextField("text"),
            VectorField("embedding", "HNSW",
                        {"TYPE": "FLOAT32", "DIM": DIM, "DISTANCE_METRIC": "COSINE"}),
        ],
        definition=IndexDefinition(prefix=[PREFIX], index_type=IndexType.HASH),
    )