# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

from typing import Optional

from executorch.examples.models.llama.tokenizer.tiktoken import Tokenizer as Tiktoken
from executorch.extension.llm.tokenizer.tokenizer import (
    Tokenizer as SentencePieceTokenizer,
)


def get_tokenizer(tokenizer_path: str, tokenizer_config_path: Optional[str] = None):
    if tokenizer_path.endswith(".json"):
        from executorch.extension.llm.tokenizer.hf_tokenizer import HuggingFaceTokenizer

        tokenizer = HuggingFaceTokenizer(tokenizer_path, tokenizer_config_path)
    else:
        try:
            tokenizer = SentencePieceTokenizer(model_path=str(tokenizer_path))
        except Exception:
            print("Using Tiktokenizer")
            tokenizer = Tiktoken(model_path=str(tokenizer_path))
    return tokenizer
