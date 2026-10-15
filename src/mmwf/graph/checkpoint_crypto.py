"""Encrypt checkpoint values, pending writes AND JSON metadata before persistence."""
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from mmwf.infra.crypto import FileCipher, MAGIC
from mmwf.infra.keystore import load_or_create_key


class CheckpointSerializer:
    def __init__(self, db_path):
        self.cipher = FileCipher(load_or_create_key(db_path.parent).key)
        self.inner = JsonPlusSerializer()

    def dumps(self, obj):
        return self.cipher.encrypt_bytes(self.inner.dumps(obj))

    def loads(self, data):
        # Existing checkpoints are readable; all subsequent writes are encrypted.
        if data.startswith(MAGIC):
            data = self.cipher.decrypt_bytes(data)
        return self.inner.loads(data)

    def dumps_typed(self, obj):
        kind, data = self.inner.dumps_typed(obj)
        return kind + '+mmwf', self.cipher.encrypt_bytes(data)

    def loads_typed(self, value):
        kind, data = value
        if kind.endswith('+mmwf'):
            kind = kind.removesuffix('+mmwf')
            data = self.cipher.decrypt_bytes(data)
        return self.inner.loads_typed((kind, data))
