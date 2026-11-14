"""OPTIONAL accelerator: embed text on a rented GPU with Modal. UNTESTED sketch; check Modal's docs.

Setup (per person who runs it):  pip install modal && modal setup
Run:   modal run pipeline/modal_embed.py
Input: pipeline/chunks.txt, texts separated by a line containing only ---
Output: pipeline/chunk_vectors.json  (list of 1024-float lists, same order as input)

Offline job only: the live demo must not call Modal.
"""
import json

import modal

app = modal.App("aikakone-embed")
image = modal.Image.debian_slim().pip_install("sentence-transformers", "torch")


@app.cls(gpu="T4", image=image, timeout=1800)
class Embedder:
    @modal.enter()
    def load(self):
        from sentence_transformers import SentenceTransformer

        self.model = SentenceTransformer("BAAI/bge-m3")

    @modal.method()
    def embed(self, texts):
        return self.model.encode(texts, normalize_embeddings=True).tolist()


@app.local_entrypoint()
def main():
    texts = open("pipeline/chunks.txt").read().split("\n---\n")
    batches = [texts[i : i + 128] for i in range(0, len(texts), 128)]
    vectors = [v for out in Embedder().embed.map(batches) for v in out]
    json.dump(vectors, open("pipeline/chunk_vectors.json", "w"))
    print(f"embedded {len(vectors)} texts")
