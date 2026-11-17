"""
QLoRA fine-tune of Qwen2.5-7B-Instruct on Ledger SFT data (loss on the answer
tokens only). Runs under the shared `unsloth` env without modifying it, and is
hard-capped on VRAM because the live vLLM server shares GPU0.
"""

from __future__ import annotations

import argparse
import json

import unsloth  # noqa: F401  (must precede transformers/peft)
import torch
from datasets import Dataset
from trl import SFTConfig, SFTTrainer
from unsloth import FastLanguageModel
from unsloth.chat_templates import train_on_responses_only

BASE = "/home/admin/munimai/models/Qwen2.5-7B-Instruct-base"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--epochs", type=float, default=3)
    ap.add_argument("--vram_fraction", type=float, default=0.45)
    a = ap.parse_args()

    torch.cuda.set_per_process_memory_fraction(a.vram_fraction, 0)
    model, tok = FastLanguageModel.from_pretrained(
        model_name=BASE, max_seq_length=1024, load_in_4bit=True, dtype=None)
    model = FastLanguageModel.get_peft_model(
        model, r=16, lora_alpha=32, lora_dropout=0.0,
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj",
                        "gate_proj", "up_proj", "down_proj"],
        use_gradient_checkpointing="unsloth", random_state=7)

    rows = [json.loads(l) for l in open(a.data, encoding="utf-8") if l.strip()]
    ds = Dataset.from_dict({"text": [
        tok.apply_chat_template(r["messages"], tokenize=False) for r in rows]})

    trainer = SFTTrainer(
        model=model, tokenizer=tok, train_dataset=ds,
        args=SFTConfig(
            output_dir=a.out, dataset_text_field="text", max_length=1024,
            per_device_train_batch_size=1, gradient_accumulation_steps=8,
            num_train_epochs=a.epochs, learning_rate=1e-4,
            lr_scheduler_type="cosine", warmup_steps=3,
            optim="paged_adamw_8bit", bf16=True, logging_steps=5,
            save_strategy="no", report_to="none", seed=7,
            dataset_num_proc=1, packing=False))
    trainer = train_on_responses_only(
        trainer, instruction_part="<|im_start|>user\n",
        response_part="<|im_start|>assistant\n")
    trainer.train()
    model.save_pretrained(a.out)
    tok.save_pretrained(a.out)
    print("PEAK_VRAM_GB", round(torch.cuda.max_memory_allocated(0) / 1e9, 2))
    print("SAVED", a.out)


if __name__ == "__main__":
    main()
