"""One shared CUDA model, independent pair scores, bounded small batches."""

from __future__ import annotations
import threading
from pathlib import Path
from contextlib import contextmanager

MODEL_ID = "Qwen/Qwen3-Reranker-0.6B"
INSTRUCTION = (
    "Given a topic query, identify records of an AI agent activity relevant to that topic, "
    "including paraphrases. The record need not answer a question."
)
PREFIX = (
    "<|im_start|>system\nJudge whether the Document meets the requirements based on the Query "
    'and the Instruct provided. Note that the answer can only be "yes" or "no".'
    "<|im_end|>\n<|im_start|>user\n"
)
SUFFIX = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"


def download_reranker(root):
    from huggingface_hub import snapshot_download

    return Path(
        snapshot_download(
            MODEL_ID, local_dir=str(Path(root) / "runtime/models/Qwen3-Reranker-0.6B")
        )
    )


class Reranker:
    def __init__(self, root):
        self.root = Path(root)
        self._lock = threading.RLock()
        self._model = None
        self._tokenizer = None
        self._cancellation = threading.local()

    @contextmanager
    def cancellation(self, event):
        self._cancellation.event = event
        try:
            yield
        finally:
            del self._cancellation.event

    def check_cancelled(self):
        event = getattr(self._cancellation, "event", None)
        if event is not None and event.is_set():
            raise InterruptedError("Memory search execution was cancelled")

    def _load(self):
        if self._model is not None:
            return
        self.check_cancelled()
        import torch
        from transformers import AutoTokenizer, AutoModelForCausalLM

        if not torch.cuda.is_available():
            raise RuntimeError("Qwen3 reranker requires an available CUDA GPU")
        path = self.root / "runtime/models/Qwen3-Reranker-0.6B"
        if not (path / "model.safetensors").exists():
            path = download_reranker(self.root)
        self._tokenizer = AutoTokenizer.from_pretrained(path, padding_side="left")
        dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
        self._model = (
            AutoModelForCausalLM.from_pretrained(
                path, torch_dtype=dtype, attn_implementation="sdpa"
            )
            .to("cuda")
            .eval()
        )

    @property
    def tokenizer(self):
        with self._lock:
            self._load()
            return self._tokenizer

    def candidate_capacity(self, query):
        tokenizer = self.tokenizer
        overhead = len(
            tokenizer.encode(self._prompt(query, ""), add_special_tokens=False)
        )
        return min(32768, self._model.config.max_position_embeddings) - overhead - 8

    @staticmethod
    def _prompt(query, document):
        return (
            PREFIX
            + f"<Instruct>: {INSTRUCTION}\n<Query>: {query}\n<Document>: {document}"
            + SUFFIX
        )

    def score(self, query, candidates):
        import torch

        with self._lock:
            self._load()
            tokenizer, model = self._tokenizer, self._model
            yes, no = (tokenizer.convert_tokens_to_ids(word) for word in ("yes", "no"))
            scores, batch, budget = [], [], 0

            def run():
                nonlocal batch, budget
                self.check_cancelled()
                if not batch:
                    return
                inputs = tokenizer(batch, padding=True, return_tensors="pt").to(
                    model.device
                )
                with torch.inference_mode():
                    logits = model(**inputs, use_cache=False, logits_to_keep=1).logits[
                        :, -1, :
                    ]
                    scores.extend(
                        logits[:, [no, yes]].float().softmax(-1)[:, 1].cpu().tolist()
                    )
                batch, budget = [], 0

            for candidate in candidates:
                self.check_cancelled()
                prompt = self._prompt(query, candidate)
                count = len(tokenizer.encode(prompt, add_special_tokens=False))
                if count > min(32768, model.config.max_position_embeddings):
                    raise ValueError(
                        "Reranker input exceeds its token window; no text was truncated"
                    )
                # Long inputs gain little from batching on the tested consumer GPU.
                if batch and (
                    len(batch) >= 4 or max(budget, count) * (len(batch) + 1) > 4096
                ):
                    run()
                batch.append(prompt)
                budget = max(budget, count)
            run()
            self.check_cancelled()
            return scores

    def close(self):
        with self._lock:
            if self._model is not None:
                self._model = self._tokenizer = None
                import gc
                import torch

                gc.collect()
                torch.cuda.empty_cache()
