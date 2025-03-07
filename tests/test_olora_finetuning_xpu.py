# Copyright 2024-present the HuggingFace Inc. team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


import os
os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"

import tempfile  # noqa: E402
import unittest  # noqa: E402
from typing import List, Optional  # noqa: E402

import torch  # noqa: E402
import transformers  # noqa: E402
from datasets import load_dataset  # noqa: E402
from peft import get_peft_model, AdaLoraConfig  # noqa: E402
from transformers import AutoModelForCausalLM, AutoTokenizer, set_seed, GPTQConfig  # noqa: E402



def train(
        base_model: str = "path/to/model",
        data_path: str = "yahma/alpaca-cleaned",
        output_dir: str = "olora",
        batch_size: int = 16,
        num_epochs: int = 1,
        learning_rate: float = 3e-4,
        cutoff_len: int = 256,
        val_set_size: int = 16,
        quantize: bool = False,
        eval_step: int = 100,
        save_step: int = 100,
        device_map: str = "auto",
        lora_r: int = 32,
        lora_alpha: int = 16,
        lora_dropout: float = 0.05,
        lora_target_modules: List[str] = None,
        torch_dtype: str = "float16",
        init_lora_weights="olora",
        seed: Optional[int] = None,
):
    # Set device_map to the right place when enabling DDP.
    world_size = int(os.environ.get("WORLD_SIZE", 0)) or int(os.environ.get("PMI_SIZE", 0))
    if world_size > 1 and device_map != "cpu":
        from accelerate import Accelerator

        device_map = {"": Accelerator().process_index}
    # Set seed
    if seed is not None:
        set_seed(seed)
    model_kwargs = {"torch_dtype": getattr(torch, torch_dtype), "device_map": "xpu"}
    if quantize:
        model_kwargs["quantization_config"] = GPTQConfig(bits=4, dataset=['allenai/c4']) # /monster/data/model/dataset/c4-train.00000-of-01024.json.gz

    model = AutoModelForCausalLM.from_pretrained(base_model, **model_kwargs)
    assert model.device.type == "xpu"

    tokenizer = AutoTokenizer.from_pretrained(base_model, trust_remote_code=True)
    # For some tokenizer with no pad token like llama
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    def tokenize(prompt, add_eos_token=True):
        result = tokenizer(
            prompt,
            truncation=True,
            max_length=cutoff_len,
            padding=False,
            return_tensors=None,
        )
        if (
                result["input_ids"][-1] != tokenizer.eos_token_id
                and len(result["input_ids"]) < cutoff_len
                and add_eos_token
        ):
            result["input_ids"].append(tokenizer.eos_token_id)
            result["attention_mask"].append(1)

        result["labels"] = result["input_ids"].copy()

        return result

    def generate_and_tokenize_prompt(example):
        full_prompt = generate_prompt(example)
        tokenized_full_prompt = tokenize(full_prompt)
        return tokenized_full_prompt

    config = AdaLoraConfig(
        r=lora_r,
        lora_alpha=lora_alpha,
        target_modules=lora_target_modules,
        lora_dropout=lora_dropout,
        bias="none",
        task_type="CAUSAL_LM",
        total_step=20,
    )

    model = get_peft_model(model, config)
    assert model.device.type == "xpu"

    data = load_dataset(data_path)

    train_val = data["train"].train_test_split(test_size=val_set_size, shuffle=True, seed=42)
    train_data = train_val["train"].shuffle().map(generate_and_tokenize_prompt)
    val_data = train_val["test"].shuffle().map(generate_and_tokenize_prompt)

    trainer = transformers.Trainer(
        model=model,
        train_dataset=train_data,
        eval_dataset=val_data,
        args=transformers.TrainingArguments(
            per_device_train_batch_size=batch_size,
            warmup_steps=100,
            num_train_epochs=num_epochs,
            learning_rate=learning_rate,
            logging_steps=100,
            optim="adamw_torch",
            evaluation_strategy="steps",
            save_strategy="steps",
            eval_steps=eval_step,
            save_steps=save_step,
            output_dir=output_dir,
            save_total_limit=3,
            load_best_model_at_end=True,
            ddp_find_unused_parameters=False if world_size > 1 else None,
        ),
        data_collator=transformers.DataCollatorForSeq2Seq(
            tokenizer, pad_to_multiple_of=8, return_tensors="pt", padding=True
        ),
    )

    trainer.train()
    model.save_pretrained(output_dir)


def generate_prompt(example):
    return f"""Below is an instruction that describes a task. Write a response that appropriately completes the request.
                ### Instruction:
                {example["instruction"]}
                ### Response:
                {example["output"]}"""


class Test(unittest.TestCase):

    def test_peft(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            train(
                base_model="hugging-quants/Meta-Llama-3.1-8B-Instruct-GPTQ-INT4", # "/monster/data/model/Meta-Llama-3.1-8B-Instruct-GPTQ-INT4",
                data_path="yahma/alpaca-cleaned", # "/monster/data/model/dataset/yahma-alpaca-cleaned",
                output_dir=tmp_dir,
                batch_size=16,
                num_epochs=1,
                learning_rate=3e-4,
                cutoff_len=256,
                val_set_size=16,
                quantize=True,
                eval_step=100,
                save_step=100,
                device_map="cuda",
                lora_r=32,
                lora_alpha=16,
                lora_dropout=0.05,
                lora_target_modules=None,
                torch_dtype="float16",
                init_lora_weights="olora",
                seed=1234,
            )
