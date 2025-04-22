from dataclasses import dataclass
from typing import Optional
import os

from datasets import load_dataset
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    TrainingArguments,
    Trainer,
    DataCollatorForLanguageModeling,
)

try:
    import wandb
except ImportError:
    wandb = None


@dataclass
class TrainingConfig:
    model_name: str = "gpt2"
    dataset_name: str = "wikitext"
    dataset_config: str = "wikitext-2-raw-v1"
    output_dir: str = "./trainer/output"
    num_train_epochs: int = 3
    per_device_train_batch_size: int = 4
    learning_rate: float = 5e-4
    max_steps: Optional[int] = 100
    wandb_token: Optional[str] = "730d1f892f99dc720c240db8f320c39607bf6995"  # Add wandb token to config


def train(config: TrainingConfig):
    # Explicitly login to wandb if token is provided
    if config.wandb_token is not None and wandb is not None:
        wandb.login(key=config.wandb_token)
    elif config.wandb_token is not None and wandb is None:
        raise ImportError("wandb is not installed but a wandb_token was provided.")

    # Load tokenizer and model
    tokenizer = AutoTokenizer.from_pretrained(config.model_name)
    model = AutoModelForCausalLM.from_pretrained(config.model_name)

    # Ensure tokenizer has a pad token
    if tokenizer.pad_token is None:
        if tokenizer.eos_token is not None:
            tokenizer.pad_token = tokenizer.eos_token
        else:
            tokenizer.add_special_tokens({'pad_token': '[PAD]'})
            model.resize_token_embeddings(len(tokenizer))

    # Load and prepare dataset
    dataset = load_dataset(config.dataset_name, config.dataset_config)
    
    def tokenize_function(examples):
        # Check if examples contain text and are not empty
        if not examples["text"]:
            return {"input_ids": [], "attention_mask": []}
        return tokenizer(examples["text"], truncation=True, max_length=512)

    tokenized_dataset = dataset.map(
        tokenize_function,
        batched=True,
        remove_columns=dataset["train"].column_names,
    )

    # Filter out empty examples
    tokenized_dataset = tokenized_dataset.filter(
        lambda example: len(example["input_ids"]) > 0
    )

    # Initialize trainer
    training_args = TrainingArguments(
        output_dir=config.output_dir,
        num_train_epochs=config.num_train_epochs,
        per_device_train_batch_size=config.per_device_train_batch_size,
        learning_rate=config.learning_rate,
        max_steps=config.max_steps,
        save_strategy="epoch",
        logging_steps=100,
        report_to=["wandb"] if config.wandb_token is not None else [],
    )

    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=tokenized_dataset["train"],
        data_collator=DataCollatorForLanguageModeling(tokenizer=tokenizer, mlm=False),
    )

    # Train the model
    trainer.train()

    # Save the model
    trainer.save_model()
    tokenizer.save_pretrained(config.output_dir)


if __name__ == "__main__":
    # You can set the wandb token here, or via environment variable, or pass as argument
    wandb_token = os.environ.get("WANDB_API_KEY", None)
    config = TrainingConfig(wandb_token=wandb_token)
    train(config)