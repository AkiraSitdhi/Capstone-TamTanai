import gc
import time
import torch
import logging
import pandas as pd
import tensorrt_llm
import tensorrt_llm.profiler
from tqdm import tqdm
from transformers import AutoTokenizer
from tensorrt_llm.logger import logger
from tensorrt_llm.runtime import ModelRunner

tokenizer = None
max_input_length = 4096
max_output_len = 512
max_attention_window_size = None
sink_token_length = None
log_level = 'warning'
sink_token_length = None
engine_dir = './tamtanai_engines_1gpu/'
tokenizer_dir = './tamtanai/'
use_py_session = True
no_prompt_template = True
tokenizer_type = None
vocab_file = None
num_beams = 1
temperature = 0.7
top_k = 1
top_p = 0.9
length_penalty = 1.0
repetition_penalty = 1.1
presence_penalty = 0.0
frequency_penalty = 0.0
early_stopping = 1
no_add_special_tokens = False
lora_dir = None
lora_task_uids = None
lora_ckpt_source = 'hf'
medusa_choices = None


def load_tokenizer(tokenizer_dir):
    tokenizer = AutoTokenizer.from_pretrained(
        tokenizer_dir,
    )
    return tokenizer


def parse_input(
    tokenizer,
    question: str,
    knowledge: str,
    max_input_length: int = 4096,
    padding: bool = False,
):
    PROMPT_INST = f"""<s><|im_start|>system
คุณคือนักกฎหมายที่จะตอบคำถามเกี่ยวกับกฎหมาย จงตอบคำถามโดยใช้ความรู้ที่ให้ดังต่อไปนี้
ถ้าหากคุณไม่รู้คำตอบ ให้ตอบว่าไม่รู้ อย่าสร้างคำตอบขึ้นมาเอง ให้ตอบคำถามโดยให้คำตอบกระชับที่สุด\n"""

    PROMPT_KNOWLEDGE = f"ความรู้ที่ให้:\n{knowledge.strip()}"

    PROMPT_QUESTION = f"""</s><|im_start|>user
{question.strip()}</s><|im_start|>assistant
"""

    input_inst_ids = tokenizer.encode(PROMPT_INST, return_tensors='pt', add_special_tokens=False)[0]
    input_knowledge_ids = tokenizer.encode(PROMPT_KNOWLEDGE, return_tensors='pt', add_special_tokens=False)[0]
    input_question_ids = tokenizer.encode(PROMPT_QUESTION, return_tensors='pt', add_special_tokens=False)[0]

    quota = max_input_length - input_inst_ids.size(0) - input_question_ids.size(0) - 100
    input_knowledge_ids = input_knowledge_ids[:quota]

    input_ids = torch.cat((input_inst_ids, input_knowledge_ids, input_question_ids), dim=0).to(torch.int32)
    padding_length = max_input_length - input_ids.size(0)
    if padding:
        input_ids = torch.nn.functional.pad(input_ids, (padding_length, 0), value=tokenizer.pad_token_id)
    batch_input_ids = [input_ids]

    return batch_input_ids


def parse_output(
    tokenizer,
    inputs,
    outputs,
):
    output_ids = outputs['output_ids']
    sequence_lengths = outputs['sequence_lengths']
    batch_size, num_beams, _ = output_ids.size()
    for batch_idx in range(batch_size):
        for beam in range(num_beams):
            output_begin = inputs[batch_idx].size(0)
            output_end = sequence_lengths[batch_idx][beam]
            outputs = output_ids[batch_idx][beam][output_begin:output_end].tolist()
            outputs_text = tokenizer.decode(outputs)
            return outputs_text


def inference(
    prompt,
    tokenizer_dir='../tamtanai/',
    engine_dir='../tamtanai_engines_1gpu/',
):
    runtime_rank = tensorrt_llm.mpi_rank()
    global tokenizer
    if tokenizer is None:
        tokenizer = load_tokenizer(tokenizer_dir)
    pad_id = tokenizer.pad_token_id
    end_id = tokenizer.eos_token_id

    # inputs = parse_input(tokenizer, question, knowledge)
    inputs = tokenizer.encode(prompt, return_tensors='pt', add_special_tokens=False)

    runner_cls = ModelRunner
    runner_kwargs = dict(
        engine_dir=engine_dir,
        lora_dir=None,
        rank=runtime_rank,
        debug_mode=False,
        lora_ckpt_source="hf"
    )
    runner = runner_cls.from_dir(**runner_kwargs)
    start_time = time.time()
    with torch.no_grad():
        outputs = runner.generate(
            inputs,
            max_new_tokens=max_output_len,
            max_attention_window_size=None,
            sink_token_length=None,
            end_id=end_id,
            pad_id=pad_id,
            temperature=0.7,
            top_k=1,
            top_p=0.9,
            num_beams=1,
            length_penalty=1.0,
            early_stopping=1,
            repetition_penalty=1.2,
            presence_penalty=0.0,
            frequency_penalty=0.1,
            stop_words_list=None,
            bad_words_list=None,
            output_cum_log_probs=False,
            output_log_probs=False,
            lora_uids=None,
            prompt_table=None,
            prompt_tasks=None,
            streaming=False,
            output_sequence_lengths=True,
            return_dict=True,
            medusa_choices=None
        )
        torch.cuda.synchronize()
    end_time = time.time()
    latency = end_time - start_time
    del runner_cls
    del runner_kwargs
    del runner

    # print(f"Runner generate took {latency} seconds")
    if runtime_rank == 0:
        output_text = parse_output(tokenizer, inputs, outputs)
        del outputs
        del inputs
        torch.cuda.empty_cache()
        gc.collect()
        return output_text, latency
    return None, 0.0


test_data = pd.read_csv("../testdata.csv")
print("Finished reading data")

answers = []
times = []
for i, (index, data_point) in tqdm(enumerate(test_data.iterrows())):
    prompt = data_point["prompt"]
    output = inference(prompt)
    answers.append(output[0])
    times.append(output[1])

test_data["response_finetune"] = answers
test_data["time_finetune"] = times

test_data.to_csv("./evaluate_inference_trtllm_base_seallmv2.csv", index=False, encoding='utf-8')
print("Finished inference test dataset")