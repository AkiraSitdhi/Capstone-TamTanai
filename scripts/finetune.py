import os
import torch
import pandas as pd

from torch.nn import DataParallel
from datasets import Dataset, DatasetDict
from sklearn.model_selection import train_test_split
from trl import SFTTrainer
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    BitsAndBytesConfig,
    TrainingArguments,
    Trainer,
    DataCollatorForLanguageModeling,
)
from peft import LoraConfig, prepare_model_for_kbit_training, get_peft_model

torch.cuda.empty_cache()
os.environ['PYTORCH_CUDA_ALLOC_CONF'] = 'expandable_segments:True'
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
print(f"using {DEVICE} device")

def print_gpu_memory_usage():
    allocated_memory = torch.cuda.memory_allocated() / 1024**2  # Convert bytes to MiB
    cached_memory = torch.cuda.memory_cached() / 1024**2  # Convert bytes to MiB
    print(f"Allocated GPU memory: {allocated_memory:.2f} MiB")
    print(f"Cached GPU memory: {cached_memory:.2f} MiB")

print_gpu_memory_usage()

# TODO download Seallms weight at https://huggingface.co/SeaLLMs/SeaLLM-7B-v2
model_name_or_path = "<base model path>"

# Load both LLM model and tokenizer
def load_LLM_and_tokenizer():
    bnb_config = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_use_double_quant=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.bfloat16
    )
    model = AutoModelForCausalLM.from_pretrained(
        model_name_or_path,
        quantization_config=bnb_config,
        local_files_only=True,
        device_map="auto",         # NOTE use gpu
        torch_dtype=torch.bfloat16,
        use_cache = False
    )
    # tokenizer = AutoTokenizer.from_pretrained(model_name_or_path)
    tokenizer = AutoTokenizer.from_pretrained(
        model_name_or_path,
        padding_side="left",
        add_eos_token=False,
        add_bos_token=False,
    )
    tokenizer.model_max_length = 4096
    tokenizer.pad_token = tokenizer.eos_token
    return model, tokenizer


model, tokenizer = load_LLM_and_tokenizer()
model.config.use_cache = False


if torch.cuda.device_count() > 1:
    print("Let's use", torch.cuda.device_count(), "GPUs!")
    # dim = 0 [30, xxx] -> [10, ...], [10, ...], [10, ...] on 3 GPUs
    model = DataParallel(model).module

# TODO: fix all instruction prompts (<s> and </s> tokens)
# Training prompt for instruction finetuning using '###' format
DEFAULT_SYSTEM_PROMPT = """คุณคือนักกฎหมายที่จะตอบคำถามเกี่ยวกับกฎหมาย จงตอบคำถามโดยใช้ความรู้ที่ให้ดังต่อไปนี้
ถ้าหากคุณไม่รู้คำตอบ ให้ตอบว่าไม่รู้ อย่าสร้างคำตอบขึ้นมาเอง"""

###------------------------------ Process Dataset ------------------------------###
def generate_training_prompt(
    question: str, response: str, knowledge: str,
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
) -> str:
    return f"""<s><|im_start|>system
{system_prompt.strip()}\nความรู้ที่ให้:
{knowledge.strip()}</s><|im_start|>user
{question.strip()}</s><|im_start|>assistant
{response.strip()}</s>
"""

def process_dataset(data: pd.DataFrame):
    data["text"] = data.apply(
        lambda row: generate_training_prompt(
            row["question"], row["answer"], row["knowledges"]
        ), axis=1
    )
    return data

def prep(text):
    input_ids = tokenizer(text, return_tensors="pt", add_special_tokens=False, padding="max_length", max_length=4096, truncation=True)
    input_ids['labels'] = input_ids['input_ids'].copy()
    return input_ids

law_dataset = pd.read_csv("<data path>",encoding='utf-8')

train_ratio = 0.8
validation_ratio = 0.1
test_ratio = 0.1

train_data, temp_data = train_test_split(law_dataset, test_size=1 - train_ratio, random_state=42)
validation_data, test_data = train_test_split(temp_data, test_size=test_ratio/(test_ratio + validation_ratio), random_state=42)

print("Train set shape:", train_data.shape)
print("Validation set shape:", validation_data.shape)
print("Test set shape:", test_data.shape)

train_data = process_dataset(train_data)
validation_data = process_dataset(validation_data)
train_dataset = Dataset.from_pandas(train_data)
validation_dataset = Dataset.from_pandas(validation_data)

combine_dataset = DatasetDict()
combine_dataset['train'] = train_dataset
combine_dataset['validation'] = validation_dataset

peft_config = LoraConfig(
    r=64, #32
    lora_alpha=16, #64
    target_modules=[
        "q_proj",
        "k_proj",
        "v_proj",
        "o_proj",
        "gate_proj",
        "up_proj",
        "down_proj",
        "lm_head",
    ],
    # target_modules=["q_proj", "v_proj"],
    bias="none",
    lora_dropout=0.05,
    task_type="CAUSAL_LM",
)

model.gradient_checkpointing_enable()
model = prepare_model_for_kbit_training(model)
model = get_peft_model(model, peft_config)


# TODO: fix all hyperparameters + try to save model weight every X epochs
# @markdown ### Enter a file path:
OUTPUT_DIR = "<export path>"          # @param {type:"string"}

training_arguments = TrainingArguments(
    output_dir=OUTPUT_DIR,
    per_device_train_batch_size=4,
    gradient_accumulation_steps=1, # 10
    gradient_checkpointing=True,
    optim="paged_adamw_32bit",
    logging_steps=25,
    learning_rate=2.5e-4,
    bf16=True,
    max_grad_norm=0.3,
    num_train_epochs=3,
    evaluation_strategy="steps",
    eval_steps=25,
    warmup_ratio=0.005,
    save_strategy="steps",
    save_steps=25, 
    # group_by_length=True,
    lr_scheduler_type="constant",
    do_eval=True
)

trainer = SFTTrainer(
    model=model,
    train_dataset=combine_dataset["train"],
    eval_dataset=combine_dataset["validation"],
    peft_config=peft_config,
    dataset_text_field="text",
    max_seq_length=4096,
    tokenizer=tokenizer,
    args=training_arguments,
)

# trainer = Trainer(
#     model=model,
#     train_dataset=tokenized_train_dataset,
#     eval_dataset=tokenized_val_dataset,
#     args=training_arguments,
#     data_collator=DataCollatorForLanguageModeling(tokenizer, mlm=False),
# )

print_gpu_memory_usage()
torch.cuda.empty_cache()

print("Starting Training\n")
trainer.train()

trainer.save_model()

log_df = pd.DataFrame(trainer.state.log_history)
log_df.to_csv("<log path>")

print("Finished Finetuning\n")
# model = PeftModel.from_pretrained(model, OUTPUT_DIR)